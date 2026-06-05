# Plan: native TLS (SSL) support for `opp_ci serve` and the worker/client

Goal: let `opp_ci serve` terminate HTTPS itself, so a small deploy can
get end-to-end TLS without putting Caddy or nginx in front. The
reverse-proxy path stays fully supported (and remains recommended for
multi-tenant / multi-service hosts), but a single-VM operator should be
able to run

```
OPP_CI_SERVE_TLS_CERT_FILE=/etc/opp_ci/tls/fullchain.pem
OPP_CI_SERVE_TLS_KEY_FILE=/etc/opp_ci/tls/privkey.pem
opp_ci serve --host 0.0.0.0 --port 443
```

…and get a working `https://ci.example.org` with no other moving parts.

The worker and Python client speak the same REST API; both must be able
to verify the coordinator's certificate when it is *not* a public CA
(self-signed lab cert, internal CA). That side gets a `CA_BUNDLE` knob.

## Scope

In scope:

- New config + CLI flags for cert/key files in `opp_ci serve`.
- `uvicorn.run(..., ssl_certfile=..., ssl_keyfile=...)` plumbing.
- Auto-flip `OPP_CI_SESSION_COOKIE_SECURE=1` when TLS is on (currently
  the operator has to remember to set both).
- Auto-flip the OAuth-callback scheme: when `OPP_CI_PUBLIC_URL` is
  empty, derive `https://…` from `Host:` if TLS is enabled; refuse to
  start if OAuth is configured with HTTPS missing.
- Worker + Python client: `OPP_CI_TLS_CA_BUNDLE` (path to a CA bundle
  PEM) and `OPP_CI_TLS_INSECURE=1` (skip verification — dev only,
  fail-noisy startup warning). Plumbed through
  [`opp_ci/client.py`](../../opp_ci/client.py) and
  [`opp_ci/worker.py`](../../opp_ci/worker.py).
- systemd: a `/etc/opp_ci/tls/` directory created by `install.sh`;
  group-readable by `opp_ci`; a shipped **drop-in** that turns on
  `CAP_NET_BIND_SERVICE` for port 443 without touching the main unit;
  the same drop-in is what enables `LoadCredential=` for operators who
  want the key to stay root-owned.
- Cert-renewal story: a shipped systemd **`.path` unit** that watches
  the cert file and restarts `opp_ci-serve.service` on change — so
  renewal works the same whether the ACME client is certbot, acme.sh,
  Caddy-as-acme-only, or a hand-copied cert. No per-client deploy
  hook needed.
- One small helper: `opp_ci tls-selfsign --out /etc/opp_ci/tls
  --host ci.lab.local` to generate a self-signed cert + key pair for
  lab / smoke-test use, with sane defaults (4096-bit RSA, 365 days, SAN
  matching `--host`).

