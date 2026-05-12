"""
REST API for opp_ci — remote workers, job submission, and GitHub integration.

Endpoints:
    POST /api/runs              — submit a test run (submitter+)
    POST /api/runs/matrix       — submit a matrix run (submitter+)
    GET  /api/runs              — list runs (readonly+)
    GET  /api/runs/{run_id}     — get run detail (readonly+)

    POST /api/workers/register  — register a new worker (admin)
    POST /api/workers/heartbeat — worker heartbeat (worker)
    POST /api/workers/poll      — worker polls for a job (worker)
    POST /api/workers/result    — worker reports job result (worker)
    GET  /api/workers           — list workers (readonly+)

    POST /api/tokens            — create an API token (admin)
    GET  /api/tokens            — list tokens (admin)

    POST /api/github/webhook    — GitHub webhook receiver (push, pull_request)
    GET  /api/github/rules      — list auto-test rules (readonly+)
    POST /api/github/rules      — create auto-test rule (admin)

    GET  /api/notes/{owner}/{repo} — git notes per commit (readonly+)
"""

import datetime
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from opp_ci.auth import require_role, require_worker_token
from opp_ci.db.connection import SessionLocal
from opp_ci.db.models import (
    Base, Worker, ApiToken, TestRun, TestRunStatus, TestResult, TestMatrix,
    Project, AutoTestRule,
)

_logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")


# ── Pydantic request/response schemas ──────────────────────────────────

class SubmitRunRequest(BaseModel):
    project: str
    test_type: str
    mode: str | None = None
    git_ref: str | None = None
    os: str | None = None
    os_version: str | None = None
    compiler: str | None = None
    compiler_version: str | None = None

class SubmitMatrixRequest(BaseModel):
    matrix_name: str

class WorkerRegisterRequest(BaseModel):
    name: str
    tags: list[str] = Field(default_factory=list)
    concurrency: int = 1

class WorkerResultRequest(BaseModel):
    run_id: int
    result_code: str
    duration_seconds: float | None = None
    stdout: str | None = None
    stderr: str | None = None
    details: dict | None = None

class CreateTokenRequest(BaseModel):
    name: str
    role: str = "readonly"

class RunResponse(BaseModel):
    id: int
    project: str
    test_type: str
    mode: str | None
    git_ref: str | None
    status: str
    trigger: str | None
    started_at: str | None
    finished_at: str | None
    duration_seconds: float | None
    worker_id: int | None

    class Config:
        from_attributes = True

class WorkerResponse(BaseModel):
    id: int
    name: str
    tags: list[str]
    concurrency: int
    status: str
    last_heartbeat: str | None
    current_job_count: int

    class Config:
        from_attributes = True


# ── Run submission ─────────────────────────────────────────────────────

@router.post("/runs")
async def submit_run(
    req: SubmitRunRequest,
    identity: dict = Depends(require_role("submitter")),
):
    """Submit a single test run to the queue."""
    session = SessionLocal()
    try:
        from opp_ci.scheduler import _build_platform_desc
        platform_desc = _build_platform_desc(req.os, req.os_version, req.compiler, req.compiler_version)

        run = TestRun(
            project=req.project,
            test_type=req.test_type,
            mode=req.mode,
            git_ref=req.git_ref,
            os=req.os,
            os_version=req.os_version,
            compiler=req.compiler,
            compiler_version=req.compiler_version,
            platform_desc=platform_desc,
            status=TestRunStatus.queued,
            trigger="remote",
        )
        session.add(run)
        session.commit()
        _logger.info("Run #%d submitted by %s", run.id, identity.get("name"))
        return {"id": run.id, "status": "queued"}
    finally:
        session.close()


@router.post("/runs/matrix")
async def submit_matrix_run(
    req: SubmitMatrixRequest,
    identity: dict = Depends(require_role("submitter")),
):
    """Expand a matrix and queue all jobs."""
    from opp_ci.scheduler import expand_matrix

    session = SessionLocal()
    try:
        matrix = session.execute(
            select(TestMatrix).where(TestMatrix.name == req.matrix_name)
        ).scalar_one_or_none()
        if matrix is None:
            raise HTTPException(status_code=404, detail=f"Matrix '{req.matrix_name}' not found")

        jobs = expand_matrix(matrix.project, matrix.config)
        run_ids = []
        for job in jobs:
            run = TestRun(
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
                status=TestRunStatus.queued,
                trigger="remote",
            )
            session.add(run)
            session.flush()
            run_ids.append(run.id)
        session.commit()
        _logger.info("Matrix '%s' queued %d jobs by %s", req.matrix_name, len(run_ids), identity.get("name"))
        return {"matrix": req.matrix_name, "jobs_queued": len(run_ids), "run_ids": run_ids}
    finally:
        session.close()


