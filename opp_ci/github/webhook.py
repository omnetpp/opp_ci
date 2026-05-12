"""
GitHub webhook receiver for opp_ci.

Handles push and pull_request events:
1. Extract repo, branch/tag/PR info, and commit SHA from the event payload.
2. Look up the Project in the DB by github_owner + github_repo.
3. Match against AutoTestRule patterns for that project.
4. For each matching rule, expand the linked matrix and queue TestRuns.
5. Post "pending" commit status on GitHub for each queued run.
"""

import fnmatch
import hashlib
import hmac
import logging

from sqlalchemy import select

from opp_ci.config import GITHUB_WEBHOOK_SECRET, COORDINATOR_URL
from opp_ci.db.connection import SessionLocal
from opp_ci.db.models import (
    Project, AutoTestRule, TestMatrix, TestRun, TestRunStatus,
)
from opp_ci.github.client import GitHubClient
from opp_ci.scheduler import expand_matrix

_logger = logging.getLogger(__name__)


def verify_signature(payload_body, signature_header):
    """
    Verify the GitHub webhook HMAC-SHA256 signature.

    Args:
        payload_body: Raw request body bytes
        signature_header: Value of X-Hub-Signature-256 header

    Returns:
        True if valid (or if no secret is configured), False otherwise.
    """
    if not GITHUB_WEBHOOK_SECRET:
        _logger.warning("No webhook secret configured — skipping signature verification")
        return True

    if not signature_header:
        return False

    expected = "sha256=" + hmac.new(
        GITHUB_WEBHOOK_SECRET.encode(),
        payload_body,
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(expected, signature_header)


def handle_webhook_event(event_type, payload):
    """
    Process a GitHub webhook event.

    Args:
        event_type: GitHub event type (from X-GitHub-Event header)
        payload: Parsed JSON payload

    Returns:
        dict with summary of actions taken
    """
    if event_type == "push":
        return _handle_push(payload)
    elif event_type == "pull_request":
        return _handle_pull_request(payload)
    elif event_type == "ping":
        return {"action": "pong", "zen": payload.get("zen", "")}
    else:
        _logger.debug("Ignoring event type: %s", event_type)
        return {"action": "ignored", "event_type": event_type}


def _handle_push(payload):
    """Handle a push event — test branch pushes and tag pushes."""
    ref = payload.get("ref", "")
    after = payload.get("after", "")
    repo_data = payload.get("repository", {})
    owner = repo_data.get("owner", {}).get("login", "") or repo_data.get("owner", {}).get("name", "")
    repo = repo_data.get("name", "")

    if not owner or not repo or not after:
        return {"action": "skipped", "reason": "incomplete payload"}

    # Determine if this is a branch or tag push
    if ref.startswith("refs/heads/"):
        branch = ref[len("refs/heads/"):]
        return _match_and_queue(
            owner=owner,
            repo=repo,
            rule_type="branch",
            ref_name=branch,
            commit_sha=after,
            git_ref=after,
        )
    elif ref.startswith("refs/tags/"):
        tag = ref[len("refs/tags/"):]
        return _match_and_queue(
            owner=owner,
            repo=repo,
            rule_type="tag",
            ref_name=tag,
            commit_sha=after,
            git_ref=after,
        )
    else:
        return {"action": "skipped", "reason": f"unrecognized ref: {ref}"}


def _handle_pull_request(payload):
    """Handle a pull_request event — test on open, synchronize (new push), reopen."""
    action = payload.get("action", "")
    if action not in ("opened", "synchronize", "reopened"):
        return {"action": "skipped", "reason": f"pr action '{action}' not handled"}

    pr = payload.get("pull_request", {})
    pr_number = pr.get("number")
    head = pr.get("head", {})
    commit_sha = head.get("sha", "")
    branch = head.get("ref", "")
    repo_data = payload.get("repository", {})
    owner = repo_data.get("owner", {}).get("login", "") or repo_data.get("owner", {}).get("name", "")
    repo = repo_data.get("name", "")

    if not owner or not repo or not commit_sha:
        return {"action": "skipped", "reason": "incomplete PR payload"}

    return _match_and_queue(
        owner=owner,
        repo=repo,
        rule_type="pr",
        ref_name=branch,
        commit_sha=commit_sha,
        git_ref=branch,
        pr_number=pr_number,
    )


def _match_and_queue(owner, repo, rule_type, ref_name, commit_sha, git_ref,
                     pr_number=None):
    """
    Look up matching AutoTestRules for this repo and event, expand matrices,
    and queue test runs.
    """
    session = SessionLocal()
    try:
        # Find the project
        project = session.execute(
            select(Project).where(
                Project.github_owner == owner,
                Project.github_repo == repo,
            )
        ).scalar_one_or_none()

        if project is None:
            _logger.info("No project found for %s/%s", owner, repo)
            return {"action": "skipped", "reason": f"no project for {owner}/{repo}"}

        # Find matching rules
        rules = session.execute(
            select(AutoTestRule).where(
                AutoTestRule.project_id == project.id,
                AutoTestRule.rule_type == rule_type,
                AutoTestRule.enabled == 1,
            )
        ).scalars().all()

        matched_rules = []
        for rule in rules:
            if fnmatch.fnmatch(ref_name, rule.pattern):
                matched_rules.append(rule)

        if not matched_rules:
            _logger.info("No rules matched %s/%s %s '%s'", owner, repo, rule_type, ref_name)
            return {"action": "no_match", "project": project.name, "ref": ref_name}

        # Queue jobs for each matched rule
        github_client = GitHubClient()
        total_queued = 0
        run_ids = []

        for rule in matched_rules:
            if rule.matrix_id:
                matrix = session.execute(
                    select(TestMatrix).where(TestMatrix.id == rule.matrix_id)
                ).scalar_one_or_none()
                if matrix is None:
                    _logger.warning("Rule %d references missing matrix %d", rule.id, rule.matrix_id)
                    continue

                jobs = expand_matrix(matrix.project, matrix.config)
            else:
                # No matrix linked — create a single smoke test job
                jobs = [{"project": project.name, "test_type": "smoke"}]

            opp_file = matrix.opp_file if matrix else None
            for job in jobs:
                job_ref = job.get("git_ref") or git_ref
                # Use the job's own ref as the GitHub status SHA when it
                # looks like a commit hash (from ref-range expansion).
                # Otherwise fall back to the push HEAD.
                job_sha = job_ref if job_ref and len(job_ref) >= 40 else commit_sha
                run = TestRun(
                    project=job.get("project", project.name),
                    test_type=job.get("test_type", "smoke"),
                    mode=job.get("mode"),
                    git_ref=job_ref,
                    opp_file=opp_file,
                    os=job.get("os"),
                    os_version=job.get("os_version"),
                    compiler=job.get("compiler"),
                    compiler_version=job.get("compiler_version"),
                    platform_desc=job.get("platform_desc"),
                    resolved_deps=job.get("resolved_deps"),
                    matrix_id=rule.matrix_id,
                    status=TestRunStatus.queued,
                    trigger="webhook",
                    github_owner=owner,
                    github_repo=repo,
                    github_commit_sha=job_sha,
                    github_pr_number=pr_number,
                )
                session.add(run)
                session.flush()
                run_ids.append(run.id)
                total_queued += 1

        session.commit()

        # Post pending status on GitHub for each queued run
        if github_client.is_configured and commit_sha:
            for run_id in run_ids:
                try:
                    target_url = f"{COORDINATOR_URL}/runs/{run_id}"
                    github_client.set_status_pending(owner, repo, commit_sha, run_id, target_url)
                except Exception as e:
                    _logger.warning("Failed to post pending status for run #%d: %s", run_id, e)

        _logger.info(
            "Webhook %s/%s %s '%s': matched %d rules, queued %d jobs",
            owner, repo, rule_type, ref_name, len(matched_rules), total_queued,
        )
        return {
            "action": "queued",
            "project": project.name,
            "ref": ref_name,
            "commit_sha": commit_sha[:8] if commit_sha else None,
            "pr_number": pr_number,
            "rules_matched": len(matched_rules),
            "jobs_queued": total_queued,
            "run_ids": run_ids,
        }
    finally:
        session.close()


def format_results_comment(runs):
    """
    Format a Markdown PR comment body summarizing a set of test runs.

    Args:
        runs: list of TestRun objects (with results loaded)

    Returns:
        Markdown string
    """
    if not runs:
        return "**opp_ci**: No test results yet."

    total = len(runs)
    passed = sum(1 for r in runs if r.status.value == "passed")
    failed = sum(1 for r in runs if r.status.value == "failed")
    errors = sum(1 for r in runs if r.status.value == "error")
    running = sum(1 for r in runs if r.status.value in ("running", "queued"))

    if failed == 0 and errors == 0 and running == 0:
        header = f"✅ **opp_ci**: All {total} tests passed"
    elif running > 0:
        header = f"⏳ **opp_ci**: {passed}/{total} passed, {running} still running"
    else:
        header = f"❌ **opp_ci**: {passed}/{total} passed, {failed} failed, {errors} errors"

    lines = [header, "", "| Test | Project | Status | Duration |", "|---|---|---|---|"]
    for run in runs:
        status_emoji = {
            "passed": "✅", "failed": "❌", "error": "⚠️",
            "running": "🔄", "queued": "⏳",
        }.get(run.status.value, "❓")
        dur = f"{run.duration_seconds:.1f}s" if run.duration_seconds else "-"
        lines.append(f"| {run.test_type} | {run.project} | {status_emoji} {run.status.value} | {dur} |")

    return "\n".join(lines)
