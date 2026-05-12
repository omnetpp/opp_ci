"""
Compatibility report generation for opp_ci (Stage 8).

Computes cross-project compatibility matrices from existing TestRun data.
For each project, shows which versions of its dependencies produced passing
vs. failing builds.
"""

import logging
from collections import defaultdict

from sqlalchemy import select

from opp_ci.db.models import Project, TestRun, TestRunStatus, Version

_logger = logging.getLogger(__name__)


def get_compatibility_matrix(session, project_name):
    """
    Build a compatibility matrix for a project against its dependencies.

    Returns a dict:
        {
            "project": "inet",
            "dependency": "omnetpp",
            "rows": [
                {
                    "version": "4.5",
                    "cells": {
                        "6.1.0": "passed",
                        "6.0.3": "passed",
                        "5.7.0": "failed",
                    },
                },
                ...
            ],
            "dep_versions": ["6.1.0", "6.0.3", "5.7.0"],
        }

    If the project has multiple dependencies, returns a list of such dicts
    (one per dependency).
    """
    project = session.execute(
        select(Project).where(Project.name == project_name)
    ).scalar_one_or_none()

    if project is None:
        return []

    dep_names = project.dependency_names or []
    if not dep_names:
        return []

    # Get all finished runs for this project
    finished = (TestRunStatus.passed, TestRunStatus.failed, TestRunStatus.error)
    runs = session.execute(
        select(TestRun).where(
            TestRun.project == project_name,
            TestRun.status.in_(finished),
        )
    ).scalars().all()

    if not runs:
        return []

    # Also get version records to map version labels to dependency info
    versions = session.execute(
        select(Version).where(Version.project_id == project.id)
    ).scalars().all()

    # Build a lookup: version_label -> resolved_dependencies
    version_deps = {}
    for v in versions:
        if v.resolved_dependencies:
            version_deps[v.opp_env_version] = v.resolved_dependencies
            if v.label and v.label != v.opp_env_version:
                version_deps[v.label] = v.resolved_dependencies

    results = []
    for dep_name in dep_names:
        matrix = _build_dep_matrix(runs, dep_name, version_deps)
        if matrix:
            matrix["project"] = project_name
            matrix["dependency"] = dep_name
            results.append(matrix)

    return results


def _build_dep_matrix(runs, dep_name, version_deps):
    """
    Build the compatibility grid for one dependency.

    Groups runs by (project_version, dep_version) and determines the
    aggregate status for each cell.
    """
    # cells[(project_version, dep_version)] -> list of statuses
    cells = defaultdict(list)

    for run in runs:
        project_version = run.version or run.git_ref
        if not project_version:
            continue

        # Determine which version of the dependency was used
        dep_version = _get_dep_version_for_run(run, dep_name, version_deps)
        if not dep_version:
            continue

        cells[(project_version, dep_version)].append(run.status)

    if not cells:
        return None

    # Collect all project versions and dep versions seen
    project_versions = sorted(set(pv for pv, _ in cells.keys()), reverse=True)
    dep_versions = sorted(set(dv for _, dv in cells.keys()), reverse=True)

    rows = []
    for pv in project_versions:
        row_cells = {}
        for dv in dep_versions:
            statuses = cells.get((pv, dv), [])
            if not statuses:
                row_cells[dv] = None
            else:
                row_cells[dv] = _aggregate_status(statuses)
        rows.append({"version": pv, "cells": row_cells})

    return {"rows": rows, "dep_versions": dep_versions}


def _get_dep_version_for_run(run, dep_name, version_deps):
    """
    Determine which version of dep_name was used for a given run.

    Checks (in order):
    1. The run's version field mapped through version_deps
    2. The run's git_ref mapped through version_deps
    """
    for key in (run.version, run.git_ref):
        if key and key in version_deps:
            deps = version_deps[key]
            if dep_name in deps:
                return deps[dep_name]
    return None


def _aggregate_status(statuses):
    """
    Aggregate a list of TestRunStatus values into a single summary string.

    Rules:
    - If all passed -> "passed"
    - If any failed -> "failed"
    - If any error (and no fail) -> "error"
    """
    has_fail = any(s == TestRunStatus.failed for s in statuses)
    has_error = any(s == TestRunStatus.error for s in statuses)
    all_pass = all(s == TestRunStatus.passed for s in statuses)

    if all_pass:
        return "passed"
    elif has_fail:
        return "failed"
    elif has_error:
        return "error"
    return "mixed"
