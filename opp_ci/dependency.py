"""
Dependency resolution for opp_ci.

Queries the opp_env project registry to discover required_projects
and resolve compatible dependency versions. Supports version pinning.

The lock a submit pins is the **complete transitive closure** — every
project opp_env would build, not just the direct ``required_projects`` —
because opp_env builds the whole closure and a version chosen deep in the
graph can still change the build outcome. ``resolve_dependencies`` walks
that closure; with ``require_complete=True`` it raises
``DependencyResolutionError`` rather than return a partial lock (the
submit-time "reject-incomplete" rule — see
plan/pending/repeatable-tests-and-moving-target-matrices.md).
"""

import json
import logging
import subprocess

_logger = logging.getLogger(__name__)


class DependencyResolutionError(ValueError):
    """A complete transitive dependency lock could not be produced.

    Raised by ``resolve_dependencies(..., require_complete=True)`` when
    opp_env cannot be queried for a node in the closure, a required project
    lists no compatible versions, or two nodes demand incompatible versions
    of the same dependency. Subclasses ``ValueError`` so the submit paths'
    existing ``except ValueError`` handlers surface it as a 400 / CLI error
    without extra plumbing.
    """


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


def resolve_dependencies(project_version, pins=None, *, transitive=True,
                         require_complete=False):
    """
    Resolve the dependency lock for a project — by default the full
    transitive closure.

    Walks ``required_projects`` breadth-first from ``project_version``: each
    dependency is pinned to one version (a pin if given, else the latest
    compatible — opp_env lists them newest-first), and every chosen
    dependency is then itself expanded so its own requirements are pinned
    too. A version is chosen once and reused everywhere it recurs.

    Args:
        project_version: e.g. "inet-4.5"
        pins: dict mapping dep name to pinned version, e.g. {"omnetpp": "6.0.3"}
        transitive: walk the whole closure (default). ``False`` resolves only
            the project's direct ``required_projects`` (legacy behaviour).
        require_complete: when True, raise ``DependencyResolutionError`` instead
            of returning a partial lock — opp_env unavailable for a node, a
            dependency with no compatible versions, or an unsatisfiable
            version conflict. This is the submit-time reject-incomplete rule.

    Returns:
        dict mapping dep name to resolved version string, e.g.
        ``{"omnetpp": "6.1.0"}`` — the complete lock that keys Test identity.

    Raises:
        ValueError: if a pinned version is not in a node's compatible list.
        DependencyResolutionError: if ``require_complete`` and the closure
            cannot be fully and consistently pinned.
    """
    pins = pins or {}
    resolved = {}
    queue = [project_version]
    visited = set()

    while queue:
        node = queue.pop(0)
        if node in visited:
            continue
        visited.add(node)

        info = query_opp_env_info(node)
        if info is None:
            # opp_env could not describe this node, so we cannot see its
            # requirements. A partial lock is not repeatable — reject when
            # the caller demands completeness, else carry on best-effort.
            if require_complete:
                raise DependencyResolutionError(
                    f"opp_env could not resolve {node!r}; cannot produce a "
                    f"complete dependency lock (is opp_env installed and the "
                    f"version known?)."
                )
            _logger.warning("opp_env has no info for %s — lock may be partial", node)
            continue

        required = info.get("required_projects", {}) or {}
        for dep_name, compatible in required.items():
            compatible = compatible or []
            if dep_name in pins:
                chosen = pins[dep_name]
                if compatible and chosen not in compatible:
                    raise ValueError(
                        f"Pinned version '{dep_name}-{chosen}' is not compatible "
                        f"with {node}. Compatible versions: {compatible}"
                    )
            elif dep_name in resolved:
                chosen = resolved[dep_name]
                if compatible and chosen not in compatible:
                    # A deeper node demands a version incompatible with the one
                    # already locked: the closure has no single consistent pick.
                    if require_complete:
                        raise DependencyResolutionError(
                            f"Dependency {dep_name!r} is locked to {chosen!r} but "
                            f"{node} requires one of {compatible}; no consistent "
                            f"transitive lock exists."
                        )
                    _logger.warning(
                        "Conflicting version for %s (%s vs %s required by %s); "
                        "keeping %s", dep_name, chosen, compatible, node, chosen)
                continue  # already locked
            elif compatible:
                chosen = compatible[0]  # opp_env orders newest-first
            else:
                if require_complete:
                    raise DependencyResolutionError(
                        f"Dependency {dep_name!r} (required by {node}) lists no "
                        f"compatible versions; cannot complete the lock."
                    )
                _logger.warning(
                    "Dependency %s (required by %s) has no compatible versions",
                    dep_name, node)
                continue

            resolved[dep_name] = chosen
            _logger.info("Locked dependency %s to %s", dep_name, chosen)
            if transitive:
                queue.append(f"{dep_name}-{chosen}")

    return resolved


def complete_lock_for_submit(project, version=None, pins=None, *,
                             require_complete=True):
    """The complete transitive dependency lock to persist for one submission.

    The single helper every submit path calls so the lock that keys Test
    identity is always present, complete, and transitive — never just the
    direct deps, never "latest at build time". `version` is the opp_env
    version id (e.g. "inet-4.5") when known, else the bare project name
    (opp_env's newest). `pins` (a --pin, the web omnetpp field, or a matrix
    deps-axis cell) constrains the resolution; an explicit pin always lands in
    the lock even for a custom project opp_env can't describe (e.g. mm1k) —
    the user named that version.

    `require_complete=True` (the default) enforces reject-incomplete (raise
    `DependencyResolutionError` rather than persist a partial lock) — the
    plan's decision #7. Pass False only for a deliberately best-effort lock.
    """
    pins = pins or {}
    if not version:
        pv = project
    elif version.startswith(f"{project}-"):
        pv = version
    else:
        pv = f"{project}-{version}"
    lock = resolve_dependencies(pv, pins=pins, transitive=True,
                                require_complete=require_complete)
    for name, ver in pins.items():
        lock.setdefault(name, ver)
    return lock


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
