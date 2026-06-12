"""Resolve loose coordinate axes against the worker fleet (Phase 4 of
plan/pending/repeatable-tests-and-moving-target-matrices.md).

When a submit leaves a coordinate axis underspecified (no compiler, no arch,
…), we pin it to a concrete value the fleet actually advertises — chosen by a
deterministic **per-axis preference order**, so re-resolving against the same
fleet tags yields the same value (decision #9 / "Resolving a loose axis"). A
loose axis the fleet can't satisfy is rejected (reject-incomplete, decision #7).

Candidates come only from advertised worker tags, so the pinned coordinate is
always schedulable. Tags are the structured capability strings the scheduler
already matches on (see persistence.required_tags_for_test):
``compiler:<name>-<ver>``, ``arch:<arch>``, plus ``distro:``/``os:``/``flavor:``.

This slice resolves the cleanly tag-encoded, commonly-loose axes — **compiler
(+version)** and **arch** — and defaults **mode** (release/debug isn't
tag-gated; every worker does both). Resolving the platform hierarchy
(os/distro/flavor) from tags is the next slice; it stays caller-specified for
now. Ordered version axes (project/dep versions) are resolved by opp_env, not
here (see dependency.complete_lock_for_submit).
"""

import logging

from sqlalchemy import select

from opp_ci.db.models import Worker

_logger = logging.getLogger(__name__)

# Coordinator default per-axis preference. Categorical axes: a ranked list,
# first available wins. Recipe-level override is a future knob.
DEFAULT_PREFERENCES = {
    "compiler": ["clang", "gcc", "msvc"],   # family order; newest version within
    "arch": ["amd64", "aarch64"],
    "mode": ["release", "debug"],
}


def fleet_tags(session, *, enabled_only=True):
    """Union of capability tags advertised across workers.

    `enabled_only` skips drained/disabled workers — a resolved value should be
    one some *usable* worker offers, but momentary online status doesn't matter
    (the resolved Test schedules whenever a matching worker is online).
    """
    tags = set()
    for w in session.execute(select(Worker)).scalars():
        if enabled_only and not w.enabled:
            continue
        tags.update(w.tags or [])
    return tags


def _version_key(ver):
    """Sort key for a version string: numeric components compare numerically,
    so "14" > "9" and "24.04" > "6.1". None sorts lowest. Non-numeric parts
    fall back to their string form (still deterministic)."""
    if not ver:
        return (0, ())
    parts = []
    for chunk in str(ver).replace("-", ".").split("."):
        parts.append((1, int(chunk)) if chunk.isdigit() else (0, chunk))
    return (1, tuple(parts))


def candidate_axes(tags):
    """Parse a flat tag set into per-axis candidate values.

    Returns a dict with ``compiler`` → set of ``(name, version|None)`` and
    ``arch`` → set of arch strings (both lower-cased).
    """
    compilers, arches = set(), set()
    for t in tags:
        if t.startswith("compiler:"):
            name, _, ver = t[len("compiler:"):].partition("-")
            if name:
                compilers.add((name.lower(), ver or None))
        elif t.startswith("arch:"):
            arch = t[len("arch:"):].strip().lower()
            if arch:
                arches.add(arch)
    return {"compiler": compilers, "arch": arches}


def _pick_categorical(values, order):
    """Pick one value from `values` by ranked `order` (first present wins).
    Values absent from `order` rank after all listed ones, lexically — so the
    choice is always deterministic. Returns None for an empty set."""
    for pref in order:
        if pref in values:
            return pref
    return sorted(values)[0] if values else None


def resolve_loose_axes(coord, tags, *, preferences=None):
    """Pin loose `compiler`/`compiler_version`/`arch`/`mode` axes of *coord*
    against the fleet `tags`, in place, and return it.

    For each axis left loose, the candidate set is gated on fleet availability,
    then the best is chosen by the per-axis preference order (compiler: family
    then newest version; arch: ranked list; mode: ranked default — not
    tag-gated). Raises ValueError if a tag-gated loose axis has no fleet
    candidate (reject-incomplete). An already-specified axis is left untouched.
    """
    prefs = preferences or DEFAULT_PREFERENCES
    cand = candidate_axes(tags)

    # ── compiler (+ version): family by preference, then newest version ──
    if not coord.get("compiler"):
        families = {name for name, _ in cand["compiler"]}
        family = _pick_categorical(families, prefs["compiler"])
        if family is None:
            raise ValueError(
                "No worker advertises a compiler; cannot resolve the loose "
                "compiler axis (reject-incomplete).")
        coord["compiler"] = family
        coord["compiler_version"] = _newest_version(cand["compiler"], family)
    elif not coord.get("compiler_version"):
        family = coord["compiler"].lower()
        ver = _newest_version(cand["compiler"], family)
        if ver is None:
            raise ValueError(
                f"No worker advertises a version for compiler {family!r}; "
                f"cannot resolve the loose compiler version.")
        coord["compiler_version"] = ver

    # ── arch: ranked list, first available wins ──
    if not coord.get("arch"):
        arch = _pick_categorical(cand["arch"], prefs["arch"])
        if arch is None:
            raise ValueError(
                "No worker advertises an arch; cannot resolve the loose arch "
                "axis (reject-incomplete).")
        coord["arch"] = arch

    # ── mode: ranked default; not tag-gated (every worker does both) ──
    if not coord.get("mode"):
        coord["mode"] = prefs["mode"][0]

    return coord


def _newest_version(compiler_candidates, family):
    """Newest advertised version string for `family`, or None if the fleet
    offers that family only without a version."""
    versions = [ver for name, ver in compiler_candidates
                if name == family and ver]
    if not versions:
        return None
    return max(versions, key=_version_key)
