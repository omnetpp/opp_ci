import datetime
import logging

import click
from sqlalchemy import select

from opp_ci.db.connection import engine, SessionLocal
from opp_ci.db.models import Base, Project, Version, TestMatrix, TestRun, TestRunStatus, TestResult, Worker, ApiToken, AutoTestRule
from opp_ci.executor import install_project, run_test, find_existing_run
from opp_ci.notes import update_ci_note


@click.group()
@click.option("-v", "--verbose", is_flag=True, help="Enable verbose logging")
@click.option("--remote", is_flag=True, help="Submit to remote coordinator instead of running locally")
@click.pass_context
def main(ctx, verbose, remote):
    """opp_ci — CI for OMNeT++ simulation projects."""
    ctx.ensure_object(dict)
    ctx.obj["remote"] = remote
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@main.command()
def init_db():
    """Create database tables."""
    Base.metadata.create_all(engine)
    click.echo("Database tables created.")


@main.command("reset-db")
@click.option("--yes", is_flag=True, help="Skip confirmation prompt")
@click.option("--preserve-tokens", is_flag=True,
              help="Snapshot api_tokens and workers rows and restore them after the reset, "
                   "so external systems (GitHub Actions, remote workers) keep working.")
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

    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

    if preserve_tokens and (saved_api_tokens or saved_workers):
        session = SessionLocal()
        try:
            for row in saved_api_tokens:
                session.add(ApiToken(**row))
            for row in saved_workers:
                session.add(Worker(**row))
            session.commit()
            click.echo("Database reset.")
            token_names = ", ".join(r["name"] for r in saved_api_tokens) or "(none)"
            worker_names = ", ".join(r["name"] for r in saved_workers) or "(none)"
            click.echo(f"  Preserved api tokens: {token_names}")
            click.echo(f"  Preserved workers:    {worker_names}")
        finally:
            session.close()
    else:
        click.echo("Database reset.")


