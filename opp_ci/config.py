import os


DATABASE_URL = os.environ.get("OPP_CI_DATABASE_URL", "sqlite:///opp_ci.db")
USE_OPP_ENV = os.environ.get("OPP_CI_USE_OPP_ENV", "0") == "1"

COORDINATOR_URL = os.environ.get("OPP_CI_COORDINATOR_URL", "http://localhost:8000")
API_TOKEN = os.environ.get("OPP_CI_API_TOKEN", "")

REFERENCE_PLATFORM = os.environ.get("OPP_CI_REFERENCE_PLATFORM", "Ubuntu 24.04/gcc-13")

WORKER_POLL_INTERVAL = int(os.environ.get("OPP_CI_WORKER_POLL_INTERVAL", "10"))
WORKER_HEARTBEAT_INTERVAL = int(os.environ.get("OPP_CI_WORKER_HEARTBEAT_INTERVAL", "30"))
WORKER_HEARTBEAT_TIMEOUT = int(os.environ.get("OPP_CI_WORKER_HEARTBEAT_TIMEOUT", "120"))

GITHUB_TOKEN_FILE = os.environ.get("OPP_CI_GITHUB_TOKEN_FILE", os.path.expanduser("~/.ssh/github_repo_token"))
GITHUB_WEBHOOK_SECRET = os.environ.get("OPP_CI_GITHUB_WEBHOOK_SECRET", "")
GITHUB_STATUS_CONTEXT = os.environ.get("OPP_CI_GITHUB_STATUS_CONTEXT", "opp_ci")
GITHUB_BASE_URL = os.environ.get("OPP_CI_GITHUB_BASE_URL", "https://api.github.com")


GITHUB_ACTIONS_TOKEN_FILE = os.environ.get(
    "OPP_CI_GITHUB_ACTIONS_TOKEN_FILE",
    os.path.expanduser("~/.ssh/github_actions_token"),
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
