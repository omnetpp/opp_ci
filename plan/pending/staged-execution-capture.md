# Staged, live execution capture

Capture a run's executed commands and their stdout/stderr — **inside and
outside podman** — organised into ordered **stages** (container prepare,
bootstrap, dependency install, compilation, test run, …), and stream them
**live** to the run-detail page as the run progresses.

> **Status:** Phases 1 + 3 done and tested (`opp_ci/stages.py`, executor
> build/test split, worker stage events, stage-aware `run_output.py`,
> `TestRunStage`, run-detail live + finished views).
>
> **Phase 2 (podman option b): lifecycle implemented, NOT yet validated on a
> real podman host.** `_run_test_in_podman` now starts the container detached
> (`--entrypoint sleep`) and drives it with separate `podman exec`s — a
> `runner.bootstrap` stage (entry script `--bootstrap-only`) then one or more
> run stages (`--skip-bootstrap` + args) — with a guaranteed `podman rm -f`
> teardown in a `finally`. The entry scripts gained `--bootstrap-only` /
> `--skip-bootstrap` modes. The in-container build/test split is implemented
> for the **nix** path (project.build = `opp_build_project`, test.run = the
> test command with `--no-build`, as two `opp_env run` execs over the
> persistent workspace; a build failure skips test); the **host** path stays
> one combined `test.run` because its `internal run-direct` builds + tests in
> a single shared-worktree process the host recorder can't see split.
> The image build/bake is wrapped as a `container.prepare` stage (its build
> output streams into the stage; near-silent when layer-cached). A nix podman
> run records: container.prepare (omnetpp baked here) → runner.bootstrap →
> deps.install (project install/resolve) → project.build (opp_build_project) →
> test.run. deps.install front-loads the idempotent `opp_env run --install`
> (build/test commands unchanged); an install failure is attributed there and
> skips build+test. Lifecycle / teardown / skip / split / prepare logic is unit-tested
> with mocked podman (`tests/test_podman_staged.py`), but **the real container
> run needs validation on a podman host, and images must be rebuilt** to pick
> up the new entry scripts.

Builds directly on the remote-worker log view (see
`plan/done/remote-worker-log-view.md`):
- Feature 1 (worker process log shipping) is unaffected — it's the
  daemon's-eye view and stays as-is.
- Feature 2 (flat live per-run output: `on_output` → `_RunOutputStreamer`
  → `RunOutputStore` → run-detail tail) is the spine this **evolves into**
  a stage-aware version. We grow that mechanism rather than add a parallel
  one.

---

## Why today's capture can't be staged

Two asymmetric vantage points:

- **Outside podman** — every shell-out goes through
  [`run_external`](../../opp_ci/executor.py) (`stream=True`), which already
  logs `$ <argv>` and tees each output line. Command + output + exit are
  captured *per call*, with a label. The host path is a sequence of these.
- **Inside podman** — the worker issues **one**
  `podman run … opp_ci <args>` ([executor.py:1229](../../opp_ci/executor.py)).
  The entrypoint ([opp_ci_entry.sh.j2](../../opp_ci/podman/opp_ci_entry.sh.j2),
  [opp_env_entry.sh.j2](../../opp_ci/podman/opp_env_entry.sh.j2)) then runs
  repo-sync → pip-install → `opp_env install` → `opp_env run -c "opp_ci …"`
  (which compiles *and* runs) — all in one process tree feeding one stdout.
  The host sees a single opaque `[podman:<image>]` blob.

And on **every** path, "compilation" is never its own command — it's buried
inside `opp_env run`/opp_repl.

## Two unlocks that make staging cheap

1. **Compilation as an explicit step.** `opp_build_project` is already a
   first-class command (`COMMAND_MAP["build"]`), and the opp_repl test
   runners accept `build=False`
   ([feature.py](../../../opp_repl/test/feature.py) uses it;
   [simulation/build.py:128](../../../opp_repl/simulation/build.py) is the
   standalone builder). So we **build first, then run with `build=False`** —
   the build stage captures the compile (+ exit), the test stage is a clean
   run with no rebuild noise. Per-stage timing and clean failure attribution
   (compile fails → test never starts) fall out for free. No opp_repl
   internals instrumented.

2. **Host drives every stage** (the podman decision, "option b"). Instead of
   one `podman run`, keep a **long-lived container** and issue one
   `podman exec` per stage. Each in-container stage is then *literally a host
   `run_external`* — captured by the exact same machinery as host-path
   stages. No in-container marker protocol, no stream parsing, and because
   the host launches each stage it **knows** the live stage boundaries
   instead of having to detect them.

Net: "inside podman" and "outside podman" collapse to the same capture path
— a sequence of host-run commands, each a stage.

---

## Stage model

A run becomes an ordered list of stages. Each stage:

