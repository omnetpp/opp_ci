"""
Dependency resolution for opp_ci.

Queries the opp_env project registry to discover required_projects
and resolve compatible dependency versions. Supports version pinning.
"""

import json
import logging
import subprocess

_logger = logging.getLogger(__name__)


def query_opp_env_info(project_version):
    """
    Query opp_env for project info in raw JSON format.

    Args:
        project_version: e.g. "inet-4.5", "omnetpp-6.1"

    Returns:
        dict with project info including 'required_projects', or None on failure.
    """
    _logger.debug("Querying opp_env info for %s", project_version)
    try:
        result = subprocess.run(
            ["opp_env", "info", project_version, "--raw"],
            capture_output=True, text=True
        )
    except FileNotFoundError:
        _logger.warning("opp_env not on PATH — cannot resolve %s", project_version)
        return None
    if result.returncode != 0:
        _logger.warning("opp_env info %s failed: %s", project_version, result.stderr.strip())
        return None

    try:
        data = json.loads(result.stdout)
        if isinstance(data, list) and len(data) == 1:
            return data[0]
        elif isinstance(data, list) and len(data) > 1:
            return data[0]
        return data
    except (json.JSONDecodeError, ValueError) as e:
        _logger.warning("Failed to parse opp_env info output: %s", e)
        return None


def query_opp_env_versions(project_name):
    """
    Query opp_env for available versions of a project.

    Args:
        project_name: e.g. "inet", "omnetpp"

    Returns:
        list of version strings, or empty list on failure.
    """
    _logger.debug("Querying opp_env versions for %s", project_name)
    try:
        result = subprocess.run(
            ["opp_env", "info", project_name, "--raw"],
            capture_output=True, text=True
        )
    except FileNotFoundError:
        _logger.warning("opp_env not on PATH — cannot list versions for %s", project_name)
        return []
    if result.returncode != 0:
        _logger.warning("opp_env info %s failed: %s", project_name, result.stderr.strip())
        return []

    try:
        data = json.loads(result.stdout)
        if isinstance(data, list):
            return [entry["version"] for entry in data if "version" in entry]
        return []
    except (json.JSONDecodeError, ValueError) as e:
        _logger.warning("Failed to parse opp_env info output: %s", e)
        return []


def get_required_projects(project_version):
    """
    Get the required_projects dict for a given project-version.

    Args:
        project_version: e.g. "inet-4.5"

    Returns:
        dict mapping dependency name to list of compatible versions,
        e.g. {"omnetpp": ["6.0.2", "6.0.3", "6.1.0"]}
        Returns empty dict if info unavailable.
    """
    info = query_opp_env_info(project_version)
    if info is None:
        return {}
    return info.get("required_projects", {})


def resolve_dependencies(project_version, pins=None):
    """
    Resolve compatible dependency versions for a project.

    For each dependency in required_projects, selects the latest
    compatible version (first in list, as opp_env orders them
    newest-first). Pins override the auto-selection.

    Args:
        project_version: e.g. "inet-4.5"
        pins: dict mapping dep name to pinned version, e.g. {"omnetpp": "6.0.3"}

    Returns:
        dict mapping dep name to resolved version string,
        e.g. {"omnetpp": "6.1.0"}

    Raises:
        ValueError: if a pinned version is not in the compatible list.
    """
    pins = pins or {}
    required = get_required_projects(project_version)
    if not required:
        _logger.info("No dependencies found for %s", project_version)
        return {}

    resolved = {}
    for dep_name, compatible_versions in required.items():
        if dep_name in pins:
            pinned = pins[dep_name]
            if compatible_versions and pinned not in compatible_versions:
                raise ValueError(
                    f"Pinned version '{dep_name}-{pinned}' is not compatible with "
                    f"{project_version}. Compatible versions: {compatible_versions}"
                )
            resolved[dep_name] = pinned
            _logger.info("Dependency %s pinned to %s", dep_name, pinned)
        elif compatible_versions:
            resolved[dep_name] = compatible_versions[0]
            _logger.info("Dependency %s resolved to %s (latest compatible)", dep_name, compatible_versions[0])
        else:
            _logger.warning("Dependency %s has no compatible versions listed", dep_name)

    return resolved


def parse_pins(pin_strings):
    """
    Parse pin strings like ["omnetpp=6.1", "inet=4.5"] into a dict.

    Args:
        pin_strings: list of "name=version" strings

    Returns:
        dict mapping name to version
    """
    pins = {}
    for pin in pin_strings or []:
        if "=" not in pin:
            raise ValueError(f"Invalid pin format '{pin}': expected 'project=version'")
        name, version = pin.split("=", 1)
        pins[name.strip()] = version.strip()
    return pins


def format_resolved_deps(resolved):
    """Format resolved dependencies for display."""
    if not resolved:
        return "-"
    return ", ".join(f"{name}-{ver}" for name, ver in resolved.items())
