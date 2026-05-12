"""
Git notes support for opp_ci (Stage 9).

Provides:
- Note formatting: compact one-line summaries per commit from TestRun data.
- API helpers: query pending notes for a repo (used by the /api/notes endpoint).
- Sync trigger: dispatch the ci-notes.yml workflow on a target repo after runs complete.
- Local note writing: update_ci_note() for direct git notes in local execution mode.
"""

import logging
import os
import subprocess
import tempfile
from collections import defaultdict

from sqlalchemy import select

from opp_ci.config import COORDINATOR_URL
from opp_ci.db.models import TestRun, TestRunStatus

_logger = logging.getLogger(__name__)

_STATUS_ICONS = {
    TestRunStatus.passed: "\u2705",
    TestRunStatus.failed: "\u274c",
    TestRunStatus.error: "\u26a0\ufe0f",
    TestRunStatus.running: "\u23f3",
    TestRunStatus.queued: "\u23f3",
}


def format_note_line(runs, run_url_base=None):
    """
    Format a list of TestRun objects (all for the same commit) into a compact
    one-line note string.

    Example output:
        ✅ smoke PASS | fingerprint 46/48 PASS, 2 FAIL | https://ci.omnetpp.org/runs/42
    """
    if not runs:
        return ""

    if run_url_base is None:
        run_url_base = COORDINATOR_URL

    # Group runs by test_type
    by_type = defaultdict(list)
    for run in runs:
        by_type[run.test_type].append(run)

    parts = []
    for test_type, type_runs in sorted(by_type.items()):
        passed = sum(1 for r in type_runs if r.status == TestRunStatus.passed)
        failed = sum(1 for r in type_runs if r.status == TestRunStatus.failed)
        errored = sum(1 for r in type_runs if r.status == TestRunStatus.error)
        pending = sum(1 for r in type_runs if r.status in (TestRunStatus.queued, TestRunStatus.running))
        total = len(type_runs)

        if total == 1:
            run = type_runs[0]
            icon = _STATUS_ICONS.get(run.status, "?")
            label = run.status.value.upper()
            if run.mode:
                parts.append(f"{icon} {test_type}/{run.mode} {label}")
            else:
                parts.append(f"{icon} {test_type} {label}")
        else:
            # Summarize counts
            if passed == total:
                parts.append(f"\u2705 {test_type} {total}/{total} PASS")
            elif failed + errored == total:
                parts.append(f"\u274c {test_type} 0/{total} PASS")
            else:
                segments = []
                if passed:
                    segments.append(f"{passed} PASS")
                if failed:
                    segments.append(f"{failed} FAIL")
                if errored:
                    segments.append(f"{errored} ERROR")
                if pending:
                    segments.append(f"{pending} pending")
                icon = "\u2705" if (failed + errored == 0 and pending == 0) else "\u274c"
                parts.append(f"{icon} {test_type} {', '.join(segments)}")

    # Append URL to the first run (as entry point)
    first_run = runs[0]
    url = f"{run_url_base}/runs/{first_run.id}"
    parts.append(url)

    return " | ".join(parts)


def get_notes_for_repo(session, owner, repo):
    """
    Return all pending notes for a given GitHub repo.

    Returns a list of dicts: [{"sha": <commit_sha>, "note": <formatted_line>}]
    Only includes commits that have at least one finished run (passed/failed/error).
    """
    finished = (TestRunStatus.passed, TestRunStatus.failed, TestRunStatus.error)

    runs = session.execute(
        select(TestRun).where(
            TestRun.github_owner == owner,
            TestRun.github_repo == repo,
            TestRun.github_commit_sha.isnot(None),
            TestRun.status.in_(finished),
        ).order_by(TestRun.id)
    ).scalars().all()

    if not runs:
        return []

    # Group by commit SHA
    by_sha = defaultdict(list)
    for run in runs:
        by_sha[run.github_commit_sha].append(run)

    results = []
    for sha, sha_runs in by_sha.items():
        note = format_note_line(sha_runs)
        if note:
            results.append({"sha": sha, "note": note})

    return results


def trigger_notes_sync(owner, repo):
    """
    Trigger the ci-notes.yml workflow on the target repo via workflow_dispatch.

    Uses the Actions:Write PAT (OPP_CI_GITHUB_ACTIONS_TOKEN) to dispatch
    the workflow. The workflow itself fetches notes from our API and pushes
    them as git notes.
    """
    from opp_ci.github.client import GitHubClient
    from opp_ci.config import get_github_actions_token

    token = get_github_actions_token()
    if not token:
        _logger.debug("OPP_CI_GITHUB_ACTIONS_TOKEN not configured, skipping notes sync")
        return False

    client = GitHubClient(token=token)
    try:
        success = client.trigger_workflow_dispatch(
            owner, repo,
            workflow_id="ci-notes.yml",
            ref="main",
        )
        if success:
            _logger.info("Triggered ci-notes.yml on %s/%s", owner, repo)
        return success
    except Exception as e:
        _logger.warning("Failed to trigger notes sync on %s/%s: %s", owner, repo, e)
        return False


# ── Local git notes (for CLI / direct execution) ──────────────────────


def _project_dir(project, opp_file=None):
    if opp_file:
        return os.path.dirname(os.path.abspath(opp_file))
    env_key = f"OPP_CI_PROJECT_DIR_{project.upper().replace('-', '_')}"
    project_dir = os.environ.get(env_key)
    if not project_dir:
        base_dir = os.environ.get("OPP_CI_PROJECT_DIR", ".")
        project_dir = os.path.join(base_dir, project)
    return project_dir


def update_ci_note(project, commit_sha, session, opp_file=None):
    """
    Write a git note (refs/notes/ci) directly into the local git repo.

    Used in local/CLI execution mode. Queries all TestRun records matching
    the commit, formats a compact one-line summary, and attaches it as a note.
    """
    if not commit_sha:
        return

    runs = session.execute(
        select(TestRun).where(TestRun.commit_sha == commit_sha)
    ).scalars().all()

    if not runs:
        return

    note_content = format_note_line(runs)
    project_dir = _project_dir(project, opp_file)
    tmp_path = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="w", prefix="opp_ci_note_", suffix=".txt", delete=False
        ) as f:
            f.write(note_content + "\n")
            tmp_path = f.name

        result = subprocess.run(
            ["git", "notes", "--ref=refs/notes/ci", "add", "-f", "-F", tmp_path, commit_sha],
            cwd=project_dir, capture_output=True, text=True,
        )
        if result.returncode != 0:
            _logger.warning("git notes failed: %s", result.stderr.strip())
        else:
            _logger.info("Updated ci note for %s", commit_sha[:8])
    except (OSError, FileNotFoundError) as e:
        _logger.warning("Could not write git note: %s", e)
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
