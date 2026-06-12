import datetime
import functools
import logging

import click
from sqlalchemy import select

from opp_ci.db.connection import engine, SessionLocal
from opp_ci.db.models import (
    ApiToken, AutoTestRule, Base, ExpectedTestResult, Project, Test,
    TestMatrix, TestMatrixRun, TestResultCode, TestRun, TestRunLifecycle,
    TestVerdict, TestVerdictKind, Version, Worker,
)
from opp_ci.executor import install_project, run_test
from opp_ci.persistence import (
    capture_system_snapshot, create_matrix_run, create_test_run, delete_worker,
    enqueue_job, finalize_verdict_for_run, get_or_create_test,
    insert_expectation, status_filter, update_worker,
)
from opp_ci.notes import update_ci_note


@click.group()
@click.option("-v", "--verbose", is_flag=True, help="Enable verbose logging")
@click.option("--remote/--local", "remote", default=None,
              help="Drive a remote coordinator over the REST API instead of "
                   "the local database (default from $OPP_CI_REMOTE)")
@click.pass_context
def main(ctx, verbose, remote):
    """opp_ci — CI for OMNeT++ simulation projects."""
    from opp_ci import config as cfg
    ctx.ensure_object(dict)
    ctx.obj["remote"] = cfg.REMOTE if remote is None else remote
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


# ── Remote dispatch infrastructure ──────────────────────────────────────
#
# `@remoteable(handler)` makes a command dual-mode: when `--remote` (or
# `OPP_CI_REMOTE=1`) is set the decorator dispatches to `handler` with the
# same keyword arguments click parsed; otherwise the plain local body runs
# against the local database. Decorating a command wires *both* paths by
# construction, so a command can't accidentally ship with a half-wired
# remote mode. The click @options must sit ABOVE @remoteable so they
# attach to the wrapper click actually invokes.


def remoteable(remote_handler):
    """Dispatch to `remote_handler(**kwargs)` when --remote is set."""
    def decorator(local_fn):
        argnames = local_fn.__code__.co_varnames[:local_fn.__code__.co_argcount]
        wants_ctx = "ctx" in argnames

        @functools.wraps(local_fn)
        @click.pass_context
        def wrapper(ctx, **kwargs):
            if ctx.obj.get("remote"):
                return remote_handler(**kwargs)
            if wants_ctx:
                return local_fn(ctx=ctx, **kwargs)
            return local_fn(**kwargs)
        return wrapper
    return decorator


def _refuse_remote(name):
    """Build a remote handler that refuses: this command is host-local."""
    def handler(**kwargs):
        click.echo(f"ERROR: {name} is local-only; ignoring --remote", err=True)
        raise SystemExit(2)
    return handler


def _remote_client():
    """Construct an OppCiClient from config, or exit with a friendly error."""
    from opp_ci.config import COORDINATOR_URL, API_TOKEN
    from opp_ci.client import OppCiClient

    if not API_TOKEN:
        click.echo("ERROR: Set OPP_CI_API_TOKEN env var for remote operations.",
                   err=True)
        raise SystemExit(1)
    base = COORDINATOR_URL.rstrip("/")
    api_url = base if base.endswith("/api") else base + "/api"
    return OppCiClient(url=api_url, token=API_TOKEN)


def _remote(op):
    """Run `op(client)`, surfacing OppCiClientError as a tidy ERROR line."""
    from opp_ci.client import OppCiClientError
    client = _remote_client()
    try:
        return op(client)
    except OppCiClientError as e:
        click.echo(f"ERROR: {e.detail}", err=True)
        raise SystemExit(1)


# ── Shared output formatters (dict in → click.echo out) ─────────────────
#
# These take plain dicts so the same rendering serves both the REST
# responses (remote mode) and, where adopted, the local DB rows. The
# remote handlers below use them so `--remote` output matches the local
# command's layout.


def _row_status(d):
    """effective_status from a /runs dict: outcome if finished, else lifecycle."""
    return d.get("result_code") or d.get("lifecycle") or "-"


def _fmt_started(value):
    """Render an ISO timestamp (or datetime) as 'YYYY-MM-DD HH:MM', or '-'."""
    if not value:
        return "-"
    if isinstance(value, str):
        try:
            value = datetime.datetime.fromisoformat(value)
        except ValueError:
            return value
    return value.strftime("%Y-%m-%d %H:%M")


def _fmt_duration(seconds):
    return f"{seconds:.1f}s" if seconds else "-"


def _format_runs(rows):
    if not rows:
        click.echo("No runs found.")
        return
    click.echo(f"{'ID':<6} {'Project':<20} {'Ref':<16} {'Kind':<14} "
               f"{'Status':<10} {'Duration':<10} {'Started'}")
    click.echo("-" * 106)
    for r in rows:
        click.echo(f"{r['id']:<6} {(r.get('project') or '-'):<20} "
                   f"{(r.get('git_ref') or '-'):<16} {(r.get('kind') or '-'):<14} "
                   f"{_row_status(r):<10} {_fmt_duration(r.get('duration_seconds')):<10} "
                   f"{_fmt_started(r.get('started_at'))}")


def _format_run_detail(r):
    click.echo(f"Run #{r['id']}")
    click.echo(f"  Project:  {r.get('project') or '-'}")
    click.echo(f"  Ref:      {r.get('git_ref') or '-'}")
    click.echo(f"  Commit:   {r.get('commit_sha') or '-'}")
    click.echo(f"  Version:  {r.get('version') or '-'}")
    click.echo(f"  Kind:     {r.get('kind') or '-'}")
    click.echo(f"  Lifecycle:{r.get('lifecycle') or '-'}")
    click.echo(f"  Result:   {r.get('result_code') or '-'}")
    click.echo(f"  Duration: {_fmt_duration(r.get('duration_seconds'))}")
    click.echo(f"  Test time:{_fmt_duration(r.get('test_exec_seconds'))}")
    click.echo(f"  Started:  {r.get('started_at') or '-'}")
    click.echo(f"  Finished: {r.get('finished_at') or '-'}")
    stdout = r.get("stdout")
    stderr = r.get("stderr")
    if stdout:
        click.echo(f"\n  stdout ({len(stdout)} chars):")
        click.echo("    " + stdout[:500].replace("\n", "\n    "))
        if len(stdout) > 500:
            click.echo("    ...")
    if stderr:
        click.echo(f"\n  stderr ({len(stderr)} chars):")
        click.echo("    " + stderr[:500].replace("\n", "\n    "))
        if len(stderr) > 500:
            click.echo("    ...")


def _format_projects(rows):
    if not rows:
        click.echo("No projects.")
        return
    click.echo(f"{'Name':<20} {'opp_env':<16} {'Deps':<24} {'GitHub'}")
    click.echo("-" * 80)
    for p in rows:
        deps = ", ".join(p.get("deps") or []) or "-"
        click.echo(f"{(p.get('name') or '-'):<20} {(p.get('opp_env_name') or '-'):<16} "
                   f"{deps[:24]:<24} {p.get('github') or '-'}")


def _format_versions(rows):
    if not rows:
        click.echo("No versions registered.")
        return
    click.echo(f"{'ID':<6} {'Project':<16} {'Label':<20} {'Git Ref':<24} "
               f"{'opp_env':<16} {'Dependencies'}")
    click.echo("-" * 110)
    for v in rows:
        deps = str(v.get("deps")) if v.get("deps") else "-"
        click.echo(f"{v['id']:<6} {(v.get('project') or '-'):<16} "
                   f"{(v.get('label') or '-'):<20} {(v.get('git_ref') or '-'):<24} "
                   f"{(v.get('opp_env_version') or '-'):<16} {deps}")


def _format_matrices(rows):
    if not rows:
        click.echo("No matrices defined.")
        return
    click.echo(f"{'Name':<24} {'Project':<16} {'Jobs'}")
    click.echo("-" * 60)
    from opp_ci.scheduler import expand_matrix
    for m in rows:
        try:
            jobs = len(expand_matrix(m["project"], m.get("config") or {}))
        except Exception:
            jobs = "?"
        click.echo(f"{m['name']:<24} {m['project']:<16} {jobs}")


def _format_workers(rows):
    if not rows:
        click.echo("No workers registered.")
        return
    click.echo(f"{'ID':<6} {'Name':<20} {'Status':<10} {'Enabled':<9} {'Jobs':<6} "
               f"{'Cap':<6} {'Tags':<30} {'Last Heartbeat'}")
    click.echo("-" * 120)
    for w in rows:
        tags = ", ".join(w.get("tags") or []) or "-"
        enabled = "no" if w.get("enabled") is False else "yes"
        hb = w.get("last_heartbeat")
        if hb:
            try:
                hb = datetime.datetime.fromisoformat(hb).strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                pass
        click.echo(f"{w['id']:<6} {w['name']:<20} {(w.get('status') or '-'):<10} "
                   f"{enabled:<9} {w.get('current_job_count', 0):<6} "
                   f"{w.get('concurrency', 0):<6} {tags:<30} {hb or '-'}")


def _format_tokens(rows):
    if not rows:
        click.echo("No tokens.")
        return
    click.echo(f"{'ID':<6} {'Name':<20} {'Role':<12} {'Enabled':<10} "
               f"{'Token (prefix)':<20} {'Created'}")
    click.echo("-" * 90)
    for t in rows:
        enabled = "yes" if t.get("enabled") else "no"
        prefix = t.get("token_prefix") or "-"
        click.echo(f"{t['id']:<6} {t['name']:<20} {t['role']:<12} {enabled:<10} "
                   f"{prefix:<20} {_fmt_started(t.get('created_at'))}")


def _format_users(rows):
    if not rows:
        click.echo("No users.")
        return
    click.echo(f"{'ID':<4} {'Login':<24} {'Role':<10} {'Locked':<7} "
               f"{'Enabled':<8} {'Last Login'}")
    click.echo("-" * 80)
    for u in rows:
        login = u.get("username") or (
            f"@{u['github_username']}" if u.get("github_username") else "-")
        click.echo(f"{u['id']:<4} {login:<24} {u['role']:<10} "
                   f"{('yes' if u.get('role_locked') else 'no'):<7} "
                   f"{('yes' if u.get('enabled') else 'no'):<8} "
                   f"{_fmt_started(u.get('last_login_at'))}")


def _format_rules(rows):
    if not rows:
        click.echo("No auto-test rules.")
        return
    click.echo(f"{'ID':<6} {'Project':<20} {'Type':<8} {'Pattern':<20} "
               f"{'Matrix':<20} {'Enabled'}")
    click.echo("-" * 90)
    for r in rows:
        enabled = "yes" if r.get("enabled") else "no"
        click.echo(f"{r['id']:<6} {(r.get('project_name') or '?'):<20} "
                   f"{r['rule_type']:<8} {r['pattern']:<20} "
                   f"{(r.get('matrix_name') or '-'):<20} {enabled}")


# ── Remote command handlers ─────────────────────────────────────────────


def _list_runs_remote(project, git_ref, kind, status, limit):
    def op(c):
        rows = c.list_runs(project=project, kind=kind, status=status, limit=limit)
        if git_ref:
            rows = [r for r in rows if r.get("git_ref") == git_ref]
        _format_runs(rows)
    _remote(op)


def _show_run_remote(run_id):
    def op(c):
        _format_run_detail(c.get_run(run_id))
    _remote(op)


def _show_results_remote(project, git_ref, kind, status, limit):
    def op(c):
        rows = c.list_runs(project=project, kind=kind, status=status, limit=limit)
        if git_ref:
            rows = [r for r in rows if r.get("git_ref") == git_ref]
        if not rows:
            click.echo("No results found.")
            return
        click.echo(f"{'ID':<6} {'Project':<20} {'Kind':<14} {'Status':<10} "
                   f"{'Duration':<10} {'Started'}")
        click.echo("-" * 90)
        for r in rows:
            click.echo(f"{r['id']:<6} {(r.get('project') or '-'):<20} "
                       f"{(r.get('kind') or '-'):<14} {_row_status(r):<10} "
                       f"{_fmt_duration(r.get('duration_seconds')):<10} "
                       f"{_fmt_started(r.get('started_at'))}")
    _remote(op)


def _list_projects_remote():
    _remote(lambda c: _format_projects(c.list_projects()))


def _add_project_remote(name, github, git_url, opp_env_name, deps):
    dep_list = [d.strip() for d in deps.split(",") if d.strip()] if deps else []

    def op(c):
        p = c.add_project(name, github=github, git_url=git_url,
                          opp_env_name=opp_env_name, deps=dep_list)
        click.echo(f"Project '{p['name']}' added (deps={p.get('deps')}).")
    _remote(op)


def _sync_catalog_remote():
    def op(c):
        result = c.sync_catalog()
        click.echo(f"Catalog sync complete: {result.get('new_projects', 0)} new "
                   f"projects, {result.get('new_versions', 0)} new versions.")
    _remote(op)


def _list_versions_remote(project):
    _remote(lambda c: _format_versions(c.list_versions(project=project)))


def _add_version_remote(project, label, git_ref, opp_env_version, deps):
    import json as _json
    resolved = _json.loads(deps) if deps else None

    def op(c):
        v = c.add_version(project, label, git_ref=git_ref,
                          opp_env_version=opp_env_version, deps=resolved)
        click.echo(f"Version '{label}' added for project '{project}' "
                   f"(ref: {v.get('git_ref')})")
    _remote(op)


def _list_matrices_remote():
    _remote(lambda c: _format_matrices(c.list_matrices()))


def _create_matrix_remote(name, project, kinds, modes, os_names, os_versions,
                          distros, distro_versions, flavors, flavor_versions,
                          compilers, compiler_versions, arches, versions, refs,
                          ref_range, deps, isolation, toolchain, opp_file, replace):
    from opp_ci.scheduler import _build_matrix_config

    try:
        config = _build_matrix_config(
            project=project, kinds=kinds, modes=modes, versions=versions,
            os_names=os_names, os_versions=os_versions,
            distros=distros, distro_versions=distro_versions,
            flavors=flavors, flavor_versions=flavor_versions,
            compilers=compilers, compiler_versions=compiler_versions,
            arches=arches, refs=refs, ref_range=ref_range,
            deps=deps, isolation=isolation, toolchain=toolchain,
        )
    except ValueError as e:
        raise click.ClickException(str(e))

    # The config carries ref_range under its own key; the REST create
    # endpoint takes it as a sibling field, so lift it out.
    ref_range_dict = config.pop("ref_range", None)

    from opp_ci.client import OppCiClientError

    def op(c):
        try:
            result = c.create_matrix(name, project, config,
                                     opp_file=opp_file, ref_range=ref_range_dict)
        except OppCiClientError as e:
            if e.status_code == 409 and replace:
                # Matrices are immutable server-side (no matrix-delete
                # endpoint), so the local delete-then-create --replace
                # affordance has no remote equivalent.
                click.echo("ERROR: --replace is not supported over --remote "
                           "(the coordinator has no matrix-delete endpoint). "
                           "Use a different --name, or replace it on the "
                           "coordinator host.", err=True)
                raise SystemExit(1)
            raise
        click.echo(f"Matrix '{result['name']}' created "
                   f"({result.get('jobs_count', '?')} jobs when expanded).")
    _remote(op)


def _run_matrix_remote(matrix_name, **kwargs):
    if not matrix_name:
        click.echo("ERROR: --remote run-matrix requires --matrix NAME "
                   "(inline/anonymous specs are local-only).", err=True)
        raise SystemExit(1)

    def op(c):
        result = c.run_matrix(matrix_name)
        click.echo(f"Matrix '{result['matrix_name']}' queued "
                   f"{result['jobs_queued']} job(s): {result['run_ids']}")
    _remote(op)


