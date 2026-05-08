import os


DATABASE_URL = os.environ.get("OPP_CI_DATABASE_URL", "sqlite:///opp_ci.db")
USE_OPP_ENV = os.environ.get("OPP_CI_USE_OPP_ENV", "0") == "1"

COORDINATOR_URL = os.environ.get("OPP_CI_COORDINATOR_URL", "http://localhost:8000")
API_TOKEN = os.environ.get("OPP_CI_API_TOKEN", "")

WORKER_POLL_INTERVAL = int(os.environ.get("OPP_CI_WORKER_POLL_INTERVAL", "10"))
WORKER_HEARTBEAT_INTERVAL = int(os.environ.get("OPP_CI_WORKER_HEARTBEAT_INTERVAL", "30"))
WORKER_HEARTBEAT_TIMEOUT = int(os.environ.get("OPP_CI_WORKER_HEARTBEAT_TIMEOUT", "120"))
