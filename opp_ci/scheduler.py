"""
Matrix expansion and job scheduling.

A TestMatrix config is a JSON dict with axes to cross-product:
{
    "test_types": ["smoke", "fingerprint"],
    "modes": ["release", "debug"],
    "versions": ["inet-4.5", "inet-4.4"],
    "features": []
}

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


def expand_matrix(project, config):
    """
    Expand a matrix config into a list of individual job specs.

    Each job spec is a dict:
        {
            "project": "inet-4.5",
            "test_type": "smoke",
            "mode": "release",
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
    os_tuples = _resolve_os_axis(config)
    compiler_tuples = _resolve_compiler_axis(config)

    jobs = []
    for version, test_type, mode, (os_name, os_ver), (comp_name, comp_ver) in itertools.product(
            versions, test_types, modes, os_tuples, compiler_tuples):
        jobs.append({
            "project": version,
            "test_type": test_type,
            "mode": mode,
            "os": os_name,
            "os_version": os_ver,
            "compiler": comp_name,
            "compiler_version": comp_ver,
            "platform_desc": _build_platform_desc(os_name, os_ver, comp_name, comp_ver),
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
