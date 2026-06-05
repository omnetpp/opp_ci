"""
Compatibility report generation for opp_ci.

Builds cross-project compatibility matrices from opp_env declared
compatibility data (Version.resolved_dependencies) and overlays empirical
pass/fail results from TestRun records where available.
"""

import logging
from collections import defaultdict

from sqlalchemy import select

from opp_ci.db.models import Project, Test, TestResultCode, TestRun, TestRunLifecycle, Version

_logger = logging.getLogger(__name__)


def get_compatibility_matrix(session, project_name):
    """
    Build compatibility matrices for a project against each of its dependencies.

    The grid is populated primarily from opp_env's declared compatibility
    (Version.resolved_dependencies).  Where test runs exist and the
    dependency version used can be determined, empirical pass/fail results
    are overlaid on the declared-compatible cells.

    Returns a list of dicts, one per dependency:
        {
            "project": "inet",
            "dependency": "omnetpp",
            "rows": [
                {
                    "version": "inet-4.5",
                    "cells": {"6.1": "PASS", "6.0.3": "compatible"},
                },
            ],
            "dep_versions": ["6.1", "6.0.3"],
        }

    Cell values:
        "compatible" - declared compatible by opp_env, not yet tested
        "PASS"       - tested and all runs passed
        "FAIL"       - tested and at least one run failed
        "ERROR"      - tested, at least one errored (none failed)
        "mixed"      - tested with mixed results
        None         - not declared compatible
    """
    project = session.execute(
        select(Project).where(Project.name == project_name)
    ).scalar_one_or_none()

    if project is None:
        return []

    dep_names = project.dependency_names or []
    if not dep_names:
        return []

    versions = session.execute(
        select(Version).where(Version.project_id == project.id)
    ).scalars().all()

    if not versions:
        return []

    test_overlays = _collect_test_overlays(session, project_name, versions)

    results = []
    for dep_name in dep_names:
        matrix = _build_declared_matrix(versions, dep_name, test_overlays)
        if matrix:
            matrix["project"] = project_name
            matrix["dependency"] = dep_name
            results.append(matrix)

    return results


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


def _build_declared_matrix(versions, dep_name, test_overlays):
    """
    Build the compatibility grid for one dependency from declared data.

    Cells start as "compatible" and are overridden by test results
    where available in test_overlays.
    """
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
            elif (pv, dep_name, dv) in test_overlays:
                row_cells[dv] = _aggregate_status(test_overlays[(pv, dep_name, dv)])
            else:
                row_cells[dv] = "compatible"
        rows.append({"version": pv, "cells": row_cells})

    return {"rows": rows, "dep_versions": dep_versions}


def _collect_test_overlays(session, project_name, versions):
    """
    Match finished test runs to (version_label, dep_name, dep_version) triples.

    A run is matched to a version record via run.version, run.git_ref, or
    run.project.  The dependency version is inferred from the version
    record's resolved_dependencies — only when a dep resolves to exactly
    one version (string format or single-element list).

    Returns: {(version_label, dep_name, dep_version): [TestRunStatus, ...]}
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

        # Prefer the run's own resolved_deps (exact pins from the matrix
        # deps axis).  Fall back to the version record's declared deps
        # only when they pin a dep to exactly one version.
        if run.resolved_deps:
            for dep_name, dep_ver in run.resolved_deps.items():
                if isinstance(dep_ver, str):
                    overlays[(vlabel, dep_name, dep_ver)].append(run.result_code)
        elif declared_deps:
            for dep_name, dep_val in declared_deps.items():
                if isinstance(dep_val, str):
                    overlays[(vlabel, dep_name, dep_val)].append(run.result_code)
                elif isinstance(dep_val, list) and len(dep_val) == 1:
                    overlays[(vlabel, dep_name, dep_val[0])].append(run.result_code)

    return overlays


def _aggregate_status(codes):
    """Aggregate a list of TestResultCode values into a single summary string."""
    has_fail = any(c == TestResultCode.FAIL for c in codes)
    has_error = any(c == TestResultCode.ERROR for c in codes)
    all_pass = all(c == TestResultCode.PASS for c in codes)

    if all_pass:
        return "PASS"
    elif has_fail:
        return "FAIL"
    elif has_error:
        return "ERROR"
    return "mixed"
