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
from opp_ci.db.models import AutoTestRule, Project, TestMatrix
from opp_ci.github.client import GitHubClient
from opp_ci.persistence import (
    create_matrix_run, enqueue_job, resolve_matrix_recipe)
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
    and queue test runs. Tag pushes set TestMatrixRun.trigger="tag" and
    TestMatrixRun.ref to the tag name; branch / PR events keep the existing
    "webhook" trigger and leave ref empty.
    """
    matrix_trigger = "tag" if rule_type == "tag" else "webhook"
    matrix_ref = ref_name if rule_type == "tag" else None
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
            matrix = None
            if rule.matrix_id:
                matrix = session.execute(
                    select(TestMatrix).where(TestMatrix.id == rule.matrix_id)
                ).scalar_one_or_none()
                if matrix is None:
                    _logger.warning("Rule %d references missing matrix %d",
                                    rule.id, rule.matrix_id)
                    continue
                # Branch-tracking: a recipe matrix auto-resolves on the event —
                # loose coordinate axes pinned against the fleet, the source
                # pinned to the pushed commit — minting a fresh snapshot each
                # push (the recipe is preserved as its lineage). The resolved
                # snapshot is what actually runs.
                if not matrix.is_resolved:
                    try:
                        matrix = resolve_matrix_recipe(
                            session, matrix, commit_sha=commit_sha)
                    except ValueError as e:
                        _logger.warning(
                            "Could not resolve recipe matrix %d for %s/%s %s: %s",
                            rule.matrix_id, owner, repo, ref_name, e)
                        continue
                jobs = expand_matrix(matrix.project, matrix.config)
                matrix_id = matrix.id
                opp_file = matrix.opp_file
            else:
                # No matrix linked — synthesize a single smoke-test job
                # against the project. The webhook still needs a
                # TestMatrixRun to group it; we treat the absence of a
                # matrix as "synthetic", so skip the matrix_run grouping
                # by leaving matrix_run_id NULL for these.
                jobs = [{"project": project.name, "kind": "smoke"}]
                matrix_id = None
                opp_file = None

            matrix_run = None
            if matrix_id is not None:
                matrix_run = create_matrix_run(
                    session,
                    matrix_id=matrix_id,
                    trigger=matrix_trigger,
                    ref=matrix_ref,
                    github_owner=owner,
                    github_repo=repo,
                    github_commit_sha=commit_sha,
                    github_pr_number=pr_number,
                )

            from opp_ci.fingerprint import compute_cache_fingerprint

            for job in jobs:
                job_ref = job.get("git_ref") or git_ref
                job_with_ref = dict(job)
                job_with_ref["git_ref"] = job_ref
                fp = compute_cache_fingerprint(
                    job_with_ref,
                    project=job.get("project", project.name),
                    opp_file=opp_file,
                )
                run, _ = enqueue_job(
                    session, job_with_ref,
                    project=job.get("project", project.name),
                    opp_file=opp_file,
                    matrix_run_id=matrix_run.id if matrix_run else None,
                    use_cache=True,
                    cache_fingerprint=fp,
                )
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


