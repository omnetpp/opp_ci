"""
Matrix expansion and job scheduling.

A TestMatrix config is a JSON dict with axes to cross-product:
{
    "test_types": ["smoke", "fingerprint"],
    "modes": ["release", "debug"],
    "versions": ["inet-4.5", "inet-4.4"],
    "refs": ["master", "topic/my-feature"],
    "deps": {"omnetpp": ["6.3.0", "6.2.0"]},
    "features": []
}

The 'refs' axis (optional) specifies git branches/tags/commits to test.
Alternatively, 'ref_range' ({"base": "...", "head": "..."}) resolves a
GitHub commit range at expansion time — so the list is always fresh.
If both 'versions' and 'refs' are present, they are cross-producted.

Platform axes support two styles:

1. Structured (cross-product) — provide separate name and version lists:
   {
       "os": ["Ubuntu", "Fedora"],
       "os_version": ["24.04", "41"],
       "compiler": ["gcc", "clang"],
       "compiler_version": ["14", "18"]
   }
   This produces all combinations: Ubuntu 24.04, Ubuntu 41, Fedora 24.04, etc.

2. Combined (pre-composed strings) — omit the _version key:
   {
       "os": ["Ubuntu 24.04", "Fedora 41"],
       "compiler": ["gcc-14", "clang-18"]
   }
   Values are parsed: OS splits on last space, compiler splits on last hyphen.

Detection rule: if the _version key is present → structured mode (cross-product).
If absent → combined mode (parse the strings).

The optional 'deps' axis maps dependency names to lists of versions.
The cross-product produces one job per combination of dep versions,
each with a ``resolved_deps`` dict pinning each dep to one version.

The scheduler expands this into individual jobs (one per combination).
"""

import itertools
import logging

_logger = logging.getLogger(__name__)


def _parse_os(combined):
    """Parse a combined OS string like 'Ubuntu 24.04' into (name, version)."""
    if not combined:
        return (None, None)
    parts = combined.rsplit(" ", 1)
    if len(parts) == 2:
        return (parts[0], parts[1])
    return (combined, None)


def _parse_compiler(combined):
    """Parse a combined compiler string like 'gcc-14' into (name, version)."""
    if not combined:
        return (None, None)
    parts = combined.rsplit("-", 1)
    if len(parts) == 2 and parts[1] and parts[1][0].isdigit():
        return (parts[0], parts[1])
    return (combined, None)


def _resolve_os_axis(config):
    """
    Resolve OS axis from config.
    Returns a list of (os_name, os_version) tuples.
    """
    os_list = config.get("os", [None])
    if "os_version" in config:
        os_versions = config["os_version"]
        return list(itertools.product(os_list, os_versions))
    else:
        return [_parse_os(o) for o in os_list]


def _resolve_compiler_axis(config):
    """
    Resolve compiler axis from config.
    Returns a list of (compiler_name, compiler_version) tuples.
    """
    compiler_list = config.get("compiler", [None])
    if "compiler_version" in config:
        compiler_versions = config["compiler_version"]
        return list(itertools.product(compiler_list, compiler_versions))
    else:
        return [_parse_compiler(c) for c in compiler_list]


def _build_platform_desc(os_name, os_version, compiler_name, compiler_version):
    """Build a human-readable platform description from components."""
    parts = []
    if os_name:
        parts.append(f"{os_name} {os_version}" if os_version else os_name)
    if compiler_name:
        parts.append(f"{compiler_name}-{compiler_version}" if compiler_version else compiler_name)
    return " / ".join(parts) if parts else None


def _resolve_deps_axis(config):
    """
    Resolve the deps axis from config.

    Config format: {"deps": {"omnetpp": ["6.3.0", "6.2.0"], "inet": ["4.5"]}}

    Returns a list of dicts, one per combination of dep versions.
    Each dict maps dep name to a single version string.
    If no deps axis is present, returns [None] (no pinning).
    """
    deps = config.get("deps")
    if not deps:
        return [None]

    dep_names = sorted(deps.keys())
    dep_version_lists = [deps[name] for name in dep_names]

    combos = []
    for combo in itertools.product(*dep_version_lists):
        combos.append(dict(zip(dep_names, combo)))

    return combos


def _resolve_ref_range(project_name, ref_range):
    """Resolve a ref_range dict to a list of commit SHAs via the GitHub API."""
    from opp_ci.db.connection import SessionLocal
    from opp_ci.db.models import Project
    from opp_ci.github.client import GitHubClient
    from sqlalchemy import select

    session = SessionLocal()
    try:
        proj = session.execute(
            select(Project).where(Project.name == project_name)
        ).scalar_one_or_none()
        if proj is None:
            raise ValueError(f"Project '{project_name}' not found")
        if not proj.github_owner or not proj.github_repo:
            raise ValueError(f"Project '{project_name}' has no GitHub owner/repo configured")

        client = GitHubClient()
        base = ref_range["base"]
        head = ref_range["head"]
        shas = client.list_commits_in_range(proj.github_owner, proj.github_repo, base, head)
        _logger.info("Resolved ref range %s..%s to %d commits for %s", base, head, len(shas), project_name)
        return shas
    finally:
        session.close()


def expand_matrix(project, config):
    """
    Expand a matrix config into a list of individual job specs.

    If config contains a ``ref_range`` key (``{"base": "...", "head": "..."}``),
    the commit range is resolved via the GitHub API at expansion time.  A static
    ``refs`` list takes precedence if both are present.

    Each job spec is a dict:
        {
            "project": "inet-4.5",
            "test_type": "smoke",
            "mode": "release",
            "git_ref": "master",
            "os": "Ubuntu",
            "os_version": "24.04",
            "compiler": "gcc",
            "compiler_version": "14",
            "platform_desc": "Ubuntu 24.04 / gcc-14",
        }
    """
    test_types = config.get("test_types", ["smoke"])
    modes = config.get("modes", ["release"])
    versions = config.get("versions", [project])
    if "refs" in config:
        refs = config["refs"]
    elif "ref_range" in config:
        refs = _resolve_ref_range(project, config["ref_range"])
    else:
        refs = [None]
    os_tuples = _resolve_os_axis(config)
    compiler_tuples = _resolve_compiler_axis(config)
    dep_combos = _resolve_deps_axis(config)

    jobs = []
    for version, ref, test_type, mode, (os_name, os_ver), (comp_name, comp_ver), dep_pins in itertools.product(
            versions, refs, test_types, modes, os_tuples, compiler_tuples, dep_combos):
        jobs.append({
            "project": version,
            "test_type": test_type,
            "mode": mode,
            "git_ref": ref,
            "os": os_name,
            "os_version": os_ver,
            "compiler": comp_name,
            "compiler_version": comp_ver,
            "platform_desc": _build_platform_desc(os_name, os_ver, comp_name, comp_ver),
            "resolved_deps": dep_pins or None,
        })

    _logger.info("Expanded matrix for %s: %d jobs", project, len(jobs))
    return jobs


DEFAULT_MATRICES = {
    "inet-default": {
        "project": "inet",
        "config": {
            "test_types": ["smoke", "fingerprint", "statistical"],
            "modes": ["release", "debug"],
            "os": ["Ubuntu 24.04"],
            "compiler": ["gcc-14", "clang-18"],
            "versions": ["inet"],
        },
    },
    "omnetpp-default": {
        "project": "omnetpp",
        "config": {
            "test_types": ["smoke", "build"],
            "modes": ["release", "debug"],
            "os": ["Ubuntu 24.04"],
            "compiler": ["gcc-14", "clang-18"],
            "versions": ["omnetpp"],
        },
    },
}
