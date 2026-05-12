import datetime
import logging

import click
from sqlalchemy import select

from opp_ci.db.connection import engine, SessionLocal
from opp_ci.db.models import Base, Project, Version, TestMatrix, TestRun, TestRunStatus, TestResult, Worker, ApiToken, AutoTestRule
from opp_ci.executor import install_project, run_test
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


@main.command("run")
@click.option("--project", required=True, help="opp_env project name (e.g. inet-4.5)")
@click.option("--test", "test_types", required=True, help="Test type(s), comma-separated (e.g. smoke,fingerprint)")
@click.option("--ref", "git_ref", default=None, help="Git branch, tag, or commit to test (e.g. master, topic/my-feature)")
@click.option("--pin", "pins", multiple=True, help="Pin dependency version (e.g. --pin omnetpp=6.1). Repeatable.")
@click.option("--skip-install", is_flag=True, help="Skip opp_env install step")
@click.pass_context
def run_cmd(ctx, project, test_types, git_ref, pins, skip_install):
    """Run test(s) for a project and store the results."""
    if ctx.obj.get("remote"):
        _run_remote(project, test_types, git_ref)
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

        # Install once for all test types
        if not skip_install:
            try:
                install_project(project, git_ref=git_ref)
            except RuntimeError as e:
                click.echo(f"ERROR during install: {e}")
                return

        for test_type in test_types.split(","):
            test_type = test_type.strip()
            if not test_type:
                continue

            test_run = TestRun(
                project=project,
                test_type=test_type,
                git_ref=git_ref,
                status=TestRunStatus.running,
                started_at=datetime.datetime.utcnow(),
            )
            session.add(test_run)
            session.commit()

            desc = f"{project}@{git_ref}" if git_ref else project
            click.echo(f"Test run #{test_run.id}: {desc} / {test_type}")

            try:
                outcome = run_test(project, test_type, git_ref=git_ref)
            except Exception as e:
                test_run.status = TestRunStatus.error
                test_run.finished_at = datetime.datetime.utcnow()
                session.add(TestResult(
                    test_run_id=test_run.id,
                    result_code="ERROR",
                    stderr=str(e),
                ))
                session.commit()
                click.echo(f"  ERROR: {e}")
                continue

            test_run.status = TestRunStatus.passed if outcome["result_code"] == "PASS" else TestRunStatus.failed
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


def _run_remote(project, test_types, git_ref):
    """Submit test run(s) to the remote coordinator."""
    from opp_ci.config import COORDINATOR_URL, API_TOKEN
    from opp_ci.client import OppCiClient

    if not API_TOKEN:
        click.echo("ERROR: Set OPP_CI_API_TOKEN env var for remote submission.")
        return

    client = OppCiClient(url=COORDINATOR_URL, token=API_TOKEN)
    for test_type in test_types.split(","):
        test_type = test_type.strip()
        if not test_type:
            continue
        try:
            result = client.submit_run(project=project, test_type=test_type, git_ref=git_ref)
            click.echo(f"Submitted run #{result['id']}: {project} / {test_type} → {result['status']}")
        except Exception as e:
            click.echo(f"ERROR submitting {project}/{test_type}: {e}")


