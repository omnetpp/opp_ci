import json
import logging
import os
import subprocess
import tempfile

from sqlalchemy import select

from opp_ci.db.models import TestRun

_logger = logging.getLogger(__name__)


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
    Write a git note (refs/notes/ci) summarizing all test results for a commit.

    Queries all TestRun records matching the given commit_sha, formats them as
    JSON, and attaches the note to the commit in the project's git repo.
    """
    if not commit_sha:
        return

    runs = session.execute(
        select(TestRun).where(TestRun.commit_sha == commit_sha)
    ).scalars().all()

    results = []
    for run in runs:
        entry = {"test": run.test_type, "status": run.status.value}
        if run.mode:
            entry["mode"] = run.mode
        results.append(entry)

    note_content = json.dumps({"results": results}, indent=2)

    project_dir = _project_dir(project, opp_file)
    tmp_path = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="w", prefix="opp_ci_note_", suffix=".json", delete=False
        ) as f:
            f.write(note_content)
            tmp_path = f.name

        result = subprocess.run(
            ["git", "notes", "--ref=refs/notes/ci", "add", "-f", "-F", tmp_path, commit_sha],
            cwd=project_dir, capture_output=True, text=True,
        )
        if result.returncode != 0:
            _logger.warning("git notes failed: %s", result.stderr.strip())
        else:
            _logger.info("Updated ci note for %s (%d results)", commit_sha[:8], len(results))
    except (OSError, FileNotFoundError) as e:
        _logger.warning("Could not write git note: %s", e)
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
