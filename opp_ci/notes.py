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
from opp_ci.db.models import TestMatrixRun, TestResultCode, TestRun, TestRunLifecycle

_logger = logging.getLogger(__name__)

_STATUS_ICONS = {
    "PASS": "\u2705",
    "FAIL": "\u274c",
    "ERROR": "\u26a0\ufe0f",
    "SKIPPED": "\u2796",
    "running": "\u23f3",
    "queued": "\u23f3",
    "cancelled": "\u23f9",
    "timed_out": "\u23f1",
}


def format_note(runs, run_url_base=None):
    """
    Format a list of TestRun objects (all for the same commit) into a
    multiline note string.

    Example output:
        ✅ build PASS | ✅ smoke PASS
          ✅ build/release  PASS  1.2s  #5
          ✅ smoke/release  PASS  4.3s  #6
        http://85.17.192.192:8080/commits/mm1k/44fad47c
    """
    if not runs:
        return ""

    if run_url_base is None:
        run_url_base = COORDINATOR_URL

    # ── Summary line ────────────────────────────────────────────────
    total_runs = len(runs)
    total_passed = sum(1 for r in runs if r.result_code == TestResultCode.PASS)
    total_failed = sum(1 for r in runs if r.result_code == TestResultCode.FAIL)
    total_errored = sum(1 for r in runs if r.result_code == TestResultCode.ERROR)

    if total_passed == total_runs:
        summary = f"\u2705 PASS {total_passed}/{total_runs}"
    elif total_failed + total_errored == total_runs:
        summary = f"\u274c FAIL 0/{total_runs}"
    else:
        icon = "\u274c"
        segments = []
        if total_passed:
            segments.append(f"{total_passed} PASS")
        if total_failed:
            segments.append(f"{total_failed} FAIL")
        if total_errored:
            segments.append(f"{total_errored} ERROR")
        summary = f"{icon} {', '.join(segments)} ({total_runs} total)"

    # ── Per-run detail lines ──────────────────────────────────────────
    detail_lines = []
    for run in sorted(runs, key=lambda r: r.id):
        status = run.effective_status
        icon = _STATUS_ICONS.get(status, "?")
        label = run.kind
        if run.mode:
            label += f"/{run.mode}"
        duration = f"{run.duration_seconds:.1f}s" if run.duration_seconds else "-"
        detail_lines.append(f"  {icon} {label}  {status}  {duration}  #{run.id}")

    # ── URL to commit page ────────────────────────────────────────────
    first_run = runs[0]
    sha = first_run.commit_sha or first_run.github_commit_sha or ""
    url = f"{run_url_base}/commits/{first_run.project}/{sha}"
    _ = first_run  # silence unused-warning on linters when sha is empty

    return "\n".join([summary] + detail_lines + ["", url])


def get_notes_for_repo(session, owner, repo):
    """
    Return all pending notes for a given GitHub repo.

    Returns a list of dicts: [{"sha": <commit_sha>, "note": <formatted_line>}]
    Only includes commits that have at least one finished run.

    GitHub identity (owner/repo/commit_sha) lives on TestMatrixRun, so we
    join through it.
    """
    runs = session.execute(
        select(TestRun)
        .join(TestMatrixRun, TestRun.matrix_run_id == TestMatrixRun.id)
        .where(
            TestMatrixRun.github_owner == owner,
            TestMatrixRun.github_repo == repo,
            TestMatrixRun.github_commit_sha.isnot(None),
            TestRun.lifecycle == TestRunLifecycle.finished,
        ).order_by(TestRun.id)
    ).scalars().all()

    if not runs:
        return []

    by_sha = defaultdict(list)
    for run in runs:
        by_sha[run.github_commit_sha].append(run)

    results = []
    for sha, sha_runs in by_sha.items():
        note = format_note(sha_runs)
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
        # Look up the repo's default branch
        repo_url = f"{client.base_url}/repos/{owner}/{repo}"
        resp = client._session.get(repo_url, timeout=15)
        ref = resp.json().get("default_branch", "main") if resp.status_code == 200 else "main"

        success = client.trigger_workflow_dispatch(
            owner, repo,
            workflow_id="ci-notes.yml",
            ref=ref,
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

    note_content = format_note(runs)
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
