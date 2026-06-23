# Replace the legacy INET test harness with opp_ci + opp_repl

## Goal

Retire INET's legacy, CSV/script-based test harness in `inet/tests/` and run all INET
tests through **opp_ci** driving **opp_repl**'s test framework. This is the test-side
counterpart of the inet → opp_repl Python migration (which already removed the
`inet_run_*` / `inet_fingerprinttest` / `inet_smoketest` wrappers from `inet/bin`).

## Context / current state (established)

- **opp_ci already runs INET via opp_repl.** `opp_ci/bin/test-inet` submits runs to the
  coordinator (`opp_ci --remote run --project inet-X --kind build --pin omnetpp=Y ...`),
  and the executor/worker invoke opp_repl. Today `test-inet` submits only `--kind build`.
- **opp_repl test API is complete** for the generic categories: `run_smoke_tests`,
  `run_fingerprint_tests`, `run_statistical_tests`, `run_speed_tests`,
  `run_feature_tests`, and `get_opp_test_tasks`/opp_test for `.test`-based suites.
  INET-specific validation stays in `inet.run_validation_tests` (reachable via
  `from inet import *`).
- **Fingerprint/speed data already migrated.** `inet/tests/fingerprint/store.json` and
  `inet/tests/speed/store.json` exist (opp_repl JSON store format). The 14 legacy
  `tests/fingerprint/*.csv` files are the old input format and are now redundant.
- **`.test` files are NOT legacy.** `tests/{unit,module,protocol,queueing,statistical}/*.test`
  are opp_test definitions; opp_repl runs them via `get_opp_test_tasks`. They stay.

### What is actually "legacy harness" (to retire)
| inet path | what it is | replacement |
|---|---|---|
| `tests/fingerprint/fingerprinttest` | calls removed `bin/inet_fingerprinttest` (CSV) | opp_repl `run_fingerprint_tests` (store.json) via opp_ci |
| `tests/fingerprint/smoketest` | calls removed `bin/inet_smoketest` (CSV) | opp_repl `run_smoke_tests` via opp_ci |
| `tests/features/featuretest` | feature-test runner | opp_repl `run_feature_tests` |
| `tests/speed/speedtest` | speed-test runner | opp_repl `run_speed_tests` |
| `tests/fingerprint/*.csv` (14) | legacy fingerprint inputs | superseded by `store.json` |
| `tests/fingerprint/fingerprinttest_selfdoc` + `bin/inet_selfdoc_json2xml` + `SelfDoc.json` | selfdoc neddoc augmentation from fingerprint runs | decide: re-home or retire (see Phase 5) |
| `tutorials/fingerprint/doc/*.rst` (~10) | tutorial text invoking `inet_fingerprinttest` | rewrite to opp_repl/opp_ci flow |

## Plan

- [x] **Phase 1 — Define the INET test kinds in opp_ci.** *(Done — code-only.)*
  Extend `bin/test-inet` (and any coordinator-side kind registry) to submit, beyond
  `build`, the test kinds: `smoke`, `fingerprint`, `statistical`, `speed`, `feature`,
  `opp` (the `.test` suites: unit/module/protocol/queueing), and `validation`.
  Confirm each kind maps to the right opp_repl entry point in the executor, and that the
  INET project is resolved from the bundled `inet.opp` descriptor. Validation runs the
  INET-specific `inet.run_validation_tests` (not an opp_repl generic), so verify the
  executor can reach `from inet import *`.

  **What was implemented** (branch `topic/replace-legacy-test-harness` in opp_repl,
  opp_ci, inet worktrees; committed, not pushed):
  - The generic kinds (`smoke`/`fingerprint`/`statistical`/`speed`/`feature`/`opp`)
    already existed in the executor's `COMMAND_MAP` / `_get_test_functions`; mm1k
    already runs `build,smoke,opp` through them, so kinds are **not** per-project
    registered. No coordinator-side registry change was needed.
  - **`validation` kind (new), resolved from the project definition.** Validation
    tests are project-specific, so opp_repl gained a generic
    `opp_repl.test.validation.run_validation_tests` entry point + the
    `opp_run_validation_tests` console script. It resolves the actual runner from a
    new `SimulationProject.validation_test_runner` attribute — a
    `"module.path:function"` dotted reference declared in the project's `.opp`. The
    bundled `inet.opp` sets it to `inet.test.validation:run_validation_tests`. The
    resolver adds the project's `python_folders` to `sys.path` on demand, so INET's
    own package is importable inside the container without extra PYTHONPATH wiring.
    opp_ci maps `kind=validation` → `opp_run_validation_tests` (and the in-process
    `run_validation_tests`). *(This is the design chosen over a per-project console
    script or hardcoding; it mirrors the still-incomplete `inprocess` runner TODO.)*
  - `bin/test-inet` now submits the full kind set
    `build,smoke,fingerprint,statistical,speed,feature,opp,validation` (overridable
    via an argument), mirroring `test-mm1k`.
  - Verified by import + an end-to-end resolution test: loading `@opp` and resolving
    the inet project yields INET's real `run_validation_tests`. **Not** yet verified
    against a live coordinator run (that is Phases 2–3).

  > Pre-existing gap noticed (out of scope): the `opp` kind maps to
  > `opp_run_opp_tests`, but opp_repl's `pyproject.toml` does not declare that console
  > script. mm1k's `opp` runs may rely on a differently-provisioned opp_repl. Worth
  > confirming separately before relying on the `opp` kind for INET.

