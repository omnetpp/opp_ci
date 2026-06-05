"""
Authentication for opp_ci.

Two parallel auth surfaces:
- REST `/api/*`: bearer tokens (ApiToken or Worker.token), see `require_role`.
- Web UI: session cookies, see `require_user` (set by the login flow in
  `opp_ci.web.app`).

Both surfaces share the same role hierarchy. The `worker` role is
exclusive to token-based callers (workers polling for jobs); human
users have `readonly`, `submitter`, or `admin`.
"""

import datetime
import logging
import secrets

from fastapi import Request
from sqlalchemy import select

from opp_ci.db.connection import SessionLocal
from opp_ci.db.models import ApiToken, User, Worker

_logger = logging.getLogger(__name__)

ROLE_HIERARCHY = {
    "readonly": 0,
    "submitter": 1,
    "worker": 2,
    "admin": 3,
}


def verify_token(token):
    """
    Verify a bearer token and return (role, identity_dict) or (None, None).

    Checks ApiToken table first, then Worker table.
    """
    if not token:
        return None, None

    session = SessionLocal()
    try:
        # Check ApiToken table
        api_token = session.execute(
            select(ApiToken).where(ApiToken.token == token, ApiToken.enabled == True)
        ).scalar_one_or_none()
        if api_token is not None:
            return api_token.role, {"type": "api_token", "id": api_token.id, "name": api_token.name}

        # Check Worker table
        worker = session.execute(
            select(Worker).where(Worker.token == token)
        ).scalar_one_or_none()
        if worker is not None:
            return "worker", {"type": "worker", "id": worker.id, "name": worker.name}

        return None, None
    finally:
        session.close()


def require_role(minimum_role):
    """
    FastAPI dependency that checks the Authorization header for a bearer token
    with at least the given role.

    Usage in a route:
        @router.post("/api/runs", dependencies=[Depends(require_role("submitter"))])
    """
    from fastapi import Header, HTTPException

    async def _check(authorization: str = Header(default="")):
        token = _extract_bearer(authorization)
        role, identity = verify_token(token)
        if role is None:
            raise HTTPException(status_code=401, detail="Invalid or missing API token")
        if ROLE_HIERARCHY.get(role, -1) < ROLE_HIERARCHY.get(minimum_role, 99):
            raise HTTPException(status_code=403, detail=f"Requires role '{minimum_role}', got '{role}'")
        return identity

    return _check


def require_worker_token():
    """
    FastAPI dependency for worker endpoints.
    Returns the Worker object identified by the bearer token.
    """
    from fastapi import Header, HTTPException

    async def _check(authorization: str = Header(default="")):
        token = _extract_bearer(authorization)
        if not token:
            raise HTTPException(status_code=401, detail="Missing worker token")

        session = SessionLocal()
        try:
            worker = session.execute(
                select(Worker).where(Worker.token == token)
            ).scalar_one_or_none()
            if worker is None:
                raise HTTPException(status_code=401, detail="Invalid worker token")
            # Update heartbeat
            worker.last_heartbeat = datetime.datetime.utcnow()
            if worker.status == "offline":
                worker.status = "online"
                # Re-queue any runs that were left in running state on this
                # worker (presumably because the worker disconnected mid-run).
                from opp_ci.db.models import TestRun, TestRunLifecycle
                orphans = session.execute(
                    select(TestRun).where(
                        TestRun.worker_id == worker.id,
                        TestRun.lifecycle == TestRunLifecycle.running,
                    )
                ).scalars().all()
                for run in orphans:
                    run.lifecycle = TestRunLifecycle.queued
                    run.worker_id = None
                    run.started_at = None
                worker.current_job_count = 0
            session.commit()
            return {"worker_id": worker.id, "worker_name": worker.name}
        finally:
            session.close()

    return _check


def _extract_bearer(authorization):
    """Extract token from 'Bearer <token>' header value."""
    if not authorization:
        return None
    parts = authorization.split()
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1]
    # Also accept bare token for convenience
    if len(parts) == 1:
        return parts[0]
    return None


# ── Web UI session auth ────────────────────────────────────────────────


def _load_enabled_user(user_id):
    if user_id is None:
        return None
    session = SessionLocal()
    try:
        user = session.execute(
            select(User).where(User.id == user_id, User.enabled == True)
        ).scalar_one_or_none()
        if user is not None:
            session.expunge(user)
        return user
    finally:
        session.close()


def require_user(minimum_role="readonly"):
    """FastAPI dependency: return the current `User`, or redirect to /login.

    Resolves the session cookie's `user_id` to a `User` row on every
    request — so disabling a user takes effect immediately. Raises 403
    if the user's role is below `minimum_role`.

    The redirect uses 303 so a POST that gets gated also lands on the
    login form via GET. The original URL (path+query) is preserved as
    `?next=…` so the user lands where they intended after logging in.
    """
    from fastapi import HTTPException, Request, status
    from urllib.parse import quote

    async def _check(request: Request):
        user_id = request.session.get("user_id")
        user = _load_enabled_user(user_id)
        if user is None:
            # Clear any stale session pointer
            if user_id is not None:
                request.session.pop("user_id", None)
            target = request.url.path
            if request.url.query:
                target = f"{target}?{request.url.query}"
            raise HTTPException(
                status_code=status.HTTP_303_SEE_OTHER,
                headers={"Location": f"/login?next={quote(target, safe='')}"},
            )
        if ROLE_HIERARCHY.get(user.role, -1) < ROLE_HIERARCHY.get(minimum_role, 99):
            raise HTTPException(status_code=403, detail=f"Requires role '{minimum_role}'")
        return user

    return _check


# ── CSRF for cookie-authenticated POSTs ────────────────────────────────


_CSRF_KEY = "csrf_token"
_CSRF_FORM_FIELD = "csrf_token"


def get_csrf_token(request):
    """Return the per-session CSRF token, creating it if needed."""
    token = request.session.get(_CSRF_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        request.session[_CSRF_KEY] = token
    return token


def rotate_csrf_token(request):
    """Generate a fresh CSRF token (call on login/logout to defeat fixation)."""
    request.session[_CSRF_KEY] = secrets.token_urlsafe(32)


async def require_csrf(request: Request):
    """FastAPI dependency: verify the form's csrf_token matches the session.

    Reads the field from the POSTed form. Raising HTTPException(403) here
    blocks the request before the route body runs.
    """
    from fastapi import HTTPException

    session_token = request.session.get(_CSRF_KEY)
    if not session_token:
        raise HTTPException(status_code=403, detail="Missing CSRF token in session")

    try:
        form = await request.form()
    except Exception:
        raise HTTPException(status_code=403, detail="Could not parse form")
    submitted = form.get(_CSRF_FORM_FIELD)
    if not submitted or not secrets.compare_digest(str(submitted), session_token):
        raise HTTPException(status_code=403, detail="CSRF token mismatch")