@main.command("run")
@click.option("--project", required=True, help="opp_env project name (e.g. inet-4.5)")
@click.option("--test", "tests", required=True, help="Test(s), comma-separated (e.g. smoke,fingerprint)")
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
@click.option("--force", is_flag=True, help="Re-run even if an identical run already exists")
@click.option("--skip-install", is_flag=True, help="Skip opp_env install step")
@click.pass_context
def run_cmd(ctx, project, tests, git_ref, mode, isolation, toolchain,
            os_name, os_version, distro, distro_version, flavor, flavor_version,
            arch, compiler, compiler_version, pins, force, skip_install):
    """Run test(s) for a project and store the results."""
    from opp_ci import platforms
    from opp_ci.scheduler import _parse_name_version, _build_platform_desc

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
            project, tests, git_ref,
            mode=mode, isolation=isolation, toolchain=toolchain,
            os_name=os_name, os_version=os_version,
            distro=distro, distro_version=distro_version,
            flavor=flavor, flavor_version=flavor_version,
            arch=arch,
            compiler=compiler, compiler_version=compiler_version,
            pins=pins, force=force,
        )
        return

    from opp_ci.dependency import resolve_dependencies, parse_pins, format_resolved_deps

    Base.metadata.create_all(engine)
    session = SessionLocal()
    try:
        # Resolve dependencies if pins are specified or project has deps
        resolved_deps = None
        if pins:
            try:
                pin_dict = parse_pins(pins)
                resolved_deps = resolve_dependencies(project, pins=pin_dict)
                if resolved_deps:
                    click.echo(f"Resolved dependencies: {format_resolved_deps(resolved_deps)}")
            except ValueError as e:
                click.echo(f"ERROR: {e}")
                return

        # Install once for all tests
        if not skip_install:
            try:
                install_project(project, git_ref=git_ref,
                                isolation=isolation, toolchain=toolchain)
            except RuntimeError as e:
                click.echo(f"ERROR during install: {e}")
                return

        for test in tests.split(","):
            test = test.strip()
            if not test:
                continue

            if not force:
                existing = find_existing_run(
                    session, project=project, test=test, mode=mode, git_ref=git_ref,
                    os=os_name, os_version=os_version,
                    distro=distro, distro_version=distro_version,
                    flavor=flavor, flavor_version=flavor_version,
                    arch=arch,
                    compiler=compiler, compiler_version=compiler_version,
                    isolation=isolation, toolchain=toolchain,
                )
                if existing:
                    click.echo(f"Skipping {project} / {test}: already has run #{existing.id} ({existing.status.value})")
                    continue

            test_run = TestRun(
                project=project,
                test=test,
                mode=mode,
                git_ref=git_ref,
                os=os_name,
                os_version=os_version,
                distro=distro,
                distro_version=distro_version,
                flavor=flavor,
                flavor_version=flavor_version,
                arch=arch,
                compiler=compiler,
                compiler_version=compiler_version,
                isolation=isolation,
                toolchain=toolchain,
                platform_desc=_build_platform_desc(
                    os_name, os_version, arch, compiler, compiler_version,
                    distro=distro, distro_version=distro_version,
                    flavor=flavor, flavor_version=flavor_version,
                ),
                status=TestRunStatus.running,
                started_at=datetime.datetime.utcnow(),
            )
            session.add(test_run)
            session.commit()

            desc = f"{project}@{git_ref}" if git_ref else project
            click.echo(f"Test run #{test_run.id}: {desc} / {test}")

            try:
                outcome = run_test(
                    project, test, git_ref=git_ref, mode=mode,
                    isolation=isolation, toolchain=toolchain,
                    os=os_name, os_version=os_version,
                    distro=distro, distro_version=distro_version,
                    flavor=flavor, flavor_version=flavor_version,
                    arch=arch,
                    compiler=compiler, compiler_version=compiler_version,
                )
            except Exception as e:
                test_run.status = TestRunStatus.ERROR
                test_run.finished_at = datetime.datetime.utcnow()
                session.add(TestResult(
                    test_run_id=test_run.id,
                    result_code="ERROR",
                    stderr=str(e),
                ))
                session.commit()
                click.echo(f"  ERROR: {e}")
                continue

            test_run.status = TestRunStatus.PASS if outcome["result_code"] == "PASS" else TestRunStatus.FAIL
            test_run.finished_at = datetime.datetime.utcnow()
            test_run.duration_seconds = outcome["duration_seconds"]
            test_run.commit_sha = outcome.get("commit_sha")
            session.add(TestResult(
                test_run_id=test_run.id,
                result_code=outcome["result_code"],
                stdout=outcome["stdout"],
                stderr=outcome["stderr"],
                details=outcome.get("details"),
            ))
            session.commit()
            update_ci_note(project, test_run.commit_sha, session)
            click.echo(f"  Result: {outcome['result_code']} ({outcome['duration_seconds']:.1f}s)")
    finally:
        session.close()


def _run_remote(project, tests, git_ref, *, mode=None,
                isolation=None, toolchain=None, os_name=None, os_version=None,
                distro=None, distro_version=None,
                flavor=None, flavor_version=None,
                arch=None, compiler=None, compiler_version=None,
                pins=None, force=False):
    """Submit test run(s) to the remote coordinator."""
    from opp_ci.config import COORDINATOR_URL, API_TOKEN
    from opp_ci.client import OppCiClient

    if not API_TOKEN:
        click.echo("ERROR: Set OPP_CI_API_TOKEN env var for remote submission.")
        return

    if pins:
        click.echo("WARNING: --pin is not yet supported over --remote; ignoring.")

    # COORDINATOR_URL points at the host (e.g. http://ci:8080); the REST
    # router is mounted under /api. Append it here so callers don't have
    # to know about the prefix.
    base = COORDINATOR_URL.rstrip("/")
    api_url = base if base.endswith("/api") else base + "/api"

    client = OppCiClient(url=api_url, token=API_TOKEN)
    for test in tests.split(","):
        test = test.strip()
        if not test:
            continue
        try:
            result = client.submit_run(
                project=project, test=test, git_ref=git_ref,
                mode=mode, isolation=isolation, toolchain=toolchain,
                os=os_name, os_version=os_version,
                distro=distro, distro_version=distro_version,
                flavor=flavor, flavor_version=flavor_version,
                arch=arch,
                compiler=compiler, compiler_version=compiler_version,
                force=force,
            )
            click.echo(f"Submitted run #{result['id']}: {project} / {test} → {result['status']}")
        except Exception as e:
            click.echo(f"ERROR submitting {project}/{test}: {e}")


