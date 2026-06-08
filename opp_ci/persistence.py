"""Helpers for writing the new test data model.

Centralises the get-or-create Test + create TestRun + create TestVerdict
pattern so every call site (web routes, REST API, CLI, github webhook)
stays consistent, and keeps verdict computation + matrix-run rollup in a
single place.
"""

import datetime
import logging
import platform as _platform

from sqlalchemy import select

from opp_ci.db.models import (
    TEST_COORD_FIELDS,
    ExpectedTestResult,
    Test,
    TestMatrix,
    TestMatrixRun,
    TestResultCode,
    TestRun,
    TestRunLifecycle,
    TestVerdict,
    TestVerdictKind,
    compute_test_coord_hash,
)

_logger = logging.getLogger(__name__)


def status_filter(query, status_str):
    """Filter a TestRun ``select()`` by a status string, lifecycle *or* outcome.

    A status string is either a lifecycle value
    (``queued``/``running``/``finished``/``cancelled``/``timed_out``) — matched
    against ``TestRun.lifecycle`` — or an outcome value
    (``PASS``/``FAIL``/``ERROR``/``SKIPPED``) — matched against
    ``TestRun.result_code``. This is the single source of truth for the
    ``status``-filter vocabulary shared by the CLI, web UI, and REST API.

    Raises ``ValueError`` if ``status_str`` is neither a lifecycle nor an
    outcome value; callers decide whether to surface that (CLI/REST 400) or
    swallow it (web UI no-rows fallback).
    """
    try:
        return query.where(TestRun.lifecycle == TestRunLifecycle(status_str))
    except ValueError:
        pass
    try:
        return query.where(TestRun.result_code == TestResultCode(status_str))
    except ValueError:
        pass
    raise ValueError(
        f"Invalid status {status_str!r}: expected a lifecycle "
        f"(queued/running/finished/cancelled/timed_out) or outcome "
        f"(PASS/FAIL/ERROR/SKIPPED) value."
    )


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


def create_matrix_run(session, *, matrix_id, trigger="manual", ref=None,
                      github_owner=None, github_repo=None,
                      github_commit_sha=None, github_pr_number=None,
                      github_status_url=None):
    """Create a TestMatrixRun row for one matrix submission."""
    matrix_run = TestMatrixRun(
        matrix_id=matrix_id,
        trigger=trigger,
        ref=ref,
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
                    resolved_deps=None, cache_fingerprint=None):
    """Create a queued TestRun targeting `test_id`."""
    run = TestRun(
        test_id=test_id,
        matrix_run_id=matrix_run_id,
        commit_sha=commit_sha,
        git_ref=git_ref,
        version=version,
        resolved_deps=resolved_deps,
        lifecycle=TestRunLifecycle.queued,
        cache_fingerprint=cache_fingerprint,
    )
    session.add(run)
    session.flush()
    return run


# ── Naming: look up / set names for Tests and TestMatrices ────────────


def get_test_by_name(session, name):
    """Return the Test with this exact `name`, or None. Blank → None."""
    if not name or not name.strip():
        return None
    return session.execute(
        select(Test).where(Test.name == name.strip())
    ).scalar_one_or_none()


def get_matrix_by_name(session, name):
    """Return the TestMatrix with this exact `name`, or None. Blank → None."""
    if not name or not name.strip():
        return None
    return session.execute(
        select(TestMatrix).where(TestMatrix.name == name.strip())
    ).scalar_one_or_none()


def set_test_name(session, test, name):
    """Set or clear `test.name`. Blank → NULL (anonymous).

    Raises ValueError if a *different* Test already holds the name, so
    every caller (web, CLI, REST) reports the same collision error.
    """
    cleaned = name.strip() if name else None
    if cleaned:
        existing = get_test_by_name(session, cleaned)
        if existing is not None and existing.id != test.id:
            raise ValueError(f"A test named {cleaned!r} already exists.")
    test.name = cleaned
    session.flush()
    return test


def set_matrix_name(session, matrix, name):
    """Set or clear `matrix.name`. Blank → NULL (anonymous).

    Raises ValueError on collision with a different TestMatrix.
    """
    cleaned = name.strip() if name else None
    if cleaned:
        existing = get_matrix_by_name(session, cleaned)
        if existing is not None and existing.id != matrix.id:
            raise ValueError(f"A matrix named {cleaned!r} already exists.")
    matrix.name = cleaned
    session.flush()
    return matrix


def create_matrix_from_axes(session, *, project, config, name=None, opp_file=None):
    """Create a TestMatrix row from a config dict.

    `name` may be None (anonymous). Shared by `matrix_create`, the web
    anonymous-run handler, and the CLI so the row is built one way.
    Raises ValueError if a named matrix collides.
    """
    cleaned = name.strip() if name else None
    if cleaned and get_matrix_by_name(session, cleaned) is not None:
        raise ValueError(f"A matrix named {cleaned!r} already exists.")
    matrix = TestMatrix(name=cleaned, project=project, opp_file=opp_file, config=config)
    session.add(matrix)
    session.flush()
    return matrix