@main.command("serve")
@click.option("--host", default="127.0.0.1", help="Bind host")
@click.option("--port", default=8000, help="Bind port")
def serve(host, port):
    """Start the web UI server."""
    import uvicorn
    from opp_ci.web.app import app
    Base.metadata.create_all(engine)
    click.echo(f"Starting opp_ci web UI at http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="info")


@main.command("list-runs")
@click.option("--project", default=None, help="Filter by project")
@click.option("--ref", "git_ref", default=None, help="Filter by git ref")
@click.option("--test", "test_type", default=None, help="Filter by test type")
@click.option("--status", default=None, help="Filter by status (passed/failed/error)")
@click.option("--limit", default=20, help="Max rows to show")
def list_runs(project, git_ref, test_type, status, limit):
    """List test runs."""
    session = SessionLocal()
    try:
        query = select(TestRun).order_by(TestRun.id.desc()).limit(limit)
        if project:
            query = query.where(TestRun.project == project)
        if git_ref:
            query = query.where(TestRun.git_ref == git_ref)
        if test_type:
            query = query.where(TestRun.test_type == test_type)
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
            click.echo(f"{run.id:<6} {run.project:<20} {ref:<16} {run.test_type:<14} {run.status.value:<10} {duration:<10} {started}")
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
        click.echo(f"  Version:  {run.version or '-'}")
        click.echo(f"  Test:     {run.test_type}")
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


@main.command("show-results")
@click.option("--project", default=None, help="Filter by project")
@click.option("--test", "test_type", default=None, help="Filter by test type")
@click.option("--status", default=None, help="Filter by status (passed/failed/error)")
@click.option("--limit", default=20, help="Max rows to show")
def show_results(project, test_type, status, limit):
    """Show test run results (alias for list-runs)."""
    session = SessionLocal()
    try:
        query = select(TestRun).order_by(TestRun.id.desc()).limit(limit)
        if project:
            query = query.where(TestRun.project == project)
        if test_type:
            query = query.where(TestRun.test_type == test_type)
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
            click.echo(f"{run.id:<6} {run.project:<20} {run.test_type:<14} {run.status.value:<10} {duration:<10} {started}")
    finally:
        session.close()


@main.command("seed-projects")
def seed_projects_cmd():
    """Seed the database with Tier 1 projects from the catalog."""
    from opp_ci.catalog import seed_projects
    Base.metadata.create_all(engine)
    session = SessionLocal()
    try:
        seed_projects(session)
        click.echo("Tier 1 projects seeded.")
    finally:
        session.close()


@main.command("add-project")
@click.option("--name", required=True, help="Project name (e.g. mm1k)")
@click.option("--github", default=None, help="GitHub owner/repo (e.g. levy/mm1k)")
@click.option("--git-url", default=None, help="Git clone URL")
@click.option("--opp-env-name", default=None, help="opp_env project name (defaults to --name)")
@click.option("--tier", default=1, type=int, help="Tier level (1 or 2, default: 1)")
@click.option("--deps", default=None, help="Comma-separated dependency project names (e.g. omnetpp,inet)")
def add_project_cmd(name, github, git_url, opp_env_name, tier, deps):
    """Register a new project in the database."""
    Base.metadata.create_all(engine)
    session = SessionLocal()
    try:
        existing = session.execute(
            select(Project).where(Project.name == name)
        ).scalar_one_or_none()
        if existing is not None:
            click.echo(f"Project '{name}' already exists (tier={existing.tier}).")
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
            tier=tier,
            dependency_names=dep_list,
        )
        session.add(project)
        session.commit()
        click.echo(f"Project '{name}' added (tier={tier}, deps={dep_list}).")
    finally:
        session.close()


