"""Login routes for the opp_ci web UI.

- `GET  /login`                    — login form (GitHub button + local form)
- `POST /login`                    — local password login
- `GET  /login/github`             — start GitHub OAuth dance
- `GET  /login/github/callback`    — finish GitHub OAuth dance
- `POST /logout`                   — clear session

The OAuth dance uses the standard `state` parameter (server-generated,
stashed in the session, verified on callback) to defeat CSRF on the
authorization redirect. After login, we discard the user's GitHub
access token; we only need it long enough to read `/user` and
`/user/orgs` + `/user/teams` for role computation.
"""

import datetime
import logging
import secrets
from urllib.parse import urlencode, quote

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from opp_ci import config as cfg
from opp_ci.auth import (
    ROLE_HIERARCHY,
    get_csrf_token,
    require_csrf,
    rotate_csrf_token,
)
from opp_ci.db.connection import SessionLocal
from opp_ci.db.models import User
from opp_ci.passwords import verify_password

_logger = logging.getLogger(__name__)

router = APIRouter()


# ── Helpers ────────────────────────────────────────────────────────────


def github_enabled():
    return bool(cfg.GITHUB_OAUTH_CLIENT_ID) and bool(cfg.get_github_oauth_client_secret())


def _safe_next(next_value):
    """Reject open-redirect attempts; only accept local paths."""
    if not next_value:
        return None
    if not next_value.startswith("/"):
        return None
    if next_value.startswith("//"):
        return None
    return next_value


def _callback_url(request):
    if cfg.PUBLIC_URL:
        return cfg.PUBLIC_URL.rstrip("/") + "/login/github/callback"
    # Dev fallback: derive from the incoming request. Don't use this
    # behind a reverse proxy — set OPP_CI_PUBLIC_URL instead.
    return str(request.url_for("github_oauth_callback"))


def _compute_role_from_github(token, login):
    """Return "admin" | "submitter" | "readonly" | None.

    None means "external user, OPP_CI_GITHUB_ALLOW_EXTERNAL=0 → reject".
    If no org is configured, every GitHub user maps to `readonly`.
    """
    org = cfg.GITHUB_ORG
    if not org:
        return "readonly"

    headers = {"Authorization": f"Bearer {token}",
               "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28"}

    with httpx.Client(timeout=10.0) as client:
        # Is the user a member of the org at all?
        r = client.get(f"{cfg.GITHUB_BASE_URL}/user/orgs", headers=headers)
        r.raise_for_status()
        org_logins = {o.get("login", "").lower() for o in r.json()}
        in_org = org.lower() in org_logins

        if not in_org:
            return "readonly" if cfg.GITHUB_ALLOW_EXTERNAL else None

        # User is in the org. Check team membership for stricter roles.
        # Admin teams take precedence over submitter teams.
        r = client.get(f"{cfg.GITHUB_BASE_URL}/user/teams", headers=headers)
        r.raise_for_status()
        team_slugs = {t.get("slug") for t in r.json()
                      if t.get("organization", {}).get("login", "").lower() == org.lower()}

    if any(slug in team_slugs for slug in cfg.GITHUB_ADMIN_TEAMS):
        return "admin"
    if cfg.GITHUB_SUBMITTER_TEAMS == ["*"]:
        return "submitter"
    if any(slug in team_slugs for slug in cfg.GITHUB_SUBMITTER_TEAMS):
        return "submitter"
    return "readonly"


def _complete_login(request, user, next_url=None):
    """Persist the login + rotate CSRF, then redirect."""
    request.session["user_id"] = user.id
    rotate_csrf_token(request)
    target = _safe_next(next_url) or "/"
    return RedirectResponse(url=target, status_code=303)


# ── Login form ─────────────────────────────────────────────────────────


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, error: str = None, next: str = None):
    from opp_ci.web.app import templates
    return templates.TemplateResponse(request, "login.html", {
        "error": error,
        "next": _safe_next(next) or "",
        "github_enabled": github_enabled(),
        "local_enabled": True,
        "csrf_token": get_csrf_token(request),
    })


@router.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form(default=""),
    _csrf=Depends(require_csrf),
):
    session = SessionLocal()
    try:
        user = session.execute(
            select(User).where(User.username == username, User.enabled == True)
        ).scalar_one_or_none()
        if user is None or not verify_password(password, user.password_hash):
            # Re-render the form with a generic error — don't leak which
            # half (username/password) was wrong.
            from opp_ci.web.app import templates
            return templates.TemplateResponse(request, "login.html", {
                "error": "Invalid username or password.",
                "next": _safe_next(next) or "",
                "github_enabled": github_enabled(),
                "local_enabled": True,
                "csrf_token": get_csrf_token(request),
            }, status_code=401)

        user.last_login_at = datetime.datetime.utcnow()
        session.commit()
        # Re-load into a fresh user object that's safe to use outside the session
        user_id = user.id
    finally:
        session.close()

    # Build a tiny stub for _complete_login
    class _Stub:
        pass
    stub = _Stub()
    stub.id = user_id
    return _complete_login(request, stub, next_url=next)


