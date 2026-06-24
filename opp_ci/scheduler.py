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


def _parse_deps_axis(deps_str):
    """Parse 'omnetpp=6.3.0,6.2.0;inet=4.5' into a deps-axis dict.

    Returns {"omnetpp": ["6.3.0", "6.2.0"], "inet": ["4.5"]}. A value may be a
    git ref ('omnetpp=git@omnetpp-6.x'), which parses to a ``{"git": "<ref>"}``
    object that resolution later pins to a commit; release strings pass through
    unchanged. Raises ValueError on a malformed clause. Framework-agnostic so
    both the CLI and the REST handler can call it.
    """
    from opp_ci.dependency import parse_dep_value
    result = {}
    for part in deps_str.split(";"):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise ValueError(
                f"Invalid deps format: expected 'name=ver1,ver2', got '{part}'")
        name, versions = part.split("=", 1)
        result[name.strip()] = [parse_dep_value(v) for v in versions.split(",") if v.strip()]
    return result


def _parse_ref_range(ref_range_str):
    """Parse a 'base..head' string into {"base": ..., "head": ...}.

    Raises ValueError if the format is wrong or either side is empty.
    """
    if ".." not in ref_range_str:
        raise ValueError(
            f"Invalid ref-range format: expected 'base..head', got '{ref_range_str}'")
    base, head = ref_range_str.split("..", 1)
    base, head = base.strip(), head.strip()
    if not base or not head:
        raise ValueError("Invalid ref-range format: both base and head must be non-empty")
    return {"base": base, "head": head}


def _build_matrix_config(*, project, kinds, modes="release", versions=None,
                         os_names=None, os_versions=None,
                         distros=None, distro_versions=None,
                         flavors=None, flavor_versions=None,
                         compilers=None, compiler_versions=None,
                         arches=None, refs=None, ref_range=None,
                         deps=None, isolation=None, toolchain=None,
                         workers=None, worker_tags=None):
    """Compose a matrix-config dict from `create-matrix`'s flat CLI flags.

    The single source of truth for "CLI flags → matrix config": both the
    local `opp_ci create-matrix` body and its `--remote` handler (which
    composes the config client-side, then posts it to `POST /matrices`)
    call this, so the two paths can never drift. Each flag is a
    comma-separated string (or None);
    `deps` is the `name=v1,v2;…` axis syntax and `ref_range` is
    `base..head`. Raises ValueError on conflicting or malformed input.
    """
    if refs and ref_range:
        raise ValueError("refs and ref-range are mutually exclusive.")

    def _split(s):
        return [x.strip() for x in s.split(",") if x.strip()]

    config = {
        "kinds": _split(kinds),
        "modes": _split(modes),
        "versions": _split(versions) if versions else [project],
    }
    if ref_range:
        config["ref_range"] = _parse_ref_range(ref_range)
    elif refs:
        config["refs"] = _split(refs)
    for key, value in (
        ("os", os_names), ("os_version", os_versions),
        ("distro", distros), ("distro_version", distro_versions),
        ("flavor", flavors), ("flavor_version", flavor_versions),
        ("compiler", compilers), ("compiler_version", compiler_versions),
        ("arch", arches), ("isolation", isolation), ("toolchain", toolchain),
    ):
        if value:
            config[key] = _split(value)
    if deps:
        config["deps"] = _parse_deps_axis(deps)
    # Routing constraint, not a cross-product axis: a single selector applied
    # to every cell of the expansion (see expand_matrix). `workers` are
    # worker *names* (each → the implicit "worker:<name>" tag); `worker_tags`
    # are raw capability tags taken verbatim (e.g. "gpu", "team:core"). Both
    # are ANDed onto the run's required set, so a worker must satisfy all of
    # them to claim a cell.
    selector = [f"worker:{w}" for w in _split(workers)] if workers else []
    if worker_tags:
        selector += _split(worker_tags)
    if selector:
        config["worker_selector"] = sorted(set(selector))
    return config


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


def _is_full_sha(ref):
    """True if `ref` is a full 40-hex commit SHA (already pinned)."""
    return (isinstance(ref, str) and len(ref) == 40
            and all(c in "0123456789abcdef" for c in ref.lower()))