- `name` — canonical id (taxonomy below)
- `ordinal` — order within the run
- `command` — the argv(s) executed (from `run_external`'s existing `$ argv`)
- `status` — `pending | running | passed | failed | skipped`
- `exit_code`
- `started_at` / `finished_at` → duration
- `output` — the captured lines; each tagged with its stream (stdout/stderr)
  so the UI can mark stderr while keeping them interleaved, and tagged as a
  command line vs ordinary output

Run-level result derives from the stages: the run fails at the first failed
required stage; `test.run`'s outcome maps to the existing `TestResultCode`.

**Taxonomy** (ordered; some stages are path-specific):

| stage | when | how captured |
|-------|------|--------------|
| `container.prepare` | podman only | host `run_external` (`podman build`/bake/commit) |
| `runner.bootstrap` | podman only | `podman exec` repo-sync + `pip install -e` |
| `checkout` | if `git_ref` | host worktree create (host path) / mount (podman) |
| `deps.install` | all | `opp_env install` (host: `install_project`; podman: exec) |
| `project.build` | all | `opp_build_project` (host: `opp_env run -c`; podman: exec) |
| `test.run` | all | test command with `build=False` |
| `cleanup` | all | worktree/scratch/container teardown |

Known coarseness to accept for v1: when omnetpp itself is compiled from Nix
(fresh, non-baked image), that compile lands in `deps.install`, not
`project.build`. Splitting omnetpp-compile from project-compile is out of
scope.

## Per-path orchestration

`run_test` gains a stage-driving orchestrator. A small `StageRunner` helper
owns: open a stage → run its command(s) → tee output (tagged with the stage)
→ close with exit/status → emit live events. All three paths feed it.

- **Direct (in-process)** — `checkout` → `project.build` (call
  `build_project`) → `test.run` (call `run_<kind>(build=False)`). Capture by
  redirecting stdout/stderr per stage, reusing Feature 2's `_CallbackStringIO`
  tee but scoped to the open stage.
- **Host-nix** — `deps.install` (`opp_env install`, today's
  `install_project`) → `project.build` (`opp_env run <pins> -c
  "opp_build_project"`) → `test.run` (`opp_env run <pins> -c "<test>"`,
  build=False). Each is a `run_external(stream=True)`; just tag the stage.
- **Podman (option b)** — host-driven container lifecycle:
  1. `podman run -d --name <run-scoped> [mounts/worktree] <image> sleep infinity`
  2. `podman exec` per stage, **in order**, each a captured `run_external`
  3. `podman rm -f <name>` in a `finally` — guaranteed teardown

  To avoid re-implementing the entrypoint's project-resolution logic in
  Python, **split the monolithic entrypoint into per-stage scripts** shipped
  in the image (`stage_bootstrap.sh`, `stage_deps.sh`, `stage_build.sh`,
  `stage_test.sh`). The host execs them one at a time — the gnarly shell
  logic (FIRST_PROJECT resolution, `PINNED_PROJECTS`/`EXTRA_PROJECTS`
  assembly, cd into the install dir) stays in tested shell, but each script
  is now a separately-invoked, separately-captured stage. Each script runs
  `set -x` so the individual shell commands it executes are echoed and
  captured (satisfies "capture executed commands inside podman").

This is the main cost of (b): the container model moves from one-shot
`run`+entrypoint to detached-`run`+`exec`-per-stage, and the entrypoint is
decomposed into per-stage scripts. The Containerfiles' `ENTRYPOINT` becomes a
no-op idle (`sleep infinity`) and `WORKDIR`/mounts are unchanged.

## Live transport (evolve Feature 2)

Reuse Feature 2's path end to end, made stage-aware:

- The `on_output(line)` callback generalises to an **event** stream. The
  orchestrator emits, in order: `stage_begin{name, ordinal, command}`,
  `output{stage, stream, text}` (many), `stage_end{name, exit, duration,
  status}`. Because the host drives every stage, these are *known*, not
  parsed.
- Worker [`_RunOutputStreamer`](../../opp_ci/worker.py) ships batched
  **events** (not just lines) to `POST /api/runs/{id}/output-append`, keyed
  by run, every flush interval — same best-effort, own-session design.
- Coordinator [`RunOutputStore`](../../opp_ci/run_output.py) grows into a
  **stage-aware store**: per run, an ordered list of stages each with
  status/exit/timing and a bounded ring of output lines, plus a monotonic
  seq cursor. Dropped on result (as today), LRU-capped over runs.
- `GET /test-runs/{id}/output/tail` returns the stage structure +
  new-since-cursor lines + `done`; the run-detail card
  ([run_detail.html](../../opp_ci/web/templates/run_detail.html)) renders it
  per the UI spec below. On `done`, reload to the persisted view (same
  rendering, sourced from `TestRunStage`).

## Run-detail UI (live + finished)

Same rendering for the live view and the finished/persisted view — only the
data source differs (live store vs `TestRunStage`).

- **Layout = terminal-like, stage-segmented.** Each stage is a collapsible
  section; the header shows name + status badge + exit code + duration.
  Auto-open rules: the running stage is open and follow-tails; passed stages
  collapse; a **failed stage auto-opens** so you land on the problem.
- **Top stage stepper.** A progress rail —
  `prepare ▸ bootstrap ▸ deps ▸ build ▸ test` — each segment status-coloured,
  click to jump to that stage. The live overview.
- **stdout/stderr interleaved, stderr marked.** One chronological stream per
  stage (exact ordering within a stream; approximate across, since they're
  teed on separate threads). stderr lines carry a subtle red tint via
  background/left-border — *not* foreground — so tools' own ANSI colours
  (rendered through `_ansi_to_html`) still read. Optional "stderr only"
  filter.
- **Commands inline as collapsible prompt lines.** Each executed command
  renders as a bold/green `$ <argv>` line followed by its output, like a
  shell. Each command is itself a collapsible wrapper (summary = the argv +
  a `(N lines)` count, body = its output).
- **"Commands only" toggle.** Collapses every command wrapper at once, so a
  stage shows just its `$ …` outline; click any command to drill into its
  output. (This is what the command-as-wrapper structure buys.)
- **Colour summary.** Preserve tool ANSI; add semantic colour only as
  background/border tints + the prompt/header colours, so the two never
  fight: command = bold/green prompt, stderr = red tint, stage header =
  status colour (green pass / red fail / blue running).

Nice-to-haves (not v1, but the event/timing data already supports them): a
per-command **timing gutter** (elapsed Δt → spot the slow step), per-stage
and whole-run **raw/download** links, an **errors-only filter**, a **sticky
stage header** while scrolling a long stage, and **deep-link anchors** per
stage.

## Final persistence

At result time the worker reports the assembled stage tree; persist it
straight into a `TestRunStage` child table (run_id, ordinal, name, status,
exit_code, started_at, finished_at, output) — queryable from day one ("runs
that failed in compile this week", per-stage dashboards). No JSON-blob
stepping-stone: schema changes are fine here — we **reset (nuke) the database
when the model changes** rather than write migrations.

`TestRun.stdout/stderr` can be dropped in favour of the per-stage output, or
kept as a derived concatenation if anything still reads them — decide when we
get there.

## Phasing (de-risked: prove the spine before the podman lifecycle change)

1. **Stage spine on host-nix + direct paths.** Introduce `StageRunner`,
   split build from test (`opp_build_project` + `build=False`), tag
   `run_external` with stages, extend the Feature 2 transport/store/endpoint
   to events, render the staged live card. Delivers staged live logs for the
   non-podman paths and builds all the shared machinery.
2. **Podman path via option (b).** Detached container + per-stage `exec` +
   per-stage scripts + guaranteed teardown. Reuses phase-1 machinery.
3. **Final persistence.** `TestRunStage` child table + staged render for
   finished runs. Nuke + recreate the DB for the schema change.

## Testing

- `StageRunner`: stage lifecycle (begin/output/end), exit→status mapping,
  abort-remaining-stages on a required-stage failure, per-stage timing.
- Build/test split: `project.build` runs `opp_build_project`; `test.run`
  uses `build=False`; a build failure marks build failed and skips test.
- Event transport: worker batches stage events + lines; coordinator store
  assembles stages, cursor round-trips, drop-on-result, old flat-output
  workers still handled (mixed fleet).
- Tail/endpoint: stage structure + incremental lines + `done`; html/ansi
  escaping per line preserved.
- Podman (option b), integration on a host with podman: detached container
  comes up, each stage exec is captured as its own stage, teardown runs even
  when a stage fails (no leaked container).
- Per-stage scripts: project resolution / pins parity with the old
  entrypoint (golden-output or unit-level shell tests).

## Open questions / risks

- **Entrypoint logic moving host-ward.** Mitigated by keeping it in
  per-stage shell scripts the host execs, rather than porting to Python —
  but the split itself needs careful parity testing against today's
  entrypoint.
- **Container teardown reliability.** `finally` + a reaper sweep for leaked
  `opp_ci`-labelled containers (worker crash mid-run). Name containers by
  run id so leaks are identifiable.
- **Extra `opp_env run` env setups.** Build and test are now two
  `opp_env run -c` invocations instead of one → env is evaluated twice.
  Cheap (deps cached) but non-zero; measure.
- **`build=False` correctness.** The test stage must find the build stage's
  artifacts — guaranteed only if both run in the *same* workspace/container
  (they do: same host workspace dir; same detached container). Verify no
  path/mode mismatch silently triggers a rebuild.
- **Mixed-version fleet.** A coordinator may get staged events from new
  workers and flat output from old ones; the store/endpoint/UI must handle
  both during rollout.
- **Output volume.** Per-stage ring bounds; a noisy compile shouldn't evict
  the test stage. Size rings per stage, not per run.
