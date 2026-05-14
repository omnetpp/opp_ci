"""
Tier 1 project catalog for opp_ci.

Defines the known projects and their dependency relationships.
Later stages will import this from the opp_env database dynamically.
"""

TIER1_PROJECTS = [
    {
        "name": "omnetpp",
        "opp_env_name": "omnetpp",
        "github_owner": "omnetpp",
        "github_repo": "omnetpp",
        "git_url": "https://github.com/omnetpp/omnetpp.git",
        "tier": 1,
        "dependency_names": [],
    },
    {
        "name": "inet",
        "opp_env_name": "inet",
        "github_owner": "inet-framework",
        "github_repo": "inet",
        "git_url": "https://github.com/inet-framework/inet.git",
        "tier": 1,
        "dependency_names": ["omnetpp"],
    },
    {
        "name": "simu5g",
        "opp_env_name": "simu5g",
        "github_owner": "Unipisa",
        "github_repo": "Simu5G",
        "git_url": "https://github.com/Unipisa/Simu5G.git",
        "tier": 1,
        "dependency_names": ["inet", "omnetpp"],
    },
    {
        "name": "veins",
        "opp_env_name": "veins",
        "github_owner": "sommer",
        "github_repo": "veins",
        "git_url": "https://github.com/sommer/veins.git",
        "tier": 1,
        "dependency_names": ["omnetpp"],
    },
]


def seed_projects(session):
    """Insert Tier 1 projects into the database if they don't already exist."""
    from opp_ci.db.models import Project

    for proj_data in TIER1_PROJECTS:
        existing = session.query(Project).filter_by(name=proj_data["name"]).first()
        if existing is None:
            session.add(Project(**proj_data))
    session.commit()


def load_platforms_catalog():
    """Read opp_ci/docker/platforms.yml. Returns {} if the file is missing.

    Kept here so both seed_platforms() and any future caller (e.g. a one-shot
    web "seed from catalog" button) read the same source.
    """
    try:
        import importlib.resources
        import yaml
        path = importlib.resources.files("opp_ci").joinpath("docker/platforms.yml")
        with open(path) as f:
            return yaml.safe_load(f) or {}
    except (ImportError, OSError, ValueError):
        return {}


def seed_platforms(session):
    """Insert (OS, version) and (Compiler, version) rows from platforms.yml.

    Existing rows with the same (name, version) are left untouched, so the
    command is idempotent: edit platforms.yml, re-run, only new entries land.
    Returns a tuple (os_inserted, compilers_inserted) for the caller to log.
    """
    from opp_ci.db.models import OS, Compiler

    catalog = load_platforms_catalog()
    os_inserted = 0
    for name in catalog.get("os_distributions", []):
        versions = catalog.get("os_versions", {}).get(name, []) or [None]
        for version in versions:
            existing = session.query(OS).filter_by(name=name, version=version).first()
            if existing is None:
                session.add(OS(name=name, version=version))
                os_inserted += 1

    comp_inserted = 0
    for name in catalog.get("compilers", []):
        versions = catalog.get("compiler_versions", {}).get(name, []) or [None]
        for version in versions:
            existing = session.query(Compiler).filter_by(name=name, version=version).first()
            if existing is None:
                session.add(Compiler(name=name, version=version))
                comp_inserted += 1

    session.commit()
    return os_inserted, comp_inserted