@main.command("serve")
@click.option("--host", default=None,
              help="Bind host (default from $OPP_CI_SERVE_HOST, or 127.0.0.1)")
@click.option("--port", default=None, type=int,
              help="Bind port (default from $OPP_CI_SERVE_PORT, or 8080)")
def serve(host, port):
    """Start the web UI server."""
    import uvicorn
    from opp_ci.web.app import app
    from opp_ci import config as cfg
    Base.metadata.create_all(engine)
    if host is None:
        host = cfg.SERVE_HOST
    if port is None:
        port = cfg.SERVE_PORT
    click.echo(f"Starting opp_ci web UI at http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="info")


@main.command("list-runs")
@click.option("--project", default=None, help="Filter by project")
@click.option("--ref", "git_ref", default=None, help="Filter by git ref")
@click.option("--test", default=None, help="Filter by test")
@click.option("--status", default=None, help="Filter by status (PASS/FAIL/ERROR)")
@click.option("--limit", default=20, help="Max rows to show")
def list_runs(project, git_ref, test, status, limit):
    """List test runs."""
    session = SessionLocal()
    try:
        query = select(TestRun).order_by(TestRun.id.desc()).limit(limit)
        if project:
            query = query.where(TestRun.project == project)
        if git_ref:
            query = query.where(TestRun.git_ref == git_ref)
        if test:
            query = query.where(TestRun.test == test)
        if status:
            query = query.where(TestRun.status == TestRunStatus(status))

        runs = session.execute(query).scalars().all()
        if not runs:
            click.echo("No runs found.")
            return

        click.echo(f"{'ID':<6} {'Project':<20} {'Ref':<16} {'Test':<14} {'Status':<10} {'Duration':<10} {'Started'}")
        click.echo("-" * 106)
        for run in runs:
            duration = f"{run.duration_seconds:.1f}s" if run.duration_seconds else "-"
            started = run.started_at.strftime("%Y-%m-%d %H:%M") if run.started_at else "-"
            ref = run.git_ref or "-"
            click.echo(f"{run.id:<6} {run.project:<20} {ref:<16} {run.test:<14} {run.status.value:<10} {duration:<10} {started}")
    finally:
        session.close()


@main.command("show-run")
@click.argument("run_id", type=int)
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
        click.echo(f"  Test:     {run.test}")
        click.echo(f"  Status:   {run.status.value}")
        click.echo(f"  Duration: {run.duration_seconds:.1f}s" if run.duration_seconds else "  Duration: -")
        click.echo(f"  Started:  {run.started_at}")
        click.echo(f"  Finished: {run.finished_at or '-'}")
        click.echo(f"  Trigger:  {run.trigger}")

        results = session.execute(
            select(TestResult).where(TestResult.test_run_id == run_id)
        ).scalars().all()

        for result in results:
            click.echo(f"\n  Result: {result.result_code}")
            if result.stdout:
                click.echo(f"  stdout ({len(result.stdout)} chars):")
                # Show first 500 chars
                click.echo("    " + result.stdout[:500].replace("\n", "\n    "))
                if len(result.stdout) > 500:
                    click.echo("    ...")
            if result.stderr:
                click.echo(f"  stderr ({len(result.stderr)} chars):")
                click.echo("    " + result.stderr[:500].replace("\n", "\n    "))
                if len(result.stderr) > 500:
                    click.echo("    ...")
    finally:
        session.close()


@main.command("delete-run")
@click.argument("run_id", type=int)
@click.option("--yes", is_flag=True, help="Skip confirmation prompt")
def delete_run(run_id, yes):
    """Delete a single test run by ID."""
    session = SessionLocal()
    try:
        run = session.execute(
            select(TestRun).where(TestRun.id == run_id)
        ).scalar_one_or_none()
        if run is None:
            click.echo(f"Run #{run_id} not found.")
            return
        if not yes:
            click.confirm(f"Delete run #{run.id} ({run.project} / {run.test} / {run.status.value})?", abort=True)
        session.delete(run)
        session.commit()
        click.echo(f"Run #{run_id} deleted.")
    finally:
        session.close()


@main.command("delete-runs")
@click.option("--project", default=None, help="Filter by project")
@click.option("--ref", "git_ref", default=None, help="Filter by git ref")
@click.option("--test", default=None, help="Filter by test")
@click.option("--status", default=None, help="Filter by status (PASS/FAIL/ERROR/running/queued)")
@click.option("--before", "before_date", default=None, help="Delete runs started before this date (YYYY-MM-DD)")
@click.option("--yes", is_flag=True, help="Skip confirmation prompt")
def delete_runs(project, git_ref, test, status, before_date, yes):
    """Delete multiple test runs matching the given filters."""
    if not any([project, git_ref, test, status, before_date]):
        click.echo("ERROR: At least one filter is required (--project, --ref, --test, --status, --before).")
        return

    session = SessionLocal()
    try:
        query = select(TestRun)
        if project:
            query = query.where(TestRun.project == project)
        if git_ref:
            query = query.where(TestRun.git_ref == git_ref)
        if test:
            query = query.where(TestRun.test == test)
        if status:
            query = query.where(TestRun.status == TestRunStatus(status))
        if before_date:
            cutoff = datetime.datetime.strptime(before_date, "%Y-%m-%d")
            query = query.where(TestRun.started_at < cutoff)

        runs = session.execute(query).scalars().all()
        if not runs:
            click.echo("No matching runs found.")
            return

        click.echo(f"Found {len(runs)} run(s) to delete:")
        for run in runs[:10]:
            started = run.started_at.strftime("%Y-%m-%d %H:%M") if run.started_at else "-"
            click.echo(f"  #{run.id} {run.project} / {run.test} / {run.status.value} ({started})")
        if len(runs) > 10:
            click.echo(f"  ... and {len(runs) - 10} more")

        if not yes:
            click.confirm(f"Delete {len(runs)} run(s)?", abort=True)

        for run in runs:
            session.delete(run)
        session.commit()
        click.echo(f"Deleted {len(runs)} run(s).")
    finally:
        session.close()


@main.command("show-results")
@click.option("--project", default=None, help="Filter by project")
@click.option("--ref", "git_ref", default=None, help="Filter by git ref")
@click.option("--test", default=None, help="Filter by test")
@click.option("--status", default=None, help="Filter by status (PASS/FAIL/ERROR)")
@click.option("--limit", default=20, help="Max rows to show")
def show_results(project, git_ref, test, status, limit):
    """Show test run results (alias for list-runs)."""
    session = SessionLocal()
    try:
        query = select(TestRun).order_by(TestRun.id.desc()).limit(limit)
        if project:
            query = query.where(TestRun.project == project)
        if git_ref:
            query = query.where(TestRun.git_ref == git_ref)
        if test:
            query = query.where(TestRun.test == test)
        if status:
            query = query.where(TestRun.status == TestRunStatus(status))

        runs = session.execute(query).scalars().all()
        if not runs:
            click.echo("No results found.")
            return

        click.echo(f"{'ID':<6} {'Project':<20} {'Test':<14} {'Status':<10} {'Duration':<10} {'Started'}")
        click.echo("-" * 90)
        for run in runs:
            duration = f"{run.duration_seconds:.1f}s" if run.duration_seconds else "-"
            started = run.started_at.strftime("%Y-%m-%d %H:%M") if run.started_at else "-"
            click.echo(f"{run.id:<6} {run.project:<20} {run.test:<14} {run.status.value:<10} {duration:<10} {started}")
    finally:
        session.close()


@main.command("seed-projects")
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
                .where(TestRun.project == p.name)
                .where(TestRun.status.in_([TestRunStatus.PASS, TestRunStatus.FAIL, TestRunStatus.ERROR]))
                .order_by(TestRun.id.desc())
                .limit(1)
            ).scalar_one_or_none()

            if last_run:
                version = last_run.version or last_run.git_ref or "-"
                status = last_run.status.value
            else:
                version = "-"
                status = "-"

            github = f"{p.github_owner}/{p.github_repo}" if p.github_owner else "-"
            click.echo(f"{p.name:<20} {version:<14} {status:<10} {github}")
    finally:
        session.close()