def _dep_is_moving(value):
    """A deps-axis value is *moving* (unresolved) when it names a git ref not
    yet pinned to a concrete commit — a ``{"git": ref}`` object lacking a
    40-hex ``commit``. Release version strings are never moving."""
    if isinstance(value, dict):
        commit = value.get("commit")
        return not (commit and _is_full_sha(commit))
    return False


def matrix_is_recipe(config):
    """True if a matrix config is a *recipe* — not yet pinned all the way down,
    so it must be resolved before it can run. A matrix is a recipe when:

      * it lacks a compiler, an arch, or a platform (os/distro/flavor) — the
        fleet-resolvable coordinate axes a job needs to pass validation; **or**
      * its source is **moving** — a ``ref_range``, or a ``refs`` entry that
        isn't a concrete commit SHA (a branch/tag/range); **or**
      * a ``deps`` entry names a **git ref** not yet pinned to a commit (a
        branch/tag). Resolution pins both source and dep refs to SHAs so a
        resolved matrix never carries a moving ref.

    A config with a full coordinate and only pinned-SHA refs is already
    resolved/runnable.
    """
    config = config or {}
    has_platform = config.get("os") or config.get("distro") or config.get("flavor")
    if not (config.get("compiler") and config.get("arch") and has_platform):
        return True
    if config.get("ref_range"):
        return True
    if any(r and not _is_full_sha(r) for r in (config.get("refs") or [])):
        return True
    return any(_dep_is_moving(v)
               for vals in (config.get("deps") or {}).values() for v in vals)


def _resolve_ref_range(project_name, range_str):
    """Resolve a ``base..head`` range string to its commit SHAs via GitHub.

    This is the *expand* of the source dimension: one ``base..topic`` ref
    expression fans out into one commit per reachable commit. Raises
    ValueError if the project has no GitHub repo or the range is empty —
    reject-incomplete for the source (decision #7).
    """
    from opp_ci.db.connection import SessionLocal
    from opp_ci.db.models import Project
    from opp_ci.github.client import GitHubClient
    from sqlalchemy import select

    base, head = (part.strip() for part in range_str.split("..", 1))
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
        shas = client.list_commits_in_range(proj.github_owner, proj.github_repo, base, head)
        if not shas:
            raise ValueError(
                f"Ref range {range_str!r} resolved to no commits for {project_name}")
        _logger.info("Resolved ref range %s..%s to %d commits for %s", base, head, len(shas), project_name)
        return shas
    finally:
        session.close()


def _pick_ref_sha(refs, git_ref):
    """Resolve `git_ref` against a ``{refname: sha}`` map: branch first, then
    tag (preferring the peeled commit ``^{}`` of an annotated tag), then an
    exact refname / HEAD. Returns the SHA or None."""
    if f"refs/heads/{git_ref}" in refs:
        return refs[f"refs/heads/{git_ref}"]
    peeled = refs.get(f"refs/tags/{git_ref}^{{}}")
    if peeled:
        return peeled
    if f"refs/tags/{git_ref}" in refs:
        return refs[f"refs/tags/{git_ref}"]
    return refs.get(git_ref)


def resolve_source_commit(project_name, git_ref):
    """Resolve a single ref (branch / tag / SHA) to a concrete commit SHA.

    The source half of "pinned all the way down" for single-run submits
    (Phase 2b). Resolution is over **HTTP** and needs no credentials for a
    public repo: a configured API token takes the REST fast path (private
    repos / higher rate limit), otherwise the git smart-HTTP ref advertisement
    resolves the ref token-free (and isn't subject to the REST 60/hr limit).
    **Strict** (decision #7): a non-None ref that can't be pinned to a real
    commit — no repo configured, unknown ref, GitHub unreachable — raises
    ValueError rather than leaving the source unpinned. Returns None when
    `git_ref` is None (no source axis to pin) and the SHA unchanged when it is
    already a full 40-hex SHA.
    """
    if not git_ref:
        return None
    if _is_full_sha(git_ref):
        return git_ref.lower()

    from opp_ci.db.connection import SessionLocal
    from opp_ci.db.models import Project
    from opp_ci.github.client import GitHubClient
    from sqlalchemy import select

    session = SessionLocal()
    try:
        proj = session.execute(
            select(Project).where(Project.name == project_name)
        ).scalar_one_or_none()
        if proj is None or not proj.github_owner or not proj.github_repo:
            raise ValueError(
                f"Cannot pin ref {git_ref!r}: project {project_name!r} has no "
                f"GitHub repo configured, so the source can't be resolved to a "
                f"commit (a run must be pinned to a concrete commit).")
        client = GitHubClient()
        # Authenticated REST fast path (private repos / high rate limit).
        if client.is_configured:
            for ref_path in (f"heads/{git_ref}", f"tags/{git_ref}", git_ref):
                sha = client.resolve_ref(proj.github_owner, proj.github_repo, ref_path)
                if sha:
                    return sha
        # Token-free HTTP fallback: the git smart-HTTP ref advertisement.
        sha = _pick_ref_sha(
            client.list_remote_refs(proj.github_owner, proj.github_repo), git_ref)
        if sha:
            return sha
        raise ValueError(
            f"Could not resolve ref {git_ref!r} to a commit for {project_name} "
            f"on github.com/{proj.github_owner}/{proj.github_repo} "
            f"(unknown ref, or GitHub unreachable).")
    finally:
        session.close()


