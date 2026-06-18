# Workers

A **worker** is a machine that polls the coordinator for queued jobs,
executes them, and reports results back. The coordinator itself does
not execute jobs — workers do.

Worker code lives in `opp_ci/worker.py`. Each worker process runs a
poll loop and a heartbeat loop.

## Lifecycle

1. **Register** on the coordinator (admin action). Generates a unique
   per-worker token.
2. **Start** on the worker machine using the token. The worker:
   - Sends an initial heartbeat (becomes `online`).
   - Polls `POST /api/workers/poll` every `OPP_CI_WORKER_POLL_INTERVAL`
     seconds for a queued job.
   - Receives a job descriptor, calls `executor.install_project()` then
     `executor.run_test()`.
   - Posts the result to `POST /api/workers/result`.
   - Sends periodic heartbeats every
     `OPP_CI_WORKER_HEARTBEAT_INTERVAL` seconds.
3. **Disappear** if no heartbeat arrives within
   `OPP_CI_WORKER_HEARTBEAT_TIMEOUT` seconds — coordinator marks the
   worker `offline`. In-flight jobs are reclaimed.

The worker handles `SIGINT` / `SIGTERM` for clean shutdown.

### Coordinator reaper

The coordinator runs a periodic reaper sweep (interval
`OPP_CI_WORKER_REAP_INTERVAL`, also once at startup) that handles two
kinds of stuck work:

- **Orphaned `running` runs** — a worker that stopped heartbeating is
  marked `offline` and its in-flight runs are re-queued, up to
  `OPP_CI_MAX_RECLAIMS` times. A run that keeps outliving its worker
  (suspected crash/OOM loop) is retired as a *poison pill* to
  `timed_out` / `ERROR`.
