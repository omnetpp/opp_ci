# Align opp_repl / opp_ci test-kind defaults with INET CI conventions

## Goal

Change **defaults** in opp_repl (and the force-mode plumbing in opp_ci) so that running an
INET test kind through opp_ci reproduces what INET's GitHub Actions CI does today, without
the caller having to re-specify the conventions on every matrix. This is the
default-tuning prerequisite for [replace-legacy-inet-test-harness.md](replace-legacy-inet-test-harness.md)
Phases 2–3 (fingerprint/speed/statistical/chart/feature parity), and it generalises the
mode-forcing introduced in [../done/coverage-test-kind.md](../done/coverage-test-kind.md).

INET CI drives `inet_run_*` (INET's own `python/inet/test/` harness); opp_repl is the same
lineage, so the per-kind *defaults already match*. The gaps are flags CI layers on top, and
how opp_ci's always-present `--mode` overrides a kind's instrumented mode.

## Differences → resolutions

| # | Difference (CI vs opp_repl default) | Fix in | Kind of change |
|---|---|---|---|
| D1 | coverage/chart enumerate **all runs** of every config, no cap | opp_repl | default arg |
| D2 | fingerprint default = `tplx` only; CI runs `tplx,~tNl,~tND` | opp_repl | default arg |
| D3 | sanitizer/speed/coverage instrumented modes overridden by opp_ci `--mode`; CLI `--mode` too narrow | opp_repl + opp_ci | choices + force-map |
| D4 | chart runs **all** working dirs; CI restricts to `showcases` | opp_repl (project attr) | default arg |
| D5 | chart + statistical compare against **baseline repos** not present in the workspace | opp_ci + project attrs | provisioning |

### D1 — Bound coverage (and document chart) to one run
- `run_simulations` with no `run_number` enumerates `range(0, num_runs)` for every config
  ([simulation/task.py](../../../opp_repl/opp_repl/simulation/task.py)); coverage passes no
  limit, so a coverage matrix runs every repetition of every config to full sim-time-limit.
- **Change:** give `run_coverage_tests` / `generate_coverage_report`
  ([test/coverage.py](../../../opp_repl/opp_repl/test/coverage.py)) a default `run_number=0`
  (mirroring smoke/sanitizer/statistical/speed/feature), threaded into `run_simulations`.
  Keep `cpu_time_limit` overridable (default `None` → each config's own limit bounds it).
- **Decision flagged:** run 0 only reduces exercised parameter combinations. Acceptable for a
  CI coverage gate; callers can widen with `run_number_filter`. (chart is the same `None`
  case but is separately constrained by D4, so leave chart's run handling as-is.)

### D2 — Fingerprint default ingredients
- `collect_fingerprint_test_groups(ingredients_list=["tplx"])`
  ([test/fingerprint/task.py:276](../../../opp_repl/opp_repl/test/fingerprint/task.py#L276))
  checks only `tplx`; CI runs `-f tplx -f ~tNl -f ~tND`
  ([fingerprint-tests.yml:57](../../../inet/.github/workflows/fingerprint-tests.yml#L57)).
- **Change:** default `ingredients_list = ["tplx", "~tNl", "~tND"]`.
- **Safe generically:** each `(config, run, ingredient)` task is dropped unless a matching
  `store.json` entry exists (task.py:280), so projects without `~tNl`/`~tND` entries emit no
  extra tasks. (Excludes `tyf` — animation/GUI, not in CI's fingerprint workflow.)

### D3 — Instrumented modes (sanitizer→sanitize, speed→profile, coverage→coverage)
- opp_ci always passes `--mode <matrix mode>` (default `release`), overriding a kind's
  default mode; and the CLI `--mode` now accepts only `debug/release/coverage`, so
  `sanitize`/`profile` can't be selected. Sanitizer through opp_ci therefore builds/runs
  `release` — no sanitizer instrumentation.
- **opp_repl:** add `sanitize`, `profile` to the `--mode` choices in
  `parse_run_tasks_arguments` and `parse_build_project_arguments`
  ([main.py](../../../opp_repl/opp_repl/main.py)) (coverage already added).
- **opp_ci:** replace the `if kind == "coverage"` special-cases in `run_test`
  ([executor.py](../../opp_ci/executor.py)) and `expand_matrix`
  ([scheduler.py](../../opp_ci/scheduler.py)) with a shared
  `KIND_FORCED_MODE = {"coverage": "coverage", "sanitizer": "sanitize", "speed": "profile"}`:
  force the mode for execution (run_test) and pin the coordinate + collapse the modes axis to
  one job (expand_matrix), exactly as coverage does now.
- **Verify:** opp_env/podman builds succeed in `sanitize` and `profile` mode (omnetpp
  supports both; mm1k only exercises build/smoke/opp today, so low blast radius).

### D4 — Chart default folder = showcases
- `get_chart_test_tasks(filter=None, ...)`
  ([test/chart.py:203](../../../opp_repl/opp_repl/test/chart.py#L203)) walks **all** analysis
  files; CI runs `inet_run_chart_tests -m release -f showcases`.
- **Change (project-declared default, mirroring `validation_test_runner`):** add a
  `chart_test_filter` (or reuse a general `chart_test_working_directory`) attribute to
  `SimulationProject`, set to `"showcases"` in the bundled `inet.opp`; `get_chart_test_tasks`
  falls back to it when no `filter`/`working_directory_filter` is given. Keeps opp_repl
  generic (no hard-coded INET path) while making `showcases` the INET default.
- **Alternative considered:** opp_ci appends `--filter showcases` for `kind=chart` — rejected,
  it bakes an INET-specific value into generic opp_ci.

### D5 — Baseline repositories for chart + statistical
Both kinds compare against baselines that must be checked out in the workspace before the run:
- **statistical:** stored `.sca` baselines under `simulation_project.statistics_folder`
  ([test/statistical.py:204](../../../opp_repl/opp_repl/test/statistical.py#L204)); CI clones
  `https://github.com/inet-framework/statistics.git`
  ([statistical-tests.yml](../../../inet/.github/workflows/statistical-tests.yml)).
- **chart:** baseline PNGs live next to each `.anf` in the project's media tree
  ([test/chart.py:157](../../../opp_repl/opp_repl/test/chart.py#L157)) — for INET a large
  separate media/baseline repo, not the main checkout.
- **Change (opp_ci provisioning + project attrs):**
  - Declare the baseline sources in `inet.opp` (e.g. `statistics_folder` →
    checkout path; a `chart_baseline_repo` / media path attribute).
  - The opp_ci install/run path checks out the named baseline repo(s) into the expected
    location (pinned to a ref, like deps) before chart/statistical kinds run; absence →
    a clear stage failure, not a silent all-`only_current` FAIL.
- **Scope note:** this is provisioning, not a one-line default; size it as its own step and
  treat it as the gating work for replace-legacy Phase 3 (statistical/chart parity).

## Out of scope (not a default change)
- **Per-kind sharding** — CI splits fingerprint 4-way (`-n/-i`) and feature 16-way
  (`SPLIT_TOTAL/SPLIT_INDEX`). opp_ci parallelises at the matrix-coordinate level; sharding a
  single kind across jobs needs a split axis + job-level test filtering. Separate feature.
- **opp-kind taxonomy** — INET's unit/module/packet/queueing/protocol are `.test` suites = the
  single `opp` kind (folder-scoped). Exposing them as distinct CI entries is a separate change.
- **`all` / `release` meta-kinds** — they run sub-tests with per-sub-test modes internally;
  one opp_ci `--mode` can't express that. Don't drive them through opp_ci matrices.
- **smoke `-e inet` exclusion** — INET's `smoketest` excluded `inet`-prefixed examples; minor,
  revisit only if smoke parity needs it.

## Related quick fix (tracked elsewhere, fold in if convenient)
- `opp` kind → `opp_run_opp_tests`, but that console script is **not** declared in opp_repl's
  `pyproject.toml` (noted in replace-legacy Phase 1 and the coverage work). Register it the
  same way `opp_run_coverage_tests` was, so the `opp` kind dispatches via the CLI path.

## Steps
- [ ] opp_repl: coverage default `run_number=0` (D1).
- [ ] opp_repl: fingerprint default `ingredients_list=["tplx","~tNl","~tND"]` (D2).
- [ ] opp_repl: add `sanitize`,`profile` to both `--mode` choice lists (D3).
- [ ] opp_ci: `KIND_FORCED_MODE` map; apply in `run_test` + `expand_matrix`, replacing the
      coverage special-cases (D3).
- [ ] opp_repl: `chart_test_filter` project attr + fallback; set `showcases` in `inet.opp` (D4).
- [ ] opp_ci + `inet.opp`: declare and check out statistical + chart baseline repos (D5).
- [ ] opp_repl: register `opp_run_opp_tests` console script (quick fix).
- [ ] Verify each kind end-to-end against current INET CI PASS/FAIL counts (feeds
      replace-legacy Phases 2–3).

## Verification
- Unit-level: `expand_matrix` collapses sanitizer/speed/coverage to one job at the forced mode;
  fingerprint emits 3 ingredient groups where the store has them; chart restricted to showcases.
- End-to-end: a bounded coverage run terminates; sanitizer builds in sanitize mode and catches a
  seeded UBSan/ASan issue; statistical/chart runs find their baselines and produce real diffs.