def _seed_projects_remote():
    def op(c):
        r = c.seed_projects()
        click.echo(f"Core projects seeded ({r.get('inserted', 0)} new).")
    _remote(op)


def _seed_platforms_remote():
    def op(c):
        r = c.seed_platforms()
        click.echo(f"Platforms seeded: {r.get('os_inserted', 0)} new OS row(s), "
                   f"{r.get('compilers_inserted', 0)} new compiler row(s).")
    _remote(op)


def _seed_matrices_remote():
    def op(c):
        r = c.seed_matrices()
        click.echo(f"Seeded {r.get('inserted', 0)} default matrices.")
    _remote(op)


def _user_create_remote(username, role, password, update_password):
    if password is None:
        password = click.prompt("Password", hide_input=True,
                                confirmation_prompt=True)

    def op(c):
        u = c.create_user(username, password, role=role,
                          update_password=update_password)
        click.echo(f"User '{u['username']}' "
                   f"{'updated' if update_password else 'created'} "
                   f"(role={u['role']}).")
    _remote(op)


def _user_list_remote():
    _remote(lambda c: _format_users(c.list_users()))


def _user_disable_remote(username):
    def op(c):
        c.update_user(username, enabled=False)
        click.echo(f"User '{username}' disabled.")
    _remote(op)


def _worker_register_remote(name, tags, auto_tags, concurrency):
    explicit = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
    detected = _detect_capability_tags() if auto_tags else []
    seen, tag_list = set(), []
    for t in detected + explicit:
        if t not in seen:
            tag_list.append(t)
            seen.add(t)

    def op(c):
        w = c.register_worker(name, tags=tag_list, concurrency=concurrency)
        click.echo(f"Worker '{w['name']}' registered.")
        click.echo(f"  ID:    {w['id']}")
        click.echo(f"  Token: {w['token']}")
        click.echo(f"  Tags:  {tag_list}")
        if auto_tags and detected:
            click.echo(f"  (detected: {', '.join(detected)} on THIS host — "
                       f"usually not the worker host under --remote)")
    _remote(op)


def _worker_list_remote():
    _remote(lambda c: _format_workers(c.list_workers()))


# Tag namespaces owned by _detect_capability_tags(). A `--auto-tags` refresh
# replaces these in place while leaving hand-added custom tags untouched, so a
# host that was re-imaged / upgraded (new distro version, new compiler) doesn't
# accumulate stale platform tags.
_AUTO_TAG_PREFIXES = ("os:", "distro:", "flavor:", "arch:", "compiler:")
_AUTO_TAG_LITERALS = frozenset({"podman", "nix"})


def _is_auto_managed_tag(tag):
    """True for a tag that _detect_capability_tags() would produce."""
    return tag in _AUTO_TAG_LITERALS or tag.startswith(_AUTO_TAG_PREFIXES)


def _resolve_tags(current, replace, add, remove, detected=None):
    """Compute the new tag list from a replace/refresh/add/remove spec.

    Returns the new list, or None when no tag change was requested (so callers
    can leave tags untouched). `replace` wins as the base set; `detected` (from
    _detect_capability_tags(), passed when --auto-tags is set) then refreshes
    the auto-managed namespace of that base — dropping stale os:/distro:/...
    /podman/nix tags and re-seeding the freshly detected ones while preserving
    custom tags; `add`/`remove` layer on last.
    """
    if replace is None and add is None and remove is None and detected is None:
        return None
    if replace is not None:
        result = [t.strip() for t in replace.split(",") if t.strip()]
    else:
        result = list(current)
    if detected is not None:
        result = list(detected) + [t for t in result if not _is_auto_managed_tag(t)]
    if add:
        for t in (s.strip() for s in add.split(",")):
            if t and t not in result:
                result.append(t)
    if remove:
        drop = {s.strip() for s in remove.split(",") if s.strip()}
        result = [t for t in result if t not in drop]
    return result


def _remote_worker_tags(c, worker_id):
    for w in c.list_workers():
        if w["id"] == worker_id:
            return w.get("tags") or []
    raise click.ClickException(f"Worker #{worker_id} not found.")


def _worker_update_remote(worker_id, concurrency, tags, add_tags, remove_tags, auto_tags):
    # Detection runs on THIS host (the one driving --remote), which is usually
    # not the worker's host — mirror the register-time caveat.
    detected = _detect_capability_tags() if auto_tags else None
    need_current = bool(add_tags or remove_tags or auto_tags)

    def op(c):
        new_tags = _resolve_tags(
            _remote_worker_tags(c, worker_id) if need_current else [],
            tags, add_tags, remove_tags, detected)
        w = c.update_worker(worker_id, concurrency=concurrency, tags=new_tags)
        click.echo(f"Worker #{w['id']} ({w['name']}) updated.")
        click.echo(f"  Concurrency: {w['concurrency']}")
        click.echo(f"  Tags:        {w['tags']}")
        if auto_tags and detected:
            click.echo(f"  (detected:   {', '.join(detected)} on THIS host — "
                       f"usually not the worker host under --remote)")
    _remote(op)


def _worker_enable_remote(worker_id):
    def op(c):
        w = c.update_worker(worker_id, enabled=True)
        click.echo(f"Worker #{w['id']} ({w['name']}) enabled.")
    _remote(op)


def _worker_disable_remote(worker_id):
    def op(c):
        w = c.update_worker(worker_id, enabled=False)
        click.echo(f"Worker #{w['id']} ({w['name']}) disabled "
                   f"(draining; in-flight jobs finish).")
    _remote(op)


def _worker_delete_remote(worker_id, yes):
    def op(c):
        if not yes:
            click.confirm(f"Delete worker #{worker_id}?", abort=True)
        c.delete_worker(worker_id)
        click.echo(f"Worker #{worker_id} deleted.")
    _remote(op)


def _token_create_remote(name, role):
    def op(c):
        t = c.create_token(name, role=role)
        click.echo("Token created:")
        click.echo(f"  Name:  {t['name']}")
        click.echo(f"  Role:  {t['role']}")
        click.echo(f"  Token: {t['token']}")
    _remote(op)


def _token_list_remote():
    _remote(lambda c: _format_tokens(c.list_tokens()))


def _token_revoke_remote(token_id):
    def op(c):
        c.revoke_token(token_id)
        click.echo(f"Token #{token_id} revoked.")
    _remote(op)


def _rule_create_remote(project, rule_type, pattern, matrix_name, replace):
    def op(c):
        if replace:
            for r in c.list_rules():
                if (r.get("project_name") == project
                        and r.get("rule_type") == rule_type
                        and r.get("pattern") == pattern
                        and (r.get("matrix_name") or None) == (matrix_name or None)):
                    c.delete_rule(r["id"])
        r = c.create_rule(project, rule_type, pattern, matrix_name=matrix_name)
        matrix_desc = r.get("matrix_name") or "(smoke only)"
        click.echo(f"Rule #{r['id']}: {project} {rule_type} '{pattern}' "
                   f"-> {matrix_desc}")
    _remote(op)


def _rule_list_remote():
    _remote(lambda c: _format_rules(c.list_rules()))


def _rule_delete_remote(rule_id):
    def op(c):
        c.delete_rule(rule_id)
        click.echo(f"Rule #{rule_id} deleted.")
    _remote(op)


def _rule_test_webhook_remote(project, ref, event_type, sha, pr_number):
    api_event = "pr" if event_type == "pr" else "push"

    def op(c):
        result = c.test_webhook(project, ref, api_event, sha=sha,
                                pr_number=pr_number)
        click.echo(f"Result: {result}")
    _remote(op)


def _image_build_matrix_remote(matrix_name, push):
    """Remote handler for `image build-matrix`: read the matrix from the
    coordinator, then build the images locally (podman lives here, not on
    the coordinator host)."""
    from opp_ci.scheduler import expand_matrix

    def op(c):
        matrices = [m for m in c.list_matrices() if m["name"] == matrix_name]
        if not matrices:
            click.echo(f"Matrix '{matrix_name}' not found on coordinator.")
            return
        matrix = matrices[0]
        jobs = expand_matrix(matrix["project"], matrix.get("config") or {})
        _build_matrix_images(jobs, matrix_name, push)
    _remote(op)


def _resolve_deps_info(project_version, pins):
    """`resolve-deps` is a pure local computation over opp_env metadata."""
    click.echo("resolve-deps runs locally (it queries opp_env metadata); "
               "--remote has no effect. Re-run without --remote.")


def _build_matrix_images(jobs, matrix_name, push):
    """Build every opp-ci-runner image referenced by a matrix's expansion.

    Shared by the local and remote `image build-matrix` paths — the only
    difference is where the matrix definition came from. Building always
    happens on THIS host (podman is local).
    """
    seen = set()
    for job in jobs:
        if (job.get("isolation") or "none") != "podman":
            continue
        deps = job.get("resolved_deps") or {}
        omnetpp_version = deps.get("omnetpp") if isinstance(deps, dict) else None
        key = (
            job.get("toolchain") or "none",
            job.get("os"), job.get("os_version"),
            job.get("distro"), job.get("distro_version"),
            job.get("flavor"), job.get("flavor_version"),
            job.get("compiler"), job.get("compiler_version"),
            omnetpp_version,
        )
        if key in seen:
            continue
        seen.add(key)
        toolchain_arg = "nix" if key[0] == "nix" else "host"
        click.echo(f"  → building image for {key}")
        _build_one_image(
            os_name=key[1], os_version=key[2],
            distro=key[3], distro_version=key[4],
            flavor=key[5], flavor_version=key[6],
            compiler=key[7], compiler_version=key[8],
            omnetpp_version=key[9], toolchain=toolchain_arg, push=push,
        )
    click.echo(f"Built {len(seen)} image(s) for matrix '{matrix_name}'")


@main.command()
@remoteable(_refuse_remote("init-db"))
def init_db():
    """Create database tables."""
    Base.metadata.create_all(engine)
    click.echo("Database tables created.")


@main.command("reset-db")
@click.option("--yes", is_flag=True, help="Skip confirmation prompt")
@click.option("--preserve-tokens", is_flag=True,
              help="Snapshot api_tokens and workers rows and restore them after the reset, "
                   "so external systems (GitHub Actions, remote workers) keep working.")
@remoteable(_refuse_remote("reset-db"))
def reset_db(yes, preserve_tokens):
    """Drop all tables and recreate them (destructive!)."""
    if not yes:
        click.confirm("This will DELETE all data. Continue?", abort=True)

    saved_api_tokens = []
    saved_workers = []
    if preserve_tokens:
        Base.metadata.create_all(engine)
        session = SessionLocal()
        try:
            saved_api_tokens = [
                {c.name: getattr(t, c.name) for c in ApiToken.__table__.columns}
                for t in session.execute(select(ApiToken)).scalars().all()
            ]
            saved_workers = [
                {c.name: getattr(w, c.name) for c in Worker.__table__.columns}
                for w in session.execute(select(Worker)).scalars().all()
            ]
        finally:
            session.close()

    _drop_everything(engine)
    Base.metadata.create_all(engine)

    if preserve_tokens and (saved_api_tokens or saved_workers):
        session = SessionLocal()
        try:
            for row in saved_api_tokens:
                session.add(ApiToken(**row))
            for row in saved_workers:
                session.add(Worker(**row))
            session.commit()
            _resync_sequences(engine)
            click.echo("Database reset.")
            token_names = ", ".join(r["name"] for r in saved_api_tokens) or "(none)"
            worker_names = ", ".join(r["name"] for r in saved_workers) or "(none)"
            click.echo(f"  Preserved api tokens: {token_names}")
            click.echo(f"  Preserved workers:    {worker_names}")
        finally:
            session.close()
    else:
        click.echo("Database reset.")


def _drop_everything(engine):
    """Drop every table in the live schema, not just those the current
    models know about.

    `Base.metadata.drop_all` walks current-model tables in FK order, but
    legacy tables left over from a prior schema (e.g. `test_results`
    from the pre-Phase-1 cutover) keep an FK on a model table and break
    the drop. Reflecting + dropping with CASCADE is robust to whatever
    is actually in the database. On Postgres we drop and recreate the
    `public` schema in one stroke; on other backends we fall back to
    SQLAlchemy reflection plus a per-table drop with CASCADE.
    """
    from sqlalchemy import MetaData, text

    backend = engine.url.get_backend_name()
    if backend == "postgresql":
        with engine.begin() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
        return
    existing = MetaData()
    existing.reflect(bind=engine)
    existing.drop_all(bind=engine)


def _resync_sequences(engine):
    """Advance Postgres identity sequences to MAX(id)+1.

    `reset-db --preserve-tokens` restores api_tokens/workers rows with
    their original primary keys, but `create_all` resets each table's
    sequence to 1. Without this the next auto-generated id collides with a
    preserved row (UniqueViolation on workers_pkey). No-op on backends
    without sequences (e.g. SQLite, which tracks MAX automatically).
    """
    from sqlalchemy import text

    if engine.url.get_backend_name() != "postgresql":
        return
    with engine.begin() as conn:
        cols = conn.execute(text(
            "SELECT table_name, column_name, "
            "       pg_get_serial_sequence(table_name, column_name) AS seq "
            "FROM information_schema.columns "
            "WHERE table_schema = 'public' "
            "  AND pg_get_serial_sequence(table_name, column_name) IS NOT NULL"
        )).all()
        for table, col, seq in cols:
            conn.execute(
                text(f'SELECT setval(:s, '
                     f'COALESCE((SELECT MAX("{col}") FROM "{table}"), 0) + 1, false)'),
                {"s": seq},
            )


@main.command("run")
@click.option("--project", default=None, help="opp_env project name (e.g. inet-4.5)")
@click.option("--kind", "kinds", default=None, help="Kind(s) of test, comma-separated (e.g. smoke,fingerprint)")
@click.option("--name", default=None, help="Label this test so it can be re-run by name later (single --kind only)")
@click.option("--test", "test_name", default=None, help="Run an existing named test (skips the coordinate flags)")
@click.option("--ref", "git_ref", default=None, help="Git branch, tag, or commit to test (e.g. master, topic/my-feature)")
@click.option("--mode", default=None, type=click.Choice(["debug", "release"]), help="Build mode (debug or release)")
@click.option("--isolation", default="none", type=click.Choice(["none", "podman"]), help="Run on the host (none) or inside a Podman container")
@click.option("--toolchain", default="none", type=click.Choice(["none", "nix"]), help="Use the host's installed toolchain (none) or opp_env/Nix")
@click.option("--os", "os_name", default=None, type=click.Choice(["Linux", "Windows", "MacOS"], case_sensitive=False),
              help="OS family for the run: Linux, Windows, or MacOS")
@click.option("--os-version", default=None, help="OS version (Windows/MacOS only, e.g. '11', '15.1')")
@click.option("--distro", default=None, help="Linux distribution name (e.g. 'Ubuntu'), or combined 'Ubuntu 24.04'")
@click.option("--distro-version", default=None, help="Distribution version (e.g. '24.04')")
@click.option("--flavor", default=None, help="Distribution flavor (e.g. 'Kubuntu'), or combined 'Kubuntu 24.04'")
@click.option("--flavor-version", default=None, help="Flavor version; defaults to --distro-version when unset")
@click.option("--arch", default=None, help="CPU architecture (e.g. 'amd64', 'aarch64'); omit to leave unconstrained")
@click.option("--compiler", default=None, help="Compiler name (e.g. 'clang'); required for isolation=podman + toolchain=none")
@click.option("--compiler-version", default=None, help="Compiler version (e.g. '22')")
@click.option("--pin", "pins", multiple=True, help="Pin dependency version (e.g. --pin omnetpp=6.1). Repeatable.")
@click.option("--expect", "expected_result_code", default=None,
              type=click.Choice(["PASS", "FAIL", "ERROR"], case_sensitive=False),
              help="Expected result stamped on a newly-created test; omit to use the global default.")
