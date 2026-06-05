"""Helpers for writing the new test data model.

Centralises the get-or-create Test + create TestRun pattern so every call
site (web routes, REST API, CLI, github webhook) stays consistent.
"""

import datetime
import logging
import platform as _platform

from sqlalchemy import select

from opp_ci.db.models import (
    TEST_COORD_FIELDS,
    Test,
    TestMatrix,
    TestMatrixRun,
    TestRun,
    TestRunLifecycle,
    compute_test_coord_hash,
)

_logger = logging.getLogger(__name__)


def job_to_coord(job, *, project=None, opp_file=None):
    """Project a job spec (or form-field dict) into the Test coord shape.

    Job-spec keys match the `Test` column names one-to-one, so this is
    just a filter to the closed `TEST_COORD_FIELDS` set. `project` and
    `opp_file` can be supplied by the caller for sites where the job
    dict omits them.
    """
    coord = {field: job.get(field) for field in TEST_COORD_FIELDS}
    if project is not None:
        coord["project"] = project
    if opp_file is not None and coord.get("opp_file") is None:
        coord["opp_file"] = opp_file
    return coord


def get_or_create_test(session, coord):
    """Return the Test row matching `coord`, creating it if missing.

    `coord` is a dict over `TEST_COORD_FIELDS` (unknown keys ignored,
    missing keys treated as None). Caller is responsible for commit/flush.
    """
    h = compute_test_coord_hash(coord)
    existing = session.execute(
        select(Test).where(Test.coord_hash == h)
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    fields = {field: coord.get(field) for field in TEST_COORD_FIELDS}
    test = Test(coord_hash=h, **fields)
    session.add(test)
    session.flush()
    return test


def create_matrix_run(session, *, matrix_id, trigger="manual",
                      github_owner=None, github_repo=None,
                      github_commit_sha=None, github_pr_number=None,
                      github_status_url=None):
    """Create a TestMatrixRun row for one matrix submission."""
    matrix_run = TestMatrixRun(
        matrix_id=matrix_id,
        trigger=trigger,
        github_owner=github_owner,
        github_repo=github_repo,
        github_commit_sha=github_commit_sha,
        github_pr_number=github_pr_number,
        github_status_url=github_status_url,
    )
    session.add(matrix_run)
    session.flush()
    return matrix_run


def create_test_run(session, *, test_id, matrix_run_id=None,
                    commit_sha=None, git_ref=None, version=None,
                    resolved_deps=None):
    """Create a queued TestRun targeting `test_id`."""
    run = TestRun(
        test_id=test_id,
        matrix_run_id=matrix_run_id,
        commit_sha=commit_sha,
        git_ref=git_ref,
        version=version,
        resolved_deps=resolved_deps,
        lifecycle=TestRunLifecycle.queued,
    )
    session.add(run)
    session.flush()
    return run


def enqueue_job(session, job, *, project, opp_file=None, matrix_run_id=None):
    """End-to-end: turn one expand_matrix job dict into a queued TestRun.

    Looks up (or creates) the matching `Test`, then creates a `TestRun`
    parented to `matrix_run_id` if given. Returns the TestRun. Caller
    commits.
    """
    coord = job_to_coord(job, project=project, opp_file=opp_file)
    test = get_or_create_test(session, coord)
    return create_test_run(
        session,
        test_id=test.id,
        matrix_run_id=matrix_run_id,
        commit_sha=None,
        git_ref=job.get("git_ref"),
        version=job.get("version"),
        resolved_deps=job.get("resolved_deps"),
    )


def capture_system_snapshot():
    """Best-effort dict of system facts captured at TestRun start.

    Phase 1: minimal — hostname, OS/arch, Python version, captured-at
    timestamp. Workers can replace this with richer probes (rolling-
    release identifiers, libc version, podman image digest, /proc/cpuinfo,
    /proc/meminfo, etc.) later. Failure of any probe leaves that key
    absent rather than raising; failure of the whole capture returns an
    empty dict.
    """
    snapshot = {
        "captured_at": datetime.datetime.utcnow().isoformat() + "Z",
    }
    try:
        snapshot["hostname"] = _platform.node()
    except Exception:
        pass
    try:
        snapshot["os"] = {
            "system": _platform.system(),
            "release": _platform.release(),
            "version": _platform.version(),
            "machine": _platform.machine(),
        }
    except Exception:
        pass
    try:
        snapshot["python"] = _platform.python_version()
    except Exception:
        pass
    return snapshot
