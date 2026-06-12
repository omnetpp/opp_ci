# Web UI login

The `opp_ci coordinator start` web UI requires every visitor to be authenticated.
Anonymous requests are redirected to `/login`. Two login paths are
supported, and you can run with one or both enabled:

1. **GitHub OAuth** ("Sign in with GitHub"). The primary path for
   shared/public deployments — no passwords to manage, and roles can
   follow GitHub org/team membership automatically.
2. **Local account** (username + password). Created on the host with
   `opp_ci user create`. Used to bootstrap the first admin before
   GitHub OAuth is configured, and as a break-glass path if GitHub is
   unreachable.

The REST API at `/api/*` is **not** affected by web login — workers
and scripts keep using `Authorization: Bearer <token>` as before.

## Bootstrap (run once)

Web login refuses every attempt until at least one user exists. After
installing, create a local admin:

```bash
opp_ci user create --username root --role admin
# password is prompted twice
```

Then set `OPP_CI_SESSION_SECRET` (any random ≥32-byte string) and start
`opp_ci coordinator start`. The coordinator refuses to start if the secret is empty.

```bash
export OPP_CI_SESSION_SECRET="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
opp_ci coordinator start
```

Log in at `/login` as `root` and use the **Admin → Manage users**
page from there on.

> **Rotating the session secret logs everyone out.** Cookies signed
> with the old secret stop validating. For day-to-day operation, keep
> the secret stable; rotate it only if you believe it has leaked.

## Configuring GitHub OAuth

### 1. Register an OAuth App on GitHub

At `https://github.com/organizations/<your-org>/settings/applications`
(or your personal settings for a single-user deploy) → **New OAuth
App**:

| Field | Value |
|---|---|
| Application name | `opp_ci` (or your deploy nickname) |
| Homepage URL | `https://opp-ci.example.com` (where users will visit) |
| Authorization callback URL | `https://opp-ci.example.com/login/github/callback` |

GitHub will show a **Client ID** and let you generate a **Client
Secret**.

### 2. Store the secret on the host

```bash
sudo install -o opp_ci -g opp_ci -m 0600 /dev/stdin \
     /etc/opp_ci/github_oauth_secret <<< 'PASTE-THE-CLIENT-SECRET'
```

### 3. Set the env vars

Add to `/etc/opp_ci/coordinator.env` (or your equivalent):

```ini
OPP_CI_SESSION_SECRET=<random-string-from-bootstrap>
OPP_CI_SESSION_COOKIE_SECURE=1            # auto-on when native TLS is on; set explicitly behind a proxy
OPP_CI_PUBLIC_URL=https://opp-ci.example.com

OPP_CI_GITHUB_OAUTH_CLIENT_ID=Iv1.abcdef0123456789
OPP_CI_GITHUB_OAUTH_CLIENT_SECRET_FILE=/etc/opp_ci/github_oauth_secret
```

`OPP_CI_PUBLIC_URL` is **required** when OAuth is on — the callback URL
sent to GitHub is built from it, and deriving it from a `Host:` header
breaks behind a reverse proxy. The coordinator refuses to start with OAuth
enabled and `PUBLIC_URL` empty.

If you've also enabled native TLS in `opp_ci coordinator start` itself (see
[ssl.md](ssl.md)), `OPP_CI_PUBLIC_URL` **must use `https://`** — the coordinator
checks this at startup. For Cloudflare-fronted deploys, set it to the
Cloudflare-fronted hostname (e.g. `https://ci.omnetpp.org`), not the
origin IP — the GitHub OAuth callback redirects the user's browser, which
must hit Cloudflare's edge, not the origin direct.

Restart `opp_ci-coordinator.service`; the "Sign in with GitHub" button now
appears on the login page.

## Authorization policy: who gets which role?

Authentication says *who* you are; authorization decides *what you can
do*. Roles, low → high:

| Role | Can browse | Can submit/edit | Can admin |
|---|---|---|---|
| `readonly`  | ✓ |   |   |
| `submitter` | ✓ | ✓ |   |
| `admin`     | ✓ | ✓ | ✓ |