@click.option("--skip-install", is_flag=True, help="Skip opp_env install step")
@click.pass_context
def run_cmd(ctx, project, kinds, name, test_name, git_ref, mode, isolation, toolchain,
            os_name, os_version, distro, distro_version, flavor, flavor_version,
            arch, compiler, compiler_version, pins, expected_result_code, skip_install):
    """Run test(s) for a project and store the results.

    Either give a coordinate (--project/--kind …) or run a previously
    named test with --test NAME. The two modes are mutually exclusive.
    """
    from opp_ci import platforms
    from opp_ci.scheduler import _parse_name_version
    from opp_ci.persistence import get_test_by_name

    if test_name:
        if project or kinds or name:
            raise click.ClickException(
                "Pick exactly one of: --test NAME, or --project/--kind coordinate flags."
            )
        if ctx.obj.get("remote"):
            raise click.ClickException("--test (run by name) is not supported with --remote.")
        # Unpack the named test's coordinate into the run variables; the
        # stored fields are already canonical, so skip resolve_platform.
        Base.metadata.create_all(engine)
        _s = SessionLocal()
        try:
            named = get_test_by_name(_s, test_name)
            if named is None:
                raise click.ClickException(f"Test {test_name!r} not found.")
            project = named.project
            kinds = named.kind
            mode = named.mode
            os_name = named.os
            os_version = named.os_version
            distro = named.distro
            distro_version = named.distro_version
            flavor = named.flavor
            flavor_version = named.flavor_version
            arch = named.arch
            compiler = named.compiler
            compiler_version = named.compiler_version
            isolation = named.isolation or "none"
            toolchain = named.toolchain or "none"
        finally:
            _s.close()
    else:
        if not project or not kinds:
            raise click.ClickException("--project and --kind are required (or use --test NAME).")
        if name and len([k for k in kinds.split(",") if k.strip()]) > 1:
            raise click.ClickException("--name applies to a single test; pass exactly one --kind.")
        # Allow combined 'Ubuntu 24.04' / 'Kubuntu 24.04' shorthand on --distro/--flavor.
        if distro and not distro_version:
            distro, distro_version = _parse_name_version(distro)
        if flavor and not flavor_version:
            flavor, flavor_version = _parse_name_version(flavor)
        try:
            resolved_os, resolved_distro, resolved_flavor = platforms.resolve_platform(
                os=os_name, distro=distro, flavor=flavor,
            )
        except ValueError as e:
            raise click.ClickException(str(e))
        os_name = platforms._os_canonical(resolved_os) if resolved_os else None
        distro = resolved_distro
        flavor = resolved_flavor
        if os_name == "Linux":
            os_version = None
        if not resolved_distro:
            distro_version = None
        if not resolved_flavor:
            flavor_version = None

    if ctx.obj.get("remote"):
        _run_remote(
            project, kinds, git_ref,
            mode=mode, isolation=isolation, toolchain=toolchain,
            os_name=os_name, os_version=os_version,
            distro=distro, distro_version=distro_version,
            flavor=flavor, flavor_version=flavor_version,
            arch=arch,
            compiler=compiler, compiler_version=compiler_version,
            pins=pins, expected_result_code=expected_result_code,
        )
        return

    from opp_ci.dependency import complete_lock_for_submit, parse_pins, format_resolved_deps
    from opp_ci.persistence import parse_expectation_override
    try:
        default_expectation = parse_expectation_override(expected_result_code)
    except ValueError:
        raise click.ClickException(f"Invalid --expect value: {expected_result_code!r}")

    Base.metadata.create_all(engine)
    session = SessionLocal()
    try:
        try:
            pin_dict = parse_pins(pins) if pins else {}
            # Always pin the complete transitive lock — not only when --pin is
            # given (that gap is how unpinned runs used to slip through).
            resolved_deps = complete_lock_for_submit(project, pins=pin_dict) or None
            if resolved_deps:
                click.echo(f"Resolved dependencies: {format_resolved_deps(resolved_deps)}")
        except ValueError as e:
            click.echo(f"ERROR: {e}")
            return

        # Resolve any loose coordinate axis against THIS host — the local run
        # executes here, so an under-specified local run is pinned to the host's
        # own compiler/arch/platform (the same resolver the fleet path uses,
        # just with local tags as the source). Done before install/run so build,
        # execution, and Test identity all use the resolved coordinate.
        from opp_ci.fleet import resolve_loose_axes
        _coord = {"os": os_name, "os_version": os_version, "distro": distro,
                  "distro_version": distro_version, "flavor": flavor,
                  "flavor_version": flavor_version, "arch": arch,
                  "compiler": compiler, "compiler_version": compiler_version,
                  "mode": mode}
        try:
            resolve_loose_axes(_coord, set(_detect_capability_tags()))
            resolve_incomplete = False
        except ValueError:
            # Partial fill; the per-kind validate_test_coord reports the rest,
            # and names this host as the cause when it couldn't supply an axis.
            resolve_incomplete = True
        os_name, os_version = _coord["os"], _coord.get("os_version")
        distro, distro_version = _coord["distro"], _coord.get("distro_version")
        arch, compiler = _coord["arch"], _coord["compiler"]
        compiler_version, mode = _coord["compiler_version"], _coord["mode"]

        if not skip_install:
            try:
                install_project(project, git_ref=git_ref,
                                isolation=isolation, toolchain=toolchain,
                                resolved_deps=resolved_deps,
                                compiler=compiler, compiler_version=compiler_version)
            except RuntimeError as e:
                click.echo(f"ERROR during install: {e}")
                return

        for kind in kinds.split(","):
            kind = kind.strip()
            if not kind:
                continue

            coord = {
                "project": project,
                "kind": kind,
                "mode": mode,
                "os": os_name,
                "os_version": os_version,
                "distro": distro,
                "distro_version": distro_version,
                "flavor": flavor,
                "flavor_version": flavor_version,
                "arch": arch,
                "compiler": compiler,
                "compiler_version": compiler_version,
                "isolation": isolation,
                "toolchain": toolchain,
                "opp_file": None,
                "resolved_deps": resolved_deps,
            }
            from opp_ci.persistence import validate_test_coord
            try:
                validate_test_coord(coord)
            except ValueError as e:
                if resolve_incomplete:
                    raise click.ClickException(
                        f"Couldn't resolve the unspecified coordinate against "
                        f"this host — it doesn't provide the missing axes. {e} "
                        f"Either specify them, or install them on this host.")
                raise click.ClickException(str(e))
            test = get_or_create_test(
                session, coord, default_expectation=default_expectation,
            )
            if name:
                from opp_ci.persistence import set_test_name
                try:
                    set_test_name(session, test, name)
                except ValueError as e:
                    raise click.ClickException(str(e))
            test_run = create_test_run(
                session,
                test_id=test.id,
                git_ref=git_ref,
                resolved_deps=resolved_deps,
            )
            test_run.lifecycle = TestRunLifecycle.running
            test_run.started_at = datetime.datetime.utcnow()
            try:
                test_run.system_snapshot = capture_system_snapshot()
            except Exception:
                pass
            session.commit()

            desc = f"{project}@{git_ref}" if git_ref else project
            click.echo(f"Test run #{test_run.id}: {desc} / {kind}")

            try:
                outcome = run_test(
                    project, kind, git_ref=git_ref, mode=mode,
                    isolation=isolation, toolchain=toolchain,
                    os=os_name, os_version=os_version,
                    distro=distro, distro_version=distro_version,
                    flavor=flavor, flavor_version=flavor_version,
                    arch=arch,
                    compiler=compiler, compiler_version=compiler_version,
                    resolved_deps=resolved_deps,
                )
            except Exception as e:
                test_run.lifecycle = TestRunLifecycle.finished
                test_run.result_code = TestResultCode.ERROR
                test_run.stderr = str(e)
                test_run.finished_at = datetime.datetime.utcnow()
                finalize_verdict_for_run(session, test_run.id)
                session.commit()
                click.echo(f"  ERROR: {e}")
                continue

            test_run.lifecycle = TestRunLifecycle.finished
            test_run.result_code = TestResultCode(outcome["result_code"])
            test_run.finished_at = datetime.datetime.utcnow()
            test_run.test_exec_seconds = outcome["test_exec_seconds"]
            test_run.commit_sha = outcome.get("commit_sha")
            test_run.stdout = outcome["stdout"]
            test_run.stderr = outcome["stderr"]
            test_run.details = outcome.get("details")
            finalize_verdict_for_run(session, test_run.id)
            session.commit()
            update_ci_note(project, test_run.commit_sha, session)
            click.echo(f"  Result: {outcome['result_code']} ({outcome['test_exec_seconds']:.1f}s)")
    finally:
        session.close()


def _run_remote(project, kinds, git_ref, *, mode=None,
                isolation=None, toolchain=None, os_name=None, os_version=None,
                distro=None, distro_version=None,
                flavor=None, flavor_version=None,
                arch=None, compiler=None, compiler_version=None,
                pins=None, expected_result_code=None):
    """Submit test run(s) to the remote coordinator."""
    from opp_ci.config import COORDINATOR_URL, API_TOKEN
    from opp_ci.client import OppCiClient

    if not API_TOKEN:
        click.echo("ERROR: Set OPP_CI_API_TOKEN env var for remote submission.")
        return

    base = COORDINATOR_URL.rstrip("/")
    api_url = base if base.endswith("/api") else base + "/api"

    client = OppCiClient(url=api_url, token=API_TOKEN)
    for kind in kinds.split(","):
        kind = kind.strip()
        if not kind:
            continue
        try:
            result = client.submit_run(
                project=project, kind=kind, git_ref=git_ref,
                mode=mode, isolation=isolation, toolchain=toolchain,
                os=os_name, os_version=os_version,
                distro=distro, distro_version=distro_version,
                flavor=flavor, flavor_version=flavor_version,
                arch=arch,
                compiler=compiler, compiler_version=compiler_version,
                pins=list(pins) if pins else None,
                expected_result_code=expected_result_code,
            )
            click.echo(f"Submitted run #{result['id']}: {project} / {kind} → {result['lifecycle']}")
        except Exception as e:
            click.echo(f"ERROR submitting {project}/{kind}: {e}")


@main.command("serve")
@click.option("--host", default=None,
              help="Bind host (default from $OPP_CI_SERVE_HOST, or 127.0.0.1)")
@click.option("--port", default=None, type=int,
              help="Bind port (default from $OPP_CI_SERVE_PORT, or 8080)")
@click.option("--cert", "cert_file", default=None,
              help="TLS certificate file (default $OPP_CI_SERVE_TLS_CERT_FILE)")
@click.option("--key", "key_file", default=None,
              help="TLS private key file (default $OPP_CI_SERVE_TLS_KEY_FILE)")
@remoteable(_refuse_remote("serve"))
def serve(host, port, cert_file, key_file):
    """Start the web UI server."""
    import os
    import uvicorn
    from opp_ci.web.app import app
    from opp_ci import config as cfg
    Base.metadata.create_all(engine)
    if host is None:
        host = cfg.SERVE_HOST
    if port is None:
        port = cfg.SERVE_PORT
    if cert_file is None:
        cert_file = cfg.SERVE_TLS_CERT_FILE
    if key_file is None:
        key_file = cfg.SERVE_TLS_KEY_FILE

    # ── TLS validation ──────────────────────────────────────────────
    if bool(cert_file) != bool(key_file):
        raise click.UsageError(
            "TLS requires both --cert/$OPP_CI_SERVE_TLS_CERT_FILE and "
            "--key/$OPP_CI_SERVE_TLS_KEY_FILE; set both or neither."
        )
    tls_on = bool(cert_file)
    if tls_on:
        for label, path in (("cert", cert_file), ("key", key_file)):
            if not os.access(path, os.R_OK):
                raise click.UsageError(
                    f"TLS {label} file is not readable: {path} "
                    f"(check ownership/permissions)"
                )
        # Auto-flip secure cookies when TLS is on, unless the operator
        # explicitly opted out via env var. Silent on plain HTTP.
        if not os.environ.get("OPP_CI_SESSION_COOKIE_SECURE"):
            cfg.SESSION_COOKIE_SECURE = True
            click.echo("TLS on → forcing OPP_CI_SESSION_COOKIE_SECURE=1")
        # OAuth callback URL must be HTTPS when TLS is on.
        if cfg.GITHUB_OAUTH_CLIENT_ID and not cfg.PUBLIC_URL:
            raise click.UsageError(
                "OPP_CI_PUBLIC_URL is required when TLS and GitHub OAuth "
                "are both enabled (set it to your public https:// URL)."
            )
    else:
        if cfg.SESSION_COOKIE_SECURE:
            click.echo(
                "WARNING: OPP_CI_SESSION_COOKIE_SECURE=1 but TLS is off — "
                "session cookies will not be sent over plain HTTP."
            )

    scheme = "https" if tls_on else "http"
    click.echo(f"Starting opp_ci web UI at {scheme}://{host}:{port}")
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="info",
        ssl_certfile=cert_file or None,
        ssl_keyfile=key_file or None,
        ssl_keyfile_password=cfg.get_tls_key_password() or None,
    )


@main.command("tls-selfsign")
@click.option("--host", required=True,
              help="Primary hostname to put in the cert's Common Name and SAN list "
                   "(e.g. ci.lab.local). Add more with --extra-san.")
@click.option("--extra-san", "extra_sans", multiple=True,
              help="Additional Subject Alternative Names. May be repeated. "
                   "IP addresses (e.g. 10.0.0.5) are recognized.")
@click.option("--out", "out_dir", default="/etc/opp_ci/tls",
              help="Output directory. Writes fullchain.pem + privkey.pem there.")
@click.option("--days", default=365, type=int, help="Validity in days.")
@click.option("--bits", default=4096, type=int, help="RSA key size.")
@click.option("--dry-run", is_flag=True,
              help="Print the PEMs to stdout instead of writing to --out.")