@router.get("/runs")
async def list_runs(
    project: str | None = None,
    test_type: str | None = None,
    status: str | None = None,
    limit: int = 50,
    _identity: dict = Depends(require_role("readonly")),
):
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
        return [_run_to_dict(r) for r in runs]
    finally:
        session.close()


@router.get("/runs/{run_id}")
async def get_run(
    run_id: int,
    _identity: dict = Depends(require_role("readonly")),
):
    """Get details of a specific test run including results."""
    session = SessionLocal()
    try:
        run = session.execute(
            select(TestRun).where(TestRun.id == run_id)
        ).scalar_one_or_none()
        if run is None:
            raise HTTPException(status_code=404, detail=f"Run #{run_id} not found")

        results = session.execute(
            select(TestResult).where(TestResult.test_run_id == run_id)
        ).scalars().all()

        d = _run_to_dict(run)
        d["results"] = [
            {
                "id": r.id,
                "result_code": r.result_code,
                "stdout": r.stdout,
                "stderr": r.stderr,
                "details": r.details,
            }
            for r in results
        ]
        return d
    finally:
        session.close()


# ── Worker endpoints ───────────────────────────────────────────────────

@router.post("/workers/register")
async def register_worker(
    req: WorkerRegisterRequest,
    _identity: dict = Depends(require_role("admin")),
):
    """Register a new worker (admin only). Returns the worker token."""
    session = SessionLocal()
    try:
        existing = session.execute(
            select(Worker).where(Worker.name == req.name)
        ).scalar_one_or_none()
        if existing is not None:
            raise HTTPException(status_code=409, detail=f"Worker '{req.name}' already exists")

        worker = Worker(
            name=req.name,
            tags=req.tags,
            concurrency=req.concurrency,
            status="offline",
        )
        session.add(worker)
        session.commit()
        _logger.info("Worker '%s' registered (id=%d)", worker.name, worker.id)
        return {"id": worker.id, "name": worker.name, "token": worker.token}
    finally:
        session.close()


@router.post("/workers/heartbeat")
async def worker_heartbeat(
    worker_info: dict = Depends(require_worker_token()),
):
    """Worker heartbeat — keeps the worker marked as online."""
    return {"status": "ok", "worker_id": worker_info["worker_id"]}


@router.post("/workers/poll")
async def worker_poll(
    worker_info: dict = Depends(require_worker_token()),
):
    """
    Worker polls for the next available job.

    Assigns a queued TestRun to this worker, sets status to 'running',
    and returns the job spec. Returns null job if nothing is queued.
    """
    session = SessionLocal()
    try:
        worker = session.execute(
            select(Worker).where(Worker.id == worker_info["worker_id"])
        ).scalar_one_or_none()
        if worker is None:
            raise HTTPException(status_code=401, detail="Worker not found")

        if not worker.is_available:
            return {"job": None, "reason": "worker at capacity"}

        # Find the oldest queued run
        # TODO: match worker tags to job requirements
        run = session.execute(
            select(TestRun)
            .where(TestRun.status == TestRunStatus.queued)
            .order_by(TestRun.id)
            .limit(1)
        ).scalar_one_or_none()

        if run is None:
            return {"job": None, "reason": "no queued jobs"}

        # Assign to this worker
        run.status = TestRunStatus.running
        run.worker_id = worker.id
        run.started_at = datetime.datetime.utcnow()
        worker.current_job_count += 1
        if worker.current_job_count >= worker.concurrency:
            worker.status = "busy"
        session.commit()

        _logger.info("Assigned run #%d to worker '%s'", run.id, worker.name)
        return {
            "job": {
                "run_id": run.id,
                "project": run.project,
                "test_type": run.test_type,
                "mode": run.mode,
                "git_ref": run.git_ref,
                "os": run.os,
                "os_version": run.os_version,
                "compiler": run.compiler,
                "compiler_version": run.compiler_version,
            }
        }
    finally:
        session.close()


