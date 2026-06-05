"""
GitHub status updater for opp_ci.

Called when a test run completes to:
1. Post the final commit status (success/failure/error) to GitHub.
2. Update or create a PR comment with a result summary (if run is from a PR).
"""

import logging

from sqlalchemy import select

from opp_ci.config import COORDINATOR_URL
from opp_ci.db.connection import SessionLocal
from opp_ci.db.models import TestMatrixRun, TestRun
from opp_ci.github.client import GitHubClient

_logger = logging.getLogger(__name__)


def format_results_comment(runs):
    """Format a Markdown PR comment body summarizing a set of test runs."""
    if not runs:
        return "**opp_ci**: No test results yet."

    total = len(runs)
    passed = sum(1 for r in runs if r.effective_status == "PASS")
    failed = sum(1 for r in runs if r.effective_status == "FAIL")
    errors = sum(1 for r in runs if r.effective_status == "ERROR")
    running = sum(1 for r in runs if r.effective_status in ("running", "queued"))

    if failed == 0 and errors == 0 and running == 0:
        header = f"✅ **opp_ci**: All {total} tests passed"
    elif running > 0:
        header = f"⏳ **opp_ci**: {passed}/{total} passed, {running} still running"
    else:
        header = f"❌ **opp_ci**: {passed}/{total} passed, {failed} failed, {errors} errors"

    lines = [header, "", "| Kind | Project | Status | Duration |", "|---|---|---|---|"]
    for run in runs:
        status_emoji = {
            "PASS": "✅", "FAIL": "❌", "ERROR": "⚠️",
            "running": "🔄", "queued": "⏳",
        }.get(run.effective_status, "❓")
        dur = f"{run.duration_seconds:.1f}s" if run.duration_seconds else "-"
        lines.append(f"| {run.kind} | {run.project} | {status_emoji} {run.effective_status} | {dur} |")

    return "\n".join(lines)


def update_github_status(run_id):
    """
    Post commit status and optionally update PR comment for a completed run.

    Called after a run finishes (from the worker result endpoint or local execution).
    """
    session = SessionLocal()
    try:
        run = session.execute(
            select(TestRun).where(TestRun.id == run_id)
        ).scalar_one_or_none()

        if run is None:
            _logger.warning("Run #%d not found for status update", run_id)
            return

        if not run.github_owner or not run.github_repo or not run.github_commit_sha:
            return  # Not a GitHub-triggered run

        client = GitHubClient()
        if not client.is_configured:
            _logger.debug("GitHub token not configured, skipping status update")
            return

        target_url = f"{COORDINATOR_URL}/runs/{run.id}"

        try:
            client.set_status_from_run(
                owner=run.github_owner,
                repo=run.github_repo,
                sha=run.github_commit_sha,
                run_id=run.id,
                run_status=run.effective_status,
                target_url=target_url,
            )
        except Exception as e:
            _logger.error("Failed to post commit status for run #%d: %s", run.id, e)

        if run.github_pr_number:
            _update_pr_comment(session, client, run)

    finally:
        session.close()


def _update_pr_comment(session, client, run):
    """Update or create the PR comment summarizing all runs for this PR.

    Runs are grouped through TestMatrixRun; GitHub identity (owner/repo/PR
    number/commit SHA) lives on that row, so we filter by joining on it.
    """
    try:
        pr_runs = session.execute(
            select(TestRun)
            .join(TestMatrixRun, TestRun.matrix_run_id == TestMatrixRun.id)
            .where(
                TestMatrixRun.github_owner == run.github_owner,
                TestMatrixRun.github_repo == run.github_repo,
                TestMatrixRun.github_pr_number == run.github_pr_number,
                TestMatrixRun.github_commit_sha == run.github_commit_sha,
            ).order_by(TestRun.id)
        ).scalars().all()

        body = format_results_comment(pr_runs)
        client.update_or_create_pr_comment(
            owner=run.github_owner,
            repo=run.github_repo,
            pr_number=run.github_pr_number,
            body=body,
        )
    except Exception as e:
        _logger.error("Failed to update PR comment for run #%d: %s", run.id, e)