@remoteable(_refuse_remote("tls-selfsign"))
def tls_selfsign(host, extra_sans, out_dir, days, bits, dry_run):
    """Generate a self-signed TLS cert + key for lab / smoke-test use.

    Not for production — workers refuse the resulting cert unless their
    OPP_CI_TLS_CA_BUNDLE points at fullchain.pem (or OPP_CI_TLS_INSECURE=1).
    For real deploys use a Cloudflare Origin Certificate, Let's Encrypt,
    or a corporate CA.
    """
    import ipaddress
    import os
    import socket as _socket
    import stat

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    # Build SAN list: --host, --extra-san, plus the usual local fallbacks
    # so a same-host worker can connect by 127.0.0.1 / localhost / the
    # machine's hostname without certificate-name mismatches.
    san_strings = [host, *extra_sans, "localhost", _socket.gethostname()]
    san_entries = []
    seen = set()
    for name in san_strings:
        if not name or name in seen:
            continue
        seen.add(name)
        try:
            ip = ipaddress.ip_address(name)
            san_entries.append(x509.IPAddress(ip))
        except ValueError:
            san_entries.append(x509.DNSName(name))
    # 127.0.0.1 added unconditionally as an IPAddress entry.
    loopback = ipaddress.ip_address("127.0.0.1")
    if not any(isinstance(e, x509.IPAddress) and e.value == loopback for e in san_entries):
        san_entries.append(x509.IPAddress(loopback))

    key = rsa.generate_private_key(public_exponent=65537, key_size=bits)
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, host),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "opp_ci self-signed"),
    ])
    now = datetime.datetime.utcnow()
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=days))
        .add_extension(x509.SubjectAlternativeName(san_entries), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, key_encipherment=True, key_agreement=False,
                content_commitment=False, data_encipherment=False, key_cert_sign=False,
                crl_sign=False, encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .sign(private_key=key, algorithm=hashes.SHA256())
    )

    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )

    if dry_run:
        click.echo("# fullchain.pem")
        click.echo(cert_pem.decode())
        click.echo("# privkey.pem")
        click.echo(key_pem.decode())
        return

    os.makedirs(out_dir, exist_ok=True)
    cert_path = os.path.join(out_dir, "fullchain.pem")
    key_path = os.path.join(out_dir, "privkey.pem")
    # Write key first then cert, so a watcher (.path unit) that fires on
    # fullchain.pem sees a consistent pair.
    _write_secure(key_path, key_pem, mode=0o640)
    _write_secure(cert_path, cert_pem, mode=0o640)
    # If running as root, hand the files to root:opp_ci so the service
    # account can read them. Best-effort: ignore if the group is missing.
    if os.geteuid() == 0:
        try:
            import grp
            gid = grp.getgrnam("opp_ci").gr_gid
            os.chown(cert_path, 0, gid)
            os.chown(key_path, 0, gid)
        except KeyError:
            pass

    click.echo(f"Wrote {cert_path} and {key_path} (valid {days} days)")
    click.echo("")
    click.echo("Next steps:")
    click.echo(f"  serve:   set OPP_CI_SERVE_TLS_CERT_FILE={cert_path}")
    click.echo(f"                OPP_CI_SERVE_TLS_KEY_FILE={key_path}")
    click.echo(f"  workers: set OPP_CI_TLS_CA_BUNDLE={cert_path}  "
               "(or OPP_CI_TLS_INSECURE=1 for dev)")


def _write_secure(path, data, *, mode):
    """Atomically write `data` to `path` with the given mode."""
    import os
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@main.command("list-runs")
@click.option("--project", default=None, help="Filter by project")
@click.option("--ref", "git_ref", default=None, help="Filter by git ref")
@click.option("--kind", default=None, help="Filter by test kind")
@click.option("--status", default=None, help="Filter by status (PASS/FAIL/ERROR/queued/running/cancelled)")
@click.option("--limit", default=20, help="Max rows to show")
@remoteable(_list_runs_remote)
def list_runs(project, git_ref, kind, status, limit):
    """List test runs."""
    session = SessionLocal()
    try:
        query = (
            select(TestRun)
            .join(Test, TestRun.test_id == Test.id)
            .order_by(TestRun.id.desc())
            .limit(limit)
        )
        if project:
            query = query.where(Test.project == project)
        if git_ref:
            query = query.where(TestRun.git_ref == git_ref)
        if kind:
            query = query.where(Test.kind == kind)
        if status:
            try:
                query = status_filter(query, status)
            except ValueError as e:
                raise click.ClickException(str(e))

        runs = session.execute(query).scalars().all()
        if not runs:
            click.echo("No runs found.")
            return

        click.echo(f"{'ID':<6} {'Project':<20} {'Ref':<16} {'Kind':<14} {'Status':<10} {'Verdict':<11} {'Duration':<10} {'Started'}")
        click.echo("-" * 118)
        for run in runs:
            duration = f"{run.duration_seconds:.1f}s" if run.duration_seconds else "-"
            started = run.started_at.strftime("%Y-%m-%d %H:%M") if run.started_at else "-"
            ref = run.git_ref or "-"
            verdict = run.recorded_verdict or "-"
            click.echo(f"{run.id:<6} {run.project:<20} {ref:<16} {run.kind:<14} {run.effective_status:<10} {verdict:<11} {duration:<10} {started}")
    finally:
        session.close()


@main.command("show-run")
@click.argument("run_id", type=int)
@remoteable(_show_run_remote)
def show_run(run_id):
    """Show details of a specific test run."""
    session = SessionLocal()
    try:
        run = session.execute(
            select(TestRun).where(TestRun.id == run_id)
        ).scalar_one_or_none()
        if run is None:
            click.echo(f"Run #{run_id} not found.")
            return

        click.echo(f"Run #{run.id}")
        click.echo(f"  Project:  {run.project}")
        click.echo(f"  Ref:      {run.git_ref or '-'}")
        click.echo(f"  Commit:   {run.commit_sha or '-'}")
        click.echo(f"  Version:  {run.version or '-'}")
        click.echo(f"  Kind:     {run.kind}")
        click.echo(f"  Lifecycle:{run.lifecycle.value}")
        click.echo(f"  Result:   {run.result_code.value if run.result_code else '-'}")
        click.echo(f"  Duration: {run.duration_seconds:.1f}s" if run.duration_seconds else "  Duration: -")
        click.echo(f"  Started:  {run.started_at}")
        click.echo(f"  Finished: {run.finished_at or '-'}")
        click.echo(f"  Trigger:  {run.trigger or '-'}")

        if run.stdout:
            click.echo(f"\n  stdout ({len(run.stdout)} chars):")
            click.echo("    " + run.stdout[:500].replace("\n", "\n    "))
            if len(run.stdout) > 500:
                click.echo("    ...")
        if run.stderr:
            click.echo(f"\n  stderr ({len(run.stderr)} chars):")
            click.echo("    " + run.stderr[:500].replace("\n", "\n    "))
            if len(run.stderr) > 500:
                click.echo("    ...")
    finally:
        session.close()


@main.command("show-results")
@click.option("--project", default=None, help="Filter by project")
@click.option("--ref", "git_ref", default=None, help="Filter by git ref")
@click.option("--kind", default=None, help="Filter by test kind")
@click.option("--status", default=None, help="Filter by status (PASS/FAIL/ERROR)")
@click.option("--limit", default=20, help="Max rows to show")
@remoteable(_show_results_remote)
def show_results(project, git_ref, kind, status, limit):
    """Show test run results (alias for list-runs)."""
    session = SessionLocal()
    try:
        query = (
            select(TestRun)
            .join(Test, TestRun.test_id == Test.id)
            .order_by(TestRun.id.desc())
            .limit(limit)
        )
        if project:
            query = query.where(Test.project == project)
        if git_ref:
            query = query.where(TestRun.git_ref == git_ref)
        if kind:
            query = query.where(Test.kind == kind)
        if status:
            try:
                query = status_filter(query, status)
            except ValueError as e:
                raise click.ClickException(str(e))

        runs = session.execute(query).scalars().all()
        if not runs:
            click.echo("No results found.")
            return

        click.echo(f"{'ID':<6} {'Project':<20} {'Kind':<14} {'Status':<10} {'Verdict':<11} {'Duration':<10} {'Started'}")
        click.echo("-" * 102)
        for run in runs:
            duration = f"{run.duration_seconds:.1f}s" if run.duration_seconds else "-"
            started = run.started_at.strftime("%Y-%m-%d %H:%M") if run.started_at else "-"
            verdict = run.recorded_verdict or "-"
            click.echo(f"{run.id:<6} {run.project:<20} {run.kind:<14} {run.effective_status:<10} {verdict:<11} {duration:<10} {started}")
    finally:
        session.close()


@main.command("seed-projects")
@remoteable(_seed_projects_remote)
def seed_projects_cmd():
    """Seed the database with core projects from the catalog."""
    from opp_ci.catalog import seed_projects
    Base.metadata.create_all(engine)
    session = SessionLocal()
    try:
        seed_projects(session)
        click.echo("Core projects seeded.")
    finally:
        session.close()


@main.command("seed-platforms")
@remoteable(_seed_platforms_remote)
def seed_platforms_cmd():
    """Seed the OS and Compiler tables from opp_ci/podman/platforms.yml.

    Idempotent — existing (name, version) rows are left alone, so editing
    platforms.yml and re-running only inserts new entries. After running,
    the new rows show up on the /os and /compilers pages and in the
    /runs/new autocomplete dropdowns.
    """
    from opp_ci.catalog import seed_platforms
    Base.metadata.create_all(engine)
    session = SessionLocal()
    try:
        os_n, comp_n = seed_platforms(session)
        click.echo(f"Platforms seeded: {os_n} new OS row(s), {comp_n} new compiler row(s).")
    finally:
        session.close()


@main.command("add-project")
@click.option("--name", required=True, help="Project name (e.g. mm1k)")
@click.option("--github", default=None, help="GitHub owner/repo (e.g. levy/mm1k)")
@click.option("--git-url", default=None, help="Git clone URL")
@click.option("--opp-env-name", default=None, help="opp_env project name (defaults to --name)")
@click.option("--deps", default=None, help="Comma-separated dependency project names (e.g. omnetpp,inet)")
@remoteable(_add_project_remote)
def add_project_cmd(name, github, git_url, opp_env_name, deps):
    """Register a new project in the database."""
    Base.metadata.create_all(engine)
    session = SessionLocal()
    try:
        existing = session.execute(
            select(Project).where(Project.name == name)
        ).scalar_one_or_none()
        if existing is not None:
            click.echo(f"Project '{name}' already exists.")
            return

        github_owner = None
        github_repo = None
        if github:
            parts = github.split("/", 1)
            if len(parts) == 2:
                github_owner, github_repo = parts
            else:
                click.echo(f"Invalid --github format: expected 'owner/repo'")
                return

        dep_list = [d.strip() for d in deps.split(",") if d.strip()] if deps else []

        project = Project(
            name=name,
            opp_env_name=opp_env_name or name,
            github_owner=github_owner,
            github_repo=github_repo,
            git_url=git_url,
            dependency_names=dep_list,
        )
        session.add(project)
        session.commit()
        click.echo(f"Project '{name}' added (deps={dep_list}).")
    finally:
        session.close()


@main.command("sync-catalog")
@remoteable(_sync_catalog_remote)
def sync_catalog_cmd():
    """Sync projects from the opp_env catalog into the database.

    Imports all opp_env projects (if not already present), adds new
    versions, and creates a default test matrix for each new project.
    """
    from opp_ci.opp_env_adapter import sync_catalog
    Base.metadata.create_all(engine)
    session = SessionLocal()
    try:
        new_projects, new_versions = sync_catalog(session)
        click.echo(f"Catalog sync complete: {new_projects} new projects, {new_versions} new versions.")
    finally:
        session.close()


@main.command("list-projects")
@remoteable(_list_projects_remote)
def list_projects():
    """List known projects with last test status and GitHub info."""
    Base.metadata.create_all(engine)
    session = SessionLocal()
    try:
        projects = session.execute(
            select(Project).order_by(Project.name)
        ).scalars().all()
        if not projects:
            click.echo("No projects. Run 'opp_ci seed-projects' or 'opp_ci sync-catalog'.")
            return

        click.echo(f"{'Name':<20} {'Last Test':<14} {'Status':<10} {'GitHub'}")
        click.echo("-" * 80)
        for p in projects:
            # Find most recent finished run for this project
            last_run = session.execute(
                select(TestRun)
                .join(Test, TestRun.test_id == Test.id)
                .where(Test.project == p.name)
                .where(TestRun.lifecycle == TestRunLifecycle.finished)
                .order_by(TestRun.id.desc())
                .limit(1)
            ).scalar_one_or_none()

            if last_run:
                version = last_run.version or last_run.git_ref or "-"
                status = last_run.effective_status
            else:
                version = "-"
                status = "-"

            github = f"{p.github_owner}/{p.github_repo}" if p.github_owner else "-"
            click.echo(f"{p.name:<20} {version:<14} {status:<10} {github}")
    finally:
        session.close()


@main.command("create-matrix")
@click.option("--name", required=True, help="Matrix name (e.g. inet-default)")
@click.option("--project", required=True, help="Project name")
@click.option("--project-versions", "versions", default=None, help="Comma-separated project versions (optional, defaults to project name)")
@click.option("--builds", "modes", default="release", help="Comma-separated build modes (default: release)")
@click.option("--os", "os_names", default=None,
              help="Comma-separated OS families: Linux, Windows, MacOS (e.g. 'Linux,Windows')")
@click.option("--os-version", "os_versions", default=None,
              help="Comma-separated OS versions for cross-product (Windows/MacOS only, e.g. '11,15')")
@click.option("--distro", "distros", default=None,
              help="Comma-separated Linux distributions, combined or structured (e.g. 'Ubuntu 24.04,Fedora 41')")
@click.option("--distro-version", "distro_versions", default=None,
              help="Comma-separated distro versions for cross-product (e.g. '24.04,41')")
@click.option("--flavor", "flavors", default=None,
              help="Comma-separated distro flavors (e.g. 'Kubuntu 24.04')")
@click.option("--flavor-version", "flavor_versions", default=None,
              help="Comma-separated flavor versions for cross-product")
@click.option("--compiler", "compilers", default=None, help="Comma-separated compilers (e.g. 'gcc-14,clang-18' or 'gcc,clang' with --compiler-version)")
@click.option("--compiler-version", "compiler_versions", default=None, help="Comma-separated compiler versions for cross-product (e.g. '14,18')")
@click.option("--arch", "arches", default=None, help="Comma-separated CPU architectures (e.g. 'amd64,aarch64')")
@click.option("--kinds", required=True, help="Comma-separated test kinds")
@click.option("--refs", default=None, help="Comma-separated git refs to test (e.g. 'master,topic/my-feature')")
@click.option("--ref-range", "ref_range", default=None, help="Git ref range (base..head) — enumerate commits via GitHub API")
@click.option("--deps", default=None, help="Dependency versions axis (e.g. 'omnetpp=6.3.0,6.2.0;inet=4.5')")
@click.option("--isolation", default=None, help="Comma-separated isolation values: 'none' and/or 'podman' (cross-product axis)")
@click.option("--toolchain", default=None, help="Comma-separated toolchain values: 'none' and/or 'nix' (cross-product axis)")
@click.option("--opp-file", "opp_file", default=None, help="Path to the project's .opp file (for opp_repl project discovery)")
@click.option("--replace", is_flag=True, help="Replace existing matrix with the same name")
@remoteable(_create_matrix_remote)
def create_matrix(name, project, kinds, modes, os_names, os_versions,
                  distros, distro_versions, flavors, flavor_versions,
                  compilers, compiler_versions, arches, versions, refs,
                  ref_range, deps, isolation, toolchain, opp_file, replace):
    """Create a test matrix configuration.

    Platform axes form a three-level hierarchy:

    \b
        --os Linux,Windows,MacOS
        --distro 'Ubuntu 24.04,Fedora 41'   (Linux only; flavor=parent distro)
        --flavor 'Kubuntu 24.04'            (variant of a distro)

    Each level supports combined ('Ubuntu 24.04') or structured
    (--distro Ubuntu,Fedora --distro-version 24.04,41) styles.
    Same for --compiler / --compiler-version.
    """
    from opp_ci.scheduler import _build_matrix_config
    try:
        config = _build_matrix_config(
            project=project, kinds=kinds, modes=modes, versions=versions,
            os_names=os_names, os_versions=os_versions,
            distros=distros, distro_versions=distro_versions,
            flavors=flavors, flavor_versions=flavor_versions,
            compilers=compilers, compiler_versions=compiler_versions,
            arches=arches, refs=refs, ref_range=ref_range,
            deps=deps, isolation=isolation, toolchain=toolchain,
        )
    except ValueError as e:
        raise click.ClickException(str(e))

    Base.metadata.create_all(engine)
    session = SessionLocal()
    try:
        existing = session.execute(
            select(TestMatrix).where(TestMatrix.name == name)
        ).scalar_one_or_none()
        if existing is not None:
            if not replace:
                click.echo(f"Matrix '{name}' already exists. Use --replace to overwrite.")
                return
            existing.project = project
            existing.opp_file = opp_file
            existing.config = config
            matrix = existing
        else:
            matrix = TestMatrix(name=name, project=project, opp_file=opp_file, config=config)
            session.add(matrix)
        session.commit()

        from opp_ci.scheduler import expand_matrix
        jobs = expand_matrix(project, config)
        click.echo(f"Matrix '{name}' created ({len(jobs)} jobs when expanded):")
        for job in jobs[:10]:
            parts = [job["project"], job["kind"], job["mode"]]
            if job.get("git_ref"):
                parts.append(f"@{job['git_ref']}")
            if job.get("resolved_deps"):
                deps_str = " ".join(f"{k}={v}" for k, v in job["resolved_deps"].items())
                parts.append(deps_str)
            click.echo(f"  {' × '.join(parts)}")
        if len(jobs) > 10:
            click.echo(f"  ... and {len(jobs) - 10} more")
    finally:
        session.close()