def pin_matrix_refs(project, config):
    """Return *config* with its source pinned all the way down — every entry of
    the refs axis replaced by a concrete commit SHA, so no moving branch/tag
    survives in a resolved matrix.

    A branch/tag → its current SHA; a ``base..topic`` range → the range's commit
    SHAs; a full SHA → itself; the legacy ``ref_range`` dict → its SHAs. A
    config with no refs/ref_range is returned unchanged (no source axis to pin).
    Strict: raises ValueError if a ref can't be resolved (decision #7).
    """
    if config.get("refs"):
        pinned = []
        for ref in config["refs"]:
            if not ref:
                continue
            if ".." in ref:
                pinned.extend(_resolve_ref_range(project, ref))
            elif _is_full_sha(ref):
                pinned.append(ref.lower())
            else:
                pinned.append(resolve_source_commit(project, ref))
    elif config.get("ref_range"):
        rr = config["ref_range"]
        pinned = _resolve_ref_range(project, f"{rr['base']}..{rr['head']}")
    else:
        return config

    out = dict(config)
    out["refs"] = pinned
    out.pop("ref_range", None)
    return out


def pin_matrix_deps(config):
    """Return *config* with every moving git-ref **dependency** pinned to a
    concrete commit SHA, so no moving dep branch/tag survives in a resolved
    matrix (the dependency analogue of :func:`pin_matrix_refs`).

    Each ``deps`` cell of the form ``{"git": <ref>}`` (no commit) is resolved
    against *that dependency's* GitHub repo and rewritten to
    ``{"git": <ref>, "commit": <sha>}``; release-version cells and cells already
    pinned to a SHA pass through. A config with no ``deps`` is returned
    unchanged. Strict: raises ValueError if a dep ref can't be resolved
    (decision #7) — the dep must be a registered project with a GitHub repo.
    """
    deps = config.get("deps")
    if not deps:
        return config
    pinned_deps = {}
    for name, cells in deps.items():
        out_cells = []
        for cell in cells:
            if isinstance(cell, dict) and not (
                    cell.get("commit") and _is_full_sha(cell["commit"])):
                ref = cell.get("git") or cell.get("ref")
                sha = resolve_source_commit(name, ref)
                out_cells.append({"git": ref, "commit": sha})
            else:
                out_cells.append(cell)
        pinned_deps[name] = out_cells
    out = dict(config)
    out["deps"] = pinned_deps
    return out


def _resolve_refs_axis(project, config):
    """Resolve the ``refs`` axis into ``(git_ref, commit_sha)`` pairs.

    Decision #5: the ``refs`` axis carries the source spec. Each element is a
    ref expression:
      * ``base..topic``  → fans out into one pair per commit in the range,
        each pinned (``git_ref`` = ``commit_sha`` = the SHA) — the moving
        target, expanded into per-commit resolved Tests;
      * a full 40-hex SHA → already pinned (``commit_sha`` = the SHA);
      * a branch / tag    → passes through unpinned (``commit_sha`` None) so a
        plain matrix preview never touches the network. Pinning a single ref
        to its current SHA is the separate ``resolve`` step.

    Falls back to the legacy ``ref_range`` dict, then to ``[(None, None)]``
    (no source axis).
    """
    if "refs" in config:
        raw_refs = config["refs"]
    elif "ref_range" in config:
        rr = config["ref_range"]
        return [(sha, sha)
                for sha in _resolve_ref_range(project, f"{rr['base']}..{rr['head']}")]
    else:
        return [(None, None)]

    pairs = []
    for ref in raw_refs:
        if ref and ".." in ref:
            pairs.extend((sha, sha) for sha in _resolve_ref_range(project, ref))
        elif _is_full_sha(ref):
            pairs.append((ref, ref))
        else:
            pairs.append((ref, None))
    return pairs


