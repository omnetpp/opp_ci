"""
Matrix expansion and job scheduling.

A TestMatrix config is a JSON dict with axes to cross-product:
{
    "kinds": ["smoke", "fingerprint"],
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

Platform axes form a three-level hierarchy — ``os`` ⊃ ``distro`` ⊃ ``flavor``:

* ``os`` ∈ {Linux, Windows, MacOS}
* ``distro`` ∈ {Ubuntu, Fedora, ...} (Linux only)
* ``flavor`` ∈ {Kubuntu, ...} (variant of a distro)

Each level has an optional ``<level>_version`` partner. Levels and their
versions support two styles, mirroring the compiler axis:

1. Structured — separate ``distro`` and ``distro_version`` lists are
   cross-producted (Ubuntu 24.04, Ubuntu 26.04, Fedora 24.04, Fedora 26.04).
2. Combined — values like ``"Ubuntu 24.04"`` are parsed into ``(name, version)``
   on the last space.

The expansion is *hierarchy-aware*: distro/flavor are only emitted under
``os=Linux``; flavor is only emitted under its parent distro. Implied
parents are filled in from [`platforms`](platforms.py).

The ``arch`` axis names the CPU architecture (e.g. ``"amd64"``, ``"aarch64"``).
omnetpp supports both; matrices that omit ``arch`` leave it unset on the
resulting jobs, and workers without an ``arch:<arch>`` tag may pick them up.
When ``arch`` is set, a worker must advertise ``arch:<arch>`` to claim the job.

The optional 'deps' axis maps dependency names to lists of versions.
The cross-product produces one job per combination of dep versions,
each with a ``resolved_deps`` dict pinning each dep to one version.

Execution-environment axes (optional):

    "isolation": ["none", "podman"]   # how to isolate the run
    "toolchain": ["none", "nix"]      # where the C++ toolchain comes from

Both are also cross-product axes; a single string is auto-promoted to a list.
When omitted, both default to "none" — direct on the worker's host with whatever
compiler is installed (matches the bare-metal "no Nix, no Podman" setup).

When ``toolchain == "nix"``, the (compiler, compiler_version) pair must map to
an option opp_env understands; otherwise expansion raises ValueError.

The scheduler expands this into individual jobs (one per combination).
"""

import itertools
import logging

from opp_ci import platforms

_logger = logging.getLogger(__name__)


def _parse_name_version(combined):
    """Parse a combined ``"Name 1.2"`` string into ``(name, version)``."""
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


def _resolve_level_axis(config, name_key, version_key):
    """Return a list of ``(name, version)`` tuples for one platform level.

    Either ``config[name_key]`` is a list of combined ``"Name version"``
    strings (combined style) or both ``config[name_key]`` and
    ``config[version_key]`` are present (structured cross-product style).
    Missing entirely → ``[(None, None)]``.
    """
    names = config.get(name_key, [None])
    if version_key in config:
        versions = config[version_key]
        return list(itertools.product(names, versions))
    return [_parse_name_version(n) for n in names]


