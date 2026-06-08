# Plan: make mm1k project testing work end-to-end

## Status: ✅ DONE (2026-06-08)

mm1k **build → smoke → opp** all PASS on the live coordinator under
`--isolation podman --toolchain none` (gcc-13 / Ubuntu 24.04 / omnetpp 6.4.0).
The opp run's `Validation.test` reports `PASS` (M/M/1/K validation).

The original root cause (the chart/matplotlib import) was only the first of
six, each found empirically by running the pipeline and reading the next
failure. Final fixes (diverged from the initial chart-guard idea — matplotlib
is now *baked into the runner*, the import left intact):

| Failure | Fix | Commit |
|---|---|---|
| `No module named matplotlib` | bake `opp_repl[all]` into host runner image (`Containerfile.host.j2`) | opp_ci `f2fd2ed` |
| omnetpp pin not reaching podman | forward `--pin` over `--remote` → server-side `resolved_deps` | opp_ci `6dd4d89` |
| `no subuid ranges for opp_ci` | provision rootless podman in `install.sh` (subuid/subgid + linger) | opp_ci `f4566a6` |
| `No C++ compiler found` | map gcc→`g++-N` / `gcc-c++` in `packages.yml` + fallback | opp_ci `03c99e7` |
| `Which version of 'mm1k'?` | split catalog name (install `mm1k-latest`, run opp_repl as bare `mm1k`) + entrypoint dir-glob | opp_ci `ba476b8` |
| `cannot find -lmm1k_dbg` | build mm1k as shared lib via `.oppbuildspec` + `library_folder="."` | omnetpp/mm1k `599a504` |

Host-side (operator) one-time setup: subuid/subgid + linger for `opp_ci`,
`/etc/sudoers.d/opp_ci-deploy`, and `systemd-journal`/`adm` group membership.

Latent gaps surfaced but **not** addressed (candidates for follow-up): a
one-off `opp_ci run` can't carry `--opp-file` or set the run `version`; the
`isolation=none` path can't read a project under `/home/levy` (0750); and
opp_repl's makefile build ignores `dynamic_libraries` (needs `.oppbuildspec`).

## Context

`mm1k` is a small M/M/1/K OMNeT++ project used as the testbed for opp_ci.
The goal: get its full test pipeline — **build → smoke → opp** (the three
kinds in [bin/recreate-mm1k](../../bin/recreate-mm1k))
— passing on the live coordinator (`ci.omnetpp.dev`, host `85.17.192.192`),
driven from this machine.

**Current state (observed via the coordinator API on the host):** all three
existing `mm1k` `build` runs (ids 1–3) finished in milliseconds with
`result_code=ERROR` and stderr `No module named 'matplotlib'`. Nothing has
ever actually built.

### Root cause (confirmed)

