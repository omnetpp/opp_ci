# opp_ci documentation consistency review

A cross-check of the documentation in [doc/](../doc/) against the code in
[opp_ci/](../opp_ci/) — both for internal self-consistency between docs and
for accuracy vs. the implementation.

Inconsistencies are grouped by severity. Bracketed paths link from this
file (which lives in [plan/](.)) back up to the repository.

---

## High-severity (docs describe behavior the code does not have)

### 1. `opp_repl --result-file` / `--output-format json` are not used

[architecture.md:96-100](../doc/architecture.md#L96-L100),
[concepts.md:19](../doc/concepts.md#L19),
[concepts.md:551-554](../doc/concepts.md#L551-L554), and
[getting_started.md:35](../doc/getting_started.md#L35) say the executor reads
structured JSON via `--result-file` or `--output-format json`.
[executor.py](../opp_ci/executor.py) passes neither flag: subprocess paths only
check exit codes, direct mode imports `opp_repl.test.*` in-process and inspects
the returned object via `is_all_results_expected()` / `to_dict()`.

### 2. `/api/notes/{owner}/{repo}/ack` does not exist

Documented in [rest_api.md:66](../doc/rest_api.md#L66). No POST route is defined
in [api.py](../opp_ci/web/api.py); `trigger_notes_sync` is called inline in the
worker-result handler at [api.py:519-544](../opp_ci/web/api.py#L519-L544). Only
the GET route exists.

### 3. Git-note format in [git_notes.md](../doc/git_notes.md) is wrong

Docs claim a pipe-separated single line with aggregated counts (e.g.
`✅ smoke PASS | ❌ fingerprint 46 PASS, 2 FAIL | https://…`) at
[git_notes.md:37-38, 74-80](../doc/git_notes.md#L37-L38). `format_note()` at
[notes.py:33-87](../opp_ci/notes.py#L33-L87) produces a multiline form:
summary header, per-run indented lines
`  <icon> <type>/<mode>  STATUS  <duration>  #<id>`, blank line, then URL. No
pipes, no `46 PASS, 2 FAIL`-style aggregation.

### 4. Web UI commit page route is wrong

[web_ui.md:28](../doc/web_ui.md#L28) lists `/commit/{sha}`. Actual route is
`/commits/{project}/{sha}` at [app.py:785](../opp_ci/web/app.py#L785) —
singular vs plural, plus a required `project` segment.

### 5. `opp_env list` is never called

[concepts.md:266-267](../doc/concepts.md#L266-L267) says `opp_env list`
"populates the version selectors". [opp_env_adapter.py](../opp_ci/opp_env_adapter.py)
only uses `opp_env info --raw` (lines 36, 84, 143).

### 6. `--remote run` silently drops most flags

[cli.py:100-102](../opp_ci/cli.py#L100-L102) routes to
`_run_remote(project, test_types, git_ref)` — only those three flags are
passed through. So
`opp_ci --remote run --project X --test smoke --isolation docker --os Ubuntu
--os-version 26.04 --compiler clang --compiler-version 22 --arch amd64
--mode release --pin omnetpp=6.1`
silently throws away isolation, toolchain, os, os_version, arch, compiler,
compiler_version, mode, pins, force. The end-to-end example in
[single_test_parameters.md:498-513](../doc/single_test_parameters.md#L498-L513)
and remote examples in [python_client.md](../doc/python_client.md) and
[cli_reference.md:142](../doc/cli_reference.md#L142) imply full pass-through.

### 7. `OppCiClient.submit_run()` is missing `arch` and `force` kwargs

[single_test_parameters.md:504-511](../doc/single_test_parameters.md#L504-L511)
shows `ci.submit_run(..., arch="amd64", ...)`, but
[client.py:36-37](../opp_ci/client.py#L36-L37) accepts only
`project, test_type, mode, git_ref, os, os_version, compiler, compiler_version`.
Calling with `arch=...` raises `TypeError`. Also no `force` kwarg, though
[single_test_parameters.md:342](../doc/single_test_parameters.md#L342)
documents `force` as a REST field.

### 8. CLI `--remote` builds the wrong URL

[cli.py:213](../opp_ci/cli.py#L213) does `OppCiClient(url=COORDINATOR_URL, ...)`.
[client.py:32](../opp_ci/client.py#L32) treats `url` as the API base — paths
are appended as `/runs`, `/workers`, etc. The FastAPI router has
`prefix="/api"` ([api.py:43](../opp_ci/web/api.py#L43)), so requests land at
`<coord>/runs` (404).

Two ways to fix:

- [cli_reference.md:139](../doc/cli_reference.md#L139) and
  [python_client.md:50](../doc/python_client.md#L50) instruct users to set
  `OPP_CI_COORDINATOR_URL=…/api` (with suffix), and the worker code at
  [worker.py:43,90,103,194](../opp_ci/worker.py) stops prepending `/api`; OR
- The CLI appends `/api` before passing to `OppCiClient`.

Right now `--remote` is broken without manual URL fiddling.

### 9. `TestResult` columns claimed in [architecture.md:76](../doc/architecture.md#L76) don't exist

Doc says `test_type, test_name, result_code, duration, stdout/stderr, details`.
[models.py:186-194](../opp_ci/db/models.py#L186-L194) has only
`result_code, stdout, stderr, details` (plus `id` and `test_run_id` FK).
`test_type`, `test_name`, `duration` are not columns.

---

## Medium-severity (mislabeled options, wrong roles, undocumented surface)

### 10. `GET /api/github/rules` role mismatch

[rest_api.md:58](../doc/rest_api.md#L58) shows role `admin`; code uses
`readonly` at [api.py:664](../opp_ci/web/api.py#L664).

### 11. `show-results` does not accept `--ref`

[cli_reference.md:53](../doc/cli_reference.md#L53) says "Same filters as
`list-runs`" (which lists `--project`, `--ref`, `--test`, `--status`,
`--limit`), but [cli.py:390-394](../opp_ci/cli.py#L390-L394) does not accept
`--ref`.

### 12. Test-type list mismatch — canonical list lives in test_matrix_dimensions.md

[test_matrix_dimensions.md:167-180](../doc/test_matrix_dimensions.md#L167-L180)
lists 11 entries (including `opp`), matching
[executor.py:102-114](../opp_ci/executor.py#L102-L114). The other two doc
lists have drifted:

- [cli_reference.md:30-32](../doc/cli_reference.md#L30-L32) lists 10 entries
  (missing `opp`).
- [concepts.md:207](../doc/concepts.md#L207) lists 14 entries, inventing
  `module, unit, packet, queueing, protocol, validation` that don't exist in
  `COMMAND_MAP`.
- [concepts.md:240-242](../doc/concepts.md#L240-L242) gives yet another partial
  list with a trailing `…`. Internal contradiction within concepts.md.

### 13. Worker capability-tag conventions disagree between docs

- [getting_started.md:148-156](../doc/getting_started.md#L148-L156) has the
  formal scheme: `docker`, `nix`, `os:<name>-<ver>`, `compiler:<name>-<ver>`.
- [workers.md:36-42](../doc/workers.md#L36-L42) has an informal scheme:
  `linux/macos/windows`, `amd64/arm64`, bare `gcc-13`/`clang-18`,
  `perf-counters`, `docker`, `nix`.
- [concepts.md:293-297](../doc/concepts.md#L293-L297) calls them "free-form".

Dispatch code is strict:
[`_worker_can_run` at api.py:444-473](../opp_ci/web/api.py#L444-L473) requires
exactly `docker`, `nix`, `os:<lc-os>-<ver>`, `compiler:<lc-compiler>-<ver>`,
and `arch:<lc-arch>`. Bare `linux`/`gcc-13`/`perf-counters` never gates
anything. The `arch:<arch>` requirement is documented nowhere.

### 14. Undocumented CLI commands and options

[cli_reference.md](../doc/cli_reference.md) is missing:

- `reset-db --preserve-tokens` ([cli.py:33](../opp_ci/cli.py#L33))
- `worker detect-tags` (subcommand; relevant given the `--auto-tags` flow)
- `seed-platforms`
- `--arch` on both `run` ([cli.py:90](../opp_ci/cli.py#L90)) and
  `create-matrix` ([cli.py:590](../opp_ci/cli.py#L590)).
  [test_matrix_dimensions.md](../doc/test_matrix_dimensions.md) is also missing
  the `arch` axis entirely; only
  [single_test_parameters.md:172-187](../doc/single_test_parameters.md#L172-L187)
  documents it.

### 15. `/api/workers/me` undocumented

[api.py:322](../opp_ci/web/api.py#L322) — used by [worker.py:43](../opp_ci/worker.py#L43)
to fetch name/tags/concurrency at startup, which both
[concepts.md:43](../doc/concepts.md#L43) and
[workers.md:82-83](../doc/workers.md#L82-L83) describe — but the endpoint
itself is missing from [rest_api.md](../doc/rest_api.md).

### 16. Undocumented env vars

[configuration.md](../doc/configuration.md) is missing five env vars the code
reads:

- `OPP_CI_PROJECT_DIR` and `OPP_CI_PROJECT_DIR_<PROJECT>`
  ([executor.py:249,396](../opp_ci/executor.py),
  [notes.py:167](../opp_ci/notes.py#L167)) — controls where project sources
  live for direct/docker modes.
- `OPP_CI_CACHE_DIR` ([executor.py:314](../opp_ci/executor.py#L314)) — clone
  cache.
- `OPP_CI_INSTALL_PROJECTS`, `OPP_ENV_GIT_REF` — set by code and passed into
  containers; worth documenting the docker entrypoint contract.

### 17. Coordinator URL default port (8080) ≠ `serve` default port (8000)

[configuration.md:11](../doc/configuration.md#L11) auto-detects
`http://<host-ip>:8080`; [cli_reference.md:102](../doc/cli_reference.md#L102)
and [config.py:14](../opp_ci/config.py#L14) confirm. But
[`opp_ci serve` defaults to port 8000](../opp_ci/cli.py#L223). The default
coordinator URL points to a port the coordinator doesn't listen on by default.
Either docs should flag this or `config.py` should default to 8000.

### 18. Web UI auth is silent

HTML routes in [app.py](../opp_ci/web/app.py) have no auth dependency.
[web_ui.md](../doc/web_ui.md) doesn't mention this. [rest_api.md](../doc/rest_api.md)
makes it sound like bearer tokens gate everything. Worth a line:
"HTML routes are unauthenticated — restrict via your reverse proxy."

---

## Low-severity (cosmetic, minor)

### 19. `"features"` axis doesn't really exist

[scheduler.py](../opp_ci/scheduler.py) docstring lists `"features": []` as an
axis but `expand_matrix()` never reads `config["features"]`.
[concepts.md:206](../doc/concepts.md#L206) leans on it being real;
[test_matrix_dimensions.md:455-469](../doc/test_matrix_dimensions.md#L455-L469)
honestly marks it as reserved.

### 20. Architecture model description hand-waves columns

[architecture.md:68](../doc/architecture.md#L68) describes the schema as if
`TestRun.project` / `TestRun.version` / `TestRun.os` / `TestRun.compiler` are
relational links. They are plain strings; no FK to Project/Version/OS/Compiler
exists.

### 21. `image build --toolchain` choices are `host`/`nix`

Not `none`/`nix` as `run`/`create-matrix` use.
[getting_started.md:127](../doc/getting_started.md#L127) uses `--toolchain host`
correctly; this special-case vocabulary deserves a note in
[cli_reference.md:107-111](../doc/cli_reference.md#L107-L111).

### 22. Stale "Tier 1 / Tier 2" comments in [bin/recreate-db:4-5](../bin/recreate-db#L4-L5)

The `projects.tier` column was dropped (migration
`4e2a31c0a4b1_drop_project_tier.py`). The script comment is the only place
left referencing tiers.

### 23. Code-link line numbers in doc files have drifted

Spot-checks:

- [test_matrix_dimensions.md:165](../doc/test_matrix_dimensions.md#L165) says
  `COMMAND_MAP` at executor.py:102 — actual 102 ✓.
- [single_test_parameters.md:59](../doc/single_test_parameters.md#L59) says
  `COMMAND_MAP` at executor.py:105 — actual 102 (off by 3).

Consider dropping the `#Lxx` anchors in `[...](path#Lxx)` links since they
bit-rot fast.

### 24. `format_results_comment` is in `webhook.py`, imported back into `status.py`

[github_integration.md:66-73](../doc/github_integration.md#L66-L73) implies
`status.py` owns PR comment formatting; it imports the formatter back from
`webhook.py`. Minor code-organization note, not a user-facing doc error.

---

## Summary index

| # | Severity | File(s) | Issue |
|---|---|---|---|
| 1 | high | architecture / concepts / getting_started | `--result-file` / `--output-format json` not used |
| 2 | high | rest_api.md | `/api/notes/{owner}/{repo}/ack` doesn't exist |
| 3 | high | git_notes.md | Note format is wrong |
| 4 | high | web_ui.md | `/commit/{sha}` should be `/commits/{project}/{sha}` |
| 5 | high | concepts.md | `opp_env list` never called |
| 6 | high | python_client / cli_reference / single_test_parameters | `--remote run` drops most flags |
| 7 | high | single_test_parameters.md | `OppCiClient.submit_run()` lacks `arch`/`force` kwargs |
| 8 | high | cli_reference / python_client | CLI `--remote` URL composition broken |
| 9 | high | architecture.md | TestResult columns wrong |
| 10 | med | rest_api.md | `GET /api/github/rules` role: should be `readonly` |
| 11 | med | cli_reference.md | `show-results` lacks `--ref` |
| 12 | med | cli_reference / concepts | Test-type list mismatch |
| 13 | med | workers / getting_started / concepts | 3 capability-tag schemes |
| 14 | med | cli_reference / test_matrix_dimensions | Missing options (`--arch`, etc.) |
| 15 | med | rest_api.md | `/api/workers/me` undocumented |
| 16 | med | configuration.md | 5 undocumented env vars |
| 17 | med | configuration / cli_reference | Default ports 8080 vs 8000 mismatch |
| 18 | med | web_ui.md | HTML routes have no auth — undocumented |
| 19 | low | concepts.md | "features" axis doesn't really exist |
| 20 | low | architecture.md | Schema description implies FKs that aren't there |
| 21 | low | cli_reference.md | `image build --toolchain` choices `host`/`nix` |
| 22 | low | bin/recreate-db | Stale "Tier" comments |
| 23 | low | various | Code-link line numbers drifted |
| 24 | low | github_integration.md | `format_results_comment` lives in webhook.py |

---

## Recommended fix order

1. **`--remote` is broken (#6, #7, #8).** Fix both the CLI (pass full args
   to the API, append `/api` to the URL, accept `arch`/`force` in the client)
   and the docs together.
2. **`/api/notes/.../ack` and the git-note format (#2, #3).** Either implement
   the documented design or rewrite the docs to match
   [notes.py](../opp_ci/notes.py).
3. **Executor flags (#1).** Decide whether `executor.py` should pass
   `--result-file` / `--output-format json` (and parse them) or update the docs
   to describe the current in-process / exit-code mechanism.
4. **Single canonical lists.** Write the test-type list once (in
   [test_matrix_dimensions.md](../doc/test_matrix_dimensions.md)), the
   capability-tag scheme once (in [getting_started.md](../doc/getting_started.md)),
   and have everything else link in. Resolves #12 and #13 in one stroke.
5. **`/commits/{project}/{sha}` and TestResult schema (#4, #9).** Trivial doc
   fixes.
6. **Remaining medium items.** Sweep `cli_reference.md` to add missing flags
   and commands; sweep `rest_api.md` for `/api/workers/me` and the
   `GET /api/github/rules` role.
