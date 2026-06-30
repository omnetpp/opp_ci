# Align opp_repl / opp_ci test-kind defaults with INET CI conventions

## Goal

Make running an INET test kind through opp_ci reproduce what INET's GitHub Actions CI does,
by splitting test configuration into two layers:

1. **opp_repl bakes in only kind-*intrinsic*, non-limiting defaults** (signatures).
2. **The project (`.opp`) carries everything project-specific** — per-kind kwarg defaults,
   result stores, baseline repos, runner functions — for *all* kinds, uniformly.

Default-tuning prerequisite for
[replace-legacy-inet-test-harness.md](replace-legacy-inet-test-harness.md) Phases 2–3, and
the generalisation of the mode-forcing from [../done/coverage-test-kind.md](../done/coverage-test-kind.md).

## Principle: scope vs. identity

- **Identity limits** (what a kind *is*) → baked into opp_repl signatures. Smoke is "boots and
  runs briefly" (`cpu_time_limit="1s"`); sanitizer runs briefly instrumented; speed builds
  `profile`. Not project choices.
- **Scope limits** (which configs, how many runs, which folder) → **project policy**, never
  baked in. opp_repl defaults to *run everything*; a project that needs less opts in.

## Baked-in defaults (opp_repl signatures — project-independent)

| Kind | Baked-in defaults |
|---|---|
| **all kinds** | `run_number=None` (all runs), all filters `None` (all configs / all sims) |
| smoke | `cpu_time_limit="1s"`, `mode="debug"` |
| sanitizer | `cpu_time_limit="1s"`, `mode="sanitize"` |
| speed | `mode="profile"` |
| coverage | `mode="coverage"` |
| feature | `concurrent=False`, `mode="debug"` |
| fingerprint | `ingredients_list=None` → check **all** ingredients present in the store; `mode="debug"` |
| chart | `filter=None` (all analysis files), `run_simulations=True`, `mode="debug"` |
| statistical / opp | `mode="debug"` |

Explicitly **not** baked in (these were INET-specific, now project policy): `run_number=0`,
`ingredients_list=["tplx"]`, any fingerprint filter, `chart filter=showcases`. `mode=debug`
and the smoke/sanitizer `cpu_time_limit="1s"` stay (identity, not scope).

## Project test configuration (`.opp`) — supported for ALL kinds

Per kind, a project may declare any of: **kwarg defaults**, **result store**, **baseline
repository** (+ ref + folder), **runner function**, and future aspects. Merged into the run as
if passed: `explicit caller kwarg > project default > signature default`. The merge happens
**inside opp_repl's `run_<kind>_tests`** (project resolved there), so opp_ci passes nothing
extra and stays generic; REPL/CLI get it too.

### DECISION — one untyped `test_parameters` dict, keyed by kind

A single `test_parameters = {kind: {aspect: value, …}}` on `SimulationProject`. **Untyped
nested dict, not a shared class** — test kinds are genuinely heterogeneous (some need a `store`,
some a `baseline` repo, some a `runner`, some only `defaults`), so a shared all-optional class
just gets in the way. Each kind's dict carries only the aspects it uses.

Recognised aspects (open-ended; add freely since untyped):
- `defaults` — literal kwargs merged as if passed: `{**params.get("defaults",{}), **explicit}`.
- `store` — expected-values file *in the project tree* (fingerprint/speed JSON).
- `baseline` — *external repo to check out* before the kind runs: `{repository, ref, folder}`
  (`folder` = mount point; replaces old `statistics_folder` / `media_folder`).
- `runner` — dotted `module:function` ref for project-specific kinds (validation).

A one-line load-time check on recognised aspect keys catches typos (untyped → otherwise silent).

### `inet.opp`
```python
test_parameters = {
    "smoke":       {"defaults": {"run_number": 0}},
    "sanitizer":   {"defaults": {"run_number": 0}},
    "feature":     {"defaults": {"run_number": 0}},
    "speed":       {"defaults": {"run_number": 0}, "store": "tests/speed/store.json"},
    "fingerprint": {"store": "tests/fingerprint/store.json"},   # ingredients derive from store
    "statistical": {"defaults": {"run_number": 0},
                    "baseline": {"repository": "inet-framework/statistics", "ref": "main", "folder": "statistics"}},
    "chart":       {"defaults": {"filter": "showcases"},
                    "baseline": {"repository": "inet-framework/media", "folder": "media"}},
    "validation":  {"runner": "inet.test.validation:run_validation_tests"},
    # coverage, opp: nothing — inherit baked-in defaults
}
```

## Residual differences NOT solved by config