- **Unserviceable `queued` runs** — a run no *enabled* worker can serve
  (none has the required [capability tags](#capability-tags), or every
  capable worker [opts out](#run-filters-opt-out-of-work-you-can-do) of
  it) is a misroute, not transient backlog. After
  `OPP_CI_QUEUE_UNSERVICEABLE_TIMEOUT` seconds it is expired to
  `timed_out` / `ERROR` with a message naming the cause (missing tags vs.
  all-opted-out), instead of waiting forever for a worker that will never
  poll. Serviceability counts enabled workers of *any* status, so
  a busy or temporarily-offline worker still covers a run; only true
  misroutes are expired. A run that is serviceable but starved (all
  matching workers busy or down) is left queued. Set the timeout to `0`
  to disable.

## Capability tags

Workers advertise their capabilities with a list of tags (set via
`--tags` / `--auto-tags` on `worker register`, stored on the
coordinator). The dispatcher
([`_worker_can_run` in api.py](../opp_ci/web/api.py)) treats tags in
this exact form:

| Tag | Gates dispatch when… |
|---|---|
| `podman` | run has `--isolation podman` |
| `nix` | run has `--toolchain nix` |
| `os:<lc-os>` (e.g. `os:linux`) | run names just an OS family with no distro |
| `os:<lc-os>-<version>` (e.g. `os:windows-11`) | run names Windows/MacOS with a version |
| `distro:<lc-name>-<version>` (e.g. `distro:ubuntu-24.04`) | run names a `distro` (and no flavor) |
| `flavor:<lc-name>-<version>` (e.g. `flavor:kubuntu-24.04`) | run names a `flavor` |
| `compiler:<lc-name>-<version>` (e.g. `compiler:gcc-14`) | run names a `compiler` + `compiler_version` |
| `arch:<lc-arch>` (e.g. `arch:amd64`) | run names an `arch` |

The dispatcher requires the *most specific* platform tag the run pins:
a run that names a flavor only matches workers tagged with that flavor;
a run that names a distro only matches workers tagged with that distro;
and so on up the hierarchy. Workers typically advertise all three
levels (e.g. `os:linux`, `distro:ubuntu-24.04`, `flavor:kubuntu-24.04`)
so they can claim runs targeting any level they cover. `--auto-tags`
emits these automatically by reading `/etc/os-release`.

Name/value parts are lowercased. Tags outside this scheme (for example
`linux`, `gcc-13`, `perf-counters`) are accepted by the API but never
gate dispatch — treat them as documentation. The scheduler matches each
queued job's required subset against the worker's advertised tags
before dispatching.

## Run-filters (opt out of work you *can* do)

Capability tags answer *can* this worker run a test. Run-filters answer
*will* it — a separate, optional gate that lets a worker **decline tests
it is capable of running**. The dispatcher serves a queued run to a
worker only when the worker both has the required tags **and** passes
its run-filters ([`worker_can_serve` in persistence.py](../opp_ci/persistence.py)).

Run-filters are stored on the worker (column `run_filters`, like `tags`)
and keyed by coordinate axis. Each axis carries **either** an allow-list
**or** a deny-list (not both):

- `allow` — run *only* these values for the axis; decline all others.
- `deny` — never run these values, even when capable.

An axis with no entry is unconstrained. An empty map (the default) means
the worker runs everything its tags qualify it for — so existing workers
are unaffected.

The common axes are `isolation` (`none` / `podman`) and `toolchain`
(`none` / `nix`), surfaced as first-class flags. The mechanism is general
over any [coordinate field](concepts.md) (`compiler`, `os`, `arch`,
`mode`, …), reachable via the general `--run-filter` flag. Note the
default values `isolation=none` / `toolchain=none` carry *no* capability
tag, so they can only be excluded via a run-filter (not by removing a tag).

```bash
# This host can run Podman jobs but should not spend cycles on them:
opp_ci worker update <id> --deny-isolation podman

# A dedicated container host: only ever run podman, never bare-metal:
opp_ci worker update <id> --accept-isolation podman

# Never run Nix-toolchain jobs:
opp_ci worker update <id> --deny-toolchain nix

# General axis (any coordinate field), repeatable:
opp_ci worker update <id> --run-filter compiler=deny:gcc-7

# Clear filters:
opp_ci worker update <id> --clear-filter isolation   # one axis
opp_ci worker update <id> --clear-run-filters        # all
```

The same flags work on `worker register`. The web worker page
(`/workers/<id>`) edits the isolation/toolchain filters directly. Unlike
the coordinator URL and token (service-env state), run-filters are
DB state — change them with `worker update` without reinstalling the
service.

When *every* enabled worker capable of a run opts out of it, the run is
[unserviceable](#coordinator-reaper) and is expired after the timeout
with a message naming the opt-out (and `details.declined_by_filter`),
rather than the "missing tags" message used for a genuine misroute.

## Register a worker (admin)

Two ways:

```bash
# CLI (on the coordinator, or remotely with --remote)
opp_ci worker register \
  --name builder-1 \
  --tags os:linux,distro:ubuntu-24.04,arch:amd64,compiler:gcc-14,perf-counters,nix \
  --concurrency 4
# → Worker 'builder-1' registered.
# →   Token: <auto-generated-token>
```

```bash
# REST API
curl -X POST https://ci.omnetpp.org/api/workers/register \
  -H "Authorization: Bearer <admin-token>" \
  -d '{"name": "builder-1", "tags": ["linux", "amd64"], "concurrency": 4}'
```

The web UI exposes the same form at `/admin`.

## Start a worker

On the worker machine:

```bash
opp_ci worker start \
    --coordinator https://ci.omnetpp.org \
    --token <worker-token> \
    --poll-interval 10 \
    --heartbeat-interval 30
```

The worker fetches its registered name, tags, run-filters and
concurrency from the coordinator on startup — the coordinator is the
single source of truth. Change them with `opp_ci worker update <id>`
(no re-register needed).

Workers only need **outbound** network access — they poll the
coordinator. No inbound port is required on the worker host.

## List workers

```bash
opp_ci worker list
```

Or via the web UI at `/admin`.

## DB model

`Worker` (`opp_ci/db/models.py`):

| Column | Purpose |
|---|---|
| `name` | Unique human-readable identifier |
| `token` | Auto-generated bearer token used for poll/heartbeat/result |
| `tags` | JSON list of capability tags |
| `run_filters` | JSON map of per-axis allow/deny [run-filters](#run-filters-opt-out-of-work-you-can-do) (willingness, vs. `tags` capability) |
| `concurrency` | Max concurrent jobs the worker will accept |
| `status` | `online` / `offline` / `busy` |
| `last_heartbeat` | Timestamp of the last heartbeat |
| `current_job_count` | Jobs currently in flight |

## Operational notes

- Run workers under a process supervisor so they restart on crash — the
  worker has no built-in supervision. The CLI installs one for you:
  `opp_ci worker service install` (systemd on Linux
  [systemd.md](systemd.md); launchd on macOS [launchd.md](launchd.md);
  a rendered module on NixOS [nixos.md](nixos.md)). Podman or k8s also
  work.
- For speed tests, pick a worker host with stable, low-noise CPU (no
  shared cloud tenants, frequency scaling off). Add a `perf-counters`
  tag and use a matrix that requires that tag.
- Concurrency >1 means the worker will run multiple `executor.run_test()`
  calls in parallel. Builds use a lot of RAM; size accordingly.