@main.command("list-matrices")
@remoteable(_list_matrices_remote)
def list_matrices():
    """List defined test matrices."""
    Base.metadata.create_all(engine)
    session = SessionLocal()
    try:
        matrices = session.execute(
            select(TestMatrix).order_by(TestMatrix.name)
        ).scalars().all()
        if not matrices:
            click.echo("No matrices defined. Use 'opp_ci create-matrix' or 'opp_ci seed-matrices'.")
            return

        click.echo(f"{'Name':<24} {'Project':<16} {'Jobs'}")
        click.echo("-" * 60)
        for m in matrices:
            from opp_ci.scheduler import expand_matrix
            jobs = expand_matrix(m.project, m.config)
            click.echo(f"{m.name:<24} {m.project:<16} {len(jobs)}")
    finally:
        session.close()


def _spec_from_flags(*, project, kinds, modes, refs, os_names, os_versions,
                     distros, distro_versions, flavors, flavor_versions,
                     compilers, compiler_versions, arches, isolation, toolchain,
                     versions):
    """Build a matrix-config dict from inline axis flags (Phase 2)."""
    config = {}
    if kinds:
        config["kinds"] = [k.strip() for k in kinds.split(",") if k.strip()]
    if modes:
        config["modes"] = [m.strip() for m in modes.split(",") if m.strip()]
    if refs:
        config["refs"] = [r.strip() for r in refs.split(",") if r.strip()]
    if os_names:
        config["os"] = [o.strip() for o in os_names.split(",") if o.strip()]
    if os_versions:
        config["os_version"] = [o.strip() for o in os_versions.split(",")]
    if distros:
        config["distro"] = [d.strip() for d in distros.split(",")]
    if distro_versions:
        config["distro_version"] = [d.strip() for d in distro_versions.split(",")]
    if flavors:
        config["flavor"] = [f.strip() for f in flavors.split(",")]
    if flavor_versions:
        config["flavor_version"] = [f.strip() for f in flavor_versions.split(",")]
    if compilers:
        config["compiler"] = [c.strip() for c in compilers.split(",")]
    if compiler_versions:
        config["compiler_version"] = [c.strip() for c in compiler_versions.split(",")]
    if arches:
        config["arch"] = [a.strip() for a in arches.split(",")]
    if isolation:
        config["isolation"] = [v.strip() for v in isolation.split(",")]
    if toolchain:
        config["toolchain"] = [v.strip() for v in toolchain.split(",")]
    if versions:
        config["versions"] = [v.strip() for v in versions.split(",")]
    elif project:
        config["versions"] = [project]
    return config


@main.command("run-matrix")
@click.option("--matrix", "matrix_name", default=None,
              help="Run a named matrix from the database")
@click.option("--name", "new_name", default=None,
              help="Name to save an inline/spec matrix under (omit to leave it anonymous)")
@click.option("--spec-file", "spec_file", default=None,
              help="JSON spec for an anonymous matrix; '-' reads stdin")
@click.option("--project", default=None, help="Anonymous matrix: project name")
@click.option("--kinds", default=None, help="Anonymous matrix: comma-separated kinds")
@click.option("--modes", default=None, help="Anonymous matrix: comma-separated modes")
@click.option("--refs", default=None,
              help="Anonymous matrix: comma-separated git refs/tags")
@click.option("--ref", "single_ref", default=None,
              help="Anonymous matrix: shorthand for a single git ref")
@click.option("--versions", "versions", default=None,
              help="Anonymous matrix: comma-separated opp_env versions")
@click.option("--os", "os_names", default=None,
              help="Anonymous matrix: comma-separated OS families")
@click.option("--os-version", "os_versions", default=None,
              help="Anonymous matrix: comma-separated OS versions")
@click.option("--distro", "distros", default=None,
              help="Anonymous matrix: comma-separated distros")
@click.option("--distro-version", "distro_versions", default=None,
              help="Anonymous matrix: comma-separated distro versions")
@click.option("--flavor", "flavors", default=None,
              help="Anonymous matrix: comma-separated flavors")
@click.option("--flavor-version", "flavor_versions", default=None,
              help="Anonymous matrix: comma-separated flavor versions")
@click.option("--compiler", "compilers", default=None,
              help="Anonymous matrix: comma-separated compilers")
@click.option("--compiler-version", "compiler_versions", default=None,
              help="Anonymous matrix: comma-separated compiler versions")
@click.option("--arch", "arches", default=None,
              help="Anonymous matrix: comma-separated arches")
@click.option("--isolation", default=None,
              help="Anonymous matrix: comma-separated isolation values")
@click.option("--toolchain", default=None,
              help="Anonymous matrix: comma-separated toolchain values")
@click.option("--no-cache", is_flag=True,
              help="Force a fresh TestRun per cell, bypassing the content cache")
@click.option("--follow", is_flag=True,
              help="Stream per-cell progress until the matrix terminates")
@click.option("--skip-install", is_flag=True, help="Skip opp_env install step")
@remoteable(_run_matrix_remote)
def run_matrix(matrix_name, new_name, spec_file, project, kinds, modes, refs, single_ref,
               versions, os_names, os_versions, distros, distro_versions,
               flavors, flavor_versions, compilers, compiler_versions, arches,
               isolation, toolchain, no_cache, follow, skip_install):
    """Expand a matrix and run all jobs sequentially.

    Three input modes (mutually exclusive):

    \b
        opp_ci run-matrix --matrix NAME
        opp_ci run-matrix --spec-file spec.json
        opp_ci run-matrix --project inet --kinds smoke --modes release ...

    `--no-cache` forces a fresh TestRun per cell (bypassing the cache
    introduced in Phase 4). `--follow` is an alias for the default
    sequential behavior — kept so scripts can opt-in explicitly.
    """
    from opp_ci.scheduler import expand_matrix
    import json as _json

    inputs = [bool(matrix_name), bool(spec_file),
              bool(project or kinds or modes or refs or os_names or distros
                   or flavors or compilers or arches)]
    if sum(inputs) > 1:
        raise click.ClickException(
            "Pick exactly one of: --matrix NAME, --spec-file FILE, or inline axis flags."
        )

    Base.metadata.create_all(engine)
    session = SessionLocal()
    try:
        if matrix_name:
            matrix = session.execute(
                select(TestMatrix).where(TestMatrix.name == matrix_name)
            ).scalar_one_or_none()
            if matrix is None:
                click.echo(f"Matrix '{matrix_name}' not found.")
                return
        else:
            from opp_ci.persistence import create_matrix_from_axes
            # Build a TestMatrix row from the inline spec / spec file. An
            # unnamed matrix stays anonymous (name = NULL); pass --name (or
            # a spec "name") to make it reusable.
            if spec_file:
                import sys as _sys
                stream = _sys.stdin if spec_file == "-" else open(spec_file)
                with stream as fh:
                    spec = _json.load(fh)
                ad_hoc_project = spec.get("project") or project
                if not ad_hoc_project:
                    raise click.ClickException(
                        "Spec file must include a 'project' key."
                    )
                ad_hoc_config = {k: v for k, v in spec.items()
                                 if k not in ("project", "opp_file", "name")}
                opp_file = spec.get("opp_file")
                proposed_name = new_name or spec.get("name")
            else:
                if not project:
                    raise click.ClickException(
                        "--project is required when building an anonymous matrix from flags."
                    )
                if single_ref and not refs:
                    refs = single_ref
                ad_hoc_project = project
                ad_hoc_config = _spec_from_flags(
                    project=project, kinds=kinds, modes=modes, refs=refs,
                    os_names=os_names, os_versions=os_versions,
                    distros=distros, distro_versions=distro_versions,
                    flavors=flavors, flavor_versions=flavor_versions,
                    compilers=compilers, compiler_versions=compiler_versions,
                    arches=arches, isolation=isolation, toolchain=toolchain,
                    versions=versions,
                )
                opp_file = None
                proposed_name = new_name

            try:
                matrix = create_matrix_from_axes(
                    session, project=ad_hoc_project, config=ad_hoc_config,
                    name=proposed_name, opp_file=opp_file,
                )
            except ValueError as e:
                raise click.ClickException(str(e))
            click.echo(f"Matrix '{matrix.display_name}' created.")

        jobs = expand_matrix(matrix.project, matrix.config)
        click.echo(f"Running matrix '{matrix.name}': {len(jobs)} jobs"
                   f"{'' if not no_cache else ' (cache disabled)'}")

        # Install once per unique build coordinate. install_project is a no-op
        # unless isolation=none and toolchain=nix, so non-nix jobs cheaply pass
        # through; for nix the coordinate (project, ref, compiler, deps) also
        # picks the per-coordinate workspace, so jobs differing in any of those
        # axes must each install (into their own workspace) — keep them in the
        # dedup key.
        if not skip_install:
            installed = set()
            for job in jobs:
                deps = job.get("resolved_deps") or {}
                install_key = (job["project"], job.get("git_ref"),
                               job.get("isolation"), job.get("toolchain"),
                               job.get("compiler"), job.get("compiler_version"),
                               frozenset(deps.items()) if isinstance(deps, dict) else None)
                if install_key not in installed:
                    try:
                        install_project(
                            job["project"], git_ref=job.get("git_ref"),
                            isolation=job.get("isolation") or "none",
                            toolchain=job.get("toolchain") or "none",
                            resolved_deps=job.get("resolved_deps"),
                            compiler=job.get("compiler"),
                            compiler_version=job.get("compiler_version"),
                        )
                        installed.add(install_key)
                    except RuntimeError as e:
                        click.echo(f"ERROR installing {job['project']}: {e}")
                        return

        # Track the whole matrix invocation as a single TestMatrixRun.
        matrix_run = create_matrix_run(session, matrix_id=matrix.id, trigger="cli")
        session.commit()

        from opp_ci.fingerprint import compute_cache_fingerprint

        passed = 0
        failed = 0
        errors = 0
        cache_hits = 0
        for i, job in enumerate(jobs, 1):
            fp = None if no_cache else compute_cache_fingerprint(
                job, project=matrix.project, opp_file=matrix.opp_file,
                # CLI runs typically lack a github token; skip the round-trip
                # so the fingerprint stays deterministic and the run starts
                # promptly.
                resolve_refs=False,
            )
            test_run, verdict_cell = enqueue_job(
                session, job,
                project=matrix.project,
                opp_file=matrix.opp_file,
                matrix_run_id=matrix_run.id,
                use_cache=not no_cache,
                cache_fingerprint=fp,
            )
            if verdict_cell is not None and verdict_cell.cache_hit:
                cache_hits += 1
                session.commit()
                parts = [job["project"], job["kind"], job.get("mode", "")]
                if job.get("git_ref"):
                    parts.append(f"@{job['git_ref']}")
                actual = test_run.result_code.value if test_run.result_code else "?"
                click.echo(f"  [{i}/{len(jobs)}] {' × '.join(parts)} → CACHED {actual}")
                if actual == "PASS":
                    passed += 1
                elif actual == "FAIL":
                    failed += 1
                elif actual == "ERROR":
                    errors += 1
                continue

            test_run.lifecycle = TestRunLifecycle.running
            test_run.started_at = datetime.datetime.utcnow()
            try:
                test_run.system_snapshot = capture_system_snapshot()
            except Exception:
                pass
            session.commit()

            parts = [job["project"], job["kind"], job.get("mode", "")]
            if job.get("git_ref"):
                parts.append(f"@{job['git_ref']}")
            click.echo(f"  [{i}/{len(jobs)}] {' × '.join(parts)}", nl=False)

            try:
                outcome = run_test(
                    job["project"], job["kind"],
                    git_ref=job.get("git_ref"), opp_file=matrix.opp_file,
                    mode=job.get("mode"),
                    isolation=job.get("isolation"), toolchain=job.get("toolchain"),
                    os=job.get("os"), os_version=job.get("os_version"),
                    distro=job.get("distro"), distro_version=job.get("distro_version"),
                    flavor=job.get("flavor"), flavor_version=job.get("flavor_version"),
                    arch=job.get("arch"),
                    compiler=job.get("compiler"), compiler_version=job.get("compiler_version"),
                    resolved_deps=job.get("resolved_deps"),
                )
            except Exception as e:
                test_run.lifecycle = TestRunLifecycle.finished
                test_run.result_code = TestResultCode.ERROR
                test_run.stderr = str(e)
                test_run.finished_at = datetime.datetime.utcnow()
                finalize_verdict_for_run(session, test_run.id)
                session.commit()
                click.echo(f" → ERROR")
                errors += 1
                continue

            test_run.lifecycle = TestRunLifecycle.finished
            test_run.result_code = TestResultCode(outcome["result_code"])
            test_run.finished_at = datetime.datetime.utcnow()
            test_run.test_exec_seconds = outcome["test_exec_seconds"]
            test_run.commit_sha = outcome.get("commit_sha")
            test_run.stdout = outcome["stdout"]
            test_run.stderr = outcome["stderr"]
            test_run.details = outcome.get("details")
            finalize_verdict_for_run(session, test_run.id)
            session.commit()
            update_ci_note(job["project"], test_run.commit_sha, session,
                           opp_file=matrix.opp_file)

            if outcome["result_code"] == "PASS":
                passed += 1
                click.echo(f" → PASS ({outcome['test_exec_seconds']:.1f}s)")
            else:
                failed += 1
                click.echo(f" → FAIL ({outcome['test_exec_seconds']:.1f}s)")

        click.echo(f"\nMatrix complete: {passed} passed, {failed} failed, "
                   f"{errors} errors ({cache_hits} cache hit(s))")
        click.echo(f"Matrix run #{matrix_run.id} — opp_ci show-matrix-run {matrix_run.id}")

        # Trigger GitHub Action notes sync once for the whole matrix
        proj = session.execute(
            select(Project).where(Project.name == matrix.project)
        ).scalar_one_or_none()
        if proj and proj.github_owner and proj.github_repo:
            from opp_ci.notes import trigger_notes_sync
            trigger_notes_sync(proj.github_owner, proj.github_repo)
    finally:
        session.close()


