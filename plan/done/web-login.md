# Plan: require login for the opp_ci web UI (GitHub OAuth + fallback)

Goal: every page served by `opp_ci serve` requires the visitor to be
authenticated. Primary login is **"Sign in with GitHub"**. A local
username/password path stays for break-glass admins and air-gapped
deploys. After login, a signed session cookie keeps the user
authenticated; `/logout` clears it.

The REST API under `/api/*` keeps its current `Authorization: Bearer`
token scheme unchanged — workers and CI scripts must keep working
without doing a browser login.

## Authorization policy (hybrid)

Authentication identifies *who* the visitor is; authorization decides
*what they can do*. The policy:

| Visitor | Role assigned at login |
|---|---|
| Member of a configured **admin team** (e.g. `omnetpp/admins`) | `admin` |
| Member of a configured **submitter team**, or of the org generally | `submitter` |
| Any other GitHub user (no org/team membership) | `readonly` |
| Local user (created via CLI) | role set at creation time |

The "any other GitHub user → readonly" branch is what lets external
contributors see their PR build results without being added to the
org. It can be turned off (`OPP_CI_GITHUB_ALLOW_EXTERNAL=0`) for
private / commercial deployments.

Admin can **promote** any user from the Users page; a promoted role
is *pinned* (`role_locked=True`) and won't be overwritten on the
user's next login. Demotion works the same way. This lets the admin
grant access to someone outside the org without changing GitHub, and
lets the admin revoke access for someone still in the org.

## Scope

In scope:

- A new `User` table keyed by GitHub user-id (stable across username
  changes), with optional local `password_hash` and a role.
- GitHub OAuth flow: `GET /login/github` → GitHub → `GET
  /login/github/callback` → session set, role computed.
- Local login form (`GET /login`, `POST /login`) as a fallback path,
  hidden by default behind a "Use local login" toggle.
- Session middleware with a server-side secret.
- A FastAPI dependency, applied via a router-level wrapper to every
  HTML route in [`opp_ci/web/app.py`](../../opp_ci/web/app.py), that
  resolves the session into a `User` or 303s to `/login`.
- Role gating: mutations require `submitter`+, `/admin/*` requires
  `admin`.
- A Users page (`GET /admin/users`) listing all known users, their
  computed-vs-pinned role, last login, and a row of buttons to
  promote/demote/disable.
- CLI: `opp_ci user create` for the first/break-glass admin.
- CSRF token on every form POST.