These live outside the in-process test runner, so no kwargs/config merge reaches them:
- **D3 instrumented build mode.** opp_ci's separate `opp_build_project --mode X` (a container
  subprocess, kind-unaware) must still be *told* sanitize/profile/coverage. opp_ci reads the
  kind's intended mode (from `tests[kind].defaults["mode"]` or a small map) to drive the build +
  collapse the modes axis to one job. opp_repl must also widen the argparse `--mode` choices to
  include `sanitize`/`profile` (coverage already added).
- **D5 baseline checkout.** `tests[kind].baseline` is a *declarative* pointer; opp_ci must clone
  the repo (pinned to ref) into the folder before chart/statistical run. Absence → clear stage
  failure, not a silent all-`only_current` FAIL.

## Out of scope (not a default/config change)
- Per-kind **sharding** (CI's fingerprint 4-way, feature 16-way) — needs a split axis.
- **opp-kind taxonomy** — unit/module/packet/queueing/protocol are `.test` suites = the `opp`
  kind, folder-scoped.
- **`all` / `release` meta-kinds** through opp_ci matrices (per-sub-test modes can't be one `--mode`).

## Related quick fix
- Register the missing `opp_run_opp_tests` console script (same as `opp_run_coverage_tests`).

## Steps

Implemented on branch `topic/align-test-defaults` (opp_repl + opp_ci worktrees; committed, not pushed):
- [x] opp_repl: drop `run_number=0` (→ `None`/all runs) from smoke/sanitizer/statistical/speed;
      fingerprint `ingredients_list=None` → all store-present ingredients (store-gated). `["tplx"]`
      and chart-filter defaults removed; `mode`/`cpu_time_limit` identity defaults kept. *(feature's
      `run_number=0` left untouched — it's hardcoded internal task-builder logic, not a kind default.)*
- [x] opp_repl: `SimulationProject.test_parameters` (untyped dict) + load-time aspect-key check +
      module-level `apply_project_test_defaults` merged at the top of each `run_<kind>_tests`
      (feature/chart import it lazily to dodge the `test/*`↔`simulation.compare` cycle). Flat attrs
      (`fingerprint_store`, `statistics_folder`, `media_folder`, `speed_store`,
      `validation_test_runner`) derived from `test_parameters` for back-compat. `get_test_baseline(kind)`.
- [x] `inet.opp`: per-kind config in `test_parameters` (run_number=0 for single-run kinds;
      chart filter=showcases; statistical baseline repo `inet-framework/statistics`).
- [x] opp_repl: `--mode` accepts `sanitize`/`profile`; `opp_run_opp_tests` console script registered.
- [x] opp_ci: `KIND_FORCED_MODE` (coverage/sanitizer/speed) drives `run_test` build/run mode and
      `expand_matrix` coordinate + modes-axis collapse (D3).
- Verified: test_parameters mechanism + compat attrs + merge precedence + aspect validation +
  inet.opp parse (in the working installed import order); expand_matrix collapse for all three
  instrumented kinds. (PYTHONPATH cold-import of opp_repl hits a **pre-existing** order quirk —
  `omnetpp.test.OppTest` via `documentation`-first — unrelated to these changes.)

D5 (baseline provisioning) — implemented for both execution paths:
- [x] Resolution + host checkout: `_resolve_test_baseline` reads the project's
      `test_parameters[kind]["baseline"]` (via opp_repl); `_checkout_baseline_on_host` clones/fetches
      `repository@ref` into `<root>/<folder>` (CHECKOUT stage). No-op (no stage) when the kind
      declares no baseline repo; checkout failure skips build+test → ERROR (no silent all-`only_current`).
- [x] **opp_env host path** — `_provision_test_baseline` runs before the build.
- [x] **podman bind-mount path** (SimulationProject w/ opp_file) — host checkout into the mounted
      tree before the container runs.
- [x] **podman catalog path** (bundled inet/omnetpp) — an in-container CHECKOUT stage inserted
      before PROJECT_BUILD: an idempotent `git clone/fetch + checkout` run via the entry script's
      `opp_env run -c` in the install dir, so it lands in `<install_dir>/<folder>`. A failed stage
      aborts the rest (`_run_podman_staged`). Unit-verified (shell shape, URL forms, stage insertion,
      host clone+checkout).

Remaining:
- [x] Confirm the **chart media-baseline repo**: `inet-framework/media`, checked out into the
      inet `media/` folder (feeds `media_folder`). Set in `inet.opp`.
- [ ] Live verification: a real chart/statistical run (host + podman) that clones the baseline and
      produces real diffs; each kind end-to-end vs current INET CI PASS/FAIL counts (replace-legacy
      Phases 2–3).