@main.command("list-matrix-runs")
@click.option("--project", default=None, help="Filter by project")
@click.option("--verdict", default=None,
              type=click.Choice(["EXPECTED", "UNEXPECTED", "UNKNOWN"]),
              help="Filter by rollup verdict")
@click.option("--since", default=None, help="Only runs created after YYYY-MM-DD")
@click.option("--limit", default=20, type=int, help="Max rows to show (default: 20)")
def list_matrix_runs(project, verdict, since, limit):
    """List recent TestMatrixRun rows with their rollup verdict."""
    Base.metadata.create_all(engine)
    session = SessionLocal()
    try:
        query = (
            select(TestMatrixRun, TestMatrix)
            .join(TestMatrix, TestMatrixRun.matrix_id == TestMatrix.id)
            .order_by(TestMatrixRun.id.desc())
            .limit(limit)
        )
        if project:
            query = query.where(TestMatrix.project == project)
        if verdict:
            query = query.where(TestMatrixRun.verdict == TestVerdictKind(verdict))
        if since:
            cutoff = datetime.datetime.strptime(since, "%Y-%m-%d")
            query = query.where(TestMatrixRun.created_at >= cutoff)

        rows = session.execute(query).all()
        if not rows:
            click.echo("No matrix runs found.")
            return

        click.echo(f"{'ID':<6} {'Matrix':<24} {'Project':<14} {'Trigger':<10} "
                   f"{'Ref':<14} {'Verdict':<11} {'P/F/E':<10} {'Total':<6} {'Created'}")
        click.echo("-" * 130)
        for mr, m in rows:
            verdict_str = mr.verdict.value if mr.verdict else "-"
            pfe = f"{mr.pass_count}/{mr.fail_count}/{mr.error_count}"
            created = mr.created_at.strftime("%Y-%m-%d %H:%M") if mr.created_at else "-"
            ref = mr.ref or "-"
            click.echo(
                f"{mr.id:<6} {m.name[:24]:<24} {m.project[:14]:<14} "
                f"{mr.trigger or '-':<10} {ref[:14]:<14} {verdict_str:<11} "
                f"{pfe:<10} {mr.total_count:<6} {created}"
            )
    finally:
        session.close()


def _parse_where(where_strs):
    """Parse repeatable --where field=value clauses into a {field: value} dict.

    Each occurrence is one filter; the same field can repeat to match
    multiple values (turned into an `in_` filter at query time).
    """
    out = {}
    for clause in where_strs or []:
        if "=" not in clause:
            raise click.ClickException(
                f"--where expects field=value, got {clause!r}"
            )
        k, v = clause.split("=", 1)
        out.setdefault(k.strip(), []).append(v.strip())
    return out


_EXPECTATION_FIELDS = {
    "project", "kind", "mode", "os", "os_version", "distro",
    "distro_version", "flavor", "flavor_version", "arch", "compiler",
    "compiler_version", "isolation", "toolchain", "opp_file",
}


@main.command("set-expectation")
@click.option("--project", default=None,
              help="Project to constrain matches to (sugar for --where project=NAME)")
@click.option("--where", "wheres", multiple=True,
              help="Field=value filter; repeatable. E.g. --where os=Linux --where kind=smoke")
@click.option("--expect", "expected",
              type=click.Choice(["pass", "fail", "error", "none"], case_sensitive=False),
              required=True,
              help="Expected outcome; 'none' inserts a retraction row")
@click.option("--reason", default=None, help="Free-form note (issue link, justification)")
@click.option("--set-by", "set_by", default="cli", help="Account name recorded with the edit")
@click.option("--limit", default=200, type=int,
              help="Safety cap on the number of Tests touched (default: 200)")
@click.option("--dry-run", is_flag=True,
              help="Print the Tests that would be touched without inserting any rows")
def set_expectation_cmd(project, wheres, expected, reason, set_by, limit, dry_run):
    """Insert ExpectedTestResult rows for matching Tests.

    A single invocation writes one row per matching Test, sharing
    `reason` / `set_by` / `set_at`. `--expect none` (or any retraction
    use) writes a row with NULL `expected_result_code`, distinguishable
    from never-set and itself audited.

    The matcher walks every existing Test row and keeps those whose
    coordinate fields equal the provided --where (and --project) values.
    Unknown fields raise.
    """
    filters = _parse_where(wheres)
    if project:
        filters.setdefault("project", []).append(project)

    bad = sorted(k for k in filters if k not in _EXPECTATION_FIELDS)
    if bad:
        raise click.ClickException(
            f"Unknown --where field(s): {bad!r}. "
            f"Allowed: {sorted(_EXPECTATION_FIELDS)}"
        )

    code = None if expected.lower() == "none" else TestResultCode(expected.upper())

    Base.metadata.create_all(engine)
    session = SessionLocal()
    try:
        query = select(Test)
        for field, values in filters.items():
            col = getattr(Test, field)
            if len(values) == 1:
                query = query.where(col == values[0])
            else:
                query = query.where(col.in_(values))
        tests = session.execute(query.limit(limit + 1)).scalars().all()
        if len(tests) > limit:
            raise click.ClickException(
                f"More than {limit} matching Tests; refine the filters or "
                f"raise --limit."
            )
        if not tests:
            click.echo("No Tests matched the filters.")
            return

        click.echo(f"{'Would set' if dry_run else 'Setting'} expectation "
                   f"{'<retract>' if code is None else code.value} for "
                   f"{len(tests)} Test(s):")
        for t in tests[:20]:
            click.echo(f"  #{t.id} {t.project}/{t.kind}/{t.mode or '-'} "
                       f"{t.distro or t.os or '-'} {t.compiler or '-'}")
        if len(tests) > 20:
            click.echo(f"  ... and {len(tests) - 20} more")

        if dry_run:
            return
        now = datetime.datetime.utcnow()
        for t in tests:
            insert_expectation(
                session, test_id=t.id,
                expected_result_code=code,
                reason=reason,
                set_by=set_by,
                set_at=now,
            )
        session.commit()
        click.echo(f"Inserted {len(tests)} ExpectedTestResult row(s).")
    finally:
        session.close()


@main.command("show-expectations")
@click.option("--test-id", "test_id", type=int, required=True,
              help="Test row id")
@click.option("--limit", default=20, type=int, help="Max history rows")
def show_expectations(test_id, limit):
    """List the expectation history of one Test row."""
    session = SessionLocal()
    try:
        test = session.get(Test, test_id)
        if test is None:
            click.echo(f"Test #{test_id} not found.")
            return
        rows = session.execute(
            select(ExpectedTestResult)
            .where(ExpectedTestResult.test_id == test_id)
            .order_by(ExpectedTestResult.set_at.desc())
            .limit(limit)
        ).scalars().all()
        click.echo(f"Test #{test.id}: {test.project}/{test.kind}/{test.mode or '-'} "
                   f"{test.distro or test.os or '-'} {test.compiler or '-'}")
        if not rows:
            click.echo("  (no expectation ever declared)")
            return
        click.echo(f"  {'When':<20} {'Code':<10} {'By':<14} Reason")
        click.echo("  " + "-" * 80)
        for r in rows:
            code = r.expected_result_code.value if r.expected_result_code else "(retract)"
            when = r.set_at.strftime("%Y-%m-%d %H:%M:%S") if r.set_at else "-"
            click.echo(f"  {when:<20} {code:<10} {(r.set_by or '-')[:14]:<14} "
                       f"{r.reason or '-'}")
    finally:
        session.close()


@main.command("show-matrix-run")
@click.argument("matrix_run_id", type=int)
@click.option("--unexpected-only", is_flag=True,
              help="Show only cells whose verdict diverged from expectation")
def show_matrix_run(matrix_run_id, unexpected_only):
    """Print rollup + per-cell table for one TestMatrixRun."""
    Base.metadata.create_all(engine)
    session = SessionLocal()
    try:
        mr = session.execute(
            select(TestMatrixRun).where(TestMatrixRun.id == matrix_run_id)
        ).scalar_one_or_none()
        if mr is None:
            click.echo(f"Matrix run #{matrix_run_id} not found.")
            return
        matrix = session.execute(
            select(TestMatrix).where(TestMatrix.id == mr.matrix_id)
        ).scalar_one_or_none()

        verdict_str = mr.verdict.value if mr.verdict else "(pending)"
        actual_str = mr.actual_summary.value if mr.actual_summary else "-"
        click.echo(f"Matrix run #{mr.id}: {matrix.name if matrix else '?'} "
                   f"({matrix.project if matrix else '?'})")
        click.echo(f"  Trigger:      {mr.trigger}")
        click.echo(f"  Ref:          {mr.ref or '-'}")
        click.echo(f"  Verdict:      {verdict_str}")
        click.echo(f"  Actual:       {actual_str}")
        click.echo(f"  Counters:     pass={mr.pass_count}  fail={mr.fail_count}  "
                   f"error={mr.error_count}  total={mr.total_count}")
        click.echo(f"                expected={mr.expected_count}  "
                   f"unexpected={mr.unexpected_count}  unknown={mr.unknown_count}  "
                   f"cache_hits={mr.cache_hit_count}")
        click.echo(f"  Created at:   {mr.created_at}")
        click.echo(f"  Completed at: {mr.completed_at or '(in progress)'}")
        click.echo("")

        rows = session.execute(
            select(TestVerdict, TestRun, Test)
            .join(TestRun, TestVerdict.test_run_id == TestRun.id)
            .join(Test, TestVerdict.test_id == Test.id)
            .where(TestVerdict.matrix_run_id == matrix_run_id)
            .order_by(TestVerdict.id)
        ).all()
        if unexpected_only:
            rows = [r for r in rows if r[0].verdict not in
                    (TestVerdictKind.EXPECTED,)]

        if not rows:
            click.echo("(no cells)" if not unexpected_only else "(no diverged cells)")
            return

        click.echo(f"{'Cell':<6} {'Run':<6} {'Kind':<14} {'Mode':<8} "
                   f"{'OS':<14} {'Compiler':<14} {'Actual':<10} "
                   f"{'Expected':<10} {'Verdict':<11} {'Cache'}")
        click.echo("-" * 130)
        for verdict, test_run, test in rows:
            actual = test_run.result_code.value if test_run.result_code else \
                test_run.lifecycle.value
            expected = "-"
            if verdict.expectation_id is not None:
                exp = session.get(ExpectedTestResult, verdict.expectation_id)
                if exp and exp.expected_result_code:
                    expected = exp.expected_result_code.value
                elif exp:
                    expected = "(retract)"
            v = verdict.verdict.value if verdict.verdict else "(pending)"
            cache = "hit" if verdict.cache_hit else "-"
            os_str = (test.os or "") + (
                f" {test.os_version}" if test.os_version else ""
            ) + (f" {test.distro}" if test.distro else "")
            compiler_str = (test.compiler or "-") + (
                f"-{test.compiler_version}" if test.compiler_version else ""
            )
            click.echo(
                f"{verdict.id:<6} #{test_run.id:<5} {test.kind[:14]:<14} "
                f"{(test.mode or '-')[:8]:<8} {os_str[:14]:<14} "
                f"{compiler_str[:14]:<14} {actual:<10} {expected:<10} "
                f"{v:<11} {cache}"
            )
    finally:
        session.close()


@main.command("seed-matrices")
@remoteable(_seed_matrices_remote)
def seed_matrices_cmd():
    """Seed the database with default matrix configurations."""
    from opp_ci.scheduler import DEFAULT_MATRICES

    Base.metadata.create_all(engine)
    session = SessionLocal()
    try:
        for name, mdef in DEFAULT_MATRICES.items():
            existing = session.execute(
                select(TestMatrix).where(TestMatrix.name == name)
            ).scalar_one_or_none()
            if existing is None:
                session.add(TestMatrix(name=name, project=mdef["project"], config=mdef["config"]))
        session.commit()
        click.echo(f"Seeded {len(DEFAULT_MATRICES)} default matrices.")
    finally:
        session.close()


@main.command("add-version")
@click.option("--project", required=True, help="Project name")
@click.option("--label", required=True, help="Human-readable version label (e.g. master, 4.5, topic/my-feature)")
@click.option("--ref", "git_ref", default=None, help="Git ref (branch, tag, or SHA)")
@click.option("--opp-env-version", default=None, help="opp_env version string (e.g. inet-4.5)")
@click.option("--deps", default=None, help="Resolved dependencies as JSON (e.g. '{\"omnetpp\": \"6.1\"}')")
@remoteable(_add_version_remote)
def add_version(project, label, git_ref, opp_env_version, deps):
    """Register a version (git ref / opp_env version) for a project."""
    import json as _json
    Base.metadata.create_all(engine)
    session = SessionLocal()
    try:
        proj = session.execute(
            select(Project).where(Project.name == project)
        ).scalar_one_or_none()
        if proj is None:
            click.echo(f"Project '{project}' not found. Run 'opp_ci seed-projects' first.")
            return

        resolved = _json.loads(deps) if deps else None
        version = Version(
            project_id=proj.id,
            label=label,
            git_ref=git_ref or label,
            opp_env_version=opp_env_version,
            resolved_dependencies=resolved,
        )
        session.add(version)
        session.commit()
        click.echo(f"Version '{label}' added for project '{project}' (ref: {version.git_ref})")
    finally:
        session.close()


@main.command("list-versions")
@click.option("--project", default=None, help="Filter by project name")
@remoteable(_list_versions_remote)
def list_versions(project):
    """List registered versions for projects."""
    Base.metadata.create_all(engine)
    session = SessionLocal()
    try:
        query = select(Version)
        if project:
            proj = session.execute(
                select(Project).where(Project.name == project)
            ).scalar_one_or_none()
            if proj is None:
                click.echo(f"Project '{project}' not found.")
                return
            query = query.where(Version.project_id == proj.id)

        versions = session.execute(query).scalars().all()
        if not versions:
            click.echo("No versions registered. Use 'opp_ci add-version' to add one.")
            return

        click.echo(f"{'ID':<6} {'Project':<16} {'Label':<20} {'Git Ref':<24} {'opp_env':<16} {'Dependencies'}")
        click.echo("-" * 110)
        for v in versions:
            proj_name = session.execute(
                select(Project.name).where(Project.id == v.project_id)
            ).scalar_one()
            deps = str(v.resolved_dependencies) if v.resolved_dependencies else "-"
            click.echo(f"{v.id:<6} {proj_name:<16} {v.label:<20} {v.git_ref or '-':<24} {v.opp_env_version or '-':<16} {deps}")
    finally:
        session.close()


@main.command("resolve-deps")
@click.argument("project_version")
@click.option("--pin", "pins", multiple=True, help="Pin dependency version (e.g. --pin omnetpp=6.1). Repeatable.")
@remoteable(_resolve_deps_info)
def resolve_deps_cmd(project_version, pins):
    """Resolve dependencies for a project version via opp_env.

    Queries opp_env for required_projects and shows compatible versions.
    Use --pin to override auto-resolution for specific dependencies.

    Example: opp_ci resolve-deps inet-4.5 --pin omnetpp=6.0.3
    """
    from opp_ci.dependency import (
        resolve_dependencies, parse_pins, get_required_projects, format_resolved_deps
    )

    required = get_required_projects(project_version)
    if not required:
        click.echo(f"No dependencies found for '{project_version}' (or opp_env query failed).")
        return

    click.echo(f"Dependencies for {project_version}:")
    for dep_name, compatible_versions in required.items():
        versions_str = ", ".join(compatible_versions[:10])
        if len(compatible_versions) > 10:
            versions_str += f" ... ({len(compatible_versions)} total)"
        click.echo(f"  {dep_name}: {versions_str}")

    try:
        pin_dict = parse_pins(pins)
        resolved = resolve_dependencies(project_version, pins=pin_dict)
    except ValueError as e:
        click.echo(f"\nERROR: {e}")
        return

    if resolved:
        click.echo(f"\nResolved: {format_resolved_deps(resolved)}")