Out of scope (note, don't implement):

- Password reset by email, MFA, email/SMTP, audit log of logins.
- Other OAuth providers (Google, GitLab).
- Storing the user's GitHub OAuth token after login (we discard it
  the moment role sync is done — we don't need it again).
- Per-user run visibility (`readonly` users see all runs; this is a
  shared CI dashboard, not a multi-tenant SaaS).

## Design

### Data model

Add to [`opp_ci/db/models.py`](../../opp_ci/db/models.py):

```python
class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    github_user_id = Column(Integer, unique=True, nullable=True)  # numeric, stable
    github_username = Column(String, nullable=True)               # display only
    username = Column(String, unique=True, nullable=True)         # for local login
    password_hash = Column(String, nullable=True)                 # bcrypt; null for GitHub-only users
    role = Column(String, nullable=False, default="readonly")
    role_locked = Column(Boolean, default=False)  # admin pinned this role; don't recompute on next login
    enabled = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    last_login_at = Column(DateTime, nullable=True)
    last_role_sync_at = Column(DateTime, nullable=True)
```

Invariant: at least one of (`github_user_id`, `username`) is set; both
are unique. The Alembic migration creates the table and a partial
unique index on each column to enforce that.

Hashing: `passlib[bcrypt]`. Added to `pyproject.toml`.

Reuse `ROLE_HIERARCHY` from [`opp_ci/auth.py`](../../opp_ci/auth.py)
(human roles are a subset: `readonly` < `submitter` < `admin`; the
`worker` role stays exclusive to token-based API callers).

### GitHub OAuth flow

Register an **OAuth App** at github.com (org-owned, not personal). It
has a Client ID, a Client Secret, and a callback URL. Settings live
in [`opp_ci/config.py`](../../opp_ci/config.py):

| Env var | Meaning | Default |
|---|---|---|
| `OPP_CI_GITHUB_OAUTH_CLIENT_ID` | OAuth App client ID | (empty → GitHub login disabled) |
| `OPP_CI_GITHUB_OAUTH_CLIENT_SECRET_FILE` | path to file containing client secret | `~/.ssh/opp_ci_github_oauth_secret` |
| `OPP_CI_PUBLIC_URL` | base URL for callback, e.g. `https://opp-ci.omnetpp.org` | (empty → derive from request, dev only) |
| `OPP_CI_GITHUB_ORG` | org name to check membership against | (empty → no org check, all GitHub users → readonly) |
| `OPP_CI_GITHUB_ADMIN_TEAMS` | comma-separated team slugs → `admin` | (empty) |
| `OPP_CI_GITHUB_SUBMITTER_TEAMS` | comma-separated team slugs → `submitter`. Special value `*` means "any member of `OPP_CI_GITHUB_ORG`" | `*` |
| `OPP_CI_GITHUB_ALLOW_EXTERNAL` | if `0`, reject non-org users | `1` |

Secret loading follows the same file-or-env pattern as
`get_github_token()` in `config.py` today.

Flow:

1. **`GET /login/github`**: generate a random `state` and PKCE
   `code_verifier`, stash both in the session, redirect to
   `https://github.com/login/oauth/authorize?client_id=…&scope=read:user%20read:org&state=…&redirect_uri=…`.

2. **`GET /login/github/callback?code=…&state=…`**:
   - Verify `state` matches the session value (CSRF on the OAuth
     dance itself).
   - `POST https://github.com/login/oauth/access_token` with the
     code → user access token.
   - `GET https://api.github.com/user` → `(id, login)`.
   - **Compute role** (see next section); reject with a flash if
     external login is disabled and the user isn't in the org.
   - Upsert into `users` by `github_user_id`. Update
     `github_username` (may have changed since last login),
     `last_login_at`, `last_role_sync_at`. If `role_locked=False`,
     overwrite `role` with the freshly computed value.
   - Set `request.session["user_id"]`, rotate the CSRF token,
     303 to the original `next` or `/`.
   - Discard the GitHub access token. We don't store it.

3. **Role computation** with the user's freshly issued token:
   - If `OPP_CI_GITHUB_ORG` is empty → role = `readonly` (and we
     skip team checks entirely).
   - Else `GET /user/orgs` → is the user in `<org>`?
     - If not, and `OPP_CI_GITHUB_ALLOW_EXTERNAL=1` → `readonly`.
     - If not, and `OPP_CI_GITHUB_ALLOW_EXTERNAL=0` → reject.
   - If in org: `GET /user/teams` → list teams the user belongs to,
     filter to `<org>/*`. If any team slug is in
     `OPP_CI_GITHUB_ADMIN_TEAMS` → `admin`. Else if any matches
     `OPP_CI_GITHUB_SUBMITTER_TEAMS` (or that var is `*`) →
     `submitter`. Else → `readonly`.
   - Cache decision implicitly via `users.role` until the user's
     next login. We don't periodically poll GitHub.

Why the user's own token for org/team queries: it avoids needing a
bot PAT with `read:org` on the OAuth App. The user already consented
to `read:org` when they logged in, so their token can read it without
extra scope grants from anyone else.

### Sessions, CSRF, dependency, mutation gating

Unchanged from the previous design — see the same sections below for
session middleware (Starlette `SessionMiddleware` with required
`OPP_CI_SESSION_SECRET`), the `require_user(minimum_role)`
dependency, moving HTML routes onto a `web_router` with
`dependencies=[Depends(require_user())]` so no route can accidentally
be left ungated, and a per-session CSRF token rendered via a Jinja
macro.

Login routes (`/login`, `/login/github`, `/login/github/callback`,
`/logout`) sit on the top-level `app`, outside the gated router. The
`/api/*` router also stays on `app` so its bearer-token auth keeps
owning that surface.

### Local login (break-glass)

The `GET /login` form shows:

- A big **"Sign in with GitHub"** button (hidden if
  `OPP_CI_GITHUB_OAUTH_CLIENT_ID` is unset).
- A collapsed "Use a local account" disclosure with username +
  password fields.

The local form `POST /login` looks up `User` by `username`, verifies
`password_hash` (constant-time via `passlib.verify`), checks
`enabled`. Same session-set + CSRF-rotate + redirect-to-next behavior
as the OAuth callback.

If both auth paths are configured, the operator can use either; if
only one is configured, only that one renders.

### Users / admin page

`GET /admin/users` lists all `User` rows:

- Columns: username (local) or `@github_username`, role, locked?,
  enabled?, last login.
- Actions per row: **Promote** (`readonly`→`submitter`→`admin`),
  **Demote** (reverse), **Lock role** / **Unlock role**, **Disable**
  / **Enable**.
  - Promote/Demote set `role_locked=True` automatically (otherwise
    the next OAuth login would undo the change).
  - Unlock means "go back to letting GitHub decide" — next login
    recomputes from team membership.

`GET /admin/users/new` lets admin create a local account directly
(same form as `opp_ci user create`).

### Bootstrap

`opp_ci user create --username root --role admin` in
[`opp_ci/cli.py`](../../opp_ci/cli.py). Prompts for password
(getpass) twice if not supplied. Errors if the user exists unless
`--update-password` is passed. This is the *only* supported way to
get the first admin in before anyone has logged in via GitHub —
there is no "default admin", no "first GitHub login wins", both of
which are deploy-time footguns.

After the local admin logs in, they can configure GitHub OAuth via
env files, restart, and from then on log in via GitHub. The local
account stays around as a break-glass path.

### What the OAuth App needs on GitHub

Documented in `doc/web-login.md`:

1. **Register an OAuth App** at
   `https://github.com/organizations/<org>/settings/applications` (or
   personal if no org).
2. **Homepage URL**: `https://opp-ci.example.com` (the deploy host).
3. **Authorization callback URL**: `https://opp-ci.example.com/login/github/callback`.
4. Click "Generate a new client secret", drop it in
   `/etc/opp_ci/github_oauth_secret` (mode 0600, owned by `opp_ci`).
5. Set `OPP_CI_GITHUB_OAUTH_CLIENT_ID` and
   `OPP_CI_GITHUB_OAUTH_CLIENT_SECRET_FILE` in
   `/etc/opp_ci/serve.env`.

If the org wants to restrict third-party access, the OAuth App needs
to be approved by an org owner — call this out in the doc.

## Implementation steps

1. **Data + crypto + CLI bootstrap** (one commit):
   - `User` model + Alembic migration (partial unique indexes,
     `github_user_id`, `role_locked`).
   - `passlib[bcrypt]` and an HTTP client (`httpx`, already a FastAPI
     transitive dep) in `pyproject.toml`.
   - `opp_ci user create` CLI.

2. **Session middleware + local login** (one commit, no gating yet):
   - `OPP_CI_SESSION_SECRET` / `OPP_CI_SESSION_COOKIE_SECURE`;
     fail-closed startup if secret is unset.
   - `SessionMiddleware`, `require_user` dependency, CSRF helper +
     Jinja macro.
   - `/login`, `/logout`, `login.html` (GitHub button stub still
     wired off).

3. **GitHub OAuth flow** (one commit):
   - Config knobs (`OPP_CI_GITHUB_OAUTH_*`,
     `OPP_CI_GITHUB_ORG`, `OPP_CI_GITHUB_ADMIN_TEAMS`,
     `OPP_CI_GITHUB_SUBMITTER_TEAMS`, `OPP_CI_GITHUB_ALLOW_EXTERNAL`,
     `OPP_CI_PUBLIC_URL`).
   - `/login/github`, `/login/github/callback`, role computation
     helper (`compute_role_from_github(token, user)` →
     `"admin"|"submitter"|"readonly"|None`).
   - GitHub button shown on `login.html` when client ID is set.

4. **Gate the HTML surface** (one commit, the visible change):
   - Refactor HTML routes from `app.get/post` onto
     `web_router = APIRouter(dependencies=[Depends(require_user())])`.
   - Apply stricter `require_user("submitter")` /
     `require_user("admin")` per route.
   - Add CSRF dependency on every `POST`.
   - User chip + logout button in `base.html`; hide
     submitter/admin links from `readonly` users.

5. **Admin users page** (one commit):
   - `GET /admin/users`, `POST /admin/users/{id}/role`,
     `POST /admin/users/{id}/lock`, `POST /admin/users/{id}/disable`.
   - `GET /admin/users/new` + local-user create.

6. **Docs** (one commit):
   - `doc/web-login.md`: OAuth App registration walkthrough,
     bootstrap with `opp_ci user create`, role-mapping config, the
     `role_locked` semantic, break-glass instructions.
   - Cross-link from `doc/deployment.md` and add
     `OPP_CI_SESSION_SECRET` + OAuth env vars to the example env
     files in [`plan/systemd-service.md`](../systemd-service.md).

## Verification

Manual checklist on a clean VM:

- Fresh DB, no users → `GET /` returns 303 → `/login`. The login
  page shows the GitHub button (if configured) and the collapsed
  local form.
- `opp_ci user create --username root --role admin` →
  local login as `root` succeeds, gets in.
- Configure GitHub OAuth (test against a sandbox org), log in via
  GitHub as a member of the configured admin team → role resolves to
  `admin`, user row upserted with `role_locked=False`.
- Log in via GitHub as an external GitHub user with
  `ALLOW_EXTERNAL=1` → resolves to `readonly`; `POST /runs/new`
  returns 403; the "+ New Run" link is hidden in the nav.
- Same external user, `ALLOW_EXTERNAL=0` → login is rejected with a
  clear flash; no `User` row is created.
- Admin promotes the external user to `submitter` via the Users page
  → `role_locked=True` set; user logs in again → role stays
  `submitter` (not recomputed from GitHub).
- Admin unlocks the same user → next login recomputes →
  `readonly`.
- `curl /api/runs` still returns 401 with no header (unchanged path).
- Restart the process with the same `OPP_CI_SESSION_SECRET` →
  existing browser session keeps working. Rotate the secret →
  every browser is logged out next request.
- Cross-site `<form action="…/runs/new" method="POST">` → blocked
  by CSRF; same-origin form → succeeds.
- `state` parameter mismatch on the OAuth callback → rejected with
  a clear error, no session set.

## Open questions

- **`OPP_CI_PUBLIC_URL` in the systemd plan.** The OAuth callback
  URL has to be reachable by the user's browser, not by workers. The
  systemd installer should refuse to start `serve` if
  `OPP_CI_GITHUB_OAUTH_CLIENT_ID` is set but `OPP_CI_PUBLIC_URL` is
  empty — otherwise we silently send people to a callback URL
  derived from `Host:` header, which is brittle behind reverse
  proxies.

- **OAuth App vs GitHub App.** Plan uses OAuth App (simpler, no
  installation concept, the right primitive for "sign in" flows).
  GitHub App buys org-installation gating but adds complexity we
  don't need for login. Note in the doc.

- **Org token caching policy.** Plan recomputes role on every
  login, never in the background. If a user is removed from a team,
  their `submitter`/`admin` access persists until they log in again
  (which re-syncs) or until an admin disables them. Acceptable for
  v1 — call it out in the doc. A periodic resync job is a future
  add-on.

- **Account linking.** What if a local-account admin later wants to
  also be a GitHub user? Add a "Link GitHub" button on a
  `/profile` page that goes through the OAuth flow and writes
  `github_user_id` onto the existing User row. Future work; not in
  v1.

- **PR-author UX.** External contributors land on `/login`, click
  "Sign in with GitHub", and end up on the dashboard as `readonly`.
  Is there a deep-link target that's more useful — e.g.
  `?next=/runs?project=inet&git_ref=pr-123`? The `next` param
  already handles this if links from GitHub status checks include
  it. Worth doing in the GitHub-status integration but tracked
  separately.

- **Session secret rotation procedure.** Plan says "rotating the
  secret logs everyone out", which is correct. Document this; for
  graceful rotation, supporting two secrets at once is a future
  change.