def _resolve_platform_axis(config):
    """Resolve the three-level platform axis from a matrix config.

    Returns a list of
    ``(os, os_version, distro, distro_version, flavor, flavor_version)``
    6-tuples.

    The three levels (os, distro, flavor) don't cross-multiply — they form
    a hierarchy and *union* into the cell list. Each level contributes its
    own cells with implied parents filled in via the registry:

    * ``os: [Linux, Windows]`` → (Linux), (Windows)
    * ``distro: [Ubuntu, Fedora]`` → (Linux, Ubuntu), (Linux, Fedora)
    * ``flavor: [Kubuntu]`` → (Linux, Ubuntu, Kubuntu)

    Versions within a level still cross-product using the same combined /
    structured rules as the compiler axis. An axis that names no entries
    contributes no cells. When no level is named at all, returns one
    ``(None, None, None, None, None, None)`` cell so the caller's
    Cartesian product still has something to iterate.
    """
    cells = []

    def _add(os_name, os_ver, distro_name, distro_ver, flavor_name, flavor_ver):
        try:
            r_os, r_distro, r_flavor = platforms.resolve_platform(
                os=os_name, distro=distro_name, flavor=flavor_name,
            )
        except ValueError:
            return
        canon_os = platforms._os_canonical(r_os) if r_os else None
        cell = (
            canon_os,
            os_ver if canon_os and canon_os != "Linux" else None,
            r_distro,
            distro_ver if r_distro else None,
            r_flavor,
            flavor_ver if r_flavor else None,
        )
        if cell not in cells:
            cells.append(cell)

    has_os = "os" in config
    has_distro = "distro" in config
    has_flavor = "flavor" in config

    if has_os:
        for name, ver in _resolve_level_axis(config, "os", "os_version"):
            _add(name, ver, None, None, None, None)

    if has_distro:
        for name, ver in _resolve_level_axis(config, "distro", "distro_version"):
            _add(None, None, name, ver, None, None)

    if has_flavor:
        for name, ver in _resolve_level_axis(config, "flavor", "flavor_version"):
            _add(None, None, None, None, name, ver)

    return cells or [(None, None, None, None, None, None)]


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


def _resolve_arch_axis(config):
    """Return the list of arch values; defaults to [None] (no constraint).

    A single string is auto-promoted to a list, matching the isolation/toolchain
    axes. omnetpp's supported arches are ``"amd64"`` and ``"aarch64"`` —
    but the axis is a free-form string and accepts any value.
    """
    value = config.get("arch", [None])
    if isinstance(value, str):
        value = [value]
    return list(value)


def _resolve_isolation_axis(config):
    """Return the list of isolation values; defaults to ["none"]."""
    value = config.get("isolation", ["none"])
    if isinstance(value, str):
        value = [value]
    return list(value)


def _resolve_toolchain_axis(config):
    """Return the list of toolchain values; defaults to ["none"]."""
    value = config.get("toolchain", ["none"])
    if isinstance(value, str):
        value = [value]
    return list(value)


# Compiler (name, version) pairs that opp_env can provide via Nix.
# opp_env currently exposes only two stdenv flavors (see
# opp_env/database/omnetpp.py): the "gcc7" option pins gcc 7, while the
# "clang" option uses llvmPackages.stdenv. Compiler version is implicit
# for clang. Extend this allow-list when opp_env grows more options.
_NIX_SUPPORTED_COMPILERS = {
    ("gcc", "7"),
    ("clang", None),  # clang of unspecified version → opp_env's llvmPackages.stdenv
}


def _validate_nix_compiler(compiler, compiler_version):
    """
    Raise ValueError if (compiler, compiler_version) can't be provided by opp_env.

    When toolchain == "nix" but the matrix names a compiler opp_env doesn't
    expose, the job would silently fall back to opp_env's default. Strict
    validation makes the matrix honest about what is actually being tested.
    """
    if compiler is None:
        return  # no constraint — opp_env's project default is fine
    name = compiler.lower()
    if (name, compiler_version) in _NIX_SUPPORTED_COMPILERS:
        return
    if name == "clang" and (name, None) in _NIX_SUPPORTED_COMPILERS:
        # clang with any version is accepted as "clang"; version is advisory.
        return
    raise ValueError(
        f"toolchain=nix does not support compiler {compiler!r}"
        f" version {compiler_version!r}; opp_env only exposes "
        f"gcc-7 and clang (unversioned). "
        f"Either set toolchain=none (host or podman) or pick a supported compiler."
    )


