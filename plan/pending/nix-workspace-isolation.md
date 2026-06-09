# Per-coordinate opp_env workspace for the non-podman (host nix) path

## Problem

When a run uses `--toolchain nix` **without** podman isolation
(`isolation=none, toolchain=nix`), the worker runs `opp_env install` and
`opp_env run` with **no workspace and no cwd**, so they inherit whatever
directory the worker process was launched from. If that directory isn't an
opp_env workspace, the run fails immediately:

```
ERROR The install operation stopped with error: No opp_env workspace found in
'/Users/rhornig/Projects/opp/opp_ci' or its parent directories, run 'opp_env init' ...
```

The two offending call sites:

- install: `["opp_env", "install", effective_project]` — `opp_ci/executor.py:338`
- run:     `["opp_env", "run", effective_project, "-c", cmd]` — `opp_ci/executor.py:1084`

Both go through `run_external(..., cwd=None)` (`opp_ci/executor.py:91`), inheriting
the worker's cwd.

The podman path does not hit this: the nix image pre-`init`s
`/opt/opp_env_workspace` and the entrypoint `cd`s into it
(`opp_ci/podman/Containerfile.nix.j2:71-74`, `opp_ci/podman/opp_env_entry.sh.j2:34-35`).
The host nix path has no equivalent.

## Why not a single shared workspace

A naive fix — point both calls at one persistent workspace — is **incorrect for
CI**. opp_env installs each project-version into a named directory in the
workspace and **compiles in-tree**; the nix toolchain only content-addresses the
toolchain and external libs in `/nix/store`, not the omnetpp/project build. A
shared workspace therefore lets:

- different omnetpp pins / dependency versions land in the same tree,
- the same effective id (e.g. `mm1k-latest`) for two different git refs clobber
  each other,
- concurrent jobs (worker concurrency > 1) race on the same directories.

The podman path already isolates by baking **one image per omnetpp version**
(`opp_ci/executor.py:493-504`). The host path should isolate the same way, just
as directories instead of images.

## Decision

**One workspace directory per build coordinate.** `OPP_CI_WORKSPACE` becomes a
*root*; each run resolves to `<root>/<key>` where `<key>` is derived from the
axes that determine the dependency closure. Identical coordinate → reuse (omnetpp
built once); different coordinate → isolated directory, no clash. Growth is
bounded by the number of *distinct* coordinates plus a retention cap.

### Coordinate key

Readable prefix + hash (hash is the source of truth; prefix is for `ls`):

```
<root>/<project>-omnetpp<pin>-<compiler><ver>-<ref8>-<hash8>
e.g.  mm1k-omnetpp6.4.0-clang21-3f9a1c2b-7e1d0a44
```

Hash inputs (the full tuple):
- effective opp_env project id (incl. version) — from `resolve_opp_env_id`
- omnetpp pin and every resolved dep — from `resolved_deps` (e.g. `{"omnetpp":"6.4.0"}`)
- toolchain (`nix`)
- compiler + compiler_version
- git ref / resolved sha

`resolved_deps` already reaches `run_test` via `run_kwargs` (`opp_ci/worker.py:178`)
and is read by the podman path at `opp_ci/executor.py:915-916`.

## Implementation

### 1. Config — `opp_ci/config.py`
Add a workspace **root** (not a single workspace):
```python
WORKSPACE_ROOT = os.path.expanduser(
    os.environ.get("OPP_CI_WORKSPACE", "~/.local/share/opp_ci/workspace"))
WORKSPACE_MAX = int(os.environ.get("OPP_CI_WORKSPACE_MAX", "10"))  # for GC, step 5
```

### 2. Executor helper — `opp_ci/executor.py`
Mirror `_opp_cache_root()` (`opp_ci/executor.py:369`):
```python
def _opp_env_workspace(*, project, resolved_deps, toolchain, compiler,
                       compiler_version, git_ref):
    """Return (and create) the per-coordinate opp_env workspace dir."""
    # build readable prefix + stable hash of the coordinate tuple
    ...
    os.makedirs(ws, exist_ok=True)
    return ws
```
Use `hashlib.sha1` (or blake2) over a canonical (sorted) repr of the tuple →
first 8 hex chars. Normalize `None`s consistently so the same coordinate always
hashes identically across install and run.

### 3. Thread the coordinate into `install_project`
`install_project` currently takes only `(project, git_ref, isolation, toolchain)`
(`opp_ci/executor.py:325`, called at `opp_ci/worker.py:194`). It must compute the
**same** key as the run step, so extend its signature to also receive
`resolved_deps`, `compiler`, `compiler_version`, and update the worker call site
to pass them (the data is already in `_execute`'s `job` / `run_kwargs`).

Then:
```python
ws = _opp_env_workspace(...)
run_external(["opp_env", "install", "--init", effective_project],
             label=..., cwd=ws)
```
`--init` marks the workspace on first use (same flag the host Containerfile uses,
`opp_ci/podman/Containerfile.host.j2:75-77`); idempotent on reuse.

### 4. Use the same workspace in the run step
In `_run_test_via_opp_env` (`opp_ci/executor.py:1068`), compute the key from the
same axes (already present in `kwargs`) and pass `cwd=ws` to `run_external` at
`opp_ci/executor.py:1087`. opp_env auto-detects the workspace from cwd.

> Note (separate but adjacent gap): the host-nix run path does **not** currently
> pin `resolved_deps` the way the podman path does (`opp_ci/executor.py:1018`).
> Decide whether to also pin omnetpp here; out of scope for the hang fix but
> relevant to correctness of `--pin`. Track separately.

### 5. Concurrency lock
Same-coordinate concurrent runs (e.g. a re-run, or concurrency > 1) would share
the dir. Guard install/build with a per-workspace file lock `<ws>/.opp_ci.lock`
(e.g. `fcntl.flock`), so the second waits instead of corrupting a half-built
tree. Different coordinates never contend.

### 6. Retention / GC
Bounded by distinct coordinates, but unbounded over time. Before each install,
sweep `<root>` and evict LRU-by-mtime beyond `WORKSPACE_MAX` (skip any currently
locked). Keep it simple; count-based eviction over age-based.

## Edge cases / open questions
- **mtime touch:** bump the workspace dir mtime on each reuse so GC LRU reflects
  actual use, not creation.
- **Key stability:** ensure `resolved_deps` ordering is normalized (sort keys)
  so dict iteration order can't change the hash.
- **Default root on the worker user:** `~/.local/share/opp_ci/workspace`
  resolves under the worker user's home; the deployment env file can pin an
  explicit path. (Deployment wiring is out of scope — see below.)
- **opp_env `--init` semantics:** confirm `install --init` is a no-op when the
  workspace already exists (expected, per Containerfile usage).

## Testing
- Unit: `_opp_env_workspace` returns identical paths for identical coordinates
  and distinct paths when any axis differs (omnetpp pin, compiler, ref, project).
- Integration (manual on the Mac mini): two runs with different `--pin
  omnetpp=...` produce two workspace dirs; re-running the same coordinate reuses
  the dir and skips the omnetpp rebuild.
- Regression: `isolation=podman` and `toolchain=none` paths unchanged
  (`install_project` early-returns for them, `opp_ci/executor.py:333-335`).

## Out of scope
- The macOS launchd LaunchDaemon / `opp_ci` service user. This plan only makes
  the worker correct regardless of launch directory; the service merely sets
  `OPP_CI_WORKSPACE` in its environment. Tracked separately in
  `plan/pending/launchd-worker-service-macos.md`.
