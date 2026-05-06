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