@router.post("/workers/result")
async def worker_report_result(
    req: WorkerResultRequest,
    worker_info: dict = Depends(require_worker_token()),
):
    """Worker reports the result of a completed job."""
    session = SessionLocal()
    try:
        run = session.execute(
            select(TestRun).where(TestRun.id == req.run_id)
        ).scalar_one_or_none()
        if run is None:
            raise HTTPException(status_code=404, detail=f"Run #{req.run_id} not found")
        if run.worker_id != worker_info["worker_id"]:
            raise HTTPException(status_code=403, detail="Run not assigned to this worker")

        run.status = TestRunStatus.passed if req.result_code == "PASS" else TestRunStatus.failed
        run.finished_at = datetime.datetime.utcnow()
        run.duration_seconds = req.duration_seconds

        session.add(TestResult(
            test_run_id=run.id,
            result_code=req.result_code,
            stdout=req.stdout,
            stderr=req.stderr,
            details=req.details,
        ))

        # Update worker job count
        worker = session.execute(
            select(Worker).where(Worker.id == worker_info["worker_id"])
        ).scalar_one_or_none()
        if worker:
            worker.current_job_count = max(0, worker.current_job_count - 1)
            if worker.status == "busy" and worker.current_job_count < worker.concurrency:
                worker.status = "online"

        session.commit()
        _logger.info("Run #%d result: %s (worker '%s')", run.id, req.result_code, worker_info["worker_name"])

        # Post GitHub commit status if this was a webhook-triggered run
        try:
            from opp_ci.github.status import update_github_status
            update_github_status(run.id)
        except Exception as e:
            _logger.warning("GitHub status update failed for run #%d: %s", run.id, e)

        # Trigger git notes sync if this run has GitHub metadata
        if run.github_owner and run.github_repo:
            try:
                from opp_ci.notes import trigger_notes_sync
                trigger_notes_sync(run.github_owner, run.github_repo)
            except Exception as e:
                _logger.warning("Notes sync trigger failed for run #%d: %s", run.id, e)

        return {"status": "ok", "run_id": run.id, "result_code": req.result_code}
    finally:
        session.close()


@router.get("/workers")
async def list_workers(
    _identity: dict = Depends(require_role("readonly")),
):
    """List all registered workers."""
    session = SessionLocal()
    try:
        workers = session.execute(
            select(Worker).order_by(Worker.name)
        ).scalars().all()
        return [
            {
                "id": w.id,
                "name": w.name,
                "tags": w.tags or [],
                "concurrency": w.concurrency,
                "status": w.status,
                "last_heartbeat": w.last_heartbeat.isoformat() if w.last_heartbeat else None,
                "current_job_count": w.current_job_count,
                "registered_at": w.registered_at.isoformat() if w.registered_at else None,
            }
            for w in workers
        ]
    finally:
        session.close()


# ── Token management ───────────────────────────────────────────────────

@router.post("/tokens")
async def create_token(
    req: CreateTokenRequest,
    _identity: dict = Depends(require_role("admin")),
):
    """Create a new API token (admin only)."""
    if req.role not in ("readonly", "submitter", "worker", "admin"):
        raise HTTPException(status_code=400, detail=f"Invalid role: {req.role}")

    session = SessionLocal()
    try:
        token = ApiToken(name=req.name, role=req.role)
        session.add(token)
        session.commit()
        _logger.info("API token '%s' created (role=%s)", token.name, token.role)
        return {"id": token.id, "name": token.name, "role": token.role, "token": token.token}
    finally:
        session.close()


@router.get("/tokens")
async def list_tokens(
    _identity: dict = Depends(require_role("admin")),
):
    """List API tokens (admin only). Token values are masked."""
    session = SessionLocal()
    try:
        tokens = session.execute(
            select(ApiToken).order_by(ApiToken.id)
        ).scalars().all()
        return [
            {
                "id": t.id,
                "name": t.name,
                "role": t.role,
                "enabled": t.enabled,
                "created_at": t.created_at.isoformat() if t.created_at else None,
                "token_prefix": t.token[:8] + "..." if t.token else None,
            }
            for t in tokens
        ]
    finally:
        session.close()


# ── GitHub integration ─────────────────────────────────────────────────

class CreateRuleRequest(BaseModel):
    project_name: str
    rule_type: str  # branch, pr, tag
    pattern: str    # glob, e.g. "master", "topic/*", "*"
    matrix_name: str | None = None
    enabled: bool = True


@router.post("/github/webhook")
async def github_webhook(request: Request):
    """Receive GitHub webhook events (push, pull_request, ping)."""
    from opp_ci.github.webhook import verify_signature, handle_webhook_event

    body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256", "")

    if not verify_signature(body, signature):
        raise HTTPException(status_code=403, detail="Invalid webhook signature")

    event_type = request.headers.get("X-GitHub-Event", "")
    if not event_type:
        raise HTTPException(status_code=400, detail="Missing X-GitHub-Event header")

    import json
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    result = handle_webhook_event(event_type, payload)
    return result