# ── User commands ──────────────────────────────────────────────────────

@main.group("user")
def user_group():
    """Web UI user management."""
    pass


_USER_ROLES = ("readonly", "submitter", "admin")


@user_group.command("create")
@click.option("--username", required=True, help="Local login username")
@click.option("--role", default="admin", type=click.Choice(_USER_ROLES))
@click.option("--password", default=None, help="Password (prompted if omitted)")
@click.option("--update-password", is_flag=True,
              help="If the user already exists, update their password and role")
@remoteable(_user_create_remote)
def user_create(username, role, password, update_password):
    """Create (or update) a local web UI user.

    Use this once after install to bootstrap the first admin before
    GitHub OAuth is configured.
    """
    from opp_ci.db.models import User
    from opp_ci.passwords import hash_password

    if password is None:
        password = click.prompt("Password", hide_input=True, confirmation_prompt=True)

    Base.metadata.create_all(engine)
    session = SessionLocal()
    try:
        existing = session.execute(
            select(User).where(User.username == username)
        ).scalar_one_or_none()
        if existing is not None:
            if not update_password:
                click.echo(f"User '{username}' already exists. "
                           f"Pass --update-password to reset their password and role.")
                return
            existing.password_hash = hash_password(password)
            existing.role = role
            existing.role_locked = True
            existing.enabled = True
            session.commit()
            click.echo(f"User '{username}' updated (role={role}).")
            return

        user = User(
            username=username,
            password_hash=hash_password(password),
            role=role,
            role_locked=True,
            enabled=True,
        )
        session.add(user)
        session.commit()
        click.echo(f"User '{username}' created (role={role}).")
    finally:
        session.close()


@user_group.command("list")
@remoteable(_user_list_remote)
def user_list():
    """List web UI users."""
    from opp_ci.db.models import User

    Base.metadata.create_all(engine)
    session = SessionLocal()
    try:
        users = session.execute(select(User).order_by(User.id)).scalars().all()
        if not users:
            click.echo("No users. Run 'opp_ci user create' to bootstrap an admin.")
            return
        click.echo(f"{'ID':<4} {'Login':<24} {'Role':<10} {'Locked':<7} {'Enabled':<8} {'Last Login'}")
        click.echo("-" * 80)
        for u in users:
            login = u.username or (f"@{u.github_username}" if u.github_username else "-")
            last = u.last_login_at.strftime("%Y-%m-%d %H:%M") if u.last_login_at else "-"
            click.echo(f"{u.id:<4} {login:<24} {u.role:<10} "
                       f"{('yes' if u.role_locked else 'no'):<7} "
                       f"{('yes' if u.enabled else 'no'):<8} {last}")
    finally:
        session.close()


@user_group.command("disable")
@click.argument("username")
@remoteable(_user_disable_remote)
def user_disable(username):
    """Disable a user (logs them out and blocks future logins)."""
    from opp_ci.db.models import User
    session = SessionLocal()
    try:
        user = session.execute(
            select(User).where(User.username == username)
        ).scalar_one_or_none()
        if user is None:
            click.echo(f"User '{username}' not found.")
            return
        user.enabled = False
        session.commit()
        click.echo(f"User '{username}' disabled.")
    finally:
        session.close()


# ── Worker commands ────────────────────────────────────────────────────

@main.group("worker")
def worker_group():
    """Worker management commands."""
    pass


def _detect_capability_tags():
    """Probe the host for execution capabilities.

    Returns a list of opp_ci worker tags reflecting what this host can run:
      - os:linux | os:windows | os:macos     from platform / /etc/os-release
      - os:windows-<ver> | os:macos-<ver>    Windows/MacOS only
      - distro:<id>-<version>                Linux only (from /etc/os-release)
      - flavor:<id>-<version>                Linux only when a flavor marker
                                             (VARIANT_ID, or a desktop-session
                                             binary on Ubuntu) is recognised
      - arch:<arch>                          amd64/aarch64 from platform.machine()
      - compiler:<name>-<major>              for each of gcc, clang on PATH
      - podman                               if "podman --version" succeeds
      - nix                                  if both nix and opp_env are on PATH
    """
    import platform as _platform
    import re
    import shutil
    import subprocess as sp

    from opp_ci import platforms as _platforms

    tags = []

    system = _platform.system().lower()
    if system == "linux":
        tags.append("os:linux")
        try:
            with open("/etc/os-release") as f:
                kv = {}
                for line in f:
                    if "=" in line:
                        k, _, v = line.strip().partition("=")
                        kv[k] = v.strip('"')
            distro_id = kv.get("ID", "").lower()
            distro_ver = kv.get("VERSION_ID", "")
            variant_id = kv.get("VARIANT_ID", "").lower()
            if distro_id and distro_ver:
                tags.append(f"distro:{distro_id}-{distro_ver}")
            flavor = None
            if variant_id and _platforms.is_known_flavor(variant_id):
                flavor = variant_id
            elif distro_id == "ubuntu":
                # No VARIANT_ID (the common case for the *buntu spins): guess
                # the flavor from the installed desktop-session binary. First
                # match wins; each target is in opp_ci.platforms.FLAVORS.
                for binary, name in (("plasmashell", "kubuntu"),    # KDE Plasma
                                     ("xfce4-session", "xubuntu"),  # XFCE
                                     ("lxqt-session", "lubuntu")):  # LXQt
                    if shutil.which(binary):
                        flavor = name
                        break
            if flavor:
                tags.append(f"flavor:{flavor}-{distro_ver}")
        except OSError:
            pass
    elif system == "windows":
        # arch/mode are mandatory on every test and Windows/MacOS tests pin a
        # versioned os tag, so emit the versioned form (the bare one is never a
        # required tag under strict specification). Fall back to bare only when
        # no version is available.
        ver = _platform.release() or _platform.version()
        tags.append(f"os:windows-{ver}" if ver else "os:windows")
    elif system == "darwin":
        ver = _platform.mac_ver()[0]
        tags.append(f"os:macos-{ver}" if ver else "os:macos")

    # Fold platform.machine() to the matrix 'arch' vocabulary (amd64/aarch64).
    # arch is mandatory on every test and the matcher requires arch:<arch>
    # exactly, so always emit one — canonical_arch falls back to the raw
    # machine string for an unrecognised CPU rather than leaving the worker
    # arch-less (which would make it unable to claim any job).
    arch = _platforms.canonical_arch(_platform.machine())
    if arch:
        tags.append(f"arch:{arch}")

    for compiler in ("gcc", "clang"):
        if not shutil.which(compiler):
            continue
        try:
            out = sp.run([compiler, "--version"], capture_output=True, text=True, timeout=5)
        except (OSError, sp.SubprocessError):
            continue
        if out.returncode != 0:
            continue
        m = re.search(r"(\d+)\.\d+(?:\.\d+)?", out.stdout)
        if m:
            tags.append(f"compiler:{compiler}-{m.group(1)}")

    if shutil.which("podman"):
        try:
            out = sp.run(["podman", "--version"], capture_output=True, timeout=5)
            if out.returncode == 0:
                tags.append("podman")
        except (OSError, sp.SubprocessError):
            pass

    if shutil.which("nix") and shutil.which("opp_env"):
        tags.append("nix")

    return tags


@worker_group.command("detect-tags")
@remoteable(_refuse_remote("worker detect-tags"))
def worker_detect_tags():
    """Print the capability tags this host would advertise (one per line)."""
    for tag in _detect_capability_tags():
        click.echo(tag)


@worker_group.command("start")
@click.option("--coordinator", default=None,
              help="Coordinator URL (default from $OPP_CI_COORDINATOR_URL)")
@click.option("--token", default=None,
              help="Worker token (default from $OPP_CI_WORKER_TOKEN)")
@click.option("--poll-interval", default=10, help="Seconds between polls (default: 10)")
@click.option("--heartbeat-interval", default=30, help="Seconds between heartbeats (default: 30)")
@click.option("--niceness", default=10,
              help="Run the worker and its build/test subprocesses at this nice "
                   "level so CI work yields to interactive use (higher = lower "
                   "priority; default: 10). Pass 0 for normal priority.")
@remoteable(_refuse_remote("worker start"))
def worker_start(coordinator, token, poll_interval, heartbeat_interval, niceness):
    """Start a worker agent that polls the coordinator for jobs.

    Tags and concurrency are configured at registration time (see
    `opp_ci worker register`) and fetched from the coordinator on startup.
    """
    from opp_ci.worker import WorkerAgent
    from opp_ci import config as cfg

    if coordinator is None:
        coordinator = cfg.COORDINATOR_URL
    if token is None:
        token = cfg.WORKER_TOKEN
    if not token:
        raise click.ClickException(
            "Worker token missing. Pass --token or set $OPP_CI_WORKER_TOKEN."
        )

    agent = WorkerAgent(coordinator_url=coordinator, token=token)
    try:
        agent.fetch_config()
    except Exception as e:
        raise click.ClickException(f"Could not fetch worker config from coordinator: {e}")
    click.echo(
        f"Starting worker '{agent.name}' — coordinator={coordinator} "
        f"tags={agent.tags} concurrency={agent.concurrency}"
    )
    agent.start(poll_interval=poll_interval, heartbeat_interval=heartbeat_interval,
                niceness=niceness)


@worker_group.command("register")
@click.option("--name", required=True, help="Worker name (unique)")
@click.option("--tags", default="", help="Comma-separated capability tags")
@click.option("--auto-tags/--no-auto-tags", default=False,
              help="Detect os:/compiler:/podman/nix tags from this host and union them with --tags")
@click.option("--concurrency", default=1, help="Max concurrent jobs")
@remoteable(_worker_register_remote)
def worker_register(name, tags, auto_tags, concurrency):
    """Register a new worker in the local database and print its token."""
    Base.metadata.create_all(engine)
    session = SessionLocal()
    try:
        existing = session.execute(
            select(Worker).where(Worker.name == name)
        ).scalar_one_or_none()
        if existing is not None:
            click.echo(f"Worker '{name}' already exists (token: {existing.token})")
            return

        explicit_tags = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
        detected_tags = _detect_capability_tags() if auto_tags else []
        # Union, preserving order: detected first, then explicit (so --tags can
        # supplement but not override the order shown to the user).
        seen = set()
        tag_list = []
        for t in detected_tags + explicit_tags:
            if t not in seen:
                tag_list.append(t)
                seen.add(t)

        worker = Worker(name=name, tags=tag_list, concurrency=concurrency)
        session.add(worker)
        session.commit()
        click.echo(f"Worker '{name}' registered.")
        click.echo(f"  ID:          {worker.id}")
        click.echo(f"  Token:       {worker.token}")
        click.echo(f"  Tags:        {worker.tags}")
        if auto_tags and detected_tags:
            click.echo(f"  (detected:   {', '.join(detected_tags)})")
        click.echo(f"  Concurrency: {worker.concurrency}")
    finally:
        session.close()


@worker_group.command("list")
@remoteable(_worker_list_remote)
def worker_list():
    """List registered workers."""
    Base.metadata.create_all(engine)
    session = SessionLocal()
    try:
        workers = session.execute(
            select(Worker).order_by(Worker.name)
        ).scalars().all()
        if not workers:
            click.echo("No workers registered.")
            return

        click.echo(f"{'ID':<6} {'Name':<20} {'Status':<10} {'Enabled':<9} {'Jobs':<6} "
                   f"{'Cap':<6} {'Tags':<30} {'Last Heartbeat'}")
        click.echo("-" * 120)
        for w in workers:
            hb = w.last_heartbeat.strftime("%Y-%m-%d %H:%M:%S") if w.last_heartbeat else "-"
            tags_str = ", ".join(w.tags) if w.tags else "-"
            enabled = "no" if w.enabled is False else "yes"
            click.echo(f"{w.id:<6} {w.name:<20} {w.status:<10} {enabled:<9} "
                       f"{w.current_job_count:<6} {w.concurrency:<6} {tags_str:<30} {hb}")
    finally:
        session.close()


@worker_group.command("update")
@click.argument("worker_id", type=int)
@click.option("--concurrency", type=int, default=None, help="Set max concurrent jobs (>= 1)")
@click.option("--tags", default=None, help="Replace tags with this comma-separated set")
@click.option("--add-tags", default=None, help="Comma-separated tags to add")
@click.option("--remove-tags", default=None, help="Comma-separated tags to remove")
@click.option("--auto-tags", is_flag=True, default=False,
              help="Re-detect os:/distro:/flavor:/arch:/compiler:/podman/nix tags "
                   "from this host and refresh them on the worker (stale ones "
                   "dropped, custom tags kept). Run on the worker's own host.")
@remoteable(_worker_update_remote)
def worker_update(worker_id, concurrency, tags, add_tags, remove_tags, auto_tags):
    """Update a worker's concurrency and/or tags."""
    if (concurrency is None and tags is None and add_tags is None
            and remove_tags is None and not auto_tags):
        raise click.UsageError(
            "Nothing to update: pass --concurrency and/or "
            "--tags/--add-tags/--remove-tags/--auto-tags.")
    detected = _detect_capability_tags() if auto_tags else None
    Base.metadata.create_all(engine)
    session = SessionLocal()
    try:
        worker = session.execute(
            select(Worker).where(Worker.id == worker_id)
        ).scalar_one_or_none()
        if worker is None:
            click.echo(f"Worker #{worker_id} not found.")
            return
        new_tags = _resolve_tags(worker.tags or [], tags, add_tags, remove_tags, detected)
        try:
            update_worker(session, worker_id, concurrency=concurrency, tags=new_tags)
        except ValueError as e:
            raise click.ClickException(str(e))
        session.commit()
        click.echo(f"Worker #{worker_id} ({worker.name}) updated.")
        click.echo(f"  Concurrency: {worker.concurrency}")
        click.echo(f"  Tags:        {worker.tags}")
        if auto_tags and detected:
            click.echo(f"  (detected:   {', '.join(detected)})")
    finally:
        session.close()


def _set_worker_enabled(worker_id, enabled):
    Base.metadata.create_all(engine)
    session = SessionLocal()
    try:
        worker = update_worker(session, worker_id, enabled=enabled)
        if worker is None:
            click.echo(f"Worker #{worker_id} not found.")
            return
        name = worker.name
        session.commit()
        state = "enabled" if enabled else "disabled"
        msg = f"Worker #{worker_id} ({name}) {state}."
        if not enabled:
            msg += " Draining; in-flight jobs finish."
        click.echo(msg)
    finally:
        session.close()


@worker_group.command("enable")
@click.argument("worker_id", type=int)
@remoteable(_worker_enable_remote)
def worker_enable(worker_id):
    """Enable a worker so the coordinator assigns it jobs again."""
    _set_worker_enabled(worker_id, True)


@worker_group.command("disable")
@click.argument("worker_id", type=int)
@remoteable(_worker_disable_remote)
def worker_disable(worker_id):
    """Disable a worker (drain): it keeps heartbeating but gets no new jobs."""
    _set_worker_enabled(worker_id, False)