1. matplotlib is in opp_repl's **optional** extras (`chart`, `optimize`) in
   [opp_repl/pyproject.toml](../../../opp_repl/pyproject.toml#L54),
   not its core deps.
2. The deployer installed opp_repl **without extras** —
   `pip install -e /opt/opp_repl` at
   [packaging/systemd/install.sh](../../packaging/systemd/install.sh) — so
   matplotlib/scipy were absent in the host venv. (Confirmed: only
   `matplotlib-inline` is present in `/opt/opp_ci/.venv`.) **Now fixed** by the
   user (installs `opp_repl[all]`); the observed ERROR runs (ids 1–3) used
   `isolation=none`, i.e. the host-venv path. The podman path has a *separate*
   no-extras gap in its entrypoint — see "How code reaches the podman container".
3. opp_ci's [opp_ci/executor.py:91](../../opp_ci/executor.py#L91)
   `_get_test_functions()` **eagerly** does `from opp_repl.test.chart import
   run_chart_tests`, and [chart.py:12](../../../opp_repl/opp_repl/test/chart.py#L12)
   does an unguarded `import matplotlib`. The map is built as one literal for
   **every** kind, so `build`/`smoke`/`opp` all die at import time before doing
   any work.

Verified that *only* the chart import pulls matplotlib — `smoke`, `opp`,
`release`, `all`, `simulation.build` all import cleanly without it.

**Resolution direction (per user):** matplotlib is *supposed* to always be
present on the runner — so the eager chart import in `executor.py` stays as-is.
The bug is that the **runner image never installs opp_repl's `[all]` extras**.
Fix it where the deps belong: bake them into the image.

## Operating constraint

Per the user, the verify loop is **report-then-approve on every round-trip**:

- After each round-trip (deploy → run on the host → read the result), **stop
  and report to the user**: what went wrong, and the specific change proposed
  to fix it.
- **Every change requires independent approval, regardless of where it lands**
  — local `~/workspace/opp_ci`, `opp_repl`, `mm1k`, `install.sh`, a matrix
  definition, or anything on the remote host. No change is pre-authorized by
  virtue of being "just a local edit."
- Remote-touching actions (syncing code to the host, running `install.sh`,
  restarting services, submitting jobs) are likewise proposed and approved
  before each execution.

## Fix (local opp_ci repo only)

### 1. Bake opp_repl `[all]` deps into the runner image — `opp_ci/podman/Containerfile.host.j2`

The host-template Containerfile creates `/opt/opp_ci_venv` with **only opp_env**
baked in ([Containerfile.host.j2:64-67](../../opp_ci/podman/Containerfile.host.j2#L64));
opp_repl's third-party deps are never installed at build time, and the
entrypoint's runtime `pip install -e /opt/opp_repl_src`
([opp_ci_entry.sh.j2:29](../../opp_ci/podman/opp_ci_entry.sh.j2#L29)) omits
`[all]`. Containers run `--rm`, so adding `[all]` to the entrypoint would
reinstall matplotlib/scipy every job — instead **bake the deps at build time**,
next to opp_env:

```dockerfile
RUN python3 -m venv /opt/opp_ci_venv \
 && /opt/opp_ci_venv/bin/pip install --upgrade pip \
 && /opt/opp_ci_venv/bin/pip install \
        git+https://github.com/omnetpp/opp_env.git@{{ opp_env_ref }} \
        "opp_repl[all] @ git+https://github.com/omnetpp/opp_repl.git"
```

This pulls opp_repl's `[all]` extras (matplotlib, numpy, scipy, …) into the
image venv once. The entrypoint's editable `pip install -e /opt/opp_repl_src`
then overlays the latest code — deps already satisfied, fast no-op. Matches the
`opp_repl[all]` choice already made in `install.sh`. (Mirror into
`Containerfile.nix.j2` for parity; out of scope for the mm1k `toolchain=none`
path but worth doing in the same change.)

**Deploy note:** this is an **image-level** change. `opp_ci image build`
renders the Containerfile from the **host's installed opp_ci package**
(`importlib.resources.files("opp_ci")`), so the `.j2` edit must reach
`/opt/opp_ci` (rsync + `install.sh`) and then the image must be **rebuilt** —
it is *not* delivered by the container's git-pull-from-main channel (that
carries runtime opp_ci/opp_repl *code*, not image templates).

### 2. `packaging/systemd/install.sh` — install opp_repl with extras — ✅ DONE (by user)

install.sh now installs each sibling with its extras (`opp_repl[all]`), so the
**host venv** at `/opt/opp_ci/.venv` gets matplotlib/scipy/etc.

**Important scope caveat:** this fixes the **host venv / `isolation=none`**
path only. Our runs use `isolation=podman`, and the podman container **does not
use install.sh or `/opt/opp_ci`'s venv** — see the next section. The equivalent
extras gap on the runner is closed by fix #1 (baking `opp_repl[all]` into the
image).

## Deploy + verify loop (report-then-approve on every round-trip)

Runs use **`--isolation podman`**: each job executes
`opp_ci internal run-direct --project mm1k --kind <kind>` **inside an
`opp-ci-runner` container** (see
[executor.py `_run_test_in_podman`](../../opp_ci/executor.py#L587)), with the
mm1k tree bind-mounted at `/work`.

### How code reaches the podman container (critical)

The container's entrypoint
([opp_ci/podman/opp_ci_entry.sh.j2](../../opp_ci/podman/opp_ci_entry.sh.j2))
runs **at every container start** and:
1. **Clones/fetches opp_ci and opp_repl from `github.com/omnetpp/{opp_ci,opp_repl}`
   and `git reset --hard origin/HEAD`** — i.e. always upstream **`main`**, *not*
   `/opt/opp_ci` and *not* `~/workspace`. (Both repos' `origin` is
   `git@github.com:omnetpp/opp_*.git`, branch `main`.)
2. `pip install -q -e /opt/opp_ci_src /opt/opp_repl_src` — **no extras**, so
   matplotlib is absent in the container.

Consequences:
- **Code changes reach the container only by committing + pushing to
  `omnetpp/opp_ci` (and/or `opp_repl`) `main`.** No image rebuild is needed for
  pure code changes — the entrypoint fetches on the next run (that's the whole
  point of the clone-at-startup design).
- The container's runtime `pip install -e opp_repl_src` omits `[all]`, so
  matplotlib is missing → **fix #1 bakes `opp_repl[all]` into the image** so the
  runner always has it (the eager chart import in `executor.py` is left intact).
- The **image** needs (re)building for: first creation, an omnetpp version
  change, or a Containerfile/entrypoint change (**including fix #1**) — not for
  everyday runtime code edits, which the entrypoint re-pulls from `main`.

**Delivery channel (decided):** iterate by committing and **`git push origin
main`** to `omnetpp/opp_ci` (and `opp_repl` when its code changes). The
container's `git reset --hard origin/HEAD` picks it up on the next run — no
image rebuild for code-only changes. Each push is an approval gate.

Toolchain is **`toolchain=none`** (matching the existing podman matrix in
[bin/recreate-mm1k](../../bin/recreate-mm1k)). Under podman, `toolchain=none`
resolves to the **"host" image template**
([executor.py:477](../../opp_ci/executor.py#L477)), which **bakes a specific
omnetpp version into the image** at build time via
`opp_env install --nixless-workspace` — so omnetpp *is* present in the runner.
Two requirements follow:
- The `opp_ci image build` must pin an omnetpp version (e.g. `6.4.0`); that
  version is part of the image tag and is installed at build time.
- **Every run must carry an omnetpp version** in `resolved_deps['omnetpp']`,
  else [`_podman_image_tag` raises](../../opp_ci/executor.py#L416)
  ("isolation=podman with toolchain=none requires an omnetpp version"). Pass
  it on the run submission (matrix dep pin `omnetpp=6.4.0`, or `--pin
  omnetpp=6.4.0` on a one-off `opp_ci run`).

Run from the host (`ssh levy@85.17.192.192`), submitting via the coordinator
on `https://127.0.0.1:8443` with the submitter token. Results read back with
`GET /api/runs/{id}` (the installed CLI's `--remote list-runs` is *not* wired
to the API yet — that's the separate in-flight remote-cli-control work — so
read via the API directly).

Each round-trip is one iteration of this cycle, and the user approves at the
gates (⛔):

1. ⛔ **Propose the change** for this iteration (the fix below for round 1;
   the next failure's fix thereafter): say what went wrong and exactly what
   will change, *wherever it lands*. Apply only after approval.
2. ⛔ **Deliver the code** to the channel the run uses:
   - *Code change (e.g. fix #1):* commit + **`git push origin main`** on
     `opp_ci` (and/or `opp_repl`). The container fetches it on the next run; no
     image rebuild. (Also rsync→`install.sh` on the host if the host-venv /
     coordinator process needs the same change.)
   - *Image-level change* (first run ever, omnetpp version, or `.j2`/Containerfile
     edit): ⛔ **rebuild** the host-template image on the host —
     `opp_ci image build --toolchain none --distro "Ubuntu 24.04" --compiler
     gcc-13 --omnetpp-version 6.4.0` (slow first time — opp_env-installs omnetpp).
3. ⛔ **Restart** `opp_ci-serve` / `opp_ci-worker@local` only if the
   coordinator/worker *process* code changed (not needed for container-only
   code, which the entrypoint re-pulls per run).
4. **Sanity check** the fix is live in the container:
   `podman run --rm <runner-image> internal run-direct --project mm1k --kind build`
   imports cleanly (no `No module named 'matplotlib'`).
5. ⛔ **Run** the next kind with `--isolation podman --toolchain none` and an
   omnetpp pin (`--pin omnetpp=6.4.0`) → poll `GET /api/runs/{id}` until
   finished → inspect stdout/stderr/details.
6. **Report** the outcome and loop back to step 1 with the next fix, until
   build → smoke → opp are all green.

### Anticipated downstream failures (to triage as they surface)

- **runner image build** — the host-template image build runs
  `opp_env install` of omnetpp 6.4.0 at build time; expect a slow first round
  and possible opp_env / podman-rootless / base-image-pull issues.
- **missing omnetpp pin** — a run without `resolved_deps['omnetpp']` fails
  early in `_podman_image_tag`; ensure the pin is passed (see Toolchain above).
- **build** inside the container uses the omnetpp baked into the host-template
  image. If `opp_makemake`/`NEDPATH`/omnetpp libs are missing or mismatched,
  that's an image-build problem (wrong omnetpp version or a broken
  `--nixless-workspace` install), not an mm1k code change.
- **smoke** runs the simulation — needs the freshly built `mm1k` dynamic lib.
- **opp** runs [tests/Validation.test](../../../mm1k/tests/Validation.test)
  via `opp_test`; needs the omnetpp test tooling on PATH and `Validator.cc`
  compiled. The glob in
  [opp_repl/test/opp.py:133](../../../opp_repl/opp_repl/test/opp.py#L133)
  discovers `**/*.test`, so the single test file will be picked up.

## Verification (definition of done)

`GET /api/runs?project=mm1k` shows the latest `build`, `smoke`, and `opp`
runs all `result_code=PASS`. The `opp` run's details show `VALIDATION PASS`
from the M/M/1/K validator.
