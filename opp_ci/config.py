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

REFERENCE_PLATFORM = os.environ.get("OPP_CI_REFERENCE_PLATFORM", "Ubuntu 24.04/gcc-13")

SERVE_HOST = os.environ.get("OPP_CI_SERVE_HOST", "127.0.0.1")
SERVE_PORT = int(os.environ.get("OPP_CI_SERVE_PORT", "8080"))

WORKER_POLL_INTERVAL = int(os.environ.get("OPP_CI_WORKER_POLL_INTERVAL", "10"))
WORKER_HEARTBEAT_INTERVAL = int(os.environ.get("OPP_CI_WORKER_HEARTBEAT_INTERVAL", "30"))
WORKER_HEARTBEAT_TIMEOUT = int(os.environ.get("OPP_CI_WORKER_HEARTBEAT_TIMEOUT", "120"))

WORKER_TOKEN = os.environ.get("OPP_CI_WORKER_TOKEN", "")

GITHUB_TOKEN_FILE = os.environ.get("OPP_CI_GITHUB_TOKEN_FILE", os.path.expanduser("~/.ssh/opp_ci_github_token"))
GITHUB_WEBHOOK_SECRET = os.environ.get("OPP_CI_GITHUB_WEBHOOK_SECRET", "")
GITHUB_STATUS_CONTEXT = os.environ.get("OPP_CI_GITHUB_STATUS_CONTEXT", "opp_ci")
GITHUB_BASE_URL = os.environ.get("OPP_CI_GITHUB_BASE_URL", "https://api.github.com")


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
