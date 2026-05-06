import datetime
import logging

import click
from sqlalchemy import select

from opp_ci.db.connection import engine, SessionLocal
from opp_ci.db.models import Base, Project, TestMatrix, TestRun, TestRunStatus, TestResult
from opp_ci.executor import install_project, run_test


@click.group()
@click.option("-v", "--verbose", is_flag=True, help="Enable verbose logging")
def main(verbose):
    """opp_ci — CI for OMNeT++ simulation projects."""
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
@click.option("--skip-install", is_flag=True, help="Skip opp_env install step")
def run_cmd(project, test_types, skip_install):
    """Run test(s) for a project and store the results."""
    Base.metadata.create_all(engine)
    session = SessionLocal()
    try:
        # Install once for all test types
        if not skip_install:
            try:
                install_project(project)
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
                status=TestRunStatus.running,
                started_at=datetime.datetime.utcnow(),
            )
            session.add(test_run)
            session.commit()

            click.echo(f"Test run #{test_run.id}: {project} / {test_type}")

            try:
                outcome = run_test(project, test_type)
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
            session.add(TestResult(
                test_run_id=test_run.id,
                result_code=outcome["result_code"],
                stdout=outcome["stdout"],
                stderr=outcome["stderr"],
                details=outcome.get("details"),
            ))
            session.commit()
            click.echo(f"  Result: {outcome['result_code']} ({outcome['duration_seconds']:.1f}s)")
    finally:
        session.close()


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
@click.option("--test", "test_type", default=None, help="Filter by test type")
@click.option("--status", default=None, help="Filter by status (passed/failed/error)")
@click.option("--limit", default=20, help="Max rows to show")
def list_runs(project, test_type, status, limit):
    """List test runs."""
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
            click.echo("No runs found.")
            return

        click.echo(f"{'ID':<6} {'Project':<20} {'Test':<14} {'Status':<10} {'Duration':<10} {'Started'}")
        click.echo("-" * 90)
        for run in runs:
            duration = f"{run.duration_seconds:.1f}s" if run.duration_seconds else "-"
            started = run.started_at.strftime("%Y-%m-%d %H:%M") if run.started_at else "-"
            click.echo(f"{run.id:<6} {run.project:<20} {run.test_type:<14} {run.status.value:<10} {duration:<10} {started}")
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


@main.command("list-projects")
def list_projects():
    """List known projects."""
    Base.metadata.create_all(engine)
    session = SessionLocal()
    try:
        projects = session.execute(
            select(Project).order_by(Project.tier, Project.name)
        ).scalars().all()
        if not projects:
            click.echo("No projects. Run 'opp_ci seed-projects' to import Tier 1 projects.")
            return

        click.echo(f"{'Name':<16} {'Tier':<6} {'Dependencies':<30} {'GitHub'}")
        click.echo("-" * 80)
        for p in projects:
            deps = ", ".join(p.dependency_names) if p.dependency_names else "-"
            github = f"{p.github_owner}/{p.github_repo}" if p.github_owner else "-"
            click.echo(f"{p.name:<16} {p.tier:<6} {deps:<30} {github}")
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
def create_matrix(name, project, test_types, modes, os_names, os_versions, compilers, compiler_versions, versions):
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
        if os_names:
            config["os"] = [o.strip() for o in os_names.split(",")]
        if os_versions:
            config["os_version"] = [o.strip() for o in os_versions.split(",")]
        if compilers:
            config["compiler"] = [c.strip() for c in compilers.split(",")]
        if compiler_versions:
            config["compiler_version"] = [c.strip() for c in compiler_versions.split(",")]
        matrix = TestMatrix(name=name, project=project, config=config)
        session.add(matrix)
        session.commit()

        from opp_ci.scheduler import expand_matrix
        jobs = expand_matrix(project, config)
        click.echo(f"Matrix '{name}' created ({len(jobs)} jobs when expanded):")
        for job in jobs[:10]:
            parts = [job["project"], job["test_type"], job["mode"]]
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

        # Install once per unique project version
        if not skip_install:
            installed = set()
            for job in jobs:
                if job["project"] not in installed:
                    try:
                        install_project(job["project"])
                        installed.add(job["project"])
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
            if job.get("platform_desc"):
                parts.append(job["platform_desc"])
            click.echo(f"  [{i}/{len(jobs)}] {' × '.join(parts)}", nl=False)

            try:
                outcome = run_test(job["project"], job["test_type"])
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
            session.add(TestResult(
                test_run_id=test_run.id,
                result_code=outcome["result_code"],
                stdout=outcome["stdout"],
                stderr=outcome["stderr"],
                details=outcome.get("details"),
            ))
            session.commit()

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
