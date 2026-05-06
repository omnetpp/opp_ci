import datetime
import logging

import click
from sqlalchemy import select

from opp_ci.db.connection import engine, SessionLocal
from opp_ci.db.models import Base, TestRun, TestRunStatus, TestResult
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
@click.option("--test", "test_type", required=True, help="Test type (e.g. smoke)")
@click.option("--skip-install", is_flag=True, help="Skip opp_env install step")
def run_cmd(project, test_type, skip_install):
    """Run a test for a project and store the result."""
    Base.metadata.create_all(engine)
    session = SessionLocal()
    try:
        # Create TestRun record
        test_run = TestRun(
            project=project,
            test_type=test_type,
            status=TestRunStatus.running,
            started_at=datetime.datetime.utcnow(),
        )
        session.add(test_run)
        session.commit()

        click.echo(f"Test run #{test_run.id}: {project} / {test_type}")

        # Install
        if not skip_install:
            try:
                install_project(project)
            except RuntimeError as e:
                test_run.status = TestRunStatus.error
                test_run.finished_at = datetime.datetime.utcnow()
                session.add(TestResult(
                    test_run_id=test_run.id,
                    result_code="ERROR",
                    stderr=str(e),
                ))
                session.commit()
                click.echo(f"ERROR: {e}")
                return

        # Run test
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
            click.echo(f"ERROR: {e}")
            return

        # Store result
        test_run.status = TestRunStatus.passed if outcome["result_code"] == "PASS" else TestRunStatus.failed
        test_run.finished_at = datetime.datetime.utcnow()
        test_run.duration_seconds = outcome["duration_seconds"]
        session.add(TestResult(
            test_run_id=test_run.id,
            result_code=outcome["result_code"],
            stdout=outcome["stdout"],
            stderr=outcome["stderr"],
        ))
        session.commit()
        click.echo(f"Result: {outcome['result_code']} ({outcome['duration_seconds']:.1f}s)")
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


@main.command("show-results")
@click.option("--project", default=None, help="Filter by project")
@click.option("--test", "test_type", default=None, help="Filter by test type")
@click.option("--status", default=None, help="Filter by status (passed/failed/error)")
@click.option("--limit", default=20, help="Max rows to show")
def show_results(project, test_type, status, limit):
    """Show test run results."""
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
