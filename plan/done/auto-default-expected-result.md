# Auto-default expected test result on Test creation

## Problem

Today a Test is created with **no expectation row**. The first time it runs,
`compute_verdict_kind` sees "result known, no expectation" and returns the
**UNKNOWN** verdict ([persistence.py:249](../../opp_ci/persistence.py#L249)).
The user then has to open the Test, set an expectation (PASS/FAIL/ERROR), and
**re-run** the test just to get a meaningful verdict. That round-trip is the
pain this plan removes.

Goal: a newly created Test gets a default expected result **at creation time**,
so its very first run already produces an EXPECTED / UNEXPECTED verdict. The
factory default is **PASS**, and the default is changeable from the web UI.

## Background — the two enums (don't conflate them)

- `TestResultCode` = `PASS / FAIL / ERROR / SKIPPED`
  ([models.py:246](../../opp_ci/db/models.py#L246)). Used for both the *actual*
  run result and `ExpectedTestResult.expected_result_code`.
- `TestVerdictKind` = `EXPECTED / UNEXPECTED / UNKNOWN`
  ([models.py:261](../../opp_ci/db/models.py#L261)). The *derived* comparison of
  actual vs. expected.

There is **no UNKNOWN expected-result code** — UNKNOWN is only a *verdict*,
produced when a result exists but no expectation was declared (no row, or a
`expected_result_code IS NULL` retraction). The expected-result UI already
offers only PASS / FAIL / ERROR (+ "(retract)")
([test_detail.html:104](../../opp_ci/web/templates/test_detail.html#L104));
SKIPPED is in the enum/API but not offered as an expected value.

## Decisions (confirmed with user)

1. **Default scope:** one **global** app setting, factory value `PASS`,
   editable in the web UI. Applied when a Test is **newly created**. Backward
   compatibility / backfilling legacy tests is explicitly *not* a concern.
2. **Keep UNKNOWN verdict + NULL/retraction state.** No model changes there.
   The feature makes UNKNOWN *rare and meaningful*: it now only arises from
   pre-feature tests or a deliberate retraction ("I assert nothing here", e.g.
   known-flaky). Green-by-default means only deviations need triage.
3. **SKIPPED stays as-is:** valid actual result, accepted by the API, not
   offered as a choosable expected value in the UI.

## Design

### Where the default is stamped

`get_or_create_test(session, coord)`
([persistence.py:80](../../opp_ci/persistence.py#L80)) is the single chokepoint
— all four creation paths route through it (persistence enqueue, web app,
cli, api). Stamp the default **only in the `existing is None` branch** (true
creation), by appending one `ExpectedTestResult` row via the existing
`insert_expectation` helper ([persistence.py:227](../../opp_ci/persistence.py#L227)).

This guarantees the expectation exists before any verdict is computed —
both the cache-hit path in `enqueue_job`
([persistence.py:578](../../opp_ci/persistence.py#L578)) and the fresh-run
path in `finalize_verdict_for_run`
([persistence.py:389](../../opp_ci/persistence.py#L389)) call
`get_current_expectation`, which will now find the default row.

New signature:

```python
def get_or_create_test(session, coord, *, default_expectation=_UNSET,
                        expectation_set_by="system"):
    # ... existing lookup ...
    if existing is not None:
        return existing
    # ... create Test as today ...
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
```

- `default_expectation=_UNSET` (sentinel) → fall back to the global setting.
- Callers may pass `default_expectation=None` to suppress (create with no
  expectation) or a specific `TestResultCode` to override per submission.
- `expectation_set_by` attributes the auto-row to the **submitting user when
  known**, falling back to `"system"` for the scheduler/internal path. This
  matches the existing manual-expectation convention
  (`set_by=current_user.display_name` on the web side
  ([app.py:1709](../../opp_ci/web/app.py#L1709)), `identity.get("name")` on the
  API side), so an auto-stamped row reads the same as a hand-set one.

The auto-row is a normal append-only entry, fully audited, and gets pinned by
`TestVerdict.expectation_id` like any other.

### Global default storage

No settings table exists today. Add a minimal key/value app-settings store so
this and future toggles have a home.

New model `AppSetting` in [models.py](../../opp_ci/db/models.py):

```python
class AppSetting(Base):
    __tablename__ = "app_settings"
    key = Column(String, primary_key=True)
    value = Column(String, nullable=True)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow,
                        onupdate=datetime.datetime.utcnow)
    updated_by = Column(String, nullable=True)
```

Persistence helpers (in [persistence.py](../../opp_ci/persistence.py)):

```python
DEFAULT_EXPECTATION_KEY = "default_expected_result"
FACTORY_DEFAULT_EXPECTATION = TestResultCode.PASS

def read_default_expectation_code(session):
    """Return the configured default TestResultCode, or the factory default
    (PASS) when unset. Returns None only if explicitly set to '' (= 'no
    default; new tests start UNKNOWN')."""
    row = session.get(AppSetting, DEFAULT_EXPECTATION_KEY)
    if row is None:
        return FACTORY_DEFAULT_EXPECTATION
    if not row.value:
        return None            # admin chose "no default"
    return TestResultCode(row.value)

def set_default_expectation_code(session, code, *, set_by=None):
    """code: a TestResultCode, or None for 'no default'. Caller commits."""
    row = session.get(AppSetting, DEFAULT_EXPECTATION_KEY)
    if row is None:
        row = AppSetting(key=DEFAULT_EXPECTATION_KEY)
        session.add(row)
    row.value = code.value if code is not None else ""
    row.updated_by = set_by
```

Allowing "no default" (empty) preserves the option of the old behaviour
(new tests start UNKNOWN) without a code change.

### Per-submission override (CLI, API, **and web UI**)

Because the annoyance is about *declaring the expected result at creation*, the
submission paths accept an inline expected result that overrides the global
default for the tests they spawn. The override is the same value space the
global setting uses: a `TestResultCode` (PASS/FAIL/ERROR), or "(use default)"
which falls back to the global setting (the `_UNSET` sentinel in
`get_or_create_test`).

**Threading.** All paths converge on `get_or_create_test`, but the matrix path
goes through two intermediaries that must forward the value:

- `enqueue_job` ([persistence.py:542](../../opp_ci/persistence.py#L542)) — add
  `default_expectation=_UNSET` and forward to `get_or_create_test` at
  [persistence.py:560](../../opp_ci/persistence.py#L560).
- `_queue_matrix_run` ([app.py:1571](../../opp_ci/web/app.py#L1571)) — add
  `default_expectation=_UNSET`, forward into the `enqueue_job` loop. Shared by
  save-and-run, run-saved-matrix, and rerun, so all three honour it.

**CLI** ([cli.py:834](../../opp_ci/cli.py#L834)) — optional
`--expect PASS|FAIL|ERROR` flag.

**API**:
- `POST /api/runs` ([api.py:105](../../opp_ci/web/api.py#L105)) — optional
  `expected_result_code` in the request body.
- `POST /api/runs/matrix` ([api.py:198](../../opp_ci/web/api.py#L198)) and
  `POST /api/matrix-runs` ([api.py:1027](../../opp_ci/web/api.py#L1027)) — same,
  applied to every Test the matrix expands into.

**Web UI** (mirror the CLI — explicitly requested):
- **New Test form** — `GET/POST /tests/new`
  ([test_new_submit](../../opp_ci/web/app.py#L684), template
  `templates/test_new.html`). Add an "Expected result" dropdown next to the
  other fields: `(use default — PASS)` / `PASS` / `FAIL` / `ERROR`. Add an
  `expected_result_code: str = Form(default="")` param and pass it as
  `get_or_create_test(session, coord, default_expectation=...)` (empty →
  `_UNSET`).
- **Matrix run form** — `POST /test-matrices/{id}/run`
  ([matrix_run](../../opp_ci/web/app.py#L1468), forms in
  [matrix_detail.html:7](../../opp_ci/web/templates/matrix_detail.html#L7) and
  [:148](../../opp_ci/web/templates/matrix_detail.html#L148)). Add the same
  dropdown to the inline run form(s); the route reads the form field and passes
  it to `_queue_matrix_run(session, matrix, trigger="web", default_expectation=...)`.

Helper to parse the form value once (shared by the web routes):

```python
def parse_expectation_override(raw):
    """'' -> _UNSET (use global default); 'PASS'/'FAIL'/'ERROR' -> TestResultCode."""
    if not raw:
        return _UNSET
    return TestResultCode(raw)
```

Reruns operate on Tests that already exist, so `get_or_create_test` won't
re-stamp — rerun keeps the Test's current expectation, which is the right
behaviour (no override needed on the rerun forms).

The global-default + auto-stamp alone already removes the re-run round-trip; the
inline overrides let a submitter declare a non-PASS expectation up front (e.g.
a known-failing test) without a second trip to the Test detail page.

> Note on "TestMatrix": a `TestMatrix` row holds no expectations — expectations
> are per-Test, and a matrix's Tests are created lazily during expansion via
> `get_or_create_test`. So "default expected result for a matrix" is just the
> default applied to each Test it spawns; the single chokepoint covers it with
> no matrix-specific schema.

### Web UI for changing the default

There is no settings/admin page yet. Add an **admin-gated** settings page
(`admin` is the top role in `ROLE_HIERARCHY`,
[auth.py:26](../../opp_ci/auth.py#L26); the web dependency is
[`require_user`](../../opp_ci/auth.py#L157)):

- `GET /settings` — render current default (dropdown: PASS / FAIL / ERROR /
  "(no default — start UNKNOWN)"), gated `require_user("admin")`.
- `POST /settings` (CSRF-protected, `require_user("admin")`) — call
  `set_default_expectation_code`, redirect back with a flash message.
- New template `templates/settings.html`; add a "Settings" nav link visible to
  admins (follow the existing `current_user.role` pattern used in
  [test_detail.html:96](../../opp_ci/web/templates/test_detail.html#L96)).

## Implementation steps

1. **Model:** add `AppSetting` to [models.py](../../opp_ci/db/models.py). The
   project bootstraps schema via `Base.metadata.create_all(engine)`
   ([cli.py:604](../../opp_ci/cli.py#L604) and other entrypoints); Alembic is
   configured but `migrations/versions/` is empty and unused. So the new table
   is auto-created like every other model — **no migration file needed**.
2. **Persistence:** add `DEFAULT_EXPECTATION_KEY`,
   `read_default_expectation_code`, `set_default_expectation_code`; extend
   `get_or_create_test` with `default_expectation` / `expectation_set_by` and
   the auto-stamp on creation.
3. **Web settings page:** `GET`/`POST /settings`, template, admin nav link.
4. **Per-submission override:** thread `default_expectation` through
   `enqueue_job` and `_queue_matrix_run`; add the inline expected-result
   dropdown to the **new-Test form** and the **matrix-run form**, the CLI
   `--expect` flag, and the API request bodies. Add `parse_expectation_override`.
5. **Tests** (see below).
6. **Docs:** note the new default behaviour and settings page in the user-facing
   docs / README if present.

## Testing

- `get_or_create_test` stamps PASS by default on a brand-new Test; the row is a
  real `ExpectedTestResult` with `reason="default expectation on creation"`.
- Returning an **existing** Test does **not** add another expectation row.
- First run of a new Test yields an **EXPECTED** verdict when it passes and
  **UNEXPECTED** when it fails — *without* a manual set + re-run (this is the
  regression test for the actual pain point).
- `read_default_expectation_code`: unset → PASS; set to `FAIL` → FAIL; set to
  `""` → None, and then a new Test gets no expectation (UNKNOWN verdict, old
  behaviour preserved).
- Matrix expansion: every spawned Test gets the default; matrix rollup verdict
  reflects EXPECTED instead of UNKNOWN on first submission.
- `set_default_expectation_code` round-trips and records `updated_by`.
- Settings page: admin can change it; non-admin is rejected; CSRF enforced.
- **Override threading:** `enqueue_job(..., default_expectation=FAIL)` and
  `_queue_matrix_run(..., default_expectation=FAIL)` stamp FAIL on the
  newly-created Tests; with the value omitted they fall back to the global
  default.
- **Web new-Test form:** submitting with the dropdown left at "(use default)"
  stamps the global default; choosing FAIL stamps FAIL on the created Test —
  no detail-page round-trip.
- **Web matrix-run form:** choosing an override applies it to every Test the
  matrix spawns; reruns keep each Test's existing expectation (no re-stamp).
- `parse_expectation_override`: `""` → `_UNSET`; `"PASS"` → `TestResultCode.PASS`;
  invalid → error surfaced to the form.

## Non-goals

- No backfill of existing tests (user explicitly doesn't care about back-compat).
- No removal of the UNKNOWN verdict or the retraction/NULL state.
- No change to SKIPPED handling.

## Resolved questions

- **Schema bootstrap** — `Base.metadata.create_all(engine)`
  ([cli.py:604](../../opp_ci/cli.py#L604) etc.). Alembic is configured but
  `migrations/versions/` is empty/unused. → Just add the `AppSetting` model;
  no migration.
- **Admin role string** — `"admin"`, the top of `ROLE_HIERARCHY`
  ([auth.py:26](../../opp_ci/auth.py#L26)). Settings page gated
  `require_user("admin")`.
- **Auto-row attribution** — submitting user when known
  (`current_user.display_name` / `identity.get("name")`), `"system"` for the
  scheduler/internal path; mirrors the manual-expectation convention.
