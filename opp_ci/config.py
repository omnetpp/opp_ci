import os


DATABASE_URL = os.environ.get("OPP_CI_DATABASE_URL", "sqlite:///opp_ci.db")
USE_OPP_ENV = os.environ.get("OPP_CI_USE_OPP_ENV", "0") == "1"