- [ ] **Phase 2 — Parity for fingerprint & speed.**
  `store.json` already holds the migrated data. Run `fingerprint`/`speed` kinds through
  opp_ci and confirm results match the legacy CSV-based expectations. Record any configs
  present in the CSVs but missing from `store.json` (and vice-versa). Only after parity,
  mark the 14 `tests/fingerprint/*.csv` (+ `.csv.ERROR`, `examples-TODO.csv_off`) for removal.

- [ ] **Phase 3 — Parity for the `.test` suites, feature, statistical.**
  Run `opp` (unit/module/protocol/queueing), `statistical`, and `feature` kinds via opp_ci
  and compare PASS/FAIL counts against the current harness. The `.test`/`.csv`-free suites
  should map cleanly to `get_opp_test_tasks`/`run_*_tests`.

- [ ] **Phase 4 — Decommission the legacy runner scripts in inet.**
  Remove `tests/fingerprint/{fingerprinttest,smoketest}`, `tests/features/featuretest`,
  `tests/speed/speedtest` (they call removed/legacy tooling). Keep all `.test` files and the
  `store.json` stores. Remove the redundant fingerprint CSVs (after Phase 2).

- [ ] **Phase 5 — Selfdoc decision.**
  `inet_selfdoc_json2xml` (+ `tests/fingerprint/fingerprinttest_selfdoc`, `SelfDoc.json`,
  the `Makefile` target) augments neddoc with data observed during fingerprint runs. It is
  coupled to the legacy fingerprint flow. Decide: (a) re-home it onto opp_repl's fingerprint
  run output, or (b) retire it. Until decided, leave `bin/inet_selfdoc_json2xml` in place
  (it is referenced by the `Makefile`).

- [ ] **Phase 6 — Tutorials & docs.** *(Blocked on Phase 2 — deliberately not started.)*
  Rewrite `tutorials/fingerprint/doc/*.rst` to describe the opp_repl/opp_ci fingerprint
  workflow instead of `inet_fingerprinttest`. (`WHATSNEW` mentions are historical — leave.)

  > **Why blocked:** the fingerprint tutorials (~10 `.rst`) and
  > `doc/src/developers-guide/ch-testing.rst` teach the legacy `inet_fingerprinttest` /
  > `fingerprinttest` CLI in depth — its `-m <Config>` selector (note: opp_repl's `-m`
  > is the *build mode*; the config selector is `-c`), the positional `<file>.csv`, the
  > `-a` accept flag (now the separate `opp_update_fingerprint_test_results` script),
  > and ad-hoc `--fingerprint-events` / `--fingerprint-modules` / `--fingerprint-ingredients`
  > filters. These are a different *model* from opp_repl's `store.json` (where ingredients
  > are stored per fingerprint entry), so the rewrite is a semantic redesign of
  > pedagogical content, not a command substitution. It must be grounded in the
  > **verified** store.json workflow (Phase 2), against a built INET, to avoid teaching
  > commands/flags that do not behave as described. Do this once Phase 2 confirms the flow.

- [ ] **Phase 7 — Cutover & cleanup.**
  Full INET suite green through opp_ci; remove the legacy scripts/CSVs (Phases 4/2);
  update `inet/tests/README`s that point at the old commands. Done when `bin/test-inet`
  submits the full kind set and the legacy harness files are gone.

## Out of scope / dependencies
- The opp_repl **in-process (cffi) runner** is marked incomplete (`opp_repl/simulation/task.py`);
  INET tests run via the subprocess runner, so this does not block the harness replacement.
- This plan assumes opp_ci's existing coordinator/worker, kinds, pins and dimensions model
  (see `plan/done/repeatable-tests-and-moving-target-matrices.md`, `mm1k-testing-end-to-end.md`).

## Open questions
- ~~Does the coordinator already accept the non-`build` kinds for `inet-*` projects, or does
  each kind need registration (Phase 1)?~~ **Answered:** kinds are generic (not per-project);
  no registration needed. `validation` was added as a generic, project-resolved kind (Phase 1).
- Fingerprint ingredients/tolerance parity between the CSV expectations and `store.json`.
- Selfdoc: keep or retire (Phase 5).
- The `opp` kind references an `opp_run_opp_tests` console script that opp_repl does not
  declare in `pyproject.toml` (see Phase 1 note). Confirm before relying on it for INET.