# ── Logout ─────────────────────────────────────────────────────────────


@router.post("/logout")
async def logout(request: Request, _csrf=Depends(require_csrf)):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)


# ── GitHub OAuth ───────────────────────────────────────────────────────


@router.get("/login/github")
def github_oauth_start(request: Request, next: str = None):
    if not github_enabled():
        raise HTTPException(status_code=404, detail="GitHub OAuth is not configured")

    state = secrets.token_urlsafe(32)
    request.session["gh_oauth_state"] = state
    safe = _safe_next(next)
    if safe:
        request.session["gh_oauth_next"] = safe
    else:
        request.session.pop("gh_oauth_next", None)

    params = {
        "client_id": cfg.GITHUB_OAUTH_CLIENT_ID,
        "redirect_uri": _callback_url(request),
        "scope": "read:user read:org",
        "state": state,
        "allow_signup": "false",
    }
    return RedirectResponse(
        url=f"https://github.com/login/oauth/authorize?{urlencode(params)}",
        status_code=303,
    )


@router.get("/login/github/callback", name="github_oauth_callback")
def github_oauth_callback(request: Request, code: str = None, state: str = None,
                          error: str = None, error_description: str = None):
    if error:
        return _login_error(request, f"GitHub returned an error: {error_description or error}")
    if not code or not state:
        return _login_error(request, "Missing code or state in OAuth callback.")

    expected_state = request.session.pop("gh_oauth_state", None)
    next_url = request.session.pop("gh_oauth_next", None)
    if not expected_state or not secrets.compare_digest(state, expected_state):
        return _login_error(request, "OAuth state mismatch. Please try again.")

    secret = cfg.get_github_oauth_client_secret()
    try:
        with httpx.Client(timeout=10.0) as client:
            tr = client.post(
                "https://github.com/login/oauth/access_token",
                headers={"Accept": "application/json"},
                data={
                    "client_id": cfg.GITHUB_OAUTH_CLIENT_ID,
                    "client_secret": secret,
                    "code": code,
                    "redirect_uri": _callback_url(request),
                },
            )
            tr.raise_for_status()
            token_resp = tr.json()
            access_token = token_resp.get("access_token")
            if not access_token:
                return _login_error(request, "GitHub did not return an access token.")

            ur = client.get(
                f"{cfg.GITHUB_BASE_URL}/user",
                headers={"Authorization": f"Bearer {access_token}",
                         "Accept": "application/vnd.github+json"},
            )
            ur.raise_for_status()
            gh_user = ur.json()
            gh_id = gh_user.get("id")
            gh_login = gh_user.get("login")
            if not gh_id or not gh_login:
                return _login_error(request, "GitHub /user response missing id or login.")

        try:
            role = _compute_role_from_github(access_token, gh_login)
        except httpx.HTTPError as e:
            _logger.warning("GitHub team/org check failed: %s", e)
            return _login_error(request, "Could not verify your GitHub org membership.")
    except httpx.HTTPError as e:
        _logger.warning("GitHub OAuth call failed: %s", e)
        return _login_error(request, "Could not reach GitHub.")

    if role is None:
        return _login_error(request, "Your GitHub account is not authorized to use this site.")

    now = datetime.datetime.utcnow()
    session = SessionLocal()
    try:
        user = session.execute(select(User).where(User.github_user_id == gh_id)).scalar_one_or_none()
        if user is None:
            user = User(
                github_user_id=gh_id,
                github_username=gh_login,
                role=role,
                role_locked=False,
                enabled=True,
                created_at=now,
            )
            session.add(user)
        else:
            if not user.enabled:
                return _login_error(request, "Your account has been disabled.")
            # Always refresh the displayed username (people rename themselves)
            user.github_username = gh_login
            if not user.role_locked:
                user.role = role
        user.last_login_at = now
        user.last_role_sync_at = now
        session.commit()
        user_id = user.id
    finally:
        session.close()

    class _Stub:
        pass
    stub = _Stub()
    stub.id = user_id
    return _complete_login(request, stub, next_url=next_url)


def _login_error(request, message):
    """Render the login page with a flash error (used by callback failures)."""
    from opp_ci.web.app import templates
    return templates.TemplateResponse(request, "login.html", {
        "error": message,
        "next": "",
        "github_enabled": github_enabled(),
        "local_enabled": True,
        "csrf_token": get_csrf_token(request),
    }, status_code=400)