@router.get("/github/rules")
async def list_rules(
    _identity: dict = Depends(require_role("readonly")),
):
    """List auto-test rules."""
    session = SessionLocal()
    try:
        rules = session.execute(
            select(AutoTestRule).order_by(AutoTestRule.id)
        ).scalars().all()
        return [
            {
                "id": r.id,
                "project_id": r.project_id,
                "project_name": r.project_rel.name if r.project_rel else None,
                "rule_type": r.rule_type,
                "pattern": r.pattern,
                "matrix_id": r.matrix_id,
                "matrix_name": r.matrix_rel.name if r.matrix_rel else None,
                "enabled": bool(r.enabled),
            }
            for r in rules
        ]
    finally:
        session.close()


@router.post("/github/rules")
async def create_rule(
    req: CreateRuleRequest,
    _identity: dict = Depends(require_role("admin")),
):
    """Create an auto-test rule (admin only)."""
    if req.rule_type not in ("branch", "pr", "tag"):
        raise HTTPException(status_code=400, detail=f"Invalid rule_type: {req.rule_type}")

    session = SessionLocal()
    try:
        project = session.execute(
            select(Project).where(Project.name == req.project_name)
        ).scalar_one_or_none()
        if project is None:
            raise HTTPException(status_code=404, detail=f"Project '{req.project_name}' not found")

        matrix_id = None
        if req.matrix_name:
            matrix = session.execute(
                select(TestMatrix).where(TestMatrix.name == req.matrix_name)
            ).scalar_one_or_none()
            if matrix is None:
                raise HTTPException(status_code=404, detail=f"Matrix '{req.matrix_name}' not found")
            matrix_id = matrix.id

        rule = AutoTestRule(
            project_id=project.id,
            rule_type=req.rule_type,
            pattern=req.pattern,
            matrix_id=matrix_id,
            enabled=1 if req.enabled else 0,
        )
        session.add(rule)
        session.commit()
        _logger.info("Auto-test rule created: %s %s '%s' -> matrix %s",
                     req.project_name, req.rule_type, req.pattern, req.matrix_name)
        return {
            "id": rule.id,
            "project_name": req.project_name,
            "rule_type": rule.rule_type,
            "pattern": rule.pattern,
            "matrix_name": req.matrix_name,
            "enabled": bool(rule.enabled),
        }
    finally:
        session.close()


@router.delete("/github/rules/{rule_id}")
async def delete_rule(
    rule_id: int,
    _identity: dict = Depends(require_role("admin")),
):
    """Delete an auto-test rule (admin only)."""
    session = SessionLocal()
    try:
        rule = session.execute(
            select(AutoTestRule).where(AutoTestRule.id == rule_id)
        ).scalar_one_or_none()
        if rule is None:
            raise HTTPException(status_code=404, detail=f"Rule #{rule_id} not found")
        session.delete(rule)
        session.commit()
        return {"status": "deleted", "id": rule_id}
    finally:
        session.close()


# ── Git notes ──────────────────────────────────────────────────────────

@router.get("/notes/{owner}/{repo}")
async def get_notes(
    owner: str,
    repo: str,
    _identity: dict = Depends(require_role("readonly")),
):
    """
    Return formatted CI note lines for all tested commits in a repo.

    Used by the ci-notes.yml GitHub Action to write git notes.
    Response: [{"sha": "<commit>", "note": "<one-line summary>"}]
    """
    from opp_ci.notes import get_notes_for_repo

    session = SessionLocal()
    try:
        return get_notes_for_repo(session, owner, repo)
    finally:
        session.close()


# ── Helpers ────────────────────────────────────────────────────────────

def _run_to_dict(run):
    return {
        "id": run.id,
        "project": run.project,
        "test_type": run.test_type,
        "mode": run.mode,
        "git_ref": run.git_ref,
        "os": run.os,
        "os_version": run.os_version,
        "compiler": run.compiler,
        "compiler_version": run.compiler_version,
        "platform_desc": run.platform_desc,
        "status": run.status.value,
        "trigger": run.trigger,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "duration_seconds": run.duration_seconds,
        "worker_id": run.worker_id,
        "matrix_id": run.matrix_id,
        "github_owner": run.github_owner,
        "github_repo": run.github_repo,
        "github_commit_sha": run.github_commit_sha,
        "github_pr_number": run.github_pr_number,
    }