@worker_group.command("delete")
@click.argument("worker_id", type=int)
@click.option("--yes", is_flag=True, help="Skip confirmation prompt")
@remoteable(_worker_delete_remote)
def worker_delete(worker_id, yes):
    """Hard-delete a worker. In-flight jobs are re-queued for other workers."""
    import datetime as _dt
    from opp_ci.config import MAX_RECLAIMS

    Base.metadata.create_all(engine)
    session = SessionLocal()
    try:
        worker = session.execute(
            select(Worker).where(Worker.id == worker_id)
        ).scalar_one_or_none()
        if worker is None:
            click.echo(f"Worker #{worker_id} not found.")
            return
        name = worker.name
        if not yes:
            click.confirm(f"Delete worker '{name}' (#{worker_id})?", abort=True)
        requeued, retired = delete_worker(
            session, worker_id, _dt.datetime.utcnow(), MAX_RECLAIMS)
        session.commit()
        msg = f"Worker #{worker_id} ({name}) deleted. Re-queued {requeued} run(s)"
        msg += f", retired {retired}." if retired else "."
        click.echo(msg)
    finally:
        session.close()


# ── Token commands ─────────────────────────────────────────────────────

@main.group("token")
def token_group():
    """API token management commands."""
    pass


@token_group.command("create")
@click.option("--name", required=True, help="Token name/description")
@click.option("--role", required=True, type=click.Choice(["readonly", "submitter", "worker", "admin"]), help="Token role")
@remoteable(_token_create_remote)
def token_create(name, role):
    """Create a new API token."""
    Base.metadata.create_all(engine)
    session = SessionLocal()
    try:
        token = ApiToken(name=name, role=role)
        session.add(token)
        session.commit()
        click.echo(f"Token created:")
        click.echo(f"  Name:  {token.name}")
        click.echo(f"  Role:  {token.role}")
        click.echo(f"  Token: {token.token}")
    finally:
        session.close()


@token_group.command("list")
@remoteable(_token_list_remote)
def token_list():
    """List API tokens."""
    Base.metadata.create_all(engine)
    session = SessionLocal()
    try:
        tokens = session.execute(
            select(ApiToken).order_by(ApiToken.id)
        ).scalars().all()
        if not tokens:
            click.echo("No tokens.")
            return

        click.echo(f"{'ID':<6} {'Name':<20} {'Role':<12} {'Enabled':<10} {'Token (prefix)':<20} {'Created'}")
        click.echo("-" * 90)
        for t in tokens:
            prefix = t.token[:12] + "..." if t.token else "-"
            created = t.created_at.strftime("%Y-%m-%d %H:%M") if t.created_at else "-"
            enabled = "yes" if t.enabled else "no"
            click.echo(f"{t.id:<6} {t.name:<20} {t.role:<12} {enabled:<10} {prefix:<20} {created}")
    finally:
        session.close()


@token_group.command("revoke")
@click.argument("token_id", type=int)
@remoteable(_token_revoke_remote)
def token_revoke(token_id):
    """Disable an API token by ID."""
    Base.metadata.create_all(engine)
    session = SessionLocal()
    try:
        token = session.execute(
            select(ApiToken).where(ApiToken.id == token_id)
        ).scalar_one_or_none()
        if token is None:
            click.echo(f"Token #{token_id} not found.")
            return
        token.enabled = False
        session.commit()
        click.echo(f"Token #{token_id} ({token.name}) revoked.")
    finally:
        session.close()


# ── GitHub rule commands ───────────────────────────────────────────────

@main.group("rule")
def rule_group():
    """Auto-test rule management (GitHub integration)."""
    pass


@rule_group.command("create")
@click.option("--project", required=True, help="Project name")
@click.option("--type", "rule_type", required=True, type=click.Choice(["branch", "pr", "tag"]), help="Rule type")
@click.option("--pattern", required=True, help="Glob pattern (e.g. 'master', 'topic/*', '*')")
@click.option("--matrix", "matrix_name", default=None, help="Matrix name to run (omit for smoke-only)")
@click.option("--replace", is_flag=True, help="Replace existing rule with the same project/type/pattern/matrix")
@remoteable(_rule_create_remote)
def rule_create(project, rule_type, pattern, matrix_name, replace):
    """Create an auto-test rule for GitHub events."""
    Base.metadata.create_all(engine)
    session = SessionLocal()
    try:
        proj = session.execute(
            select(Project).where(Project.name == project)
        ).scalar_one_or_none()
        if proj is None:
            click.echo(f"Project '{project}' not found.")
            return

        matrix_id = None
        if matrix_name:
            matrix = session.execute(
                select(TestMatrix).where(TestMatrix.name == matrix_name)
            ).scalar_one_or_none()
            if matrix is None:
                click.echo(f"Matrix '{matrix_name}' not found.")
                return
            matrix_id = matrix.id

        if replace:
            existing = session.execute(
                select(AutoTestRule).where(
                    AutoTestRule.project_id == proj.id,
                    AutoTestRule.rule_type == rule_type,
                    AutoTestRule.pattern == pattern,
                    AutoTestRule.matrix_id == matrix_id,
                )
            ).scalar_one_or_none()
            if existing is not None:
                session.delete(existing)
                session.flush()

        rule = AutoTestRule(
            project_id=proj.id,
            rule_type=rule_type,
            pattern=pattern,
            matrix_id=matrix_id,
            enabled=1,
        )
        session.add(rule)
        session.commit()
        matrix_desc = matrix_name or "(smoke only)"
        click.echo(f"Rule #{rule.id}: {project} {rule_type} '{pattern}' -> {matrix_desc}")
    finally:
        session.close()


@rule_group.command("list")
@remoteable(_rule_list_remote)
def rule_list():
    """List auto-test rules."""
    Base.metadata.create_all(engine)
    session = SessionLocal()
    try:
        rules = session.execute(
            select(AutoTestRule).order_by(AutoTestRule.id)
        ).scalars().all()
        if not rules:
            click.echo("No auto-test rules.")
            return

        click.echo(f"{'ID':<6} {'Project':<20} {'Type':<8} {'Pattern':<20} {'Matrix':<20} {'Enabled'}")
        click.echo("-" * 90)
        for r in rules:
            proj_name = r.project_rel.name if r.project_rel else "?"
            matrix_name = r.matrix_rel.name if r.matrix_rel else "-"
            enabled = "yes" if r.enabled else "no"
            click.echo(f"{r.id:<6} {proj_name:<20} {r.rule_type:<8} {r.pattern:<20} {matrix_name:<20} {enabled}")
    finally:
        session.close()


@rule_group.command("delete")
@click.argument("rule_id", type=int)
@remoteable(_rule_delete_remote)
def rule_delete(rule_id):
    """Delete an auto-test rule by ID."""
    Base.metadata.create_all(engine)
    session = SessionLocal()
    try:
        rule = session.execute(
            select(AutoTestRule).where(AutoTestRule.id == rule_id)
        ).scalar_one_or_none()
        if rule is None:
            click.echo(f"Rule #{rule_id} not found.")
            return
        session.delete(rule)
        session.commit()
        click.echo(f"Rule #{rule_id} deleted.")
    finally:
        session.close()


@rule_group.command("test-webhook")
@click.option("--project", required=True, help="Project name")
@click.option("--ref", required=True, help="Branch, tag, or PR branch to simulate")
@click.option("--type", "event_type", default="push", type=click.Choice(["push", "pr"]), help="Event type")
@click.option("--sha", default="0000000000000000000000000000000000000000", help="Commit SHA (default: dummy)")
@click.option("--pr-number", default=None, type=int, help="PR number (for pr events)")
@remoteable(_rule_test_webhook_remote)
def rule_test_webhook(project, ref, event_type, sha, pr_number):
    """Simulate a webhook event to test rule matching (dry-run style)."""
    Base.metadata.create_all(engine)
    session = SessionLocal()
    try:
        proj = session.execute(
            select(Project).where(Project.name == project)
        ).scalar_one_or_none()
        if proj is None:
            click.echo(f"Project '{project}' not found.")
            return
        if not proj.github_owner or not proj.github_repo:
            click.echo(f"Project '{project}' has no GitHub owner/repo configured.")
            return

        from opp_ci.github.webhook import handle_webhook_event

        if event_type == "push":
            payload = {
                "ref": f"refs/heads/{ref}",
                "after": sha,
                "repository": {
                    "name": proj.github_repo,
                    "owner": {"login": proj.github_owner},
                },
            }
            result = handle_webhook_event("push", payload)
        else:
            payload = {
                "action": "synchronize",
                "pull_request": {
                    "number": pr_number or 1,
                    "head": {"sha": sha, "ref": ref},
                },
                "repository": {
                    "name": proj.github_repo,
                    "owner": {"login": proj.github_owner},
                },
            }
            result = handle_webhook_event("pull_request", payload)

        click.echo(f"Result: {result}")
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Internal commands — invoked by container entrypoints, not end users.
# ---------------------------------------------------------------------------

@main.group("internal", hidden=True)
def internal_group():
    """Internal commands used by container entrypoints."""


@internal_group.command("run-direct")
@click.option("--project", required=True)
@click.option("--kind", required=True)
@click.option("--mode", default=None)
@click.option("--opp-file", default=None)
@click.option("--git-ref", default=None)
@click.option("--no-build", is_flag=True, default=False,
              help="Skip the build stage and run the test against an already-built "
                   "project (the build ran as a separate step).")
@remoteable(_refuse_remote("internal run-direct"))
def internal_run_direct(project, kind, mode, opp_file, git_ref, no_build):
    """Run a single test by calling opp_repl directly (host-toolchain path).

    Designed to be invoked inside an opp-ci-runner image whose base OS already
    has the requested compiler installed. Writes the test's stdout/stderr to
    this process's stdout/stderr and exits 0 on PASS, 1 otherwise.

    The podman host path drives this twice — once with ``--kind build`` (the
    build) and once with ``--no-build`` (the test) — so each shows as its own
    stage; both share the bind-mounted /work so the build's artifacts persist.
    """
    from opp_ci.executor import _run_test_direct
    outcome = _run_test_direct(
        project, kind, opp_file=opp_file, git_ref=git_ref, mode=mode,
        skip_build=no_build,
    )
    if outcome.get("stdout"):
        click.echo(outcome["stdout"], nl=False)
    if outcome.get("stderr"):
        click.echo(outcome["stderr"], nl=False, err=True)
    raise SystemExit(0 if outcome["result_code"] == "PASS" else 1)


# ---------------------------------------------------------------------------
# Image management — build the opp-ci-runner container images for a matrix.
# ---------------------------------------------------------------------------

@main.group("image")
def image_group():
    """Build and manage the opp-ci-runner container images."""


@image_group.command("build")
@click.option("--os", "os_name", default=None,
              type=click.Choice(["Linux", "Windows", "MacOS"], case_sensitive=False),
              help="OS family (defaults to Linux when --distro is set)")
@click.option("--os-version", default=None, help="OS version (Windows/MacOS only)")
@click.option("--distro", default=None, help="Linux distribution name (e.g. 'Ubuntu')")
@click.option("--distro-version", default=None, help="Distribution version (e.g. '24.04')")
@click.option("--flavor", default=None, help="Distribution flavor (e.g. 'Kubuntu')")
@click.option("--flavor-version", default=None, help="Flavor version; defaults to --distro-version")
@click.option("--compiler", default=None, help="Compiler name (required for --toolchain=host)")
@click.option("--compiler-version", default=None, help="Compiler version (required for --toolchain=host)")
@click.option("--omnetpp-version", default=None,
              help="OMNeT++ version baked into the image (required for both toolchains)")
@click.option("--toolchain", type=click.Choice(["host", "nix"]), required=True,
              help="Whether the compiler comes from the OS package manager (host) or opp_env/Nix")
@click.option("--push", is_flag=True, help="Push the built image to the configured registry")
@remoteable(_refuse_remote("image build"))
def image_build(os_name, os_version, distro, distro_version, flavor, flavor_version,
                compiler, compiler_version, omnetpp_version, toolchain, push):
    """Build one opp-ci-runner image for a (toolchain, platform, compiler, omnetpp) combination."""
    _build_one_image(
        os_name=os_name, os_version=os_version,
        distro=distro, distro_version=distro_version,
        flavor=flavor, flavor_version=flavor_version,
        compiler=compiler, compiler_version=compiler_version,
        omnetpp_version=omnetpp_version, toolchain=toolchain, push=push,
    )


def _build_one_image(*, os_name, os_version, distro, distro_version,
                     flavor, flavor_version, compiler, compiler_version,
                     omnetpp_version, toolchain, push):
    """Resolve a platform, derive the runner image tag, and build it locally.

    Plain function (no click context) so both the `image build` command
    and `_build_matrix_images` share one build path. Building always runs
    against the local podman daemon.
    """
    from opp_ci.executor import build_runner_image, _runner_image_tag
    from opp_ci import platforms

    if toolchain == "host":
        if not compiler or not compiler_version:
            raise click.ClickException("--compiler and --compiler-version are required when --toolchain=host")
    if not omnetpp_version:
        raise click.ClickException("--omnetpp-version is required (baked into the image for both toolchains)")

    try:
        resolved_os, resolved_distro, resolved_flavor = platforms.resolve_platform(
            os=os_name, distro=distro, flavor=flavor,
        )
    except ValueError as e:
        raise click.ClickException(str(e))
    os_name = platforms._os_canonical(resolved_os) if resolved_os else None
    distro = resolved_distro
    flavor = resolved_flavor

    slug = platforms.platform_slug(
        os=os_name, os_version=os_version,
        distro=distro, distro_version=distro_version,
        flavor=flavor, flavor_version=flavor_version,
    )
    if not slug or "-" not in slug:
        raise click.ClickException(
            "Need a fully-specified platform: pass --distro NAME --distro-version VER, "
            "or --flavor NAME --flavor-version VER, or --os Windows/MacOS --os-version VER."
        )
    tag = _runner_image_tag(slug, toolchain, compiler, compiler_version, omnetpp_version)

    try:
        build_runner_image(
            tag, toolchain, os_name, os_version, compiler, compiler_version,
            distro=distro, distro_version=distro_version,
            flavor=flavor, flavor_version=flavor_version,
            omnetpp_version=omnetpp_version, push=push,
        )
    except RuntimeError as e:
        raise click.ClickException(str(e))
    click.echo(f"Built {tag}{' and pushed' if push else ''}")


@image_group.command("build-matrix")
@click.option("--matrix", "matrix_name", required=True, help="Name of a matrix whose images to build")
@click.option("--push", is_flag=True, help="Push each built image to the configured registry")
@remoteable(_image_build_matrix_remote)
def image_build_matrix(matrix_name, push):
    """Build every opp-ci-runner image referenced by a matrix's expansion.

    Walks the expanded job list, derives the unique image tags for jobs
    with isolation=podman, and builds each one locally. With --remote the
    matrix definition is read from the coordinator (since the laptop's DB
    doesn't have it) but the build still happens on this host.
    """
    from opp_ci.scheduler import expand_matrix

    session = SessionLocal()
    try:
        matrix = session.execute(
            select(TestMatrix).where(TestMatrix.name == matrix_name)
        ).scalar_one_or_none()
        if matrix is None:
            click.echo(f"Matrix '{matrix_name}' not found.")
            return
        jobs = expand_matrix(matrix.project, matrix.config)
        _build_matrix_images(jobs, matrix_name, push)
    finally:
        session.close()