def _build_platform_desc(os_name, os_version, arch, compiler_name, compiler_version,
                         distro=None, distro_version=None,
                         flavor=None, flavor_version=None):
    """Build a human-readable platform description from components."""
    return platforms.build_platform_desc(
        os=os_name, os_version=os_version,
        distro=distro, distro_version=distro_version,
        flavor=flavor, flavor_version=flavor_version,
        arch=arch, compiler=compiler_name, compiler_version=compiler_version,
    )


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
            "project": "inet",
            "version": "inet-4.5",
            "kind": "smoke",
            "mode": "release",
            "git_ref": "master",
            "os": "Linux",
            "os_version": None,
            "distro": "ubuntu",
            "distro_version": "24.04",
            "flavor": None,
            "flavor_version": None,
            "arch": "amd64",
            "compiler": "gcc",
            "compiler_version": "14",
            "isolation": "podman",
            "toolchain": "none",
            "platform_desc": "Ubuntu 24.04 (amd64) / gcc-14",
        }

    ``project`` is the matrix's project name (constant across the expansion).
    ``version`` comes from the ``versions`` axis and identifies which version
    record of the project to build (parallels TestRun.version for single runs).
    """
    kinds = config.get("kinds", ["smoke"])
    modes = config.get("modes", ["release"])
    versions = config.get("versions", [None])
    if "refs" in config:
        refs = config["refs"]
    elif "ref_range" in config:
        refs = _resolve_ref_range(project, config["ref_range"])
    else:
        refs = [None]
    platform_cells = _resolve_platform_axis(config)
    compiler_tuples = _resolve_compiler_axis(config)
    dep_combos = _resolve_deps_axis(config)
    isolations = _resolve_isolation_axis(config)
    toolchains = _resolve_toolchain_axis(config)
    arches = _resolve_arch_axis(config)

    jobs = []
    for (version, ref, kind, mode, platform_cell, arch,
         (comp_name, comp_ver), dep_pins, isolation, toolchain) in itertools.product(
            versions, refs, kinds, modes, platform_cells, arches, compiler_tuples,
            dep_combos, isolations, toolchains):
        if toolchain == "nix":
            _validate_nix_compiler(comp_name, comp_ver)
        os_name, os_ver, distro, distro_ver, flavor, flavor_ver = platform_cell
        jobs.append({
            "project": project,
            "version": version,
            "kind": kind,
            "mode": mode,
            "git_ref": ref,
            "os": os_name,
            "os_version": os_ver,
            "distro": distro,
            "distro_version": distro_ver,
            "flavor": flavor,
            "flavor_version": flavor_ver,
            "arch": arch,
            "compiler": comp_name,
            "compiler_version": comp_ver,
            "isolation": isolation,
            "toolchain": toolchain,
            "platform_desc": _build_platform_desc(
                os_name, os_ver, arch, comp_name, comp_ver,
                distro=distro, distro_version=distro_ver,
                flavor=flavor, flavor_version=flavor_ver,
            ),
            "resolved_deps": dep_pins or None,
        })

    _logger.info("Expanded matrix for %s: %d jobs", project, len(jobs))
    return jobs


DEFAULT_MATRICES = {
    "inet-default": {
        "project": "inet",
        "config": {
            "kinds": ["smoke", "fingerprint", "statistical"],
            "modes": ["release", "debug"],
            "distro": ["Ubuntu 24.04"],
            "compiler": ["gcc-14", "clang-18"],
        },
    },
    "omnetpp-default": {
        "project": "omnetpp",
        "config": {
            "kinds": ["smoke", "build"],
            "modes": ["release", "debug"],
            "distro": ["Ubuntu 24.04"],
            "arch": ["amd64", "aarch64"],
            "compiler": ["gcc-14", "clang-18"],
        },
    },
    # Example exercising the execution-environment axes: same project, four
    # different ways of running. Useful as a smoke matrix while developing.
    "omnetpp-platforms": {
        "project": "omnetpp",
        "config": {
            "kinds": ["smoke"],
            "modes": ["release"],
            "distro": ["Ubuntu 26.04", "Fedora 42"],
            "compiler": ["clang-22"],
            "isolation": ["podman"],
            "toolchain": ["none"],
        },
    },
}