# ── Expectations ──────────────────────────────────────────────────────


def get_current_expectation(session, test_id):
    """Return the most recent `ExpectedTestResult` row for `test_id`, or None.

    "Most recent" means the row with the highest `set_at`. A retraction
    row (where `expected_result_code IS NULL`) is treated like any other
    row — callers that need to distinguish "no row" from "retracted" must
    look at the returned object's `expected_result_code`.
    """
    return session.execute(
        select(ExpectedTestResult)
        .where(ExpectedTestResult.test_id == test_id)
        .order_by(ExpectedTestResult.set_at.desc(),
                  ExpectedTestResult.id.desc())
        .limit(1)
    ).scalar_one_or_none()


def insert_expectation(session, *, test_id, expected_result_code,
                       expected_result_description=None, reason=None,
                       set_by=None, set_at=None):
    """Append a new ExpectedTestResult row. `expected_result_code` may be
    None — that records an explicit retraction, distinguishable from
    never-set. Caller commits."""
    row = ExpectedTestResult(
        test_id=test_id,
        expected_result_code=expected_result_code,
        expected_result_description=expected_result_description,
        reason=reason,
        set_by=set_by,
        set_at=set_at or datetime.datetime.utcnow(),
    )
    session.add(row)
    session.flush()
    return row


# ── Verdicts ──────────────────────────────────────────────────────────


def compute_verdict_kind(actual_code, expectation):
    """Three-state verdict for one cell.

    `actual_code` is the TestRun.result_code value (an enum or None);
    `expectation` is an ExpectedTestResult row or None.

    Returns:
      None       — actual is not yet known (TestRun still queued/running)
      UNKNOWN    — actual known, but no expectation existed (or it was
                   explicitly retracted)
      EXPECTED   — actual matched the expectation
      UNEXPECTED — actual diverged from the expectation
    """
    if actual_code is None:
        return None
    if expectation is None or expectation.expected_result_code is None:
        return TestVerdictKind.UNKNOWN
    if actual_code == expectation.expected_result_code:
        return TestVerdictKind.EXPECTED
    return TestVerdictKind.UNEXPECTED


def create_test_verdict(session, *, matrix_run_id, test_id, test_run_id,
                        expectation=None, verdict=None,
                        recorded_at=None, cache_hit=False):
    """Insert a TestVerdict row. When `verdict` is None the cell is
    pending; otherwise `recorded_at` must be set (the caller's
    `datetime.utcnow()` for cache hits, the run's `finished_at` for
    miss-then-execute). Caller commits."""
    row = TestVerdict(
        matrix_run_id=matrix_run_id,
        test_id=test_id,
        test_run_id=test_run_id,
        expectation_id=expectation.id if expectation else None,
        verdict=verdict,
        recorded_at=recorded_at,
        cache_hit=cache_hit,
    )
    session.add(row)
    session.flush()
    return row


# ── Rollup ────────────────────────────────────────────────────────────


_RESULT_RANK = {
    TestResultCode.PASS: 0,
    TestResultCode.SKIPPED: 0,
    TestResultCode.FAIL: 1,
    TestResultCode.ERROR: 2,
}