def describe_expansion(config):
    """Human description of how many Tests this matrix expands into — computed
    **offline** (no GitHub round-trip), so it's safe to call on every page
    render.

    It multiplies the cartesian axis cardinalities exactly as `expand_matrix`
    would, but treats the refs axis specially: static refs (branches/tags/SHAs)
    are counted, while a ``base..topic`` range's commit count is unknown until
    resolved at run time, so it's described "per commit" rather than enumerated.

    Loose coordinate axes (a recipe's missing compiler/arch/platform) count as
    one each — matching resolution, which pins each to a single fleet value —
    so a recipe and the snapshot it mints report the same count.
    """
    config = config or {}

    def _n(seq):
        return len(seq) if seq else 1

    per_coord = (
        _n(config.get("versions", [None]))
        * _n(config.get("kinds", ["smoke"]))
        * _n(config.get("modes", ["release"]))
        * len(_resolve_platform_axis(config))
        * len(_resolve_arch_axis(config))
        * len(_resolve_compiler_axis(config))
        * len(_resolve_deps_axis(config))
        * len(_resolve_isolation_axis(config))
        * len(_resolve_toolchain_axis(config))
    )

    range_expr = None
    static_refs = 0
    if config.get("refs"):
        for r in config["refs"]:
            if r and ".." in r:
                range_expr = r
            else:
                static_refs += 1
    elif config.get("ref_range"):
        rr = config["ref_range"]
        range_expr = f"{rr.get('base', '')}..{rr.get('head', '')}"

    def _plural(n):
        return "Test" if n == 1 else "Tests"

    if range_expr:
        prefix = ""
        if static_refs:
            pinned = per_coord * static_refs
            prefix = f"{pinned} {_plural(pinned)} for the pinned refs, plus "
        return (f"{prefix}{per_coord} {_plural(per_coord)} per commit in "
                f"{range_expr} (commits resolved at run time)")

    total = per_coord * (static_refs or 1)
    return f"{total} {_plural(total)}"


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
    # The refs axis carries the source spec; each entry resolves to a
    # (git_ref, commit_sha) pair — a base..topic range fans out to one pinned
    # pair per commit (the moving-target case).
    ref_pairs = _resolve_refs_axis(project, config)
    platform_cells = _resolve_platform_axis(config)
    compiler_tuples = _resolve_compiler_axis(config)
    dep_combos = _resolve_deps_axis(config)
    isolations = _resolve_isolation_axis(config)
    toolchains = _resolve_toolchain_axis(config)
    arches = _resolve_arch_axis(config)
    # Routing constraint applied to every cell (not a product axis): the same
    # worker_selector rides on each job dict and onto each TestRun.
    worker_selector = config.get("worker_selector") or None

    jobs = []
    for (version, (ref, commit_sha), kind, mode, platform_cell, arch,
         (comp_name, comp_ver), dep_pins, isolation, toolchain) in itertools.product(
            versions, ref_pairs, kinds, modes, platform_cells, arches, compiler_tuples,
            dep_combos, isolations, toolchains):
        # Coverage forces a coverage-instrumented build and ignores the modes
        # axis: emit one job (not one per mode) and pin its coordinate to
        # mode=coverage so it agrees with what the executor actually runs.
        if kind == "coverage":
            if mode != modes[0]:
                continue
            job_mode = "coverage"
        else:
            job_mode = mode
        if toolchain == "nix":
            _validate_nix_compiler(comp_name, comp_ver)
        os_name, os_ver, distro, distro_ver, flavor, flavor_ver = platform_cell
        jobs.append({
            "project": project,
            "version": version,
            "kind": kind,
            "mode": job_mode,
            "git_ref": ref,
            "commit_sha": commit_sha,
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
            "worker_selector": worker_selector,
        })

    _logger.info("Expanded matrix for %s: %d jobs", project, len(jobs))
    return jobs
