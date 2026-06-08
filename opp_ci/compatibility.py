"""
Compatibility report generation for opp_ci.

Builds cross-project compatibility matrices from opp_env declared
compatibility data (Version.resolved_dependencies) and overlays empirical
pass/fail results from TestRun records where available.
"""

import logging
from collections import defaultdict

from sqlalchemy import select

from opp_ci.db.models import (
    Project, Test, TestRun, TestRunLifecycle,
    TestVerdictKind, Version,
)

_logger = logging.getLogger(__name__)

# Execution dimensions that live on the Test row and that the empirical
# overlay can be filtered by. Single source of truth shared by the per-run
# capture, the filter, the scoped-option collection, and the web route.
_DIMENSIONS = (
    "os", "os_version", "distro", "distro_version",
    "flavor", "flavor_version", "compiler", "compiler_version",
    "mode", "kind", "toolchain", "isolation", "arch",
)


def get_compatibility_matrix(session, project_name, filters=None):
    """
    Build compatibility matrices for a project against each of its dependencies.

    The grid is populated primarily from opp_env's declared compatibility
    (Version.resolved_dependencies).  Where test runs exist and the
    dependency version used can be determined, empirical results are
    overlaid on the declared-compatible cells.

    `filters` is an optional dict of {dimension: value} (keys drawn from
    `_DIMENSIONS`) that subsets the empirical overlay only: a cell keeps
    just the runs whose Test row matches every active filter, then
    aggregates what survives. Declared compatibility carries no
    OS/compiler, so a filter can never remove a `compatible` cell — it can
    only revert a tested cell back toward `compatible` when no run on the
    selected platform exists. Empty/None filters reproduce the unfiltered
    page exactly.

    Returns a dict:
        {
            "matrices": [               # one entry per dependency
                {
                    "project": "inet",
                    "dependency": "omnetpp",
                    "rows": [
                        {
                            "version": "inet-4.5",
                            "cells": {"6.1": <cell>, "6.0.3": <cell>},
                        },
                    ],
                    "dep_versions": ["6.1", "6.0.3"],
                },
            ],
            "options": {dim: [sorted distinct values], ...},  # scoped to project
        }

    Each cell is either None (not declared compatible) or a dict carrying
    the two encoded channels plus the contributing runs:
        {"status": str, "verdict": str | None, "runs": [run-dict, ...]}

    Cell `status` values (→ color):
        "compatible" - declared compatible, no matching test run
        "PASS"/"FAIL"/"ERROR"/"SKIPPED" - all surviving runs share that code
        "mixed"      - surviving runs disagree
    Cell `verdict` values (→ symbol), aggregated the same homogeneous way:
        "EXPECTED"/"UNKNOWN"/"UNEXPECTED", "mixed", or None (no runs).
    """
    empty = {"matrices": [], "options": {dim: [] for dim in _DIMENSIONS}}

    project = session.execute(
        select(Project).where(Project.name == project_name)
    ).scalar_one_or_none()

    if project is None:
        return empty

    dep_names = project.dependency_names or []
    if not dep_names:
        return empty

    versions = session.execute(
        select(Version).where(Version.project_id == project.id)
    ).scalars().all()

    if not versions:
        return empty

    test_overlays = _collect_test_overlays(session, project_name, versions)
    options = _collect_options(test_overlays)

    # Drop blank/None filter values so an unset dropdown is a no-op.
    filters = {k: v for k, v in (filters or {}).items() if v}

    results = []
    for dep_name in dep_names:
        matrix = _build_declared_matrix(versions, dep_name, test_overlays, filters)
        if matrix:
            matrix["project"] = project_name
            matrix["dependency"] = dep_name
            results.append(matrix)

    return {"matrices": results, "options": options}


def _collect_options(test_overlays):
    """Sorted distinct non-empty value of each dimension across all overlay
    runs for the project — so the filter dropdowns offer only values that
    actually occur here."""
    opts = {dim: set() for dim in _DIMENSIONS}
    for runs in test_overlays.values():
        for run in runs:
            for dim in _DIMENSIONS:
                val = run.get(dim)
                if val not in (None, ""):
                    opts[dim].add(val)
    return {dim: sorted(vals) for dim, vals in opts.items()}


def _version_label(v):
    """Return a display label for a Version record."""
    return v.opp_env_version or v.label or f"id-{v.id}"


def _dep_compatible_versions(resolved_deps, dep_name):
    """
    Extract the list of compatible versions for dep_name.

    Handles both formats stored in Version.resolved_dependencies:
    - List: {"omnetpp": ["6.0", "6.1"]}  (from sync_catalog)
    - String: {"omnetpp": "6.1"}  (from add-version CLI)
    """
    if not resolved_deps or dep_name not in resolved_deps:
        return []
    val = resolved_deps[dep_name]
    if isinstance(val, list):
        return val
    if isinstance(val, str):
        return [val]
    return []