@main.command("sync-catalog")
def sync_catalog_cmd():
    """Sync projects from the opp_env catalog into the database.

    Imports all opp_env projects as Tier 2 (if not already present),
    adds new versions, and creates default test matrices for new projects.
    Existing Tier 1 projects are not demoted.
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
    """List known projects with tier, last test status, and GitHub info."""
    Base.metadata.create_all(engine)
    session = SessionLocal()
    try:
        projects = session.execute(
            select(Project).order_by(Project.tier, Project.name)
        ).scalars().all()
        if not projects:
            click.echo("No projects. Run 'opp_ci seed-projects' or 'opp_ci sync-catalog'.")
            return

        click.echo(f"{'Name':<20} {'Tier':<6} {'Last Test':<14} {'Status':<10} {'GitHub'}")
        click.echo("-" * 80)
        for p in projects:
            # Find most recent finished run for this project
            last_run = session.execute(
                select(TestRun)
                .where(TestRun.project == p.name)
                .where(TestRun.status.in_([TestRunStatus.passed, TestRunStatus.failed, TestRunStatus.error]))
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
            click.echo(f"{p.name:<20} {p.tier:<6} {version:<14} {status:<10} {github}")
    finally:
        session.close()


@main.command("create-matrix")
@click.option("--name", required=True, help="Matrix name (e.g. inet-default)")
@click.option("--project", required=True, help="Project name")
@click.option("--project-versions", "versions", default=None, help="Comma-separated project versions (optional, defaults to project name)")
@click.option("--builds", "modes", default="release", help="Comma-separated build modes (default: release)")
@click.option("--os", "os_names", default=None, help="Comma-separated OS (e.g. 'Ubuntu 24.04,Fedora 41' or 'Ubuntu,Fedora' with --os-version)")
@click.option("--os-version", "os_versions", default=None, help="Comma-separated OS versions for cross-product (e.g. '24.04,41')")
@click.option("--compiler", "compilers", default=None, help="Comma-separated compilers (e.g. 'gcc-14,clang-18' or 'gcc,clang' with --compiler-version)")
@click.option("--compiler-version", "compiler_versions", default=None, help="Comma-separated compiler versions for cross-product (e.g. '14,18')")
@click.option("--tests", "test_types", required=True, help="Comma-separated test types")
@click.option("--refs", default=None, help="Comma-separated git refs to test (e.g. 'master,topic/my-feature')")
@click.option("--opp-file", "opp_file", default=None, help="Path to the project's .opp file (for opp_repl project discovery)")
def create_matrix(name, project, test_types, modes, os_names, os_versions, compilers, compiler_versions, versions, refs, opp_file):
    """Create a test matrix configuration.

    Platform axes support two styles:

    Combined: --os 'Ubuntu 24.04,Fedora 41' (parsed into name+version automatically)

    Structured: --os 'Ubuntu,Fedora' --os-version '24.04,41' (cross-product)

    Same for --compiler / --compiler-version.
    """
    Base.metadata.create_all(engine)
    session = SessionLocal()
    try:
        config = {
            "test_types": [t.strip() for t in test_types.split(",")],
            "modes": [m.strip() for m in modes.split(",")],
            "versions": [v.strip() for v in versions.split(",")] if versions else [project],
        }
        if refs:
            config["refs"] = [r.strip() for r in refs.split(",")]
        if os_names:
            config["os"] = [o.strip() for o in os_names.split(",")]
        if os_versions:
            config["os_version"] = [o.strip() for o in os_versions.split(",")]
        if compilers:
            config["compiler"] = [c.strip() for c in compilers.split(",")]
        if compiler_versions:
            config["compiler_version"] = [c.strip() for c in compiler_versions.split(",")]
        matrix = TestMatrix(name=name, project=project, opp_file=opp_file, config=config)
        session.add(matrix)
        session.commit()

        from opp_ci.scheduler import expand_matrix
        jobs = expand_matrix(project, config)
        click.echo(f"Matrix '{name}' created ({len(jobs)} jobs when expanded):")
        for job in jobs[:10]:
            parts = [job["project"], job["test_type"], job["mode"]]
            if job.get("git_ref"):
                parts.append(f"@{job['git_ref']}")
            if job.get("platform_desc"):
                parts.append(job["platform_desc"])
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
@click.option("--skip-install", is_flag=True, help="Skip opp_env install step")
def run_matrix(matrix_name, skip_install):
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

        # Install once per unique project+ref combination
        if not skip_install:
            installed = set()
            for job in jobs:
                install_key = (job["project"], job.get("git_ref"))
                if install_key not in installed:
                    try:
                        install_project(job["project"], git_ref=job.get("git_ref"))
                        installed.add(install_key)
                    except RuntimeError as e:
                        click.echo(f"ERROR installing {job['project']}: {e}")
                        return

        passed = 0
        failed = 0
        errors = 0
        for i, job in enumerate(jobs, 1):
            test_run = TestRun(
                project=job["project"],
                test_type=job["test_type"],
                mode=job.get("mode"),
                git_ref=job.get("git_ref"),
                os=job.get("os"),
                os_version=job.get("os_version"),
                compiler=job.get("compiler"),
                compiler_version=job.get("compiler_version"),
                platform_desc=job.get("platform_desc"),
                matrix_id=matrix.id,
                status=TestRunStatus.running,
                started_at=datetime.datetime.utcnow(),
            )
            session.add(test_run)
            session.commit()

            parts = [job["project"], job["test_type"], job.get("mode", "")]
            if job.get("git_ref"):
                parts.append(f"@{job['git_ref']}")
            if job.get("platform_desc"):
                parts.append(job["platform_desc"])
            click.echo(f"  [{i}/{len(jobs)}] {' × '.join(parts)}", nl=False)

            try:
                outcome = run_test(job["project"], job["test_type"], git_ref=job.get("git_ref"), opp_file=matrix.opp_file)
            except Exception as e:
                test_run.status = TestRunStatus.error
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

            test_run.status = TestRunStatus.passed if outcome["result_code"] == "PASS" else TestRunStatus.failed
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

        click.echo(f"\nMatrix complete: {passed} passed, {failed} failed, {errors} errors")
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


# ── Worker commands ────────────────────────────────────────────────────

@main.group("worker")
def worker_group():
    """Worker management commands."""
    pass


@worker_group.command("start")
@click.option("--coordinator", required=True, help="Coordinator URL (e.g. https://ci.omnetpp.org)")
@click.option("--token", required=True, help="Worker token")
@click.option("--tags", default="", help="Comma-separated capability tags (e.g. linux,amd64,perf-counters)")
@click.option("--concurrency", default=1, help="Max concurrent jobs (default: 1)")
@click.option("--poll-interval", default=10, help="Seconds between polls (default: 10)")
@click.option("--heartbeat-interval", default=30, help="Seconds between heartbeats (default: 30)")
def worker_start(coordinator, token, tags, concurrency, poll_interval, heartbeat_interval):
    """Start a worker agent that polls the coordinator for jobs."""
    from opp_ci.worker import WorkerAgent

    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
    agent = WorkerAgent(
        coordinator_url=coordinator,
        token=token,
        tags=tag_list,
        concurrency=concurrency,
    )
    click.echo(f"Starting worker — coordinator={coordinator} tags={tag_list} concurrency={concurrency}")
    agent.start(poll_interval=poll_interval, heartbeat_interval=heartbeat_interval)


@worker_group.command("register")
@click.option("--name", required=True, help="Worker name (unique)")
@click.option("--tags", default="", help="Comma-separated capability tags")
@click.option("--concurrency", default=1, help="Max concurrent jobs")
def worker_register(name, tags, concurrency):
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

        tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
        worker = Worker(name=name, tags=tag_list, concurrency=concurrency)
        session.add(worker)
        session.commit()
        click.echo(f"Worker '{name}' registered.")
        click.echo(f"  ID:          {worker.id}")
        click.echo(f"  Token:       {worker.token}")
        click.echo(f"  Tags:        {worker.tags}")
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
def rule_create(project, rule_type, pattern, matrix_name):
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