When a user logs in via GitHub, their role is computed from these
config vars (set in `coordinator.env`):

| Env var | Meaning | Default |
|---|---|---|
| `OPP_CI_GITHUB_ORG` | GitHub org to check membership against | empty → no check, every GitHub user → `readonly` |
| `OPP_CI_GITHUB_ADMIN_USERS` | comma-separated GitHub logins (org members) who always get `admin`, regardless of team | empty |
| `OPP_CI_GITHUB_ADMIN_TEAMS` | comma-separated team slugs whose members get `admin` | empty |
| `OPP_CI_GITHUB_SUBMITTER_TEAMS` | team slugs that get `submitter`. Special value `*` = any member of the org | `*` |
| `OPP_CI_GITHUB_ALLOW_EXTERNAL` | `1`: non-org GitHub users sign in as `readonly`. `0`: rejected | `1` |

Example for an OMNeT++-style open-source project:

```ini
OPP_CI_GITHUB_ORG=omnetpp
OPP_CI_GITHUB_ADMIN_USERS=rhornig
OPP_CI_GITHUB_ADMIN_TEAMS=admins
OPP_CI_GITHUB_SUBMITTER_TEAMS=*
OPP_CI_GITHUB_ALLOW_EXTERNAL=1
```

Result:

- The org member `rhornig` → `admin` (named explicitly)
- Members of `omnetpp/admins` → `admin`
- Other `omnetpp` org members → `submitter`
- Anyone else with a GitHub account → `readonly` (can see PR build
  results without needing to be added to the org)

For a private / commercial deployment, set
`OPP_CI_GITHUB_ALLOW_EXTERNAL=0` so only org members can log in.

### Overriding the GitHub-derived role

An admin can promote/demote any user from **Admin → Manage users**.
Changing the role in the UI **locks** it — the next time that user
logs in, their team membership is not re-read. Click **unlock** to go
back to GitHub-derived behavior.

This lets you:

- Grant `admin` to someone who isn't in the admin team (their
  promotion sticks across logins).
- Revoke access for someone still in the org by disabling them or
  setting them to `readonly` and locking it.

### Recomputation frequency

The role is recomputed *only at login time*. If someone is removed
from an admin team, they keep `admin` until they log in again (which
re-syncs from GitHub) or until you disable them in Manage users.

## Local accounts

Use `opp_ci user create` for accounts that don't go through GitHub:

```bash
opp_ci user create --username alice --role submitter
opp_ci user create --username bot   --role admin       # break-glass
opp_ci user create --username alice --role admin --update-password
opp_ci user list
opp_ci user disable alice
```

Local accounts always have `role_locked=True`. They are independent
from API tokens (`opp_ci token create …`); a person who needs both a
UI account and API access gets one of each.

## CSRF

Every form on the gated HTML surface includes a per-session CSRF
token. The cookie is set with `samesite=lax`, and the server rejects
POSTs missing or mismatched on the form. There is nothing to
configure.

## Troubleshooting

- **`/login` says "OAuth state mismatch"** — the user's session
  expired between clicking "Sign in with GitHub" and the callback. Try
  again. If it keeps happening, check that `OPP_CI_SESSION_SECRET` is
  stable across the server processes the user hits (i.e. you're not
  rotating it on every restart, and load balancers stick a user to a
  single backend or share state).

- **`/login` says "Your GitHub account is not authorized"** — the
  user isn't in `OPP_CI_GITHUB_ORG` and `OPP_CI_GITHUB_ALLOW_EXTERNAL=0`.

- **coordinator refuses to start** — `OPP_CI_SESSION_SECRET` is empty, or
  `OPP_CI_GITHUB_OAUTH_CLIENT_ID` is set without `OPP_CI_PUBLIC_URL`.
  Both are deliberate fail-closed checks; set the missing var.

- **Locked out (no admin account exists)** — SSH to the host and run
  `opp_ci user create --username rescue --role admin
  --update-password`. The CLI talks to the same DB as the coordinator and
  doesn't need the web UI.
