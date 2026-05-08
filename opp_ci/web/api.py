"""
REST API for opp_ci Stage 5 — remote workers and job submission.

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
"""

import datetime
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from opp_ci.auth import require_role, require_worker_token
from opp_ci.db.connection import SessionLocal
from opp_ci.db.models import (
    Base, Worker, ApiToken, TestRun, TestRunStatus, TestResult, TestMatrix,
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
    }
