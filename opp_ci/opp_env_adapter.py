"""
opp_env adapter for opp_ci (Stage 8).

Wraps the opp_env Python API / CLI to list all projects, versions, and
dependencies in a normalized format suitable for catalog sync.
"""

import json
import logging
import re
import subprocess

_logger = logging.getLogger(__name__)


def list_all_projects():
    """
    Query opp_env for the full project catalog.

    Returns a list of dicts:
        [
            {
                "name": "inet",
                "versions": [
                    {
                        "version": "4.5",
                        "dependencies": {"omnetpp": ["6.0.2", "6.0.3", "6.1.0"]},
                    },
                    ...
                ],
            },
            ...
        ]
    """
    result = subprocess.run(
        ["opp_env", "info", "--raw"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        _logger.error("opp_env info --raw failed: %s", result.stderr.strip())
        return []

    try:
        entries = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError) as e:
        _logger.error("Failed to parse opp_env info output: %s", e)
        return []

    # opp_env info --raw returns a flat list of version entries, each with
    # "name" (project name) and "version" fields. Group by project name.
    by_project = {}
    for entry in entries:
        name = entry.get("name") or entry.get("project")
        if not name:
            continue
        version = entry.get("version", "")
        deps = entry.get("required_projects", {})

        if name not in by_project:
            # Extract GitHub info from first version's URLs
            git_url = entry.get("git_url") or ""
            download_url = entry.get("download_url") or ""
            owner, repo = _parse_github_url(git_url or download_url)
            by_project[name] = {"name": name, "versions": [], "github_owner": owner, "github_repo": repo}

        by_project[name]["versions"].append({
            "version": version,
            "dependencies": deps,
        })

    projects = list(by_project.values())
    _logger.info("opp_env catalog: %d projects, %d total versions",
                 len(projects), sum(len(p["versions"]) for p in projects))
    return projects


def get_project_github_info(project_name):
    """
    Try to extract GitHub owner/repo from opp_env project metadata.

    Returns (owner, repo) or (None, None) if not available.
    """
    result = subprocess.run(
        ["opp_env", "info", project_name, "--raw"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return (None, None)

    try:
        data = json.loads(result.stdout)
        if isinstance(data, list):
            data = data[0] if data else {}
    except (json.JSONDecodeError, ValueError):
        return (None, None)

    # Look for git_url or download_url to infer GitHub info
    git_url = data.get("git_url", "") or data.get("download_url", "")
    return _parse_github_url(git_url)


def _parse_github_url(url):
    """
    Extract (owner, repo) from a GitHub URL.

    Handles: https://github.com/owner/repo.git
             https://github.com/owner/repo
             git@github.com:owner/repo.git
    """
    if not url:
        return (None, None)

    # HTTPS format
    if "github.com/" in url:
        parts = url.split("github.com/", 1)[1]
        parts = parts.rstrip("/").removesuffix(".git")
        segments = parts.split("/")
        if len(segments) >= 2:
            return (segments[0], segments[1])

    # SSH format
    if "github.com:" in url:
        parts = url.split("github.com:", 1)[1]
        parts = parts.rstrip("/").removesuffix(".git")
        segments = parts.split("/")
        if len(segments) >= 2:
            return (segments[0], segments[1])

    return (None, None)


def list_platforms():
    """
    Extract available OS and compiler options from opp_env.

    Returns a dict:
        {
            "os": [{"name": "nixos", "version": "24.05"}],
            "compilers": [{"name": "clang"}, {"name": "gcc", "version": "7"}],
        }
    """
    result = subprocess.run(
        ["opp_env", "info", "--raw"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        _logger.error("opp_env info --raw failed: %s", result.stderr.strip())
        return {"os": [], "compilers": []}

    try:
        entries = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError) as e:
        _logger.error("Failed to parse opp_env info output: %s", e)
        return {"os": [], "compilers": []}

    os_set = set()
    compiler_set = set()
    for entry in entries:
        options = entry.get("options", {})
        for key, opt in options.items():
            cat = opt.get("option_category", "")
            if cat == "compiler":
                # Extract name and version from option key (e.g. "gcc7" -> gcc/7, "clang" -> clang/None)
                m = re.match(r"([a-zA-Z]+)(\d+.*)?", key)
                if m:
                    compiler_set.add((m.group(1), m.group(2) or None))
            elif cat == "nixos":
                nixos_ver = opt.get("nixos", "")
                if nixos_ver:
                    os_set.add(("nixos", nixos_ver))

    return {
        "os": [{"name": name, "version": ver} for name, ver in sorted(os_set)],
        "compilers": [{"name": name, "version": ver} for name, ver in sorted(compiler_set)],
    }


def sync_catalog(session):
    """
    Sync the full opp_env catalog into the opp_ci database.

    - Inserts new projects
    - Adds new versions for existing projects
    - Generates a default TestMatrix for new projects
    - Seeds OS and Compiler tables from opp_env options

    Returns (new_projects, new_versions) counts.
    """
    from opp_ci.config import REFERENCE_PLATFORM
    from opp_ci.db.models import Project, Version, TestMatrix, OS, Compiler
    from opp_ci.scheduler import _parse_os, _parse_compiler

    projects = list_all_projects()
    if not projects:
        _logger.warning("No projects returned from opp_env, skipping sync")
        return (0, 0)

    new_projects = 0
    new_versions = 0

    for proj_data in projects:
        name = proj_data["name"]

        # Check if project exists
        existing = session.query(Project).filter_by(opp_env_name=name).first()
        if existing is None:
            existing = session.query(Project).filter_by(name=name).first()

        if existing is None:
            owner = proj_data.get("github_owner")
            repo = proj_data.get("github_repo")

            # Collect dependency names from all versions
            all_deps = set()
            for v in proj_data["versions"]:
                all_deps.update(v.get("dependencies", {}).keys())

            project = Project(
                name=name,
                opp_env_name=name,
                github_owner=owner,
                github_repo=repo,
                dependency_names=sorted(all_deps),
            )
            session.add(project)
            session.flush()
            new_projects += 1
            _logger.info("New project: %s", name)

            # Create default matrix
            os_name, os_ver = _parse_os(REFERENCE_PLATFORM.split("/")[0] if "/" in REFERENCE_PLATFORM else REFERENCE_PLATFORM)
            comp_name, comp_ver = _parse_compiler(REFERENCE_PLATFORM.split("/")[1] if "/" in REFERENCE_PLATFORM else "")

            matrix_config = {
                "tests": ["build", "smoke"],
                "modes": ["release"],
                "versions": [name],
            }
            if os_name:
                os_str = f"{os_name} {os_ver}" if os_ver else os_name
                matrix_config["os"] = [os_str]
            if comp_name:
                comp_str = f"{comp_name}-{comp_ver}" if comp_ver else comp_name
                matrix_config["compiler"] = [comp_str]

            matrix = TestMatrix(
                name=f"{name}-default",
                project=name,
                config=matrix_config,
            )
            session.add(matrix)
        else:
            project = existing

        # Add missing versions
        existing_versions = {
            v.opp_env_version
            for v in session.query(Version).filter_by(project_id=project.id).all()
        }
        for v_data in proj_data["versions"]:
            version_str = v_data["version"]
            if version_str and version_str not in existing_versions:
                version = Version(
                    project_id=project.id,
                    opp_env_version=version_str,
                    label=version_str,
                    resolved_dependencies=v_data.get("dependencies"),
                )
                session.add(version)
                new_versions += 1

    # Seed OS and Compiler tables from opp_env options
    platforms = list_platforms()
    for os_entry in platforms["os"]:
        exists = session.query(OS).filter_by(name=os_entry["name"], version=os_entry["version"]).first()
        if not exists:
            session.add(OS(name=os_entry["name"], version=os_entry["version"]))
            _logger.info("New OS: %s %s", os_entry["name"], os_entry["version"])
    for comp in platforms["compilers"]:
        exists = session.query(Compiler).filter_by(name=comp["name"], version=comp["version"]).first()
        if not exists:
            session.add(Compiler(name=comp["name"], version=comp["version"]))
            _logger.info("New compiler: %s %s", comp["name"], comp.get("version") or "")

    session.commit()
    _logger.info("Catalog sync complete: %d new projects, %d new versions",
                 new_projects, new_versions)
    return (new_projects, new_versions)
