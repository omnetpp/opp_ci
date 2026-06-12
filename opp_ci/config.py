import os
import socket


def _load_system_env_file(path):
    """Overlay simple KEY=value lines from `path` into os.environ.

    Skips lines that don't parse as KEY=value, skips KEYs already set in
    the environment (so values supplied by systemd's EnvironmentFile= or
    by an interactive `export FOO=…` always win). Quiet no-op if the file
    is missing — that's the normal case in a developer checkout.
    """
    try:
        f = open(path)
    except OSError:
        return
    with f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            key, sep, value = line.partition("=")
            if not sep:
                continue
            key = key.strip()
            value = value.strip()
            if (len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'")):
                value = value[1:-1]
            if key and key not in os.environ:
                os.environ[key] = value


# Pick up systemd-style shared config when running outside a unit
# (e.g. `sudo -u opp_ci opp_ci worker register …` from a shell).
_load_system_env_file("/etc/opp_ci/opp_ci.env")


DATABASE_URL = os.environ.get("OPP_CI_DATABASE_URL", "sqlite:///opp_ci.db")


def _default_coordinator_url():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return f"http://{ip}:8080"
    except OSError:
        return "http://localhost:8080"


COORDINATOR_URL = os.environ.get("OPP_CI_COORDINATOR_URL", _default_coordinator_url())
API_TOKEN = os.environ.get("OPP_CI_API_TOKEN", "")

# Default for the top-level `--remote` flag. Operators who only ever
# drive a coordinator from their laptop can set OPP_CI_REMOTE=1 once and
# drop `--remote` from every invocation. Host-local commands (coordinator
# start, init-db, worker start, …) ignore it.
REMOTE = os.environ.get("OPP_CI_REMOTE", "0") == "1"

REFERENCE_PLATFORM = os.environ.get("OPP_CI_REFERENCE_PLATFORM", "Ubuntu 24.04/gcc-13")

COORDINATOR_HOST = os.environ.get("OPP_CI_COORDINATOR_HOST", "127.0.0.1")
COORDINATOR_PORT = int(os.environ.get("OPP_CI_COORDINATOR_PORT", "8080"))

# ── Log viewer ────────────────────────────────────────────────────────
#
# The web UI's Logs pages read process logs straight from systemd-journald
# (the coordinator and worker units). These name the units to query; override
# only for a non-standard install. `{instance}` in the worker template is
# the worker's registered name (used verbatim as the systemd instance).
COORDINATOR_UNIT = os.environ.get("OPP_CI_COORDINATOR_UNIT", "opp_ci-coordinator.service")
WORKER_UNIT_TEMPLATE = os.environ.get(
    "OPP_CI_WORKER_UNIT_TEMPLATE", "opp_ci-worker@{instance}.service")
# Lines to fetch on the initial (cursor-less) tail load.
LOG_TAIL_LINES = int(os.environ.get("OPP_CI_LOG_TAIL_LINES", "1000"))

# Per-worker log shipping (for workers running on a different host than the
# coordinator, whose journal the coordinator can't read). The worker keeps
# the last WORKER_LOG_RING records in memory and ships up to WORKER_LOG_BATCH
# new ones per heartbeat; the coordinator keeps the last COORDINATOR_WORKER_LOG_RING
# per worker for the log view. All in-memory — dropped on restart.
WORKER_LOG_RING = int(os.environ.get("OPP_CI_WORKER_LOG_RING", "2000"))
WORKER_LOG_BATCH = int(os.environ.get("OPP_CI_WORKER_LOG_BATCH", "500"))
COORDINATOR_WORKER_LOG_RING = int(os.environ.get("OPP_CI_COORDINATOR_WORKER_LOG_RING", "2000"))

# Live per-run test output (streamed to the run-detail page while a run runs).
# The worker batches output lines and ships them every FLUSH_INTERVAL seconds;
# the coordinator keeps the last COORDINATOR_RUN_OUTPUT_RING lines for each of
# at most COORDINATOR_RUN_OUTPUT_MAX_RUNS in-flight runs (LRU). All in-memory.
RUN_OUTPUT_FLUSH_INTERVAL = float(os.environ.get("OPP_CI_RUN_OUTPUT_FLUSH_INTERVAL", "2"))
COORDINATOR_RUN_OUTPUT_RING = int(os.environ.get("OPP_CI_COORDINATOR_RUN_OUTPUT_RING", "5000"))
COORDINATOR_RUN_OUTPUT_MAX_RUNS = int(os.environ.get("OPP_CI_COORDINATOR_RUN_OUTPUT_MAX_RUNS", "64"))

# ── TLS ───────────────────────────────────────────────────────────────
#
# Native TLS termination in `opp_ci coordinator start`. Empty pair → plain HTTP, the
# default. Setting only one of CERT_FILE / KEY_FILE is a startup error.
# When TLS is enabled, SESSION_COOKIE_SECURE is auto-flipped on at
# startup, and OAuth callback requires OPP_CI_PUBLIC_URL.
COORDINATOR_TLS_CERT_FILE = os.environ.get("OPP_CI_COORDINATOR_TLS_CERT_FILE", "")
COORDINATOR_TLS_KEY_FILE = os.environ.get("OPP_CI_COORDINATOR_TLS_KEY_FILE", "")
COORDINATOR_TLS_KEY_PASSWORD_FILE = os.environ.get("OPP_CI_COORDINATOR_TLS_KEY_PASSWORD_FILE", "")

# Outbound TLS verification, used by the worker and the Python client when
# the coordinator presents a non-public-CA cert (self-signed, Cloudflare
# Origin Certificate, internal CA). Empty → use the system CA store.
TLS_CA_BUNDLE = os.environ.get("OPP_CI_TLS_CA_BUNDLE", "")
TLS_INSECURE = os.environ.get("OPP_CI_TLS_INSECURE", "0") == "1"

# ── opp_env host workspace (isolation=none, toolchain=nix) ────────────
#
# Root under which the worker keeps one opp_env workspace *per build
# coordinate* (project × omnetpp pin × compiler × git ref). Each host-nix
# run resolves to <root>/<key>: identical coordinate reuses the directory
# (omnetpp built once), a different coordinate gets its own isolated tree
# so concurrent or differently-pinned runs can't clobber each other. The
# podman path isolates the same way via one image per omnetpp version; the
# host path has no container, hence directories. Only the host-nix path
# reads this. `OPP_CI_WORKSPACE` is the *root*, not a single workspace.
WORKSPACE_ROOT = os.path.expanduser(
    os.environ.get("OPP_CI_WORKSPACE", "~/.local/share/opp_ci/workspace"))
# Retention cap: before each install, sweep the root and evict LRU-by-mtime
# directories beyond this count (currently-locked ones are skipped).
WORKSPACE_MAX = int(os.environ.get("OPP_CI_WORKSPACE_MAX", "10"))

# Command the worker shells out to for the host-nix opp_env path. Default is
# the bare `opp_env` on PATH; a uvx-based service install sets this to
# "uvx --from opp-env opp_env" so opp_env runs from its own isolated venv.
# Parsed with shlex.split at the call sites in opp_ci/executor.py.
OPP_ENV_CMD = os.environ.get("OPP_CI_OPP_ENV_CMD", "opp_env")

# Absolute path to the `uvx` binary baked into generated service units. Empty
# → the service installer resolves/copies uvx for the service user and writes
# the absolute path into the unit. Set to override (e.g. a Nix-store path).
UVX = os.environ.get("OPP_CI_UVX", "")

WORKER_POLL_INTERVAL = int(os.environ.get("OPP_CI_WORKER_POLL_INTERVAL", "10"))
WORKER_HEARTBEAT_INTERVAL = int(os.environ.get("OPP_CI_WORKER_HEARTBEAT_INTERVAL", "30"))
# Default nice level for `worker start` (and its build/test subprocesses) so
# CI work yields to interactive use. Expressible via the worker env file so a
# service install can persist it. Higher = lower priority; 0 = normal.
WORKER_NICENESS = int(os.environ.get("OPP_CI_WORKER_NICENESS", "10"))
WORKER_HEARTBEAT_TIMEOUT = int(os.environ.get("OPP_CI_WORKER_HEARTBEAT_TIMEOUT", "120"))

# How often the coordinator sweeps for workers that have gone silent past
# WORKER_HEARTBEAT_TIMEOUT, marks them offline, and reclaims their orphaned
# `running` runs. Defaults to half the timeout (min 15s) so worst-case
# detection latency is timeout + interval.
WORKER_REAP_INTERVAL = int(os.environ.get(
    "OPP_CI_WORKER_REAP_INTERVAL", str(max(15, WORKER_HEARTBEAT_TIMEOUT // 2))))
# A `running` run is re-queued up to this many times when its worker goes
# dark before it is treated as a poison pill (a run that keeps killing its
# worker) and retired to a terminal `timed_out`/ERROR state. See
# opp_ci.persistence.reclaim_orphaned_runs.
MAX_RECLAIMS = int(os.environ.get("OPP_CI_MAX_RECLAIMS", "2"))

# A `queued` run that no enabled worker's tags can ever satisfy (a misrouted
# submission, not transient backlog) is retired to `timed_out`/ERROR once it
# has waited this long — long enough that a worker still coming up has time
# to register and heartbeat. Serviceable-but-starved runs (right tags, fleet
# busy/offline) are never auto-expired. Swept on the same tick as the stale-
# worker reaper. `0` disables the sweep. See
# opp_ci.persistence.expire_unserviceable_queued_runs.
QUEUE_UNSERVICEABLE_TIMEOUT = int(os.environ.get(
    "OPP_CI_QUEUE_UNSERVICEABLE_TIMEOUT", "300"))

WORKER_TOKEN = os.environ.get("OPP_CI_WORKER_TOKEN", "")

GITHUB_TOKEN_FILE = os.environ.get("OPP_CI_GITHUB_TOKEN_FILE", os.path.expanduser("~/.ssh/opp_ci_github_token"))
GITHUB_WEBHOOK_SECRET = os.environ.get("OPP_CI_GITHUB_WEBHOOK_SECRET", "")
GITHUB_STATUS_CONTEXT = os.environ.get("OPP_CI_GITHUB_STATUS_CONTEXT", "opp_ci")
GITHUB_BASE_URL = os.environ.get("OPP_CI_GITHUB_BASE_URL", "https://api.github.com")


# ── Web UI login (session + GitHub OAuth) ─────────────────────────────
#
# The session secret signs session cookies. Empty by default so a
# misconfigured deploy fails closed at startup rather than handing out
# unsigned-but-look-signed cookies. For development, `opp_ci coordinator start` will
# refuse to start without a value.
SESSION_SECRET = os.environ.get("OPP_CI_SESSION_SECRET", "")
SESSION_COOKIE_SECURE = os.environ.get("OPP_CI_SESSION_COOKIE_SECURE", "0") == "1"

# Public origin (scheme+host[:port]) used to build the OAuth callback URL.
# Required when GitHub OAuth is enabled; without it we'd derive the URL
# from the Host: header, which breaks behind a reverse proxy.
PUBLIC_URL = os.environ.get("OPP_CI_PUBLIC_URL", "")

# GitHub OAuth App credentials. Leaving the client ID empty disables the
# "Sign in with GitHub" button entirely; local password login remains.
GITHUB_OAUTH_CLIENT_ID = os.environ.get("OPP_CI_GITHUB_OAUTH_CLIENT_ID", "")
GITHUB_OAUTH_CLIENT_SECRET_FILE = os.environ.get(
    "OPP_CI_GITHUB_OAUTH_CLIENT_SECRET_FILE",
    os.path.expanduser("~/.ssh/opp_ci_github_oauth_secret"),
)

# Authorization policy applied after a successful GitHub login.
# `OPP_CI_GITHUB_ORG` empty → no org check, every GitHub user → readonly.
# `OPP_CI_GITHUB_SUBMITTER_TEAMS` == "*" → any member of the org gets
# `submitter`; else only listed team slugs do.
GITHUB_ORG = os.environ.get("OPP_CI_GITHUB_ORG", "")
# Org members whose login is listed here always resolve to `admin`,
# regardless of team membership. Comma-separated GitHub logins.
GITHUB_ADMIN_USERS = [s.strip().lower() for s in os.environ.get("OPP_CI_GITHUB_ADMIN_USERS", "").split(",") if s.strip()]
GITHUB_ADMIN_TEAMS = [s.strip() for s in os.environ.get("OPP_CI_GITHUB_ADMIN_TEAMS", "").split(",") if s.strip()]
GITHUB_SUBMITTER_TEAMS = [s.strip() for s in os.environ.get("OPP_CI_GITHUB_SUBMITTER_TEAMS", "*").split(",") if s.strip()]
GITHUB_ALLOW_EXTERNAL = os.environ.get("OPP_CI_GITHUB_ALLOW_EXTERNAL", "1") == "1"


def get_tls_key_password():
    """Read the TLS key passphrase from file (or env override). Empty if not set."""
    pw = os.environ.get("OPP_CI_COORDINATOR_TLS_KEY_PASSWORD", "")
    if pw:
        return pw
    if not COORDINATOR_TLS_KEY_PASSWORD_FILE:
        return ""
    try:
        with open(COORDINATOR_TLS_KEY_PASSWORD_FILE) as f:
            return f.read().strip()
    except (OSError, FileNotFoundError):
        return ""


def get_github_oauth_client_secret():
    """Read the OAuth App client secret from env or file."""
    secret = os.environ.get("OPP_CI_GITHUB_OAUTH_CLIENT_SECRET", "")
    if secret:
        return secret
    try:
        with open(GITHUB_OAUTH_CLIENT_SECRET_FILE) as f:
            return f.read().strip()
    except (OSError, FileNotFoundError):
        return ""


GITHUB_ACTIONS_TOKEN_FILE = os.environ.get(
    "OPP_CI_GITHUB_ACTIONS_TOKEN_FILE",
    os.path.expanduser("~/.ssh/opp_ci_github_actions_token"),
)


def get_github_token():
    """Read the GitHub API token from file or env var."""
    token = os.environ.get("OPP_CI_GITHUB_TOKEN", "")
    if token:
        return token
    try:
        with open(GITHUB_TOKEN_FILE) as f:
            return f.read().strip()
    except (OSError, FileNotFoundError):
        return ""


def get_github_actions_token():
    """Read the fine-grained PAT with Actions:Write scope (for workflow_dispatch)."""
    token = os.environ.get("OPP_CI_GITHUB_ACTIONS_TOKEN", "")
    if token:
        return token
    try:
        with open(GITHUB_ACTIONS_TOKEN_FILE) as f:
            return f.read().strip()
    except (OSError, FileNotFoundError):
        return ""