Out of scope (note, don't implement):

- Bundling an ACME client. Certbot / acme.sh / Caddy stay external. The
  plan only commits to *consuming* the renewed files and reloading the
  service.
- HTTP/2, HTTP/3, ALPN tuning. uvicorn's defaults are fine for now.
- mTLS for worker → coordinator. A nice-to-have for hardened deploys
  but out of scope; the bearer-token + HTTPS combination is enough for
  v1.
- SNI / multi-host on one process. One cert per `opp_ci serve`.
- Native HTTP→HTTPS redirect by also binding port 80 from the same
  process. If an operator wants that, they install a reverse proxy
  (which is what they were doing already) or add an `iptables -j
  REDIRECT` rule. Adding a second uvicorn worker for port 80 doubles
  the surface area for very little benefit.

## Design

### Config

Add to [`opp_ci/config.py`](../../opp_ci/config.py):

```python
SERVE_TLS_CERT_FILE = os.environ.get("OPP_CI_SERVE_TLS_CERT_FILE", "")
SERVE_TLS_KEY_FILE  = os.environ.get("OPP_CI_SERVE_TLS_KEY_FILE", "")
SERVE_TLS_KEY_PASSWORD_FILE = os.environ.get("OPP_CI_SERVE_TLS_KEY_PASSWORD_FILE", "")

TLS_CA_BUNDLE  = os.environ.get("OPP_CI_TLS_CA_BUNDLE", "")
TLS_INSECURE   = os.environ.get("OPP_CI_TLS_INSECURE", "0") == "1"
```

`SERVE_TLS_CERT_FILE` and `SERVE_TLS_KEY_FILE` are paired: setting one
without the other is a startup error. Empty pair → plain HTTP, the
current behaviour, no change for existing deploys. `*_PASSWORD_FILE` is
optional for encrypted keys; uvicorn maps it to `ssl_keyfile_password`.

Validation rules at `serve` startup, after config load:

| Condition | Action |
|---|---|
| `CERT_FILE` set, `KEY_FILE` empty (or vice versa) | abort with a clear error |
| `CERT_FILE` set, file unreadable for the running user | abort with the path + errno |
| `CERT_FILE` set, `SESSION_COOKIE_SECURE` empty | set it to `1`, log "TLS on → forcing secure cookies" |
| `CERT_FILE` set, `PUBLIC_URL` empty, `GITHUB_OAUTH_CLIENT_ID` set | abort: "PUBLIC_URL required for OAuth callback" (existing rule, now also fires when TLS is on so the URL is `https://`) |
| `CERT_FILE` empty, `SESSION_COOKIE_SECURE=1` | warn (cookies will not flow on plain HTTP — likely a misconfiguration) |

The "auto-secure-cookies" branch is the one with the highest
foot-shoot value today: an operator who turns on TLS but forgets the
secure-cookie flag silently keeps cookies that work over plain HTTP
too. Default-on is correct here.

### CLI

`opp_ci serve` gains two flags in [`opp_ci/cli.py`](../../opp_ci/cli.py):

```
--cert PATH    SSL certificate file (default $OPP_CI_SERVE_TLS_CERT_FILE)
--key PATH     SSL private key file (default $OPP_CI_SERVE_TLS_KEY_FILE)
```

The startup banner switches scheme accordingly:

```
Starting opp_ci web UI at https://0.0.0.0:443
```

`uvicorn.run` call:

```python
uvicorn.run(
    app,
    host=host,
    port=port,
    log_level="info",
    ssl_certfile=cert_file or None,
    ssl_keyfile=key_file or None,
    ssl_keyfile_password=_read_password_file(cfg.SERVE_TLS_KEY_PASSWORD_FILE) or None,
)
```

uvicorn handles "None means plain HTTP" already; no extra branching.

### Worker + Python client trust

Both `opp_ci/client.py` and `opp_ci/worker.py` build a `requests`
session. Centralize the TLS verification choice:

```python
# opp_ci/http.py  (new tiny module)
def configure_session(session, *, ca_bundle="", insecure=False):
    if insecure:
        session.verify = False
        # Silence the urllib3 warning once at process start, not per request.
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    elif ca_bundle:
        session.verify = ca_bundle
    # else: leave default (system CA store via certifi)
```

Worker and client call this once on their `requests.Session`. The
`OppCiClient` constructor gains an optional `verify` parameter so
programmatic callers can override; falls back to config.

Worker startup logs a warning line when `TLS_INSECURE=1`:

```
WARNING: TLS verification disabled (OPP_CI_TLS_INSECURE=1) — never use this in production.
```

…so an operator who flipped it for "just to test it" notices it in
journalctl every restart.

### `opp_ci tls-selfsign` helper

A small subcommand for lab / smoke-test use. Wraps Python's
`cryptography` (already a transitive dep of `passlib[bcrypt]`'s
optional `crypto`? — verify; if not, add it explicitly):

```
opp_ci tls-selfsign \
    --host ci.lab.local \
    --out /etc/opp_ci/tls \
    [--days 365] [--bits 4096]
```

Writes `tls/fullchain.pem` + `tls/privkey.pem`, mode 0640
`root:opp_ci`. SAN list includes `--host`, `localhost`, `127.0.0.1`,
and the machine's hostname so a worker on the same VM can connect by
hostname.

This is documented as **non-production**: workers will refuse the cert
unless their `OPP_CI_TLS_CA_BUNDLE` points at the same `fullchain.pem`
(or `TLS_INSECURE=1`). The selfsign command prints those two lines as
post-install hints.

### systemd integration

The goal here is that an operator who already deployed `opp_ci`
via `packaging/systemd/install.sh` can turn on TLS by editing
*only* env files (and, for port 443, dropping in one extra unit).
The shipped `opp_ci-serve.service` does **not** need to be edited;
TLS-specific overrides live in a drop-in, so a re-run of `install.sh`
that overwrites the main unit doesn't blow them away.

#### File: cert/key location and ownership

`install.sh` gains one new line:

```bash
install -d -o root -g "$OPP_CI_GROUP" -m 0750 "$CONFIG_DIR/tls"
```

…producing `/etc/opp_ci/tls/`, root-owned, group `opp_ci`, mode
`0750`. The cert and key are operator-supplied (Let's Encrypt copy,
self-signed via `opp_ci tls-selfsign`, or a corporate CA file). The
documented permissions are:

| File | Owner | Mode | Notes |
|---|---|---|---|
| `/etc/opp_ci/tls/` | `root:opp_ci` | `0750` | dir |
| `/etc/opp_ci/tls/fullchain.pem` | `root:opp_ci` | `0640` | cert + chain, group-readable |
| `/etc/opp_ci/tls/privkey.pem` | `root:opp_ci` | `0640` | key; tighter via `LoadCredential=` (below) |

Mode `0640` with group `opp_ci` lets the service read the key without
being root. Operators uncomfortable with that — even though the service
is the only thing in that group — should use the `LoadCredential=`
path described below, where the key file stays `0600 root:root`.

`opp_ci tls-selfsign --out /etc/opp_ci/tls` writes the files with
these exact owners/modes by default.

#### Sandbox interaction with the existing unit

The shipped unit already sets `NoNewPrivileges=true`,
`ProtectSystem=strict`, `ProtectHome=true`, `PrivateTmp=true`,
`ReadWritePaths=/var/lib/opp_ci`. All four are TLS-compatible:

- `ProtectSystem=strict` makes the filesystem read-only but does not
  hide `/etc/opp_ci/`, so the cert and key are still readable.
- `NoNewPrivileges=true` is compatible with `AmbientCapabilities=`:
  ambient caps are granted at exec, not "gained" later, so the
  no-new-privs ratchet doesn't block them. (Confirmed in `systemd.exec(5)`.)
- `PrivateTmp=true` is fine — uvicorn doesn't use `/tmp` for cert
  state.

No `ReadWritePaths=` additions are needed: the service only reads the
cert files.

#### Drop-in: `opp_ci-serve.service.d/tls.conf`

Shipped at `packaging/systemd/dropins/tls.conf.example`. `install.sh`
copies it (only if missing) to
`/etc/systemd/system/opp_ci-serve.service.d/tls.conf.example` — the
`.example` suffix means the operator must rename to `tls.conf` to
activate it. This avoids the "installer secretly opened port 443"
surprise.

```ini
# /etc/systemd/system/opp_ci-serve.service.d/tls.conf
#
# Activates native TLS in opp_ci-serve.
#
# Cert + key path comes from OPP_CI_SERVE_TLS_{CERT,KEY}_FILE in
# /etc/opp_ci/serve.env. This drop-in only handles the systemd-side
# concerns: privileged-port binding and (optionally) credential isolation.

[Service]
# Bind 443 without running as root. Compatible with NoNewPrivileges=true.
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE

# Option A (default): the service reads the key directly from
# /etc/opp_ci/tls/privkey.pem (must be mode 0640, group opp_ci).
#
# Option B (hardened): keep the key root-owned at 0600 and have systemd
# hand a copy to the unit on each start. Requires systemd ≥ 250.
# Uncomment, then change OPP_CI_SERVE_TLS_KEY_FILE in serve.env to
# ${CREDENTIALS_DIRECTORY}/privkey.pem :
#
# LoadCredential=privkey.pem:/etc/opp_ci/tls/privkey.pem
```

Why a drop-in rather than editing the main unit:

- `install.sh` overwrites the main unit on every re-run (the only safe
  default). A drop-in survives.
- The drop-in is empty/absent on a host that doesn't use native TLS.
  Nothing to maintain, nothing to mis-configure.
- An operator running behind a reverse proxy keeps the main unit
  unchanged and never touches the drop-in.

#### `LoadCredential=` (option B above) — when to recommend it

systemd ≥ 250 (Ubuntu 22.04 ships 249; 24.04 ships 255). On 24.04 the
hardened path is a one-liner. On 22.04 hosts the operator falls back
to option A (group-readable key); the doc says so explicitly.

With `LoadCredential=privkey.pem:/etc/opp_ci/tls/privkey.pem`:

- The key file on disk stays `0600 root:root`.
- At service start, systemd copies it to a private tmpfs at
  `$CREDENTIALS_DIRECTORY/privkey.pem`, only readable by the unit.
- `serve.env` points
  `OPP_CI_SERVE_TLS_KEY_FILE=${CREDENTIALS_DIRECTORY}/privkey.pem`
  (systemd substitutes the env var before exec).

Trade-off: a restart is required to re-read the key (acceptable —
that's what the `.path` unit below does anyway).

#### Cert-renewal: a shipped `.path` unit

Instead of writing per-ACME-client deploy hooks, we ship a `.path` unit
that watches the cert file and restarts the service when it changes.
Renewal becomes "drop the new files into `/etc/opp_ci/tls/`"
regardless of the source.

`packaging/systemd/opp_ci-serve-cert.path`:

```ini
[Unit]
Description=Watch opp_ci TLS cert for renewal
# Only meaningful when serve is running; stop it together.
PartOf=opp_ci-serve.service

[Path]
# Fires on close-after-write or atomic rename, which is what acme.sh,
# certbot's deploy hook, and `install -m` all do.
PathChanged=/etc/opp_ci/tls/fullchain.pem
Unit=opp_ci-serve-cert-reload.service

[Install]
WantedBy=opp_ci.target
```

`packaging/systemd/opp_ci-serve-cert-reload.service`:

```ini
[Unit]
Description=Restart opp_ci-serve after TLS cert change
After=opp_ci-serve.service
# Don't fire if serve isn't even running — avoids accidentally starting it.
Requisite=opp_ci-serve.service

[Service]
Type=oneshot
ExecStart=/bin/systemctl restart opp_ci-serve.service
```

`install.sh` installs both files and updates the "Next steps" hint
to mention `systemctl enable --now opp_ci-serve-cert.path` for hosts
running native TLS.

The renewal flow is then:

1. ACME client (or hand) writes new `/etc/opp_ci/tls/fullchain.pem`
   (and updates `privkey.pem` first, or atomically).
2. `.path` unit fires.
3. `cert-reload.service` runs `systemctl restart opp_ci-serve.service`.
4. New cert served on next handshake.

Active HTTP sessions get one TLS reconnect (~ms). Workers retry their
poll on the next interval. Acceptable for an event that happens twice
a quarter.

**Edge case:** if the key is updated *after* the cert and the `.path`
fires in between, the service comes up with mismatched cert/key and
crashes on the first handshake. Document: write the key first, then
the cert (acme.sh and certbot's `--deploy-hook` do this in the right
order when copying; `tls-selfsign` writes them transactionally).

#### `serve.env.example`

Gains a TLS block (commented out):

```
# ── TLS ───────────────────────────────────────────────────────────
# Native TLS termination. To use, also enable the shipped drop-in:
#   sudo mv /etc/systemd/system/opp_ci-serve.service.d/tls.conf.example \
#           /etc/systemd/system/opp_ci-serve.service.d/tls.conf
#   sudo systemctl daemon-reload
#   sudo systemctl enable --now opp_ci-serve-cert.path
#
#OPP_CI_SERVE_TLS_CERT_FILE=/etc/opp_ci/tls/fullchain.pem
#OPP_CI_SERVE_TLS_KEY_FILE=/etc/opp_ci/tls/privkey.pem
#OPP_CI_SERVE_TLS_KEY_PASSWORD_FILE=/etc/opp_ci/tls/key.password
#
# For LoadCredential= mode (option B in the drop-in), use:
#OPP_CI_SERVE_TLS_KEY_FILE=${CREDENTIALS_DIRECTORY}/privkey.pem
#
# Required when TLS is on (for OAuth callbacks and worker URLs):
#OPP_CI_PUBLIC_URL=https://ci.example.org
# Auto-on when TLS is detected; uncomment only to override:
#OPP_CI_SESSION_COOKIE_SECURE=1
# Bind to 443 (needs the drop-in for CAP_NET_BIND_SERVICE):
#OPP_CI_SERVE_PORT=443
#OPP_CI_SERVE_HOST=0.0.0.0
```

#### Worker unit: trust, not termination

Worker hosts don't terminate TLS; they connect outbound to the
coordinator. The `opp_ci-worker@.service` unit needs **no changes**.
The only TLS-related thing on a worker is the env file:

`packaging/systemd/worker.env.example` gains:

```
# When the coordinator uses a non-public CA (self-signed or internal):
#OPP_CI_TLS_CA_BUNDLE=/etc/opp_ci/tls/fullchain.pem
#
# Dev only: skip verification entirely. Logs a warning each restart.
#OPP_CI_TLS_INSECURE=0
```

On a same-host install (coordinator + worker on one VM), this is
typically the same `fullchain.pem` the serve unit reads; mode 0640
with group `opp_ci` already grants the worker access.

On a separate worker host, `install.sh` does not copy the bundle —
that's the operator's job (`scp` or pulled from a known location).
Document this.

#### Renewal-time health check

After a `.path`-triggered restart, the operator can verify with:

```bash
systemctl is-active opp_ci-serve-cert.path           # active (watching)
systemctl status   opp_ci-serve.service              # active, recent start
curl --cacert /etc/opp_ci/tls/fullchain.pem \
     https://ci.example.org/api/health               # 200
```

Don't health-check via TLS *immediately* after the restart — uvicorn's
bind+listen takes ~tens of ms; a tight `curl` race can hit
`Connection refused`. Either retry or skip; `systemctl is-active`
already covers "is it up?".

#### Why not ACME-in-process

We deliberately *don't* embed an ACME client in `opp_ci serve`.
That would couple our release cadence to a TLS-PKI dependency, and an
operator that wants Caddy in front (or already runs certbot) gets no
benefit. The `.path` + drop-in design means "however you get a cert
into `/etc/opp_ci/tls/`, the rest is automatic" — which is the right
seam.

### Deployment shape: Cloudflare Origin Certificate

The chosen deployment pattern. Traffic flows
**browser → Cloudflare edge → opp_ci origin**, where Cloudflare's
edge presents a publicly trusted cert and the origin presents a
**Cloudflare Origin Certificate** (issued by Cloudflare's Origin CA,
not in any public trust store, valid up to 15 years).

#### Setup

1. In the Cloudflare dashboard for the zone (e.g. `ci.omnetpp.org`):
   - SSL/TLS → Origin Server → **Create Certificate**. Hostnames:
     the public hostname and optionally `*.<zone>`. RSA-2048 or
     ECDSA. Validity 15 years (the maximum).
   - Dashboard shows the cert PEM and key PEM **once** — copy both
     immediately into `/etc/opp_ci/tls/` on the origin host:
     ```bash
     install -m 0640 -o root -g opp_ci /dev/stdin /etc/opp_ci/tls/privkey.pem    <<< '...key PEM...'
     install -m 0640 -o root -g opp_ci /dev/stdin /etc/opp_ci/tls/fullchain.pem  <<< '...cert PEM...'
     ```
   - SSL/TLS → Overview → mode: **Full (strict)**. Anything less
     defeats the point of having an origin cert — Cloudflare won't
     verify what the origin presents.

2. On the origin host:
   - Enable native TLS exactly as documented above
     (`OPP_CI_SERVE_TLS_CERT_FILE`, `*_KEY_FILE`, rename `tls.conf.example`
     → `tls.conf`, bind 443).
   - `OPP_CI_PUBLIC_URL=https://ci.omnetpp.org` — the
     Cloudflare-fronted URL. This is what GitHub's OAuth callback,
     webhook receiver registration, and worker `OPP_CI_COORDINATOR_URL`
     must match.

3. In the GitHub OAuth App (per [`web-login.md`](../../doc/web-login.md)):
   - Authorization callback URL =
     `https://ci.omnetpp.org/login/github/callback`. Cloudflare-fronted,
     publicly trusted — GitHub doesn't need any Origin-CA awareness.

#### What about workers and the Python client?

It depends on whether they go through Cloudflare's edge or hit the
origin directly:

| Connector | Endpoint | CA bundle? |
|---|---|---|
| Worker / `OppCiClient` using `OPP_CI_COORDINATOR_URL=https://ci.omnetpp.org` | Cloudflare edge | **no** — edge cert is publicly trusted, system CA store works |
| Same, but pointing at the origin IP/internal DNS to bypass Cloudflare | origin Cloudflare Origin Cert | **yes** — set `OPP_CI_TLS_CA_BUNDLE=/etc/opp_ci/tls/cloudflare-origin-ca.pem` |

The Cloudflare Origin CA root is a public download from Cloudflare's
docs site. `install.sh` ships it under
`packaging/systemd/cloudflare-origin-ca.pem` and copies it into
`/etc/opp_ci/tls/` so operators on the origin host can point workers
at it without a second download step. Re-fetch the file when
Cloudflare publishes a rotated root (rare; document the URL in
`doc/ssl.md`).

Default recommendation: **route workers through Cloudflare's edge**
(set their `OPP_CI_COORDINATOR_URL` to the public hostname). Reasons:

- No `CA_BUNDLE` config per worker — keeps env files boring.
- DDoS / abuse protections at the edge apply to worker traffic too.
- The origin IP can stay firewalled to Cloudflare ranges only (see
  open questions below).

Bypassing the edge only makes sense for same-host workers (worker on
the coordinator VM connecting via `127.0.0.1` or `localhost`); for
those, set `OPP_CI_COORDINATOR_URL=http://127.0.0.1:8080` and disable
TLS for that one call (loopback is already trusted) — *or* keep
HTTPS + `OPP_CI_TLS_CA_BUNDLE`.

#### Renewal

Cloudflare Origin Certs default to 15 years. The shipped `.path` unit
sits dormant essentially forever, but it stays installed so a manual
rotation (e.g. key compromise, hostname change) goes through the same
restart machinery as any other cert change. Operators who rotate
mid-lifetime just repeat step 1 (dashboard → install over the
existing PEM files).

There is no ACME loop for this path — `certbot` / `acme.sh` /
`opp_ci tls-selfsign` are all unused. `tls-selfsign` stays in scope
for lab use where Cloudflare isn't involved (developer laptop,
air-gapped install).

#### Notes specific to this shape

- **Origin cert SAN coverage.** When generating the cert, include
  every hostname the origin will be reached as. If a worker is going
  to be told `OPP_CI_COORDINATOR_URL=https://origin.internal:443`
  *and* `--cacert cloudflare-origin-ca.pem`, then `origin.internal`
  must be in the SAN list — otherwise the worker fails hostname
  verification even though the chain validates.
- **Cookie scope.** `SESSION_COOKIE_SECURE` flips on automatically;
  the cookie's `Domain` attribute remains the request host
  (`ci.omnetpp.org`). Nothing extra to configure.
- **HSTS at Cloudflare.** Enable HSTS in the Cloudflare dashboard
  (SSL/TLS → Edge Certificates → HSTS). Belongs at the edge, not in
  uvicorn — that resolves the earlier open question about whether to
  add an in-process HSTS middleware. The middleware is unnecessary
  for this deployment shape.

### What changes outside the package

`OPP_CI_COORDINATOR_URL` and `OPP_CI_PUBLIC_URL`: switch from `http://`
to `https://` when migrating. The plan does not auto-rewrite these;
it just documents that they must be updated *before* TLS is enabled,
otherwise OAuth callback URLs and worker registrations break.

The reverse-proxy path stays in
[`doc/deployment.md`](../../doc/deployment.md) and
[`doc/systemd.md`](../../doc/systemd.md) — both stay valid. The new
`doc/ssl.md` is additive: it covers the native-TLS path and links back
to the proxy section for "you probably want this instead if you also
host other services on this VM".

## Implementation steps

1. **Config + CLI plumbing** (one commit):
   - Add `SERVE_TLS_*`, `TLS_CA_BUNDLE`, `TLS_INSECURE` to
     `opp_ci/config.py`.
   - Add `--cert`, `--key` to `cli.py:serve`. Pass through to
     `uvicorn.run`. Validation rules above. Auto-flip cookie-secure.

2. **Worker + client trust** (one commit):
   - New `opp_ci/http.py:configure_session()`.
   - Call from `OppCiClient.__init__` and from the worker's
     `requests.Session` construction in [`opp_ci/worker.py`](../../opp_ci/worker.py).
   - Add `verify=...` param to `OppCiClient.__init__` for programmatic
     callers.

3. **Self-sign helper** (one commit):
   - `opp_ci tls-selfsign` subcommand in `cli.py`, uses
     `cryptography` to build a cert. Add to `pyproject.toml` if not
     already present transitively.
   - Print post-install hints (where the cert went, env vars to
     uncomment, CA-bundle path for workers).

4. **Systemd artefacts** (one commit, all under
   `packaging/systemd/`):
   - `install.sh`: create `/etc/opp_ci/tls/` (root:opp_ci, 0750);
     install the new unit files; install the drop-in as
     `tls.conf.example` (operator renames to activate); update the
     "Next steps" block to mention `opp_ci-serve-cert.path`.
   - New `dropins/tls.conf.example` (the
     `AmbientCapabilities=CAP_NET_BIND_SERVICE` +
     `CapabilityBoundingSet=` drop-in, with `LoadCredential=`
     commented).
   - New `opp_ci-serve-cert.path` (watches
     `/etc/opp_ci/tls/fullchain.pem`).
   - New `opp_ci-serve-cert-reload.service` (oneshot that restarts
     serve).
   - `serve.env.example`: TLS block including the
     `${CREDENTIALS_DIRECTORY}` variant and the drop-in activation
     reminder.
   - `worker.env.example`: `OPP_CI_TLS_CA_BUNDLE` /
     `OPP_CI_TLS_INSECURE` lines, with the Cloudflare-direct-origin
     example commented in.
   - Ship `packaging/systemd/cloudflare-origin-ca.pem` (Cloudflare's
     Origin CA root, copied verbatim from Cloudflare's docs URL).
     `install.sh` places it at `/etc/opp_ci/tls/cloudflare-origin-ca.pem`
     with mode `0644 root:opp_ci`.
   - **No change** to `opp_ci-serve.service` itself (TLS lives in the
     drop-in).
   - **No change** to `opp_ci-worker@.service` itself (TLS-related
     config is env-only).

5. **Docs** (one commit):
   - New `doc/ssl.md` with the following sections:
     - "Recommended: Cloudflare Origin Certificate" — the chosen
       shape, with the dashboard walkthrough, the
       Full-(strict)-mode requirement, the
       `OPP_CI_PUBLIC_URL` value, the firewall-to-Cloudflare-IPs
       note, the worker-routing recommendation (through the edge),
       and the `cloudflare-origin-ca.pem` story for workers that
       bypass.
     - "Alternative: reverse proxy with HTTPS" — link to existing
       `deployment.md` content.
     - "Alternative: native TLS with public ACME (certbot / acme.sh)"
       — the `.path`-unit renewal model.
     - "Alternative: self-signed for lab use" — the `tls-selfsign`
       command + worker CA bundle.
     - "Option A vs option B for the private key" — group-readable
       vs `LoadCredential=`, with the Ubuntu 22.04 / 24.04 systemd
       caveat.
     - "Five-line laptop recipe" — quickest dev path (selfsign +
       `TLS_INSECURE=1` client).
   - Update `doc/deployment.md` to cross-link to `ssl.md` and note
     that the reverse-proxy section remains the recommended path for
     multi-service hosts.
   - Update `doc/systemd.md` "Exposing serve externally" section: add
     a fourth bullet for "native TLS via drop-in + `.path`" alongside
     the existing SSH-tunnel / direct-bind / reverse-proxy options.
   - Update `doc/web-login.md` to note that with native TLS,
     `OPP_CI_PUBLIC_URL=https://...` is mandatory before enabling
     OAuth.

## Verification

Manual checklist on a clean Ubuntu 24.04 VM running the installed
systemd units:

CLI-only (no systemd) sanity:

- `opp_ci tls-selfsign --host ci.lab.local --out /etc/opp_ci/tls`
  creates `fullchain.pem` + `privkey.pem` with the right owners
  (`root:opp_ci`, 0640) and the expected SANs.
- Foreground `opp_ci serve --cert .../fullchain.pem --key
  .../privkey.pem --host 127.0.0.1 --port 8443` → `curl
  --cacert .../fullchain.pem https://127.0.0.1:8443/` returns
  200/303. Without `--cacert`, verification fails — expected.
- Set only `*_CERT_FILE`, leave `*_KEY_FILE` empty → `serve` aborts
  with the validation message and exits non-zero.
- Set both, leave `SESSION_COOKIE_SECURE` unset → serve logs the
  "TLS on → forcing secure cookies" line; cookies arrive with the
  `Secure` flag.
- Configure GitHub OAuth, leave `OPP_CI_PUBLIC_URL` empty → serve
  aborts with the OAuth-needs-PUBLIC_URL message.

Under systemd:

- `install.sh` on a clean VM creates `/etc/opp_ci/tls/` with
  `0750 root:opp_ci`. The drop-in is at
  `/etc/systemd/system/opp_ci-serve.service.d/tls.conf.example`
  (suffix intact). `systemctl status opp_ci-serve.service` works
  unchanged (TLS off).
- Drop a cert+key into `/etc/opp_ci/tls/`, set the TLS env vars in
  `/etc/opp_ci/serve.env`, leave port at 8080, leave the drop-in
  *not* renamed. `systemctl restart opp_ci-serve.service` →
  HTTPS works on 8080. `ss -ltnp` shows the listener on 8080 owned by
  `opp_ci`.
- Rename `tls.conf.example` → `tls.conf`, set
  `OPP_CI_SERVE_PORT=443` in `serve.env`, `systemctl daemon-reload`,
  `systemctl restart opp_ci-serve.service` → `ss -ltnp` shows
  `:443` owned by `opp_ci`. `systemctl show -p AmbientCapabilities
  opp_ci-serve.service` includes `cap_net_bind_service`.
- `NoNewPrivileges` interaction: same step,
  `systemctl show -p NoNewPrivileges opp_ci-serve.service` → `yes`.
  Confirms ambient cap + no-new-privs coexist.
- `LoadCredential=` mode (option B): uncomment the `LoadCredential=`
  line in the drop-in; chmod the key file to `0600 root:root`; set
  `OPP_CI_SERVE_TLS_KEY_FILE=${CREDENTIALS_DIRECTORY}/privkey.pem`
  in `serve.env`; restart → service starts. `ls -l /proc/$PID/cwd`
  and `cat /proc/$PID/status | grep Groups` confirm the service
  cannot read `/etc/opp_ci/tls/privkey.pem` directly.

Renewal:

- `systemctl enable --now opp_ci-serve-cert.path` →
  `systemctl status opp_ci-serve-cert.path` shows active (waiting).
- Overwrite `/etc/opp_ci/tls/fullchain.pem` with a freshly re-issued
  cert (e.g. another `opp_ci tls-selfsign` with `--days 730`).
  Within ~1s, `journalctl -fu opp_ci-serve-cert-reload.service`
  shows the oneshot ran, and `journalctl -fu opp_ci-serve.service`
  shows the restart. `openssl s_client -connect ci.lab.local:443
  </dev/null` shows the new fingerprint.
- Stop `opp_ci-serve.service`. Overwrite the cert again → cert-reload
  service fails its `Requisite=opp_ci-serve.service` check and does
  not silently start serve. Restart serve manually → fine.

Worker side:

- Register a worker against `https://ci.lab.local/api`. Worker
  refuses to start (cert not in system CA store). Set
  `OPP_CI_TLS_CA_BUNDLE=/etc/opp_ci/tls/fullchain.pem` in
  `/etc/opp_ci/workers/default.env` → worker registers, heartbeats,
  picks up jobs.
- `OPP_CI_TLS_INSECURE=1` → worker starts; journalctl shows the
  warning line on each restart.

Cloudflare Origin Cert path (the real deployment):

- Generate an Origin Certificate in the Cloudflare dashboard with
  hostnames `ci.omnetpp.org` and `*.ci.omnetpp.org` (or whichever is
  appropriate). Install the two PEMs at `/etc/opp_ci/tls/`.
- Set SSL/TLS mode = **Full (strict)** in the Cloudflare dashboard.
- `curl https://ci.omnetpp.org/api/health` from a laptop (going via
  Cloudflare's edge) returns 200 with no `--cacert` needed (edge
  cert is publicly trusted).
- From the origin host, `curl --resolve
  ci.omnetpp.org:443:127.0.0.1 --cacert
  /etc/opp_ci/tls/cloudflare-origin-ca.pem
  https://ci.omnetpp.org/api/health` returns 200 (verifies the chain
  against Cloudflare's Origin CA root we shipped).
- Register a worker with
  `OPP_CI_COORDINATOR_URL=https://ci.omnetpp.org` (no
  `OPP_CI_TLS_CA_BUNDLE` set) → worker connects through Cloudflare
  fine.
- Register a second worker with
  `OPP_CI_COORDINATOR_URL=https://<origin-internal-host>:443` and
  `OPP_CI_TLS_CA_BUNDLE=/etc/opp_ci/tls/cloudflare-origin-ca.pem` →
  worker bypasses the edge, validates against the Origin CA, also
  works.
- Tighten origin firewall to Cloudflare IP ranges (`ufw allow from
  173.245.48.0/20 to any port 443`, repeated for each published
  range). External `curl` straight to the origin IP → connection
  refused. Cloudflare-routed traffic still works.

Uninstall:

- `uninstall.sh` (existing) removes serve + worker units and the
  target. Confirm the new `.path` and `cert-reload.service` are also
  removed (add to the existing teardown list). `/etc/opp_ci/tls/`
  is left in place — operator's data.

## Open questions

- **Default port when TLS is enabled.** Right now the default is
  `8080` regardless. Should setting `*_TLS_CERT_FILE` flip the default
  port to `443`? Tempting (matches HTTPS scheme convention), but
  binding 443 requires the ambient capability — silently changing the
  port could surprise an operator whose unit doesn't have the cap. Lean
  toward: don't change the default, document that "if you're using
  native TLS you almost certainly also want `OPP_CI_SERVE_PORT=443`."

- **`SESSION_COOKIE_SECURE` auto-flip.** Plan above defaults it on
  when TLS is on. Edge case: operator runs TLS on `127.0.0.1` for
  testing in a browser via `http://localhost:something` over an SSH
  tunnel — that's HTTP-to-the-browser, and secure cookies would
  disappear. Counter-argument: that case already needs
  `SESSION_COOKIE_SECURE=0` explicit override. Keeping the auto-on
  default + documented escape hatch is the right call; flag for review.

- **Should `tls-selfsign` write to `/etc/opp_ci/tls/` directly, or
  print to stdout and let the operator place it?** Writing is more
  ergonomic for first-time users; printing is safer (no surprise file
  writes as root). Plan picks writing, with a `--dry-run` flag that
  prints the PEMs instead. Reconsider if writing turns out to fight
  with selinux/apparmor on common distros.

- **mTLS for worker → coordinator.** Out of scope per above. Worth
  revisiting once we have a hardened-deploy customer asking for it;
  the bearer-token + server-cert combo is the right v1.

- **HSTS / `Strict-Transport-Security` header.** uvicorn doesn't emit
  it. We could add a tiny middleware that sets it when TLS is
  configured (`max-age=31536000; includeSubDomains`). Low-risk; defer
  to a follow-up if anyone asks, since it's noticeable mainly on
  public deploys, which are also the deploys most likely to be behind
  a proxy that already adds the header.

- **HTTP/2 via uvicorn workers.** uvicorn can serve HTTP/2 only when
  the `httptools` + `hypercorn` combo is used; the stock `uvicorn`
  worker is HTTP/1.1. Acceptable for now; if a customer needs HTTP/2
  for long-poll perf they're already at "use a reverse proxy"
  scale. Note in `doc/ssl.md`.

- **systemd version skew.** Ubuntu 22.04 ships systemd 249, which
  predates `LoadCredential=`. Plan defaults to option A
  (group-readable key) and treats option B as a 24.04+ upgrade path.
  Worth checking whether any of our current deploy targets are still
  on 22.04 — if so, option B is documented but not the default.

- **`.path` unit vs symlink-based ACME layouts.** Let's Encrypt's
  default layout is
  `/etc/letsencrypt/live/<name>/fullchain.pem -> ../../archive/<name>/fullchainN.pem`,
  and renewal rewrites the symlink. `PathChanged=` on the symlink
  target does not fire when only the symlink's target changes.
  Mitigation: the plan documents copying the renewed file into
  `/etc/opp_ci/tls/fullchain.pem` (atomic `install -m`) rather than
  symlinking to the letsencrypt tree. If an operator insists on the
  symlink layout, they need a deploy hook that does `touch
  /etc/opp_ci/tls/fullchain.pem` after renewal — note in
  `doc/ssl.md`.

- **`socket activation`.** A future iteration could bind the TLS
  socket via a `.socket` unit, so renewal is purely "restart the
  service while keeping the listening socket alive" — no client sees
  a refused-connection window. Out of scope for v1; uvicorn doesn't
  trivially support handed-down sockets via `LISTEN_FDS=`. Track
  separately.

- **Origin lockdown to Cloudflare IPs.** With the Cloudflare Origin
  Cert in place, anyone who learns the origin IP can still connect
  to port 443 — they'll get a "this connection is not private"
  warning, but it does mean the origin is internet-reachable.
  Recommendation: firewall the origin to Cloudflare's published IP
  ranges (`https://www.cloudflare.com/ips/`). `install.sh` should
  *not* do this automatically (it would lock out same-host workers
  and admin SSH-on-port-22 isn't affected, but a same-host
  `127.0.0.1` worker would need an explicit allow rule). Document a
  `ufw` recipe in `doc/ssl.md` and leave application to the
  operator. The Cloudflare IP list rotates; a periodic refresh
  (`cloudflare-ip-update` cron or similar) is the operator's
  problem.

- **Cloudflare "Authenticated Origin Pulls"** (mTLS from edge to
  origin). Cloudflare can present a client cert that the origin
  verifies, so origin-IP-firewalling is no longer the only defence.
  This requires turning on uvicorn-side client-cert verification
  (`ssl_cert_reqs=CERT_REQUIRED`, `ssl_ca_certs=<cloudflare's client
  CA>`) — uvicorn supports it, but the plan doesn't currently expose
  the knobs. Worth adding as a follow-up once the basic Cloudflare
  Origin Cert path is in production. Track separately.

- **What happens if the operator forgets Full (strict) mode in
  Cloudflare.** In "Full" (no strict) mode, the edge will accept any
  cert at the origin including an expired or hostname-mismatched
  one. `doc/ssl.md` should call this out explicitly: the Origin Cert
  only buys real protection when Cloudflare is configured to verify
  it.