def _build_declared_matrix(versions, dep_name, test_overlays, filters=None):
    """
    Build the compatibility grid for one dependency from declared data.

    Declared cells start as "compatible" and are overlaid with the
    aggregated status/verdict of the test runs that match `filters`.
    """
    filters = filters or {}
    declared = set()
    all_dep_versions = set()

    for v in versions:
        vlabel = _version_label(v)
        for dv in _dep_compatible_versions(v.resolved_dependencies, dep_name):
            declared.add((vlabel, dv))
            all_dep_versions.add(dv)

    if not all_dep_versions:
        return None

    project_versions = sorted(
        {_version_label(v) for v in versions
         if _dep_compatible_versions(v.resolved_dependencies, dep_name)},
        reverse=True,
    )
    dep_versions = sorted(all_dep_versions, reverse=True)

    rows = []
    for pv in project_versions:
        row_cells = {}
        for dv in dep_versions:
            if (pv, dv) not in declared:
                row_cells[dv] = None
                continue
            runs = test_overlays.get((pv, dep_name, dv), [])
            if filters:
                runs = [r for r in runs if _run_matches_filters(r, filters)]
            # Empty `runs` (declared but untested, or filtered away) yields
            # status "compatible" / verdict None from the aggregators.
            row_cells[dv] = {
                "status": _aggregate_status(runs),
                "verdict": _aggregate_verdict(runs),
                "runs": runs,
            }
        rows.append({"version": pv, "cells": row_cells})

    return {"rows": rows, "dep_versions": dep_versions}


def _run_matches_filters(run, filters):
    """True iff the run-dict matches every active dimension filter."""
    return all(run.get(dim) == val for dim, val in filters.items())


def _run_dict(run):
    """Flatten a TestRun (and its joined Test dimensions) into the per-run
    record kept in the overlay. `verdict` is the recorded-verdict string
    (or None); `result_code` is the TestResultCode enum."""
    rec = {
        "result_code": run.result_code,
        "verdict": run.recorded_verdict,   # "EXPECTED"/"UNEXPECTED"/"UNKNOWN" or None
        "run_id": run.id,
        "finished_at": run.finished_at,
    }
    for dim in _DIMENSIONS:
        rec[dim] = getattr(run, dim)
    return rec


def _collect_test_overlays(session, project_name, versions):
    """
    Match finished test runs to (version_label, dep_name, dep_version) triples.

    A run is matched to a version record via run.version, run.git_ref, or
    run.project.  The dependency version is inferred from the version
    record's resolved_dependencies — only when a dep resolves to exactly
    one version (string format or single-element list).

    Returns: {(version_label, dep_name, dep_version): [run-dict, ...]}
    where each run-dict is produced by `_run_dict` (carries result_code,
    verdict, and every `_DIMENSIONS` value).
    """
    # All keys that might appear as TestRun.project for this project
    project_keys = {project_name}
    for v in versions:
        if v.opp_env_version:
            project_keys.add(v.opp_env_version)
        if v.label:
            project_keys.add(v.label)

    runs = session.execute(
        select(TestRun)
        .join(Test, TestRun.test_id == Test.id)
        .where(
            Test.project.in_(project_keys),
            TestRun.lifecycle == TestRunLifecycle.finished,
        )
    ).scalars().all()

    if not runs:
        return {}

    # key -> (version_label, resolved_dependencies)
    key_to_version = {}
    for v in versions:
        info = (_version_label(v), v.resolved_dependencies)
        if v.opp_env_version:
            key_to_version[v.opp_env_version] = info
        if v.label and v.label != v.opp_env_version:
            key_to_version[v.label] = info

    overlays = defaultdict(list)
    for run in runs:
        matched = None
        for candidate in (run.version, run.git_ref, run.project):
            if candidate and candidate in key_to_version:
                matched = key_to_version[candidate]
                break
        if not matched:
            continue

        vlabel, declared_deps = matched
        rd = _run_dict(run)

        # Prefer the run's own resolved_deps (exact pins from the matrix
        # deps axis).  Fall back to the version record's declared deps
        # only when they pin a dep to exactly one version.
        if run.resolved_deps:
            for dep_name, dep_ver in run.resolved_deps.items():
                if isinstance(dep_ver, str):
                    overlays[(vlabel, dep_name, dep_ver)].append(rd)
        elif declared_deps:
            for dep_name, dep_val in declared_deps.items():
                if isinstance(dep_val, str):
                    overlays[(vlabel, dep_name, dep_val)].append(rd)
                elif isinstance(dep_val, list) and len(dep_val) == 1:
                    overlays[(vlabel, dep_name, dep_val[0])].append(rd)

    return overlays


def _aggregate_status(runs):
    """Aggregate the surviving runs' result_code into one status string by
    pure homogeneity: a single shared code → that code's value; an empty
    set → "compatible" (declared, untested); a disagreeing set → "mixed".
    Note this is deliberately *not* the old precedence logic (where any
    FAIL won) — the cell color must distinguish "all FAIL" from "mixed"."""
    codes = {r["result_code"] for r in runs if r["result_code"] is not None}
    if not codes:
        return "compatible"
    if len(codes) == 1:
        return next(iter(codes)).value   # PASS / FAIL / ERROR / SKIPPED
    return "mixed"


def _aggregate_verdict(runs):
    """Aggregate the surviving runs' recorded verdict into one symbol-channel
    string, same homogeneity rule. A run with no recorded verdict folds into
    UNKNOWN. Empty set → None (a compatible/untested cell carries no symbol).
    """
    if not runs:
        return None
    kinds = {(r["verdict"] or TestVerdictKind.UNKNOWN.value) for r in runs}
    if len(kinds) == 1:
        return next(iter(kinds))         # EXPECTED / UNKNOWN / UNEXPECTED
    return "mixed"