def _worst_result(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return b if _RESULT_RANK.get(b, 0) > _RESULT_RANK.get(a, 0) else a


def recompute_matrix_run_rollup(session, matrix_run_id):
    """Recompute the counter / verdict / actual_summary / completed_at
    columns on a TestMatrixRun from its child TestVerdict + TestRun rows.

    Called whenever a cell promotes (verdict written) or a TestRun
    transitions lifecycle. The rollup is stored so the UI and API never
    have to fan out across cells.
    """
    matrix_run = session.get(TestMatrixRun, matrix_run_id)
    if matrix_run is None:
        return

    rows = session.execute(
        select(TestVerdict, TestRun)
        .join(TestRun, TestVerdict.test_run_id == TestRun.id)
        .where(TestVerdict.matrix_run_id == matrix_run_id)
    ).all()

    pass_n = fail_n = error_n = 0
    exp_n = unexp_n = unk_n = 0
    cache_hits = 0
    total = 0
    actual_summary = None
    all_finished = True
    latest_finished = None

    for verdict, test_run in rows:
        total += 1
        if verdict.cache_hit:
            cache_hits += 1
        rc = test_run.result_code
        if rc == TestResultCode.PASS:
            pass_n += 1
            actual_summary = _worst_result(actual_summary, rc)
        elif rc == TestResultCode.FAIL:
            fail_n += 1
            actual_summary = _worst_result(actual_summary, rc)
        elif rc == TestResultCode.ERROR:
            error_n += 1
            actual_summary = _worst_result(actual_summary, rc)

        if verdict.verdict == TestVerdictKind.EXPECTED:
            exp_n += 1
        elif verdict.verdict == TestVerdictKind.UNEXPECTED:
            unexp_n += 1
        elif verdict.verdict == TestVerdictKind.UNKNOWN:
            unk_n += 1

        if test_run.lifecycle not in (TestRunLifecycle.finished,
                                      TestRunLifecycle.cancelled,
                                      TestRunLifecycle.timed_out):
            all_finished = False
        if test_run.finished_at is not None:
            if latest_finished is None or test_run.finished_at > latest_finished:
                latest_finished = test_run.finished_at

    matrix_run.pass_count = pass_n
    matrix_run.fail_count = fail_n
    matrix_run.error_count = error_n
    matrix_run.expected_count = exp_n
    matrix_run.unexpected_count = unexp_n
    matrix_run.unknown_count = unk_n
    matrix_run.cache_hit_count = cache_hits
    matrix_run.total_count = total
    matrix_run.actual_summary = actual_summary

    if unexp_n > 0:
        matrix_run.verdict = TestVerdictKind.UNEXPECTED
    elif unk_n > 0:
        matrix_run.verdict = TestVerdictKind.UNKNOWN
    elif exp_n > 0 and (exp_n + unexp_n + unk_n) == total:
        matrix_run.verdict = TestVerdictKind.EXPECTED
    else:
        matrix_run.verdict = None

    matrix_run.completed_at = latest_finished if (total > 0 and all_finished) else None


def finalize_verdict_for_run(session, run_id):
    """Promote any pending TestVerdict rows attached to `run_id` and
    refresh the parent TestMatrixRun rollup.

    Called from the worker result handler and from any local executor
    once the TestRun outcome is known. Idempotent — if the verdict has
    already been written it is left alone.
    """
    run = session.get(TestRun, run_id)
    if run is None:
        return
    if run.lifecycle != TestRunLifecycle.finished or run.result_code is None:
        return

    pending = session.execute(
        select(TestVerdict).where(
            TestVerdict.test_run_id == run_id,
            TestVerdict.verdict.is_(None),
        )
    ).scalars().all()

    affected_matrix_runs = set()
    for verdict in pending:
        expectation = get_current_expectation(session, verdict.test_id)
        verdict.expectation_id = expectation.id if expectation else None
        verdict.verdict = compute_verdict_kind(run.result_code, expectation)
        verdict.recorded_at = run.finished_at or datetime.datetime.utcnow()
        affected_matrix_runs.add(verdict.matrix_run_id)

    # Even cells whose verdict was pre-populated (cache hits) belong to a
    # matrix run that may still need rollup recomputation if other cells
    # changed lifecycle; pick up every matrix run this TestRun touches.
    for vid, mid in session.execute(
        select(TestVerdict.id, TestVerdict.matrix_run_id)
        .where(TestVerdict.test_run_id == run_id)
    ).all():
        affected_matrix_runs.add(mid)

    for mid in affected_matrix_runs:
        if mid is not None:
            recompute_matrix_run_rollup(session, mid)


# ── Deletion ──────────────────────────────────────────────────────────


class CannotDeleteRunningRun(Exception):
    """Raised when a delete would remove a TestRun that is still running.

    Running runs are never deleted — a worker may still be writing to the
    row. Callers surface this as a 409 (REST) / error flash (web) / message
    (CLI) and the caller leaves the run alone.
    """


def delete_test_run(session, run_id):
    """Delete a single TestRun, its TestVerdict cells, and refresh rollups.

    A TestRun can be referenced by TestVerdict cells in more than one
    TestMatrixRun (cache hits reuse a prior run), so every referencing cell
    is removed and each affected matrix run's rollup is recomputed.

    Returns the deleted `run_id`, or None if no such run exists. Raises
    `CannotDeleteRunningRun` if the run is currently executing. Caller
    commits.
    """
    run = session.get(TestRun, run_id)
    if run is None:
        return None
    if run.lifecycle == TestRunLifecycle.running:
        raise CannotDeleteRunningRun(
            f"Run #{run_id} is still running; let it finish first."
        )

    verdicts = session.execute(
        select(TestVerdict).where(TestVerdict.test_run_id == run_id)
    ).scalars().all()
    affected_matrix_runs = {v.matrix_run_id for v in verdicts}
    for verdict in verdicts:
        session.delete(verdict)
    session.delete(run)
    session.flush()

    for mid in affected_matrix_runs:
        if mid is not None:
            recompute_matrix_run_rollup(session, mid)
    return run_id


def delete_matrix_run(session, matrix_run_id):
    """Delete a TestMatrixRun and cascade to its own TestRuns and cells.

    The matrix run's TestVerdict cells are removed, then each child TestRun
    (``matrix_run_id == matrix_run_id``) is either deleted or — if it is
    still referenced by another matrix run's cache-hit cell — detached
    (``matrix_run_id`` set NULL) so that other run keeps its referent.

    Returns ``{"deleted_runs": int, "detached_runs": int}``, or None if no
    such matrix run exists. Raises `CannotDeleteRunningRun` if any child
    run is currently executing (the whole delete is refused — a matrix run
    with a running child cannot be partially removed). Caller commits.
    """
    matrix_run = session.get(TestMatrixRun, matrix_run_id)
    if matrix_run is None:
        return None

    child_runs = session.execute(
        select(TestRun).where(TestRun.matrix_run_id == matrix_run_id)
    ).scalars().all()
    running = sum(1 for r in child_runs if r.lifecycle == TestRunLifecycle.running)
    if running:
        raise CannotDeleteRunningRun(
            f"Matrix run #{matrix_run_id} has {running} running child "
            f"run(s); let them finish first."
        )

    # Drop this matrix run's own cells first, so a child run that is
    # exclusive to this matrix run becomes unreferenced (and deletable);
    # one still referenced afterwards is shared via another run's cache hit.
    own_verdicts = session.execute(
        select(TestVerdict).where(TestVerdict.matrix_run_id == matrix_run_id)
    ).scalars().all()
    for verdict in own_verdicts:
        session.delete(verdict)
    session.flush()

    deleted = detached = 0
    for run in child_runs:
        still_referenced = session.execute(
            select(TestVerdict.id)
            .where(TestVerdict.test_run_id == run.id)
            .limit(1)
        ).scalar_one_or_none()
        if still_referenced is not None:
            run.matrix_run_id = None
            detached += 1
        else:
            session.delete(run)
            deleted += 1

    session.delete(matrix_run)
    session.flush()
    return {"deleted_runs": deleted, "detached_runs": detached}


# ── Enqueue ───────────────────────────────────────────────────────────


def enqueue_job(session, job, *, project, opp_file=None, matrix_run_id=None,
                use_cache=False, cache_fingerprint=None):
    """End-to-end: turn one expand_matrix job dict into a queued TestRun
    plus (when `matrix_run_id` is given) a corresponding TestVerdict cell.

    Cache strategy:
      * `use_cache=True` and a `cache_fingerprint` is given → look up the
        most recent finished TestRun with the same fingerprint. On hit,
        no new TestRun is created; the cell points at the existing
        TestRun and the verdict is computed immediately.
      * Miss or `use_cache=False` → a fresh TestRun is queued. The
        verdict cell is created with `verdict=None`; it promotes when
        the run finishes.

    Returns a (TestRun, TestVerdict|None) tuple. The TestVerdict is None
    only when no `matrix_run_id` was given.
    """
    coord = job_to_coord(job, project=project, opp_file=opp_file)
    test = get_or_create_test(session, coord)

    cached_run = None
    if use_cache and cache_fingerprint:
        cached_run = session.execute(
            select(TestRun)
            .where(
                TestRun.cache_fingerprint == cache_fingerprint,
                TestRun.lifecycle == TestRunLifecycle.finished,
                TestRun.result_code.isnot(None),
            )
            .order_by(TestRun.finished_at.desc(), TestRun.id.desc())
            .limit(1)
        ).scalar_one_or_none()

    if cached_run is not None:
        run = cached_run
        if matrix_run_id is not None:
            expectation = get_current_expectation(session, test.id)
            verdict = compute_verdict_kind(run.result_code, expectation)
            tv = create_test_verdict(
                session,
                matrix_run_id=matrix_run_id,
                test_id=test.id,
                test_run_id=run.id,
                expectation=expectation,
                verdict=verdict,
                recorded_at=datetime.datetime.utcnow(),
                cache_hit=True,
            )
            recompute_matrix_run_rollup(session, matrix_run_id)
            return run, tv
        return run, None

    run = create_test_run(
        session,
        test_id=test.id,
        matrix_run_id=matrix_run_id,
        commit_sha=None,
        git_ref=job.get("git_ref"),
        version=job.get("version"),
        resolved_deps=job.get("resolved_deps"),
        cache_fingerprint=cache_fingerprint,
    )
    tv = None
    if matrix_run_id is not None:
        tv = create_test_verdict(
            session,
            matrix_run_id=matrix_run_id,
            test_id=test.id,
            test_run_id=run.id,
            expectation=None,
            verdict=None,
            recorded_at=None,
            cache_hit=False,
        )
        recompute_matrix_run_rollup(session, matrix_run_id)
    return run, tv


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
