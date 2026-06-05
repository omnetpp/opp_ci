# Plan: test data model — future phases

Phase 1 of the redesign (the Test / TestMatrix / TestRun / TestMatrixRun
schema, the `kind` rename, the persistence helpers, and the cutover
across REST / web / worker / CLI) is shipped. The full record of what
was decided and implemented in phase 1 is in
[plan/done/test-data-model-phase-1-schema.md](../done/test-data-model-phase-1-schema.md).
The schema itself lives in
[`opp_ci/db/models.py`](../opp_ci/db/models.py); persistence helpers in
[`opp_ci/persistence.py`](../opp_ci/persistence.py).

This file tracks the remaining future-phase work. It's neither a design
doc nor a backlog — just the questions and small features deferred from
phase 1.

## Open questions

1. **Promote `TestMatrixRun.lifecycle` to a real column?** Phase 1 rolls
   up child `TestRun.lifecycle` in app code. Worth promoting once we see
   real query patterns where the roll-up is the bottleneck (dashboards
   over many matrix runs, "list active matrix runs" filtered queries).
   If promoted: who keeps it in sync — a trigger on `TestRun`, a
   write-time recompute in the persistence helpers, or a periodic
   reconciler?

2. **Filtered matrix rerun.** Phase 1 only supports "rerun every child"
   when a `TestMatrixRun` is re-submitted. Add "rerun only the
   failed/errored children" as a UI/CLI option? If so, does the new
   `TestMatrixRun` re-expand the (possibly evolved) `TestMatrix` and
   pick a subset, or copy the surviving subset of `Test`s from the old
   `TestMatrixRun` directly? Both produce different semantics when the
   matrix has been edited between attempts.

3. **Audit / history for mutable columns on `Test`.** `Test` has three
   mutables (`name`, `expected_result_code`,
   `expected_result_description`). A silent overwrite of
   `expected_result_code` changes how every past `TestRun` grades, and a
   silent rename loses context. Do we want a single audit mechanism
   covering all three (audit table, trigger, or `updated_at`/`updated_by`
   columns), or nothing at all? Phase 1 has nothing.

4. **`TestRun.details` JSON schema.** Phase 1 stores it as a free-form
   blob. If a particular field becomes a common query target (e.g.
   per-subtest breakdown for a comparison view, fingerprint mismatch
   data for triage), promote it to its own column or codify a JSON
   schema. Risk of becoming a junk drawer otherwise.

5. **`system_snapshot` retention policy.** Pruning is `UPDATE … SET
   system_snapshot = NULL WHERE …` — operationally cheap, semantically
   safe (lifecycle row + outcome stay). The policy is open: drop after
   N months, drop above a size threshold, drop only for
   `result_code=PASS` runs, never drop? Likely a deployment-config
   question rather than a code one.

6. **Cancel / abort for running `TestRun`s.** Phase 1 lets running runs
   finish — cancel only transitions queued rows. If we ever want a real
   abort (worker-side signal, polling flag), it lands here. Out of
   scope until there's a concrete need.

7. **Suite-internal granularity.** Deferred: a `Test` represents a
   *full suite* at coordinates, and the suite's internal per-test
   results collapse into one aggregate outcome on `TestRun`. When we
   eventually want per-individual-test results, the path is to add a
   per-individual-test entity below `Test` and split the outcome
   accordingly (re-introducing a child outcome table is one option).
   The names should not block this future split.
