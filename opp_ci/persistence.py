"""Helpers for writing the new test data model.

Centralises the get-or-create Test + create TestRun + create TestVerdict
pattern so every call site (web routes, REST API, CLI, github webhook)
stays consistent, and keeps verdict computation + matrix-run rollup in a
single place.
"""

import datetime
import logging
import platform as _platform

from sqlalchemy import func, or_, select, update

from opp_ci.db.models import (
    TEST_COORD_FIELDS,
    AppSetting,
    ExpectedTestResult,
    Test,
    TestMatrix,
    TestMatrixRun,
    TestResultCode,
    TestRun,
    TestRunLifecycle,
    TestVerdict,
    TestVerdictKind,
    Worker,
    compute_matrix_hash,
    compute_test_coord_hash,
)

_logger = logging.getLogger(__name__)

# Sentinel for "no per-call override given — use the global default". Distinct
# from None, which means "explicitly: create with no expectation".
USE_GLOBAL_DEFAULT = object()
_UNSET = USE_GLOBAL_DEFAULT  # short internal alias


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
    just a filter to the closed `TEST_COORD_FIELDS` set plus the
    `resolved_deps` mapping (also part of Test identity). `project` and
    `opp_file` can be supplied by the caller for sites where the job
    dict omits them.
    """
    coord = {field: job.get(field) for field in TEST_COORD_FIELDS}
    coord["resolved_deps"] = job.get("resolved_deps")
    if project is not None:
        coord["project"] = project
    if opp_file is not None and coord.get("opp_file") is None:
        coord["opp_file"] = opp_file
    return coord


# Human labels for the coord fields, matching the test-creation form's field
# labels (web/templates/test_new.html) so a validation message names fields the
# way the user sees them ("Compiler Version", not "compiler_version").
_COORD_FIELD_LABELS = {
    "project": "Project", "kind": "Kind", "mode": "Build Mode",
    "os": "OS", "os_version": "OS Version",
    "distro": "Distro", "distro_version": "Distro Version",
    "flavor": "Flavor", "flavor_version": "Flavor Version",
    "arch": "Architecture",
    "compiler": "Compiler", "compiler_version": "Compiler Version",
}


def validate_test_coord(coord):
    """Raise ValueError if *coord* under-specifies the execution environment.

    A Test identity (its ``coord_hash``) must pin every dimension along which
    two runs could otherwise diverge; otherwise dedup is meaningless — runs of
    "the same" Test would execute in different environments and their results
    could not be compared, and expectations/trends key on an ambiguous identity.
    Called at every submit entry point so an under-specified submission fails
    fast with a clear message instead of silently running wherever a worker
    happens to match (or timing out unserviceable).

    Rules (see plan/pending/strict-test-coordinate-specification.md):
      * project, kind                  — required (what is run)
      * arch, mode                     — required (change the binary/behaviour)
      * compiler, compiler_version     — both required (change the build); also
                                         for podman/nix, where they select the
                                         image / nix option
      * os                             — required
      * os = Linux   → distro + distro_version required; os_version must be
                       unset (Linux carries its version in the distro); flavor
                       optional, but if set needs a version (distro_version
                       suffices)
      * os = Windows/MacOS → os_version required; distro/flavor must be unset

    Isolation-independent: podman runs need full spec too (the coords select
    the container image).
    """
    missing = [f for f in ("project", "kind", "arch", "mode",
                           "compiler", "compiler_version", "os")
               if not coord.get(f)]

    os_folded = (coord.get("os") or "").strip().lower()
    if os_folded == "linux":
        missing += [f for f in ("distro", "distro_version") if not coord.get(f)]
        if coord.get("os_version"):
            raise ValueError(
                "Test coordinate over-specifies os_version for Linux: Linux "
                "carries its version in the distro — pin distro_version (and "
                "flavor_version for a flavor) instead, and leave os_version unset."
            )
        if coord.get("flavor") and not (
                coord.get("flavor_version") or coord.get("distro_version")):
            missing.append("flavor_version")
    elif os_folded in ("windows", "macos"):
        if not coord.get("os_version"):
            missing.append("os_version")
        if coord.get("distro") or coord.get("flavor"):
            raise ValueError(
                "Test coordinate sets distro/flavor on a non-Linux os: "
                "distro and flavor are only valid when os=Linux."
            )

    if missing:
        labels = sorted({_COORD_FIELD_LABELS.get(f, f) for f in missing})
        raise ValueError(
            "Test coordinate under-specifies the execution environment; a test "
            "must fully specify it so its runs share one identity and are "
            "comparable. Missing/empty: " + ", ".join(labels) + "."
        )


def resolve_and_validate_coord(coord, tags, *, source="the fleet",
                               remedy="connect a worker that advertises them"):
    """Best-effort resolve *coord*'s loose axes against *tags*, then validate it.

    Resolution fills what *tags* can supply (mutating `coord` in place); the
    strict `validate_test_coord` is the gate. The point of this wrapper is the
    error: when resolution was attempted but the source couldn't supply a loose
    axis, the failure is the *source's* (an empty/under-tagged fleet, or a host
    missing a compiler) — not the user under-specifying. So the message names
    the source as the cause instead of the misleading "you under-specified",
    while still listing the missing axes. `source`/`remedy` tailor the wording
    (fleet vs local host).
    """
    from opp_ci.fleet import resolve_loose_axes
    resolve_incomplete = False
    try:
        resolve_loose_axes(coord, tags)
    except ValueError:
        resolve_incomplete = True
    try:
        validate_test_coord(coord)
    except ValueError as e:
        if resolve_incomplete:
            raise ValueError(
                f"Couldn't resolve the unspecified coordinate against {source} — "
                f"nothing there advertises the missing axes. {e} "
                f"Either specify them explicitly, or {remedy}."
            ) from e
        raise


def get_or_create_test(session, coord, *, default_expectation=_UNSET,
                       expectation_set_by="system"):
    """Return the Test row matching `coord`, creating it if missing.

    `coord` is a dict over `TEST_COORD_FIELDS` plus `resolved_deps`
    (unknown keys ignored, missing keys treated as None). Caller is
    responsible for commit/flush.

    On *creation only*, an initial `ExpectedTestResult` is stamped so the
    Test's first run already yields a meaningful verdict instead of
    UNKNOWN. `default_expectation` controls the stamped code:
      * `_UNSET` (default) — use the global default from app settings.
      * a `TestResultCode` — stamp that code (per-submission override).
      * `None` — stamp nothing (create with no expectation; first run is
        UNKNOWN, the pre-feature behaviour).
    An existing Test is returned untouched — never re-stamped.
    `expectation_set_by` attributes the auto-row (submitting user when
    known, else "system").
    """
    h = compute_test_coord_hash(coord)
    existing = session.execute(
        select(Test).where(Test.coord_hash == h)
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    fields = {field: coord.get(field) for field in TEST_COORD_FIELDS}
    test = Test(coord_hash=h, resolved_deps=coord.get("resolved_deps"), **fields)
    session.add(test)
    session.flush()

    code = (read_default_expectation_code(session)
            if default_expectation is _UNSET else default_expectation)
    if code is not None:
        insert_expectation(
            session, test_id=test.id,
            expected_result_code=code,
            reason="default expectation on creation",
            set_by=expectation_set_by,
        )
    return test


def test_coord_is_recipe(coord):
    """True if a Test coordinate is a *recipe* — underspecified along a
    fleet-resolvable axis (no compiler, no arch, or no platform), so it must be
    resolved (pinned against the fleet/host) before it can run. Mirrors
    `scheduler.matrix_is_recipe` for a single Test coordinate.
    """
    coord = coord or {}
    has_platform = coord.get("os") or coord.get("distro") or coord.get("flavor")
    return not (coord.get("compiler") and coord.get("arch") and has_platform)


def get_or_create_test_recipe(session, coord):
    """Return the unresolved Test (recipe) matching the loose `coord`, creating
    it if missing.

    A recipe is intentionally under-specified, so this *skips*
    `validate_test_coord` (which a recipe would fail) and creates the row with
    `is_resolved=False`. Its `coord_hash` keys on the loose coordinate, so two
    distinct loose specs are distinct recipes and re-submitting one dedups.
    No default expectation is stamped — a recipe never runs; its resolved
    snapshots carry expectations. Caller commits/flushes.
    """
    h = compute_test_coord_hash(coord)
    existing = session.execute(
        select(Test).where(Test.coord_hash == h)
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    fields = {field: coord.get(field) for field in TEST_COORD_FIELDS}
    recipe = Test(coord_hash=h, resolved_deps=coord.get("resolved_deps"),
                  is_resolved=False, **fields)
    session.add(recipe)
    session.flush()
    return recipe


def resolve_test_recipe(session, recipe, tags, *, source_commit=None,
                        default_expectation=_UNSET, expectation_set_by="system"):
    """Resolve a recipe Test into a pinned resolved Test, returning it.

    Pins the recipe's loose coordinate axes (compiler/arch/platform/mode)
    against `tags` (fleet or local-host capability tags) and, when given, the
    source commit; then `get_or_create_test` mints the resolved Test
    (`is_resolved=True`) and `resolved_from` is linked back to the recipe.

    Unlike a matrix snapshot (always a new row), a resolved Test is
    content-addressed: re-resolving to the *same* coordinate reuses the existing
    Test, while a different result (fleet/source changed) yields a new one — so a
    recipe's `resolved_instances` is the set of distinct pinned Tests it has
    produced over time. Raises ValueError if already resolved or the fleet/host
    can't satisfy a loose axis (reject-incomplete).
    """
    if recipe.is_resolved:
        raise ValueError("Test is already resolved.")
    from opp_ci.fleet import resolve_loose_axes
    coord = {field: getattr(recipe, field) for field in TEST_COORD_FIELDS}
    coord["resolved_deps"] = recipe.resolved_deps
    resolve_loose_axes(coord, tags)   # strict: explicit resolve must complete
    if source_commit:
        coord["commit_sha"] = source_commit
    validate_test_coord(coord)
    resolved = get_or_create_test(
        session, coord, default_expectation=default_expectation,
        expectation_set_by=expectation_set_by)
    if resolved.id != recipe.id and resolved.resolved_from is None:
        resolved.resolved_from = recipe.id
    session.flush()
    return resolved


def create_matrix_run(session, *, matrix_id, trigger="manual", ref=None,
                      github_owner=None, github_repo=None,
                      github_commit_sha=None, github_pr_number=None,
                      github_status_url=None):
    """Create a TestMatrixRun row for one matrix submission.

    Refuses an unresolved (recipe) matrix: only a resolved matrix may run
    (resolve + expand it first). See the resolve-in-place invariants.
    """
    matrix = session.get(TestMatrix, matrix_id)
    if matrix is not None and not matrix.is_resolved:
        raise ValueError(
            "Cannot run an unresolved TestMatrix (a recipe); resolve and "
            "expand it into pinned Tests first.")
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
    """Create a queued TestRun targeting `test_id`.

    Refuses an unresolved (recipe) Test: a recipe carries loose/moving inputs
    and is inert until resolve() mints a pinned Test from it (the "can't run a
    recipe" invariant).
    """
    test = session.get(Test, test_id)
    if test is not None and not test.is_resolved:
        raise ValueError(
            "Cannot run an unresolved Test (a recipe); resolve it to a pinned "
            "Test first.")
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


def create_matrix_from_axes(session, *, project, config, name=None, opp_file=None,
                            is_resolved=True):
    """Create a TestMatrix row from a config dict.

    `name` may be None (anonymous). Shared by `matrix_create`, the web
    anonymous-run handler, and the CLI so the row is built one way.
    `is_resolved=False` marks it a recipe (the web form passes this for an
    underspecified matrix; see `scheduler.matrix_is_recipe`); the default keeps
    every other caller's matrix runnable. Raises ValueError on a name collision.
    """
    cleaned = name.strip() if name else None
    if cleaned and get_matrix_by_name(session, cleaned) is not None:
        raise ValueError(f"A matrix named {cleaned!r} already exists.")
    matrix = TestMatrix(name=cleaned, project=project, opp_file=opp_file,
                        config=config, is_resolved=is_resolved,
                        matrix_hash=compute_matrix_hash(project, opp_file, config))
    session.add(matrix)
    session.flush()
    return matrix


def resolve_matrix_recipe(session, recipe, *, commit_sha=None):
    """Resolve a recipe matrix into a new pinned snapshot matrix.

    Pins the recipe's loose coordinate axes (compiler/arch) against the fleet
    and returns a new ``TestMatrix`` (``is_resolved=True``,
    ``resolved_from=recipe.id``) carrying the pinned config. The recipe is
    preserved, so re-resolving later mints another snapshot — that lineage is
    the moving-target history.

    When ``commit_sha`` is given (branch-tracking: a push event auto-resolving
    the recipe), the source is also pinned — the snapshot's ``refs`` axis is set
    to that one commit, so it tests exactly what was pushed. Without it (a
    manual UI resolve) the recipe's refs are left as authored.

    Raises ValueError if the matrix is already resolved or the fleet can't
    satisfy a loose axis (reject-incomplete).
    """
    if recipe.is_resolved:
        raise ValueError("Matrix is already resolved.")
    from opp_ci.fleet import fleet_tags, resolve_loose_matrix_axes
    from opp_ci.scheduler import pin_matrix_refs
    resolved_config = resolve_loose_matrix_axes(recipe.config or {},
                                                fleet_tags(session))
    if commit_sha:
        # Branch-tracking (a push): pin the source to the pushed commit.
        resolved_config["refs"] = [commit_sha]
        resolved_config.pop("ref_range", None)
    else:
        # Manual resolve: pin any moving branch/tag/range to concrete SHAs, so
        # the snapshot is pinned all the way down on its source too.
        resolved_config = pin_matrix_refs(recipe.project, resolved_config)
    # Content-addressed: reuse an existing resolved snapshot with the same
    # pinned content rather than minting a duplicate (mirrors get_or_create_test).
    h = compute_matrix_hash(recipe.project, recipe.opp_file, resolved_config)
    existing = session.execute(
        select(TestMatrix).where(TestMatrix.matrix_hash == h,
                                 TestMatrix.is_resolved.is_(True))
    ).scalar_one_or_none()
    if existing is not None:
        if existing.resolved_from is None and existing.id != recipe.id:
            existing.resolved_from = recipe.id
        return existing
    snapshot = TestMatrix(
        name=None, project=recipe.project, opp_file=recipe.opp_file,
        config=resolved_config, is_resolved=True, resolved_from=recipe.id,
        matrix_hash=h)
    session.add(snapshot)
    session.flush()
    return snapshot


# ── Expectations ──────────────────────────────────────────────────────

# Global default expected result, stamped on newly-created Tests. Stored in
# the AppSetting key/value table; factory value is PASS. An empty stored value
# means "no default" (new Tests start with no expectation → UNKNOWN verdict).
DEFAULT_EXPECTATION_KEY = "default_expected_result"
FACTORY_DEFAULT_EXPECTATION = TestResultCode.PASS


def read_default_expectation_code(session):
    """Return the configured default `TestResultCode`, or the factory
    default (PASS) when unset. Returns None only when the setting has been
    explicitly cleared (admin chose "no default")."""
    row = session.get(AppSetting, DEFAULT_EXPECTATION_KEY)
    if row is None:
        return FACTORY_DEFAULT_EXPECTATION
    if not row.value:
        return None
    return TestResultCode(row.value)


def set_default_expectation_code(session, code, *, set_by=None):
    """Set the global default expected result. `code` is a `TestResultCode`,
    or None for "no default". Caller commits."""
    row = session.get(AppSetting, DEFAULT_EXPECTATION_KEY)
    if row is None:
        row = AppSetting(key=DEFAULT_EXPECTATION_KEY)
        session.add(row)
    row.value = code.value if code is not None else ""
    row.updated_by = set_by
    session.flush()
    return row


def parse_expectation_override(raw):
    """Map a submission-form/CLI value to a `get_or_create_test`
    `default_expectation` argument.

    `""`/`None` → `_UNSET` (fall back to the global default); a code string
    ("PASS"/"FAIL"/"ERROR"/"SKIPPED") → the matching `TestResultCode`.
    Raises `ValueError` on an unknown code string.
    """
    if not raw:
        return _UNSET
    return TestResultCode(raw)


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
    # `timed_out` is a terminal outcome too (a run retired after exhausting
    # its reclaim budget — see retire_poison_run), and it carries a
    # synthetic result_code, so it must promote its verdict just like a
    # `finished` run does.
    if run.lifecycle not in (TestRunLifecycle.finished,
                             TestRunLifecycle.timed_out) or run.result_code is None:
        return

    # Every finished run carries its own verdict. Matrix runs already have
    # their cell(s) (created in enqueue_job); a standalone run has none, so
    # give it a bare cell (matrix_run_id=NULL) that the loop below promotes.
    has_verdict = session.execute(
        select(TestVerdict.id).where(TestVerdict.test_run_id == run_id).limit(1)
    ).scalar_one_or_none()
    if has_verdict is None:
        create_test_verdict(
            session,
            matrix_run_id=None,
            test_id=run.test_id,
            test_run_id=run_id,
            expectation=None,
            verdict=None,
            recorded_at=None,
            cache_hit=False,
        )

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


# ── Orphan recovery ───────────────────────────────────────────────────


def retire_poison_run(session, run, now):
    """Terminally fail a run that has exhausted its reclaim budget.

    Marks it `timed_out` with a synthetic ERROR outcome and resolves its
    matrix cell so the parent TestMatrixRun can complete instead of being
    wedged open forever by a run that keeps killing its worker. Caller
    owns the transaction (does NOT commit).
    """
    run.lifecycle = TestRunLifecycle.timed_out
    run.worker_id = None
    run.finished_at = now            # started_at left as-is for forensics
    run.result_code = TestResultCode.ERROR
    run.stderr = (run.stderr or "") + (
        f"\n[opp_ci] retired after {run.reclaim_count} reclaim(s): the run "
        f"repeatedly outlived its worker (suspected crash/OOM loop)."
    )
    run.details = {**(run.details or {}),
                   "reclaim_exhausted": True,
                   "reclaim_count": run.reclaim_count}
    finalize_verdict_for_run(session, run.id)


def reclaim_orphaned_runs(session, worker_id, now, max_reclaims):
    """Reclaim every TestRun left `running` on `worker_id`.

    Each orphan is either re-queued for another attempt or, once it has
    burned through `max_reclaims` attempts, retired to a terminal
    `timed_out` state (see retire_poison_run). Returns
    ``(requeued, retired)``. Caller owns the transaction (does NOT commit).
    """
    orphans = session.execute(
        select(TestRun).where(
            TestRun.worker_id == worker_id,
            TestRun.lifecycle == TestRunLifecycle.running,
        )
    ).scalars().all()
    requeued = retired = 0
    for run in orphans:
        run.reclaim_count = (run.reclaim_count or 0) + 1
        if run.reclaim_count > max_reclaims:
            retire_poison_run(session, run, now)
            retired += 1
        else:
            run.lifecycle = TestRunLifecycle.queued
            run.worker_id = None
            run.started_at = None
            # running->queued keeps the matrix cell non-finished, so the
            # parent rollup's completed_at stays NULL either way — no
            # rollup recompute needed for the re-queue path.
            requeued += 1
    return requeued, retired


def _platform_required_tag(test):
    """Return the most-specific platform capability tag a worker must
    advertise to claim a TestRun targeting *test*, or None when the test
    doesn't pin a platform.

    Rules:
      - test names a flavor   →  flavor:<flavor>-<flavor_version-or-distro_version>
      - test names a distro   →  distro:<distro>-<distro_version>
      - test names Windows/MacOS with a version → os:<os>-<ver>
      - test names just an OS family → os:<os>
    """
    if test.flavor:
        ver = test.flavor_version or test.distro_version
        return f"flavor:{test.flavor.lower()}-{ver}" if ver else f"flavor:{test.flavor.lower()}"
    if test.distro:
        return (f"distro:{test.distro.lower()}-{test.distro_version}"
                if test.distro_version else f"distro:{test.distro.lower()}")
    if test.os:
        os_lower = test.os.lower()
        if os_lower != "linux" and test.os_version:
            return f"os:{os_lower}-{test.os_version}"
        return f"os:{os_lower}"
    return None


def required_tags_for_test(test):
    """Return the set of capability tags a worker must advertise to claim a
    TestRun targeting *test*.

    Required tags by execution environment:
      - isolation=podman             →  {"podman"}
      - isolation=none, toolchain=nix → {"nix", "<platform>", "compiler:<c>-<cv>"}
      - isolation=none, toolchain=none → {"<platform>", "compiler:<c>-<cv>"}
    `arch:<arch>` is added whenever the test pins an arch. A worker may run
    the test iff this set is a subset of its tags (see web.api._worker_can_run).
    """
    isolation = test.isolation or "none"
    toolchain = test.toolchain or "none"
    required = set()
    if isolation == "podman":
        required.add("podman")
    else:
        if toolchain == "nix":
            required.add("nix")
        platform_tag = _platform_required_tag(test)
        if platform_tag:
            required.add(platform_tag)
        if test.compiler and test.compiler_version:
            required.add(f"compiler:{test.compiler.lower()}-{test.compiler_version}")
    if test.arch:
        required.add(f"arch:{test.arch.lower()}")
    return required


def retire_unserviceable_run(session, run, now, required):
    """Terminally fail a queued run that no enabled worker's tags can satisfy.

    Mirrors retire_poison_run: marks it `timed_out`/ERROR and resolves its
    matrix cell so the parent TestMatrixRun completes instead of hanging on
    a run that would never be claimed. `required` is the unsatisfiable tag
    set (for the operator-facing message). Caller owns the transaction.
    """
    missing = ", ".join(sorted(required)) or "(none)"
    run.lifecycle = TestRunLifecycle.timed_out
    run.worker_id = None
    run.finished_at = now
    run.result_code = TestResultCode.ERROR
    run.stderr = (run.stderr or "") + (
        f"\n[opp_ci] expired from queue: no enabled worker advertises the "
        f"required tags {{{missing}}}."
    )
    run.details = {**(run.details or {}),
                   "unserviceable": True,
                   "required_tags": sorted(required)}
    finalize_verdict_for_run(session, run.id)


def expire_unserviceable_queued_runs(session, now, workers, timeout_seconds):
    """Retire `queued` runs that no enabled worker can ever claim.

    A run is *unserviceable* when its required tag set is not a subset of
    any enabled worker's tags — a misrouted submission, not transient
    backlog. Only runs queued longer than `timeout_seconds` are touched, so
    a worker still coming up (registered, not yet heartbeating) has time to
    appear. `workers` is the full worker list; enabled workers of ANY status
    count toward serviceability, so a worker the heartbeat sweep just flipped
    `offline` (or one that is merely rebooting) does not falsely condemn the
    runs only it can serve. Returns the number of runs expired. A
    `timeout_seconds <= 0` disables the sweep. Caller owns the transaction.
    """
    if timeout_seconds <= 0:
        return 0
    enabled_tag_sets = [set(w.tags or []) for w in workers if w.enabled]
    threshold = now - datetime.timedelta(seconds=timeout_seconds)
    stale_queued = session.execute(
        select(TestRun).where(
            TestRun.lifecycle == TestRunLifecycle.queued,
            TestRun.created_at < threshold,
        )
    ).scalars().all()
    expired = 0
    for run in stale_queued:
        required = required_tags_for_test(run.test)
        if any(required.issubset(tags) for tags in enabled_tag_sets):
            continue  # serviceable — legitimate backlog, leave it queued
        retire_unserviceable_run(session, run, now, required)
        expired += 1
    return expired


def mark_stale_workers_offline(session, now, timeout_seconds, max_reclaims):
    """Flip online/busy workers whose last_heartbeat is older than the
    timeout to `offline`, reclaim their orphaned `running` runs, and zero
    their job count.

    A freshly-registered worker (status `offline`, last_heartbeat NULL) is
    not matched by the online/busy filter, so this never churns workers
    that have not connected yet. Returns a list of
    ``(worker_name, requeued, retired)`` for workers that were flipped.
    Caller owns the transaction (does NOT commit).
    """
    threshold = now - datetime.timedelta(seconds=timeout_seconds)
    stale = session.execute(
        select(Worker).where(
            Worker.status.in_(("online", "busy")),
            or_(Worker.last_heartbeat.is_(None),
                Worker.last_heartbeat < threshold),
        )
    ).scalars().all()
    results = []
    for w in stale:
        requeued, retired = reclaim_orphaned_runs(session, w.id, now, max_reclaims)
        w.status = "offline"
        w.current_job_count = 0
        results.append((w.name, requeued, retired))
    return results


# ── Worker administration ─────────────────────────────────────────────


def update_worker(session, worker_id, *, concurrency=None, tags=None,
                  enabled=None):
    """Patch a worker's concurrency, tags, and/or enabled flag (admin).

    Only the fields passed (non-None) are changed. Returns the Worker, or
    None if no worker has that id. Raises ValueError on an invalid value.
    Caller owns the transaction (does NOT commit).
    """
    worker = session.execute(
        select(Worker).where(Worker.id == worker_id)
    ).scalar_one_or_none()
    if worker is None:
        return None
    if concurrency is not None:
        if concurrency < 1:
            raise ValueError("concurrency must be >= 1")
        worker.concurrency = concurrency
    if tags is not None:
        worker.tags = list(tags)
    if enabled is not None:
        worker.enabled = bool(enabled)
    return worker


def delete_worker(session, worker_id, now, max_reclaims):
    """Hard-delete a worker, freeing the runs that still reference it.

    In-flight (`running`) runs are reclaimed first — re-queued for another
    worker, or retired if they've exhausted their reclaim budget (see
    reclaim_orphaned_runs). Any remaining references (finished/other runs)
    have their `worker_id` nulled so the FK doesn't block the delete on
    enforcing backends; historical worker attribution is dropped, which is
    acceptable for a hard delete. Returns ``(requeued, retired)``, or None if
    no worker has that id. Caller owns the transaction (does NOT commit).
    """
    worker = session.execute(
        select(Worker).where(Worker.id == worker_id)
    ).scalar_one_or_none()
    if worker is None:
        return None
    requeued, retired = reclaim_orphaned_runs(session, worker.id, now, max_reclaims)
    session.execute(
        update(TestRun).where(TestRun.worker_id == worker.id).values(worker_id=None)
    )
    session.delete(worker)
    return requeued, retired


# ── Enqueue ───────────────────────────────────────────────────────────


def enqueue_job(session, job, *, project, opp_file=None, matrix_run_id=None,
                use_cache=False, cache_fingerprint=None,
                default_expectation=_UNSET, expectation_set_by="system"):
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

    `default_expectation` / `expectation_set_by` are forwarded to
    `get_or_create_test` so a fresh Test is stamped with the global
    default (or a per-submission override) on creation.

    Returns a (TestRun, TestVerdict|None) tuple. The TestVerdict is None
    only when no `matrix_run_id` was given.
    """
    coord = job_to_coord(job, project=project, opp_file=opp_file)
    # Always persist the complete transitive lock (Phase 1): the dep versions
    # the matrix cell named are pins *into* the closure, not the whole lock.
    # opp_env builds the full closure, so a deeper version still keys identity.
    from opp_ci.dependency import complete_lock_for_submit
    lock = complete_lock_for_submit(
        project, version=job.get("version"),
        pins=job.get("resolved_deps") or None) or None
    coord["resolved_deps"] = lock
    validate_test_coord(coord)
    test = get_or_create_test(session, coord,
                              default_expectation=default_expectation,
                              expectation_set_by=expectation_set_by)

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
        commit_sha=coord.get("commit_sha"),
        git_ref=job.get("git_ref"),
        version=job.get("version"),
        resolved_deps=coord["resolved_deps"],
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
