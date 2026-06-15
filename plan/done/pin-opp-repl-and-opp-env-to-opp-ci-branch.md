# Plan: always source opp_repl and opp_env from their `opp_ci` branches

Goal: make **every** path that brings opp_repl or opp_env into a worker or
coordinator use the repo's **`opp_ci` branch**, and prefer *installing that
version into the process environment up front* (at service/worker start) over
fetching an arbitrary version at test time. Today this holds only partially for
opp_repl and not at all for opp_env.

Both branches exist on the remotes (verified):
`git+https://github.com/omnetpp/opp_repl.git@opp_ci` and
`git+https://github.com/omnetpp/opp_env.git@opp_ci`.

Two closely-related podman cleanups ride along, since they touch the same entry
scripts / `_run_podman_staged`: **§2.6** removes opp_ci from the runner images
entirely (the host path converges onto direct opp_repl console scripts, as the
nix path already does), and **§2.7** finally captures each podman run's
structured result into `TestRun.details` (today `None` for *all* container runs).

---

## 1. Current state — where each tool comes from

Every place either tool is sourced, and whether it already pins `opp_ci`:

### opp_repl

| # | Surface | File | Pins `opp_ci`? |
|---|---|---|---|
| R1 | uvx service env (worker + coordinator process) | [service.py:36-37](../../opp_ci/service.py#L36-L37), [service.py:218](../../opp_ci/service.py#L218) (`OPP_REPL_REF="opp_ci"`) | ✅ yes |
| R2 | NixOS modules | [worker.nix:20](../../opp_ci/data/nixos/worker.nix#L20), [coordinator.nix:23](../../opp_ci/data/nixos/coordinator.nix#L23) (`@opp_ci`) | ✅ yes |
| R3 | podman host image — baked `[all]` deps | [Containerfile.host.j2:69](../../opp_ci/podman/Containerfile.host.j2#L69) (`opp_repl[all] @ git+…opp_repl.git`, **no `@ref`**) | ❌ default branch |
| R4 | podman entry scripts — code clone each start | [opp_ci_entry.sh.j2:40](../../opp_ci/podman/opp_ci_entry.sh.j2#L40), [opp_env_entry.sh.j2:39](../../opp_ci/podman/opp_env_entry.sh.j2#L39) → `sync_repo … reset --hard origin/HEAD` ([:34](../../opp_ci/podman/opp_ci_entry.sh.j2#L34)) | ❌ default branch |

### opp_env

| # | Surface | File | Pins `opp_ci`? |
|---|---|---|---|
| E1 | worker host-nix `OPP_CI_OPP_ENV_CMD` | [service.py:286](../../opp_ci/service.py#L286) / [config.py:131](../../opp_ci/config.py#L131) / [worker.nix:38](../../opp_ci/data/nixos/worker.nix#L38) → `uvx --from opp-env opp_env` | ❌ **PyPI release** |
| E2 | coordinator dependency-lock + compat-matrix resolution | [dependency.py:49](../../opp_ci/dependency.py#L49), [dependency.py:84](../../opp_ci/dependency.py#L84) → hard-coded `["opp_env", …]`; coordinator env never sets the cmd ([render_coordinator_env](../../opp_ci/service.py#L261)) | ❌ **bare `opp_env` on PATH**, not even configurable |
| E3 | podman host + nix images | [executor.py:962](../../opp_ci/executor.py#L962), [executor.py:964](../../opp_ci/executor.py#L964) → `_resolve_remote_head(_OPP_ENV_REPO)` bakes **default-branch HEAD** into [Containerfile.host.j2:68](../../opp_ci/podman/Containerfile.host.j2#L68) / [Containerfile.nix.j2:68](../../opp_ci/podman/Containerfile.nix.j2#L68) | ❌ default-branch HEAD |

Why the coordinator needs opp_env (E2): `complete_lock_for_submit` runs on the
coordinator at submit time ([persistence.py:1054](../../opp_ci/persistence.py#L1054))
and calls `opp_env info … --raw` to build the transitive dependency lock and
the cross-project compatibility matrix. Today that silently degrades to a
partial/empty lock when `opp_env` is missing from the coordinator's PATH — the
"coordinator when needed" the request calls out.

The host-nix path on the worker invokes opp_env as `… run -w <ws> <pins> <proj>
-c "opp_ci …"` ([executor.py:1584](../../opp_ci/executor.py#L1584)); the inner
`opp_ci` resolves via the worker's PATH, so opp_env can live in any venv without
breaking that hand-off.

---

## 2. Design

### 2.1 Single source of truth for the refs

Centralise the repo URLs and branch names in [config.py](../../opp_ci/config.py)
(the lowest module — already imported by both `service` and `executor`):

```python
# opp_ci/config.py
OPP_REPL_REPO = "https://github.com/omnetpp/opp_repl.git"   # plain (git clone / ls-remote)
OPP_ENV_REPO  = "https://github.com/omnetpp/opp_env.git"
OPP_REPL_GIT  = "git+" + OPP_REPL_REPO                       # pip/uvx spec
OPP_ENV_GIT   = "git+" + OPP_ENV_REPO
OPP_REPL_REF  = os.environ.get("OPP_CI_OPP_REPL_REF", "opp_ci")
OPP_ENV_REF   = os.environ.get("OPP_CI_OPP_ENV_REF",  "opp_ci")
```

Making the ref env-overridable lets a dev or a topic build point at a different
branch without editing code; the default stays `opp_ci`.

Then:
- `service.py` imports `OPP_REPL_GIT`/`OPP_REPL_REF` from config (drop the local
  copies at [service.py:36-37](../../opp_ci/service.py#L36-L37)) and gains
  `OPP_ENV_GIT`/`OPP_ENV_REF`.
- `executor.py`'s `_OPP_REPL_REPO`/`_OPP_ENV_REPO`
  ([executor.py:894-895](../../opp_ci/executor.py#L894-L895)) reference the
  config constants.
- The j2 templates receive the branch via render context; the static `.nix`
  module files keep the literal `opp_ci` string (they already do for opp_repl).

### 2.2 opp_env is bundled into the worker/coordinator uvx env (E1, E2)

opp_repl is already **bundled into the worker/coordinator's own uvx env** via
`--with` and refreshed on every service restart with `--refresh-package`
([service.py:207-228](../../opp_ci/service.py#L207-L228)). opp_env is the
outlier: a *separate* `uvx --from opp-env opp_env` per shell-out.

**Decided: bundle opp_env into the same uvx env as opp_ci + opp_repl.** Add
`--with "opp-env @ {OPP_ENV_GIT}@{OPP_ENV_REF}"` and a matching
`--refresh-package opp-env` to `uvx_argv`
([service.py:218-227](../../opp_ci/service.py#L218-L227)) for **both** roles,
and set `OPP_CI_OPP_ENV_CMD=opp_env` so the host-nix path and the coordinator's
dependency resolver both reach the bundled console script (on PATH for the
process and its children). The resulting `uvx_argv` for each role:

```
<uvx> --from "opp_ci[<extras>] @ {OPP_CI_GIT}@<ref>" \
      --with "opp_repl[all] @ {OPP_REPL_GIT}@{OPP_REPL_REF}" \
      --with "opp-env @ {OPP_ENV_GIT}@{OPP_ENV_REF}" \
      --refresh-package opp_ci --refresh-package opp_repl --refresh-package opp-env \
      opp_ci <coordinator start | worker start>
```

This is the most literal reading of "installed with that version in the worker …
and the coordinator": one env, resolved once at start, refreshed each restart,
identical mechanism to opp_repl, zero per-test fetch. It also removes the only
remaining place we pull opp_env from PyPI.

**Risk to watch:** opp_env and `opp_repl[all]` must co-resolve in a single venv.
If their pins conflict, the fallback is to keep opp_env in its own isolated uvx
tool env and repoint only its source —
`OPP_CI_OPP_ENV_CMD="uvx --from \"opp-env @ {OPP_ENV_GIT}@{OPP_ENV_REF}\" opp_env"`
— with a warm-refresh at service start to get "latest of `opp_ci` each restart".
Phase 2 should `uvx` resolve both roles once on a real host before wiring the
env, to confirm there is no conflict.

### 2.3 Coordinator honours the configured opp_env command (E2)

`dependency.py` hard-codes `["opp_env", …]`. Route both call sites
([dependency.py:48-49](../../opp_ci/dependency.py#L48-L49),
[dependency.py:83-84](../../opp_ci/dependency.py#L83-L84)) through a shared
argv builder so the configured command (and thus the `opp_ci` branch) is used:

- Lift `_opp_env_cmd()` ([executor.py:481-490](../../opp_ci/executor.py#L481-L490))
  to a neutral home (e.g. `config.opp_env_argv()` or a tiny `opp_env_adapter`
  helper) so `dependency.py` can call it **without importing the heavy
  `executor` module**; have `executor._opp_env_cmd` delegate to it.
- `dependency.query_opp_env_info` / `query_opp_env_versions` become
  `opp_env_argv() + ["info", project_version, "--raw"]`.
- Under Option A bare `opp_env` already resolves to the bundled binary, but
  routing through the helper keeps Option B and bare-metal working too.

### 2.4 podman images and entry scripts (R3, R4, E3)

- **opp_env baked SHA (E3):** resolve the **`opp_ci` branch** head instead of
  default HEAD — `_resolve_remote_head(cfg.OPP_ENV_REPO, cfg.OPP_ENV_REF)` at
  [executor.py:962](../../opp_ci/executor.py#L962) and
  [:964](../../opp_ci/executor.py#L964). `_resolve_remote_head` already accepts
  a `ref` arg ([executor.py:876](../../opp_ci/executor.py#L876)); `git ls-remote
  <url> opp_ci` returns the branch SHA. Image cache invalidates only when the
  branch moves — the intended behaviour.
- **opp_repl baked deps (R3):** pin `Containerfile.host.j2:69` to
  `@{{ opp_repl_ref }}`, passing `opp_repl_ref = cfg.OPP_REPL_REF` through the
  render context in `render_containerfile`
  ([executor.py:941-964](../../opp_ci/executor.py#L941-L964)). (This layer only
  supplies the `[all]` dependency set; the code itself is re-cloned at start —
  next item — so this mainly keeps the baked deps consistent.)
- **Entry-script clone (R4):** teach `sync_repo` in both entry scripts
  ([opp_ci_entry.sh.j2](../../opp_ci/podman/opp_ci_entry.sh.j2#L26-L36),
  [opp_env_entry.sh.j2](../../opp_ci/podman/opp_env_entry.sh.j2#L26-L36)) a third
  `ref` argument and `fetch`/`reset` to it, then clone **opp_repl only** at
  `opp_ci`:
  ```sh
  sync_repo() {        # name url ref
      name=$1; url=$2; ref=${3:-HEAD}; dir=/opt/${name}_src
      if [ ! -d "$dir/.git" ]; then
          run_cmd git clone --depth 50 --branch "$ref" "$url" "$dir"
      else
          run_cmd git -C "$dir" fetch --depth 50 origin "$ref"
          run_cmd git -C "$dir" reset --hard FETCH_HEAD
      fi
  }
  do_bootstrap() {
      sync_repo opp_repl {{ opp_repl_repo }} {{ opp_repl_ref }}   # = opp_ci
      run_cmd pip install -q -e /opt/opp_repl_src
  }
  ```
  opp_ci is **no longer cloned or installed** in either image — see §2.6. Render
  `opp_repl_ref="opp_ci"` (and the repo URL) into both entry templates. `--branch`
  accepts a branch name, which is what `opp_ci` is.

### 2.5 Service env + NixOS modules

- `render_worker_env` ([service.py:274-289](../../opp_ci/service.py#L274-L289)):
  set `OPP_CI_OPP_ENV_CMD=opp_env` (the bundled binary).
- `render_coordinator_env` ([service.py:261-271](../../opp_ci/service.py#L261-L271)):
  **add** `OPP_CI_OPP_ENV_CMD=opp_env` (today absent) so coordinator dependency
  resolution uses the bundled `opp_ci`-branch opp_env.
- NixOS: `worker.nix` `oppEnvCmd` default ([worker.nix:38](../../opp_ci/data/nixos/worker.nix#L38))
  and the coordinator module → `opp_env`; add the opp_env `--with` +
  `--refresh-package opp-env` to the shared ExecStart builder for both roles
  ([worker.nix:20](../../opp_ci/data/nixos/worker.nix#L20),
  [coordinator.nix:23](../../opp_ci/data/nixos/coordinator.nix#L23)).

### 2.6 Remove opp_ci from the podman images entirely

opp_ci is used inside a container in exactly one place: the **host-toolchain**
entry script runs `opp_ci internal run-direct`
([executor.py:1391](../../opp_ci/executor.py#L1391),
[opp_ci_entry.sh.j2:111](../../opp_ci/podman/opp_ci_entry.sh.j2#L111)). The
**nix** path never invokes opp_ci — it drives opp_repl's own console scripts
(`opp_build_project`, `opp_run_*_tests`) through `opp_env run -c`
([executor.py:1369-1373](../../opp_ci/executor.py#L1369-L1373)). Converge the
host path onto that same model and opp_ci drops out of both images.

`opp_ci internal run-direct` → `_run_test_direct`
([executor.py:1644](../../opp_ci/executor.py#L1644)) is a thin in-process driver
over the same opp_repl functions the console scripts expose. Crucially its
structured `details` (`result.to_dict()`) is **already discarded** at the
container boundary — [`internal_run_direct`](../../opp_ci/cli.py#L3341) only
echoes stdout/stderr and exits 0/1 — so nothing is lost by switching to the
console scripts. All required behaviour is preserved because none of it comes
from opp_ci-in-the-container:

| Requirement | Source — unchanged by removal |
|---|---|
| stdout/stderr reporting | opp_repl console scripts write them; `podman exec` captures (as the nix path already does) |
| command log (`@@oppci:cmd@@`) | the **entry script** `run_cmd`/`printf`, not opp_ci |
| build/test split into stages | two execs — `opp_build_project` then `opp_run_*_tests --no-build` (identical to the nix path) |
| PASS/FAIL → stage exit code | opp_repl `*_main` exit codes (the nix path already relies on this) |
| project discovery / `--opp-file` | opp_repl `--load @opp --load $ROOT -p <bare>` (catalog) or cwd `*.opp` auto-discovery / `--load /work/<x>.opp` (bind-mount) — see [opp_repl/main.py](../../../opp_repl/opp_repl/main.py) `--load`/`-p`/`-m`/`--no-build` |
| build mode | opp_repl `-m/--mode` |

**Changes:**
- Rebuild the **host** branch of `_run_test_in_podman`
  ([executor.py:1375-1414](../../opp_ci/executor.py#L1375-L1414)) to emit the
  same opp_repl-console-script `run_stages` the nix branch builds
  ([:1369-1373](../../opp_ci/executor.py#L1369-L1373)): `opp_build_project …`
  for `PROJECT_BUILD`, `COMMAND_MAP[kind] … --no-build` for `TEST_RUN`, with the
  `--load @opp --load "$<NAME>_ROOT" -p <bare>` / `--opp-file` and `--mode`
  suffixes. The host entry script (`opp_ci_entry.sh.j2`) execs these via
  `opp_env run -w <ws> -c "env -u PYTHONPATH <opp_repl cmd>"` instead of
  `opp_ci $*`.
- Drop the opp_ci clone + `pip install` from `do_bootstrap` in **both** entry
  scripts (§2.4) — opp_repl only.
- This makes opp_ci's ref-inside-the-container moot, so it removes the §5 open
  question and the residual vestigial-install cleanup.

**Keep:** `internal run-direct` / `_run_test_direct` stay in opp_ci — they are
also the **bare-metal host path** (isolation=none, toolchain=none), where they
run *in the worker process* with a real `recorder` and full per-stage capture.
Only the *podman* use is removed; the code is untouched.

**Verify before shipping** (the one real risk): a project with **no `.opp`
anywhere**. `_load_workspace` ([executor.py:1644-…](../../opp_ci/executor.py#L1644))
has a last-resort `define_simulation_project(root_folder=cwd)` fallback; opp_repl
console scripts auto-discover `*.opp` from cwd but may lack that fallback. In a
container this is unlikely (catalog projects resolve via `@opp`/`$ROOT`;
bind-mounted projects ship a `.opp`), but confirm on a real run; if it bites, the
fallback belongs in opp_repl's CLI, not opp_ci. Optionally fold the two
now-near-identical entry scripts into one (host = `-w <ws>` + nixless omnetpp;
nix = `--install --no-isolated`).

### 2.7 Capture the structured result into `TestRun.details` for *all* runs

**Problem.** A test's structured result (`result.to_dict()` — the per-simulation
`results` list and `elapsed_wall_time`) reaches `TestRun.details` on **only one
of the three execution paths**:

| Path | Function | `details` today |
|---|---|---|
| in-process (isolation=none, toolchain=none) | [`_run_test_direct`](../../opp_ci/executor.py#L1644) — has the result object | ✅ `result.to_dict()` |
| host-nix subprocess (isolation=none, toolchain=nix / nixless) | [`_run_test_via_opp_env`](../../opp_ci/executor.py#L1528) — `opp_env run -c "<opp_repl cmd>"` | ❌ `None` (lines [1610](../../opp_ci/executor.py#L1610)/[1621](../../opp_ci/executor.py#L1621)/[1639](../../opp_ci/executor.py#L1639)) |
| podman container (isolation=podman, either toolchain) | [`_run_podman_staged`](../../opp_ci/executor.py#L1422) — exit code only | ❌ `None` ([~1538](../../opp_ci/executor.py#L1538)) |

Both subprocess/container paths run opp_repl out-of-process and keep only
stdout/stderr + exit code, so the structured result is lost. (The current podman
host path discards it even though opp_ci computes it —
[`internal_run_direct`](../../opp_ci/cli.py#L3341) echoes only stdout/stderr +
exit code.) This is pre-existing and independent of removing opp_ci (§2.6).

**The tail already exists.** Worker forwards `outcome.get("details")`
([worker.py:413-420](../../opp_ci/worker.py#L413-L420)); coordinator persists
`run.details = req.details` ([web/api.py:794](../../opp_ci/web/api.py#L794)) into
the `TestRun.details` JSON column; the UI renders `run.details.results` /
`details.elapsed_wall_time` ([run_detail.html:303-326](../../opp_ci/web/templates/run_detail.html#L303-L326)).
The only missing link is **getting the result out of the out-of-process run**.

**The enabler already exists in opp_repl.** Every `COMMAND_MAP` kind routes
through opp_repl's `run_tasks_main`, which already implements **`--result-file`**
([opp_repl/main.py:95-111](../../../opp_repl/opp_repl/main.py)): writes
`result.to_dict()` as JSON (to a file, or stdout via `-`) and exits 0/1 from
`is_all_results_expected()`. Present on opp_repl's `opp_ci` branch now. No
opp_repl change needed for the common kinds.

A small shared helper reads a result file into `details` for both
out-of-process paths:
```python
def _read_result_file(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None   # missing/corrupt → details stays None; exit code still rules
```

**(a) host-nix subprocess — [`_run_test_via_opp_env`](../../opp_ci/executor.py#L1528):**
- Allocate a host temp file (e.g. `tempfile.mkstemp(suffix=".json")`); append
  `--result-file <path>` to `test_inner` only (not `build_inner`). No mount — the
  opp_repl subprocess runs on the host filesystem, so it writes the path directly.
- After the TEST_RUN stage, set `details = _read_result_file(path)` instead of the
  hardcoded `None` at [:1639](../../opp_ci/executor.py#L1639); remove the temp file
  in a `finally`. Keep `--result-file` out of the `recorder.begin(..., command=…)`
  display string (`test_inner`) — it is plumbing.

**(b) podman container — [`_run_test_in_podman`](../../opp_ci/executor.py#L1422)
/ `_run_podman_staged`:**
- Create a host result dir, add `-v {result_dir}:/opp_ci_result:Z` to `run_flags`
  (mirrors the `/work` mount at [executor.py:1290](../../opp_ci/executor.py#L1290)
  and the `scratch_dir` cleanup pattern).
- Append `--result-file /opp_ci_result/result.json` to the **TEST_RUN** stage's
  opp_repl command in **both** the nix branch
  ([executor.py:1369-1373](../../opp_ci/executor.py#L1369-L1373)) and the
  §2.6-converged host branch — out of the curated `_display` string.
- In `_run_podman_staged`, after the staged loop, set
  `details = _read_result_file(f"{result_dir}/result.json")` (replacing the
  hardcoded `None`); `shutil.rmtree` the dir in the existing `finally`.

In all cases the exit code still drives `result_code` (a crashed stage with no
file is still FAIL); `details` only *enriches*, reaching parity with the
in-process path (all three then yield `to_dict()`).

**Alternative (no file/mount):** have opp_repl wrap `--result-file -` JSON in a
sentinel line (`@@oppci:result@@ <json>`, mirroring the `@@oppci:cmd@@` marker)
and parse it from the captured stream. Operationally cleaner (survives
`opp_env run -c` piping, no volume) but needs a small opp_repl change. Default to
the file approach for minimal cross-repo coupling.

**Out of scope:** **build-only** jobs (`opp_build_project` has no `--result-file`)
keep `details=None` — a build is pass/fail; extendable later if wanted.

---

## 3. Implementation phases

1. **Central refs (§2.1).** Add the constants to `config.py`; rewire
   `service.py` and `executor.py` to consume them. No behaviour change yet
   (opp_repl already `opp_ci`; opp_env still PyPI). Green tests prove the
   refactor is inert.
2. **opp_env into worker + coordinator (§2.2, §2.5).** First `uvx`-resolve both
   roles once on a real host to confirm opp_env + `opp_repl[all]` co-resolve;
   then add the opp_env `--with`/`--refresh-package` to `uvx_argv`, set
   `OPP_CI_OPP_ENV_CMD=opp_env` in both env renderers, and update the NixOS
   modules.
3. **Coordinator dependency resolution honours the cmd (§2.3).** Extract the
   argv helper; route `dependency.py` through it.
4. **podman pinning (§2.4).** opp_env SHA → `opp_ci` head; opp_repl image dep
   pin; `sync_repo` ref argument + template context in both entry scripts.
5. **Remove opp_ci from the podman images (§2.6).** Converge the host branch of
   `_run_test_in_podman` onto opp_repl console-script stages (mirroring the nix
   branch); make `opp_ci_entry.sh.j2` exec opp_repl via `opp_env run -c`; drop
   the opp_ci clone + `pip install` from both `do_bootstrap`s. Land after phase 4
   so the entry-script edits are made once. Verify the no-`.opp` edge case on a
   real run.
6. **Capture results into `TestRun.details` for all runs (§2.7).** Add the
   `_read_result_file` helper. Host-nix (`_run_test_via_opp_env`): temp file +
   `--result-file` on the test stage → `details`. Podman (`_run_podman_staged`):
   mount a result dir, pass `--result-file` on the TEST_RUN stage (both branches),
   read the JSON into `details`. Naturally follows phase 5 — the podman half
   touches the same stage-args / `_run_podman_staged` code, and once the host path
   runs opp_repl directly the same wiring serves both branches.
7. **Docs.** Update [doc/systemd.md](../../doc/systemd.md) (lines ~30/126/128),
   [doc/nixos.md:110](../../doc/nixos.md#L110), and any concepts/architecture
   note describing where opp_repl/opp_env come from (and that opp_ci is no longer
   inside the runner images). Note that podman runs now persist `details`.

---

## 4. Tests

Update existing:
- [test_service.py:84](../../tests/test_service.py#L84) and
  [:153](../../tests/test_service.py#L153) (and the docstring at
  [:10](../../tests/test_service.py#L10)) assert the old
  `OPP_CI_OPP_ENV_CMD="uvx --from opp-env opp_env"` — change to the new value.
- [test_service.py:44](../../tests/test_service.py#L44) (opp_repl `@opp_ci` in
  the uvx cmd) stays; add the parallel opp_env `@opp_ci` assertion.
- The bare-metal tests ([test_bare_metal_opp_env.py](../../tests/test_bare_metal_opp_env.py))
  assert `["opp_env", …]` against the default `OPP_ENV_CMD="opp_env"`; keep that
  default in config so they stay green (only the *service-rendered* env and the
  coordinator change).

Add:
- `render_coordinator_env` now emits `OPP_CI_OPP_ENV_CMD`.
- `uvx_argv` includes the opp_env `--with` + `--refresh-package opp-env` for
  both roles, and `render_coordinator_env`/`render_worker_env` emit
  `OPP_CI_OPP_ENV_CMD=opp_env`.
- `render_containerfile` bakes the `opp_ci`-branch opp_env SHA (mock
  `_resolve_remote_head`, assert it's called with `OPP_ENV_REF`) and pins
  opp_repl `@opp_ci`.
- Rendered entry scripts call `sync_repo opp_repl … opp_ci` and **no longer**
  reference opp_ci (assert `opp_ci` absent from the rendered bootstrap / no
  `pip install … opp_ci_src`).
- The host branch of `_run_test_in_podman` emits opp_repl-console-script stages
  (`opp_build_project` / `opp_run_*_tests --no-build` with `--load`/`-p`/`--mode`)
  — not `opp_ci internal run-direct`. Add a podman-staged render/exec test that
  asserts the stage argv carries no `opp_ci`.
- `dependency.query_opp_env_info` uses `opp_env_argv()` (assert it shells out via
  the configured command, not a hard-coded `opp_env`).
- The bare-metal `_run_test_direct` path is unchanged — its existing tests
  ([test_bare_metal_opp_env.py](../../tests/test_bare_metal_opp_env.py)) and any
  `internal run-direct` tests stay green (the code is kept; only its podman use
  is removed).
- **§2.7 (podman):** TEST_RUN stage argv carries
  `--result-file /opp_ci_result/result.json` (both branches) and `run_flags`
  mounts the result dir; `--result-file` is absent from the curated `_display`
  string. With a fixture `result.json` in the mounted dir, `_run_podman_staged`
  returns `details == json.loads(...)` (not `None`) and still derives
  `result_code` from the exit code; a missing file leaves `details=None` without
  erroring. Build-only stages get no `--result-file`.
- **§2.7 (host-nix):** `_run_test_via_opp_env`'s `test_inner` carries
  `--result-file <tmp>` (but `build_inner` and the recorder display do not); with
  a fixture file present the returned `details` is the parsed JSON, and the temp
  file is removed afterwards. `_read_result_file` returns `None` on missing/corrupt
  input. Existing `test_bare_metal_opp_env.py` assertions on the `opp_env run`
  argv must be updated to tolerate the trailing `--result-file` token on the test
  command.

---

## 5. Open questions

1. **No-`.opp` project fallback (§2.6).** Confirm opp_repl's console scripts
   resolve a project that ships no `.opp` anywhere, matching `_load_workspace`'s
   `define_simulation_project(root_folder=cwd)` last resort. If not, add the
   fallback to opp_repl's CLI. (Only this — the earlier opp_ci-ref-inside-podman
   and vestigial-nix-install questions are resolved by removing opp_ci entirely.)
2. **Merge the two entry scripts (§2.6, optional).** Once neither calls opp_ci
   they differ only in the `opp_env run` flags (host `-w <ws>` + nixless omnetpp
   vs nix `--install --no-isolated`). Unify into one template, or keep separate?