def _parse_deps_axis(deps_str):
    """Parse 'omnetpp=6.3.0,6.2.0;inet=4.5' into {"omnetpp": ["6.3.0","6.2.0"], "inet": ["4.5"]}."""
    result = {}
    for part in deps_str.split(";"):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise click.ClickException(f"Invalid --deps format: expected 'name=ver1,ver2', got '{part}'")
        name, versions = part.split("=", 1)
        result[name.strip()] = [v.strip() for v in versions.split(",") if v.strip()]
    return result


def _parse_ref_range(ref_range_str):
    """Parse a 'base..head' string into a {"base": ..., "head": ...} dict."""
    if ".." not in ref_range_str:
        raise click.ClickException(f"Invalid --ref-range format: expected 'base..head', got '{ref_range_str}'")
    base, head = ref_range_str.split("..", 1)
    base, head = base.strip(), head.strip()
    if not base or not head:
        raise click.ClickException(f"Invalid --ref-range format: both base and head must be non-empty")
    return {"base": base, "head": head}


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
@click.option("--tests", required=True, help="Comma-separated tests")
@click.option("--refs", default=None, help="Comma-separated git refs to test (e.g. 'master,topic/my-feature')")
@click.option("--ref-range", "ref_range", default=None, help="Git ref range (base..head) — enumerate commits via GitHub API")
@click.option("--deps", default=None, help="Dependency versions axis (e.g. 'omnetpp=6.3.0,6.2.0;inet=4.5')")
@click.option("--isolation", default=None, help="Comma-separated isolation values: 'none' and/or 'podman' (cross-product axis)")
@click.option("--toolchain", default=None, help="Comma-separated toolchain values: 'none' and/or 'nix' (cross-product axis)")
@click.option("--opp-file", "opp_file", default=None, help="Path to the project's .opp file (for opp_repl project discovery)")
@click.option("--replace", is_flag=True, help="Replace existing matrix with the same name")
def create_matrix(name, project, tests, modes, os_names, os_versions,
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
    if refs and ref_range:
        click.echo("ERROR: --refs and --ref-range are mutually exclusive.")
        return

    Base.metadata.create_all(engine)
    session = SessionLocal()
    try:
        config = {
            "tests": [t.strip() for t in tests.split(",")],
            "modes": [m.strip() for m in modes.split(",")],
            "versions": [v.strip() for v in versions.split(",")] if versions else [project],
        }
        if ref_range:
            config["ref_range"] = _parse_ref_range(ref_range)
        elif refs:
            config["refs"] = [r.strip() for r in refs.split(",")]
        if os_names:
            config["os"] = [o.strip() for o in os_names.split(",")]
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
        if deps:
            config["deps"] = _parse_deps_axis(deps)
        if isolation:
            config["isolation"] = [v.strip() for v in isolation.split(",")]
        if toolchain:
            config["toolchain"] = [v.strip() for v in toolchain.split(",")]
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
            parts = [job["project"], job["test"], job["mode"]]
            if job.get("git_ref"):
                parts.append(f"@{job['git_ref']}")
            if job.get("platform_desc"):
                parts.append(job["platform_desc"])
            if job.get("resolved_deps"):
                deps_str = " ".join(f"{k}={v}" for k, v in job["resolved_deps"].items())
                parts.append(deps_str)
            click.echo(f"  {' × '.join(parts)}")
        if len(jobs) > 10:
            click.echo(f"  ... and {len(jobs) - 10} more")
    finally:
        session.close()


@main.command("list-matrices")
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


@main.command("run-matrix")
@click.option("--matrix", "matrix_name", required=True, help="Matrix name to run")
@click.option("--force", is_flag=True, help="Re-run even if identical runs already exist")
@click.option("--skip-install", is_flag=True, help="Skip opp_env install step")
def run_matrix(matrix_name, force, skip_install):
    """Expand a matrix and run all jobs sequentially."""
    from opp_ci.scheduler import expand_matrix

    Base.metadata.create_all(engine)
    session = SessionLocal()
    try:
        matrix = session.execute(
            select(TestMatrix).where(TestMatrix.name == matrix_name)
        ).scalar_one_or_none()
        if matrix is None:
            click.echo(f"Matrix '{matrix_name}' not found.")
            return

        jobs = expand_matrix(matrix.project, matrix.config)
        click.echo(f"Running matrix '{matrix_name}': {len(jobs)} jobs")

        # Install once per unique project+ref+toolchain+isolation combination.
        # install_project is a no-op unless isolation=none and toolchain=nix,
        # so non-nix jobs cheaply pass through.
        if not skip_install:
            installed = set()
            for job in jobs:
                install_key = (job["project"], job.get("git_ref"),
                               job.get("isolation"), job.get("toolchain"))
                if install_key not in installed:
                    try:
                        install_project(
                            job["project"], git_ref=job.get("git_ref"),
                            isolation=job.get("isolation") or "none",
                            toolchain=job.get("toolchain") or "none",
                        )
                        installed.add(install_key)
                    except RuntimeError as e:
                        click.echo(f"ERROR installing {job['project']}: {e}")
                        return

        passed = 0
        failed = 0
        errors = 0
        skipped = 0
        for i, job in enumerate(jobs, 1):
            if not force:
                existing = find_existing_run(
                    session,
                    project=job["project"],
                    test=job["test"],
                    mode=job.get("mode"),
                    git_ref=job.get("git_ref"),
                    os=job.get("os"),
                    os_version=job.get("os_version"),
                    distro=job.get("distro"),
                    distro_version=job.get("distro_version"),
                    flavor=job.get("flavor"),
                    flavor_version=job.get("flavor_version"),
                    arch=job.get("arch"),
                    compiler=job.get("compiler"),
                    compiler_version=job.get("compiler_version"),
                    isolation=job.get("isolation"),
                    toolchain=job.get("toolchain"),
                )
                if existing:
                    click.echo(f"  [{i}/{len(jobs)}] SKIP (run #{existing.id} {existing.status.value})")
                    skipped += 1
                    continue

            test_run = TestRun(
                project=job["project"],
                test=job["test"],
                mode=job.get("mode"),
                git_ref=job.get("git_ref"),
                os=job.get("os"),
                os_version=job.get("os_version"),
                distro=job.get("distro"),
                distro_version=job.get("distro_version"),
                flavor=job.get("flavor"),
                flavor_version=job.get("flavor_version"),
                arch=job.get("arch"),
                compiler=job.get("compiler"),
                compiler_version=job.get("compiler_version"),
                isolation=job.get("isolation"),
                toolchain=job.get("toolchain"),
                platform_desc=job.get("platform_desc"),
                resolved_deps=job.get("resolved_deps"),
                matrix_id=matrix.id,
                status=TestRunStatus.running,
                started_at=datetime.datetime.utcnow(),
            )
            session.add(test_run)
            session.commit()

            parts = [job["project"], job["test"], job.get("mode", "")]
            if job.get("git_ref"):
                parts.append(f"@{job['git_ref']}")
            if job.get("platform_desc"):
                parts.append(job["platform_desc"])
            click.echo(f"  [{i}/{len(jobs)}] {' × '.join(parts)}", nl=False)

            try:
                outcome = run_test(
                    job["project"], job["test"],
                    git_ref=job.get("git_ref"), opp_file=matrix.opp_file,
                    mode=job.get("mode"),
                    isolation=job.get("isolation"), toolchain=job.get("toolchain"),
                    os=job.get("os"), os_version=job.get("os_version"),
                    distro=job.get("distro"), distro_version=job.get("distro_version"),
                    flavor=job.get("flavor"), flavor_version=job.get("flavor_version"),
                    arch=job.get("arch"),
                    compiler=job.get("compiler"), compiler_version=job.get("compiler_version"),
                )
            except Exception as e:
                test_run.status = TestRunStatus.ERROR
                test_run.finished_at = datetime.datetime.utcnow()
                session.add(TestResult(
                    test_run_id=test_run.id,
                    result_code="ERROR",
                    stderr=str(e),
                ))
                session.commit()
                click.echo(f" → ERROR")
                errors += 1
                continue

            test_run.status = TestRunStatus.PASS if outcome["result_code"] == "PASS" else TestRunStatus.FAIL
            test_run.finished_at = datetime.datetime.utcnow()
            test_run.duration_seconds = outcome["duration_seconds"]
            test_run.commit_sha = outcome.get("commit_sha")
            session.add(TestResult(
                test_run_id=test_run.id,
                result_code=outcome["result_code"],
                stdout=outcome["stdout"],
                stderr=outcome["stderr"],
                details=outcome.get("details"),
            ))
            session.commit()
            update_ci_note(job["project"], test_run.commit_sha, session, opp_file=matrix.opp_file)

            if outcome["result_code"] == "PASS":
                passed += 1
                click.echo(f" → PASS ({outcome['duration_seconds']:.1f}s)")
            else:
                failed += 1
                click.echo(f" → FAIL ({outcome['duration_seconds']:.1f}s)")

        click.echo(f"\nMatrix complete: {passed} passed, {failed} failed, {errors} errors, {skipped} skipped")

        # Trigger GitHub Action notes sync once for the whole matrix
        proj = session.execute(
            select(Project).where(Project.name == matrix.project)
        ).scalar_one_or_none()
        if proj and proj.github_owner and proj.github_repo:
            from opp_ci.notes import trigger_notes_sync
            trigger_notes_sync(proj.github_owner, proj.github_repo)
    finally:
        session.close()


@main.command("seed-matrices")
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
                                             (VARIANT_ID or kubuntu-desktop)
                                             is recognised
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
            if variant_id and _platforms.is_known_flavor(variant_id):
                tags.append(f"flavor:{variant_id}-{distro_ver}")
            elif distro_id == "ubuntu" and shutil.which("plasmashell"):
                # Heuristic: KDE Plasma on Ubuntu ⇒ Kubuntu.
                tags.append(f"flavor:kubuntu-{distro_ver}")
        except OSError:
            pass
    elif system == "windows":
        tags.append("os:windows")
        ver = _platform.release() or _platform.version()
        if ver:
            tags.append(f"os:windows-{ver}")
    elif system == "darwin":
        tags.append("os:macos")
        ver = _platform.mac_ver()[0]
        if ver:
            tags.append(f"os:macos-{ver}")

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
def worker_start(coordinator, token, poll_interval, heartbeat_interval):
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
    agent.start(poll_interval=poll_interval, heartbeat_interval=heartbeat_interval)


@worker_group.command("register")
@click.option("--name", required=True, help="Worker name (unique)")
@click.option("--tags", default="", help="Comma-separated capability tags")
@click.option("--auto-tags/--no-auto-tags", default=False,
              help="Detect os:/compiler:/podman/nix tags from this host and union them with --tags")
@click.option("--concurrency", default=1, help="Max concurrent jobs")
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

        click.echo(f"{'ID':<6} {'Name':<20} {'Status':<10} {'Jobs':<6} {'Cap':<6} {'Tags':<30} {'Last Heartbeat'}")
        click.echo("-" * 110)
        for w in workers:
            hb = w.last_heartbeat.strftime("%Y-%m-%d %H:%M:%S") if w.last_heartbeat else "-"
            tags_str = ", ".join(w.tags) if w.tags else "-"
            click.echo(f"{w.id:<6} {w.name:<20} {w.status:<10} {w.current_job_count:<6} {w.concurrency:<6} {tags_str:<30} {hb}")
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
@click.option("--test", required=True)
@click.option("--mode", default=None)
@click.option("--opp-file", default=None)
@click.option("--git-ref", default=None)
def internal_run_direct(project, test, mode, opp_file, git_ref):
    """Run a single test by calling opp_repl directly (host-toolchain path).

    Designed to be invoked inside an opp-ci-runner image whose base OS already
    has the requested compiler installed. Writes the test's stdout/stderr to
    this process's stdout/stderr and exits 0 on PASS, 1 otherwise.
    """
    from opp_ci.executor import _run_test_direct
    outcome = _run_test_direct(
        project, test, opp_file=opp_file, git_ref=git_ref, mode=mode,
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
              help="OMNeT++ version baked into the image (required for --toolchain=host)")
@click.option("--toolchain", type=click.Choice(["host", "nix"]), required=True,
              help="Whether the compiler comes from the OS package manager (host) or opp_env/Nix")
@click.option("--push", is_flag=True, help="Push the built image to the configured registry")
def image_build(os_name, os_version, distro, distro_version, flavor, flavor_version,
                compiler, compiler_version, omnetpp_version, toolchain, push):
    """Build one opp-ci-runner image for a (toolchain, platform, compiler, omnetpp) combination."""
    from opp_ci.executor import build_runner_image
    from opp_ci import platforms

    if toolchain == "host":
        if not compiler or not compiler_version:
            raise click.ClickException("--compiler and --compiler-version are required when --toolchain=host")
        if not omnetpp_version:
            raise click.ClickException("--omnetpp-version is required when --toolchain=host")

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
    if toolchain == "nix":
        tag = f"opp-ci-runner:nix-{slug}"
    else:
        tag = f"opp-ci-runner:host-{slug}-{compiler.lower()}-{compiler_version}-omnetpp-{omnetpp_version}"

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
@click.pass_context
def image_build_matrix(ctx, matrix_name, push):
    """Build every opp-ci-runner image referenced by a matrix's expansion.

    Walks the expanded job list, derives the unique image tags for jobs
    with isolation=podman, and invokes 'opp_ci image build' for each one.
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
            ctx.invoke(
                image_build,
                os_name=key[1],
                os_version=key[2],
                distro=key[3],
                distro_version=key[4],
                flavor=key[5],
                flavor_version=key[6],
                compiler=key[7],
                compiler_version=key[8],
                omnetpp_version=key[9],
                toolchain=toolchain_arg,
                push=push,
            )
        click.echo(f"Built {len(seen)} image(s) for matrix '{matrix_name}'")
    finally:
        session.close()
