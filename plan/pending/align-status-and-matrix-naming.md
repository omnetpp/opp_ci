# Plan: align REST `status` / `matrix` field naming with the DB

Goal: close the two naming-drift gaps between
[`opp_ci/db/models.py`](../../opp_ci/db/models.py),
[`opp_ci/web/api.py`](../../opp_ci/web/api.py),
[`opp_ci/cli.py`](../../opp_ci/cli.py), and
[`opp_ci/client.py`](../../opp_ci/client.py) so every layer reaches the
same name when it means the same thing. Nothing else in the audit was
worth a code change — test coordinate fields, worker state, and rule
type are already consistent across layers.

The fix has two unrelated sub-fixes packaged into one plan because both
are small and both touch the REST API surface.

## Background: where the names drift today

### Sub-fix 1: run lifecycle vs. result\_code vs. "status"

The DB splits run state into two orthogonal columns:

- [`TestRun.lifecycle`](../../opp_ci/db/models.py#L401) — enum
  `TestRunLifecycle` with values `queued`, `running`, `finished`,
  `cancelled`, `timed_out`. This is the **state machine**.
- [`TestRun.result_code`](../../opp_ci/db/models.py#L408) — enum
  `TestResultCode` with values `PASS`, `FAIL`, `ERROR`, `SKIPPED`.
  Populated only when `lifecycle == finished`. This is the **outcome**.

The model exposes a derived
[`effective_status`](../../opp_ci/db/models.py#L520-L531) that flattens
the two columns into one display string (returns the result code if
the run finished, otherwise the lifecycle value). Templates, the CLI
table renderer, and the web UI all reach for this flattened view.

Around that two-column model, three downstream surfaces have grown
*three different* meanings for the word "status":

| Surface | What "status" means | Behaviour | File:line |
|---|---|---|---|
| CLI `list-runs --status` / `delete-runs --status` / `show-results --status` | Union of lifecycle ∪ result\_code | Helper [`_status_where`](../../opp_ci/cli.py#L501-L511) tries `TestRunLifecycle(status)` then `TestResultCode(status)`; accepts `"queued"`, `"PASS"`, etc. | [cli.py:501](../../opp_ci/cli.py#L501) |
| Web UI `/runs?status=` / `/results?status=` | Same union | Helper [`_status_filter`](../../opp_ci/web/app.py#L265-L280) does the same trick. | [web/app.py:265](../../opp_ci/web/app.py#L265) |
| REST `GET /runs?status=` | Lifecycle only | Calls `TestRunLifecycle(status)` directly; `?status=PASS` silently returns nothing because `"PASS"` is not a lifecycle value. | [web/api.py:233-234](../../opp_ci/web/api.py#L233) |
| REST `POST /runs` response | Lifecycle only | Returns `{"id": ..., "status": run.lifecycle.value}` — the response key is `status` but the value space is just `queued`. | [web/api.py:150](../../opp_ci/web/api.py#L150) |
| REST `GET /runs` row | Both, separate | `_run_to_dict` returns `"lifecycle"` *and* `"result_code"` as distinct keys. | [web/api.py:1088-1089](../../opp_ci/web/api.py#L1088) |
| Python client | "FAIL" in docstring example | `OppCiClient.list_runs(..., status="FAIL")` is shown as the canonical example — but against the current REST API, that filter is silently ignored. | [client.py:12](../../opp_ci/client.py#L12), [client.py:74-84](../../opp_ci/client.py#L74) |

So:
- A REST client that sends `?status=FAIL` gets *no* error and *no*
  matches — the value isn't a lifecycle, so the `TestRunLifecycle(...)`
  constructor raises `ValueError`, the `HTTPException` handler at
  [api.py:118](../../opp_ci/web/api.py#L118) does **not** catch it
  here (this is inside `list_runs`, not `submit_run`), and the request
  500s. Either way it does not do what the client expects.
- The CLI says one thing, the REST API says another, the docstring on
  the Python client says a third — all using the bare word "status".

We also use "status" as a generic *operation* result in several places
(`{"status": "ok"}` on worker heartbeat / result acks,
`{"status": "deleted"}` on rule deletion). That use is fine — it's a
different concept ("did the API call succeed") and renaming it would
just churn the wire format. The plan leaves those untouched.

### Sub-fix 2: `matrix_name` vs. `matrix` in matrix responses

`POST /runs/matrix` accepts a `SubmitMatrixRequest` whose only field is
`matrix_name` ([api.py:73](../../opp_ci/web/api.py#L73)), but the
response returns it back under the shortened key `"matrix"`
([api.py:197](../../opp_ci/web/api.py#L197)):

```python
return {
    "matrix": req.matrix_name,
    "matrix_run_id": matrix_run.id,
    "jobs_queued": len(run_ids),
}
```

Meanwhile the AutoTestRule endpoints — both request and response —
consistently use `matrix_name`
([api.py:683](../../opp_ci/web/api.py#L683),
[api.py:730](../../opp_ci/web/api.py#L730),
[api.py:781](../../opp_ci/web/api.py#L781),
[api.py:893](../../opp_ci/web/api.py#L893)). The `submit_matrix_run`
response is the lone outlier.

## Design decisions

| Question | Decision |
|---|---|
| Should REST `?status=` accept both lifecycle and result codes (like CLI/UI), or stay lifecycle-only? | **Accept both.** Match what CLI and web UI already do; reuse a shared helper so the three filters can never drift again. |
| Where does the shared helper live? | New `opp_ci/persistence.py::status_filter(query, status_str)` (or a small module-level function in `db/models.py`). `cli.py`, `web/app.py`, `web/api.py` all import it. The existing two private copies (`_status_where`, `_status_filter`) collapse into one call site each. |
| Should we add explicit `?lifecycle=` and `?result_code=` as separate REST params? | **Yes.** Keep `?status=` as the convenience union; add `?lifecycle=…` (validates as `TestRunLifecycle`) and `?result_code=…` (validates as `TestResultCode`) for callers that want strict filtering. Mirrors how the response already separates the two. |
| Should `POST /runs` response stay `{"status": "queued"}` or move to `{"lifecycle": "queued"}`? | **Move to `{"lifecycle": "queued"}`.** The value is a lifecycle value; the response should say so. Matches the row shape returned by `GET /runs`. Breaks one wire-format consumer (see Risks). |
| `POST /runs/matrix` response field `"matrix"` — rename to `"matrix_name"`? | **Yes.** Matches the request field and matches AutoTestRule responses. |
| Invalid status string — error or silent no-op? | **Error with HTTP 400.** Today the union falls through to a no-op fallback in [web/app.py:280](../../opp_ci/web/app.py#L280) and to a `ValueError` 500 in [web/api.py:234](../../opp_ci/web/api.py#L234). Both are wrong; the shared helper should validate and raise a typed error. The CLI helper [cli.py:511](../../opp_ci/cli.py#L511) silently returns the unfiltered query — same kind of bug, same fix. |
| Generic `{"status": "ok"}` / `{"status": "deleted"}` API-result responses — change? | **No.** Different concept, conventional shape, not worth churning. |

## Renaming / API-change table

| Surface | Before | After |
|---|---|---|
| `POST /runs` response body | `{"id": ..., "status": "queued"}` | `{"id": ..., "lifecycle": "queued"}` |
| `GET /runs?status=` semantics | Lifecycle only; bad value → 500 | Union (lifecycle ∪ result\_code); bad value → 400 |
| `GET /runs?lifecycle=` | did not exist | new strict-lifecycle filter |
| `GET /runs?result_code=` | did not exist | new strict-result filter |
| `POST /runs/matrix` response key | `"matrix"` | `"matrix_name"` |
| Shared status helper | private `_status_where` in cli.py, private `_status_filter` in web/app.py | one public `status_filter(query, status_str)` (raises `ValueError` on bad input) — re-imported by cli.py, web/app.py, web/api.py |
| `OppCiClient.list_runs(status=…)` docstring example | `status="FAIL"` works against `/runs?status=` | unchanged — works after the union is implemented |
| `OppCiClient.submit_run` docstring | `"status": "queued"` | `"lifecycle": "queued"` |

DB schema and column names do **not** change. No Alembic migration is
needed.

## Files to touch

### Shared helper (new code)

- Add `status_filter(query, status_str)` to
  [opp_ci/persistence.py](../../opp_ci/persistence.py) (this is where
  cross-layer DB helpers already live —
  `get_or_create_test`, `create_test_run`, `enqueue_job`). The function
  takes a SQLAlchemy `select()` and a string, tries
  `TestRunLifecycle(s)` then `TestResultCode(s)`, raises `ValueError`
  with a clear message if neither matches, returns the filtered query
  otherwise.

### REST API

- [opp_ci/web/api.py](../../opp_ci/web/api.py)
  - Line 150 (`submit_run` response): `"status": run.lifecycle.value`
    → `"lifecycle": run.lifecycle.value`.
  - Lines 207–234 (`list_runs`): add `lifecycle: str | None = None`,
    `result_code: str | None = None` parameters. Replace the
    `status` block with: if `status` is set, call `status_filter`;
    if `lifecycle` is set, validate via `TestRunLifecycle(...)` and
    filter `TestRun.lifecycle`; if `result_code` is set, validate
    via `TestResultCode(...)` and filter `TestRun.result_code`.
    Each branch catches `ValueError` and raises `HTTPException(400)`
    with the offending value.
  - Line 197 (`submit_matrix_run` response): `"matrix": req.matrix_name`
    → `"matrix_name": req.matrix_name`.

### CLI

- [opp_ci/cli.py](../../opp_ci/cli.py)
  - Delete `_status_where` (lines 501–511), import `status_filter`
    from `opp_ci.persistence` instead.
  - Three call sites that use it: `list_runs` (line 537),
    `delete_runs` (line 640), `show_results` (line 691). Wrap each in
    a `try/except ValueError` that prints a clean `click.echo`
    error and exits — same behaviour the user already gets for any
    other validation failure.
  - The `--status` help strings (lines 518, 621, 672) already list
    both lifecycle and result-code values; leave the wording alone.

### Web UI

- [opp_ci/web/app.py](../../opp_ci/web/app.py)
  - Delete `_status_filter` (lines 265–280), import `status_filter`
    from `opp_ci.persistence`. Two call sites: lines 249–250 (`/runs`)
    and 339–340 (`/results`). On `ValueError`, fall through to "no
    rows" — that's the current silent-fallback behaviour and the web
    UI doesn't need to surface validation errors; the user just sees
    an empty table and corrects the filter.

### Python client

- [opp_ci/client.py](../../opp_ci/client.py)
  - Line 51 docstring for `submit_run`: `{"id": ..., "status": "queued"}`
    → `{"id": ..., "lifecycle": "queued"}`.
  - Line 74 signature: keep `status=` for backwards-compat with
    existing scripts, but add `lifecycle=` and `result_code=`
    kwargs that get forwarded as the corresponding query params.
    Follow the `**kwargs` pass-through convention — don't redeclare
    every filter param.

  See [[feedback_kwargs_passthrough]] — the current `list_runs`
  signature already redeclares every filter, so this fix is also
  an opportunity to switch it to `**filters` and forward as-is. If
  that's too much scope creep, just add the two new kwargs explicitly.

### Documentation

Audit and update any docs that show the old shapes. From a quick
grep, the candidates are:

- [doc/rest_api.md](../../doc/rest_api.md) — `POST /runs` response
  example, `GET /runs?status=` description, `POST /runs/matrix`
  response example. Add `?lifecycle=` and `?result_code=` to the
  query-param table.
- [doc/python_client.md](../../doc/python_client.md) — `submit_run`
  response example, `list_runs(status=...)` example.
- [doc/cli_reference.md](../../doc/cli_reference.md) — already says
  `--status` accepts both; verify the help text matches.

### Out of scope

- DB schema, column names, enum values.
- Worker heartbeat / result POST responses (`{"status": "ok"}`).
- Rule delete response (`{"status": "deleted"}`).
- The `effective_status` model property — it's a templating convenience
  and renaming it would touch every template for no benefit. Templates
  already display it as the user-facing "Status" column, and "status"
  *is* the right word for the flattened display value.
- The CLI's `worker list` `status` column ([cli.py:1689](../../opp_ci/cli.py#L1689))
  — that's `Worker.status`, a different concept (online/offline/busy).
  No drift, no change.
- Matrix CLI flag `--matrix` (vs. the DB class `TestMatrix`) — Click
  convention, already consistent with `--project` etc.
- `AutoTestRule.project_id` (DB) ↔ `project_name` (API) — intentional
  ID↔name translation at the API boundary. Documented as such.

## Migration sequence

Each step is one commit. Order matters because the helper has to exist
before its callers can import it.

1. **Add `status_filter` helper** to `opp_ci/persistence.py`. Pure
   addition, no callers yet. Includes a small unit test (one happy
   path each for lifecycle and result\_code, one `ValueError` path).
2. **Wire `status_filter` into CLI and web UI.** Delete the two
   private copies, route the three CLI call sites through the helper
   with a `try/except`, route the two web-app call sites through it.
   No behavioural change for the user — same filtering semantics,
   same fallback on bad input on the web side, cleaner error on
   the CLI side.
3. **Extend the REST API.** Add `lifecycle` and `result_code` query
   params to `GET /runs`, change `?status=` to use the union helper,
   rename the `POST /runs` response field, rename the
   `POST /runs/matrix` response field. **This is the breaking step
   for REST clients.**
4. **Update `OppCiClient`.** Adjust the docstring example, add the
   two new kwargs (or switch to `**filters`). Existing
   `client.list_runs(status="FAIL")` calls now work end-to-end —
   they were broken before.
5. **Sweep docs.** `rest_api.md`, `python_client.md`,
   `cli_reference.md`.

Steps 1, 2, 4, 5 are non-breaking. Step 3 is the only one a deploy
needs to coordinate around.

## Verification

- `pytest` passes — the new `status_filter` test covers the helper;
  existing CLI / web tests that exercise `--status` or `?status=`
  cover the wiring.
- Manual REST checks against a running coordinator:
  - `curl '…/runs?status=PASS'` returns finished+PASS runs (previously
    500 or empty).
  - `curl '…/runs?status=queued'` still returns queued runs.
  - `curl '…/runs?status=bogus'` returns 400 with a clear error.
  - `curl '…/runs?lifecycle=running'` returns running runs only.
  - `curl '…/runs?result_code=FAIL'` returns finished+FAIL only.
  - `curl -X POST '…/runs' -d '…'` returns `{"id": N, "lifecycle": "queued"}`.
  - `curl -X POST '…/runs/matrix' -d '{"matrix_name": "…"}'` returns
    a body with `"matrix_name"`, not `"matrix"`.
- Manual CLI checks:
  - `opp_ci list-runs --status PASS` still works (unchanged behaviour).
  - `opp_ci list-runs --status bogus` exits cleanly with a friendly
    error instead of an empty table.
- Manual web UI: `/runs?status=PASS`, `/results?status=FAIL` still
  filter as before.
- `grep -rn '_status_where\|_status_filter' opp_ci/` returns zero
  hits — both copies have been collapsed.

## Risks & notes

- **REST clients break on `POST /runs` response.** Anything reading
  `response["status"]` after submitting a run will `KeyError` after
  step 3. The fix is `response["lifecycle"]`. Call this out in
  release notes; the Python client gets the fix in step 4 so users on
  `OppCiClient` are unaffected.
- **REST clients break on `POST /runs/matrix` response.** Same shape
  fix, smaller blast radius (matrix submission is rarer than single-run
  submission). Same release-note treatment.
- **REST clients on `GET /runs?status=…` get *better* behaviour.**
  Anything sending `?status=PASS` was already broken (silent no-op /
  500); they now get correct results. No back-compat shim needed.
- **Shared helper raises on bad input — CLI and web app handle it
  differently.** CLI exits with an error message; web UI silently
  returns no rows. This matches the two surfaces' existing styles
  (CLI is strict, web UI is forgiving on URL params) and is
  intentional — don't unify the error handling, only the validation
  helper.
- **The word "status" still appears in three meanings across the
  codebase** even after this plan: (1) the union filter input on
  CLI/UI, (2) the generic `{"status": "ok"}` API-result envelope,
  (3) `Worker.status`. That's acceptable — each is a distinct concept
  and each is locally consistent. The bug was that meaning (1) on the
  REST API silently meant something different from meaning (1) on
  CLI/UI.
