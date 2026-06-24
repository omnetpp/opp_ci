# Add a `coverage` test kind (+ sorted kind picker)

## Goal

1. Add a new opp_ci test kind `coverage` that runs the project's simulations with
   coverage instrumentation and produces an HTML coverage report, end-to-end through
   the real CLI path (host opp_env + podman).
2. Keep the kind picker on the New-Matrix web form sorted and driven from a single
   source of truth (the dispatch table) so the UI can't drift from what actually runs.

## Key facts discovered

- Both execution paths run the CLI command from `COMMAND_MAP`; `_run_test_direct` is
  dead code (`executor.py:514`). So `coverage` needs a real `opp_run_coverage_tests`
  CLI entry point in **opp_repl**.
- opp_repl already has `opp_repl/test/coverage.py` with `generate_coverage_report` /
  `open_coverage_report`, but no `run_*_tests` entry and it returned `None`.
- `run_simulations` returns a `MultipleTaskResults` (has `is_all_results_expected()` +
  `to_dict()`), so a coverage runner can return it for a CI verdict + `--result-file`.
- Coverage needs a `mode=coverage` instrumented build, but the CLI `--mode` only
  accepted `debug`/`release` (`main.py:26`). opp_ci builds + runs as two `--mode`
  stages, so `coverage` must be an allowed mode and forced for `kind==coverage`.
- The web datalist in `matrix_new.html` was hardcoded + included 5 stale non-dispatch
  values (module/packet/protocol/queueing/unit). DB-derived filter lists are already
  sorted (`_distinct_options` / `matrix_axis_options`).

## Decisions (confirmed with user)

- End-to-end wiring (opp_repl + opp_ci).
- Auto-force `mode=coverage` for `kind==coverage` in opp_ci (don't rely on the modes axis).

## Steps

### opp_repl
- [x] `test/coverage.py`: `generate_coverage_report` takes `mode` (default coverage),
      captures + returns the `run_simulations` result; add `run_coverage_tests` wrapper.
- [x] `main.py`: add `run_coverage_tests_main`; add `coverage` to `--mode` choices.
- [x] `pyproject.toml`: register `opp_run_coverage_tests`.

### opp_ci
- [x] `executor.py`: add `coverage` to `COMMAND_MAP` + `_get_test_functions`; force
      `mode=coverage` in `run_test` when `kind==coverage`.
- [x] `scheduler.py`: `expand_matrix` pins coverage jobs to `mode=coverage` and emits
      one (not one per modes-axis entry).
- [x] `web/app.py`: `_matrix_form_context` exposes `kind_options = sorted(COMMAND_MAP)`.
- [x] `web/templates/matrix_new.html`: render the kind datalist from `kind_options`.

## Notes / out of scope
- The coverage HTML report is written under `<project>/coverage` in the run workspace;
  collecting/publishing it as a CI artifact is a separate feature.
- `llvm-profdata` / `llvm-cov` must be present in the opp_env/podman environment.
