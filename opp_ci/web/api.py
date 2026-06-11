"""
REST API for opp_ci — remote workers, job submission, and GitHub integration.

Endpoints:
    POST /api/runs              — submit a single test run (submitter+)
    POST /api/runs/matrix       — submit a matrix run (submitter+)
    GET  /api/runs              — list runs (readonly+)
    GET  /api/runs/{run_id}     — get run detail (readonly+)

    POST /api/workers/register  — register a new worker (admin)
    GET  /api/workers/me        — worker fetches its own registered config (worker)
    POST /api/workers/heartbeat — worker heartbeat (worker)
    POST /api/workers/poll      — worker polls for a job (worker)
    POST /api/workers/result    — worker reports job result (worker)
    GET  /api/workers           — list workers (readonly+)

    POST /api/tokens            — create an API token (admin)
    GET  /api/tokens            — list tokens (admin)

    POST /api/github/webhook    — GitHub webhook receiver (push, pull_request)
    GET  /api/github/rules      — list auto-test rules (readonly+)
    POST /api/github/rules      — create auto-test rule (admin)

    GET  /api/notes/{owner}/{repo}     — git notes per commit (readonly+)
    POST /api/notes/{owner}/{repo}/ack — acknowledge synced commit SHAs (readonly+)
"""

import datetime
import logging

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from opp_ci.auth import require_role, require_worker_token
from opp_ci.db.connection import SessionLocal
from opp_ci.db.models import (
    ApiToken, AutoTestRule, ExpectedTestResult, Project, Test, TestMatrix,
    TestMatrixRun, TestResultCode, TestRun, TestRunLifecycle, TestVerdict,
    TestVerdictKind, User, Version, Worker,
)
from opp_ci.persistence import (
    create_matrix_from_axes, create_matrix_run, create_test_run, delete_worker,
    enqueue_job, finalize_verdict_for_run, get_current_expectation,
    get_matrix_by_name, get_or_create_test, get_test_by_name, insert_expectation,
    job_to_coord, parse_expectation_override, set_test_name, status_filter,
    update_worker,
)

_logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")


# ── Pydantic request/response schemas ──────────────────────────────────

class SubmitRunRequest(BaseModel):
    project: str | None = None      # required unless test_name is given
    kind: str | None = None         # smoke, fingerprint, statistical, build, …
    name: str | None = None         # optional label for the Test (set on first run)
    test_name: str | None = None    # run an existing named Test by name
    mode: str | None = None
    git_ref: str | None = None
    version: str | None = None
    os: str | None = None           # "Linux" | "Windows" | "MacOS"
    os_version: str | None = None   # Windows/MacOS only
    distro: str | None = None       # Linux only — "ubuntu", "fedora", ...
    distro_version: str | None = None
    flavor: str | None = None       # Linux only — "kubuntu", "xubuntu", ...
    flavor_version: str | None = None
    arch: str | None = None         # "amd64", "aarch64", ...
    compiler: str | None = None
    compiler_version: str | None = None
    isolation: str | None = None    # "none" | "podman"
    toolchain: str | None = None    # "none" | "nix"
    pins: list[str] | None = None   # ["omnetpp=6.4.0", ...] → sets the run's resolved_deps
    # Inline expected-result override for a freshly-created Test: PASS/FAIL/
    # ERROR, or null/"" to use the global default.
    expected_result_code: str | None = None

class SubmitMatrixRequest(BaseModel):
    matrix_name: str
    expected_result_code: str | None = None

class WorkerRegisterRequest(BaseModel):
    name: str
    tags: list[str] = Field(default_factory=list)
    concurrency: int = 1

class WorkerUpdateRequest(BaseModel):
    concurrency: int | None = None
    tags: list[str] | None = None
    enabled: bool | None = None

class WorkerSnapshotRequest(BaseModel):
    run_id: int
    snapshot: dict

class WorkerResultRequest(BaseModel):
    run_id: int
    result_code: str
    test_exec_seconds: float | None = None
    commit_sha: str | None = None
    stdout: str | None = None
    stderr: str | None = None
    details: dict | None = None

class CreateTokenRequest(BaseModel):
    name: str
    role: str = "readonly"


# ── Run submission ─────────────────────────────────────────────────────

@router.post("/runs")
async def submit_run(
    req: SubmitRunRequest,
    identity: dict = Depends(require_role("submitter")),
):
    """Submit a single test run to the queue.

    Always creates a new TestRun (and a new Test on first occurrence of
    the coordinate); there is no result-cache dedup at submission time.
    """
    session = SessionLocal()
    try:
        try:
            default_expectation = parse_expectation_override(req.expected_result_code)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid expected_result_code: {req.expected_result_code!r}",
            )
        resolved_deps = None
        if req.test_name:
            # Run an existing named test by name.
            test = get_test_by_name(session, req.test_name)
            if test is None:
                raise HTTPException(
                    status_code=404, detail=f"Test '{req.test_name}' not found",
                )
        else:
            if not req.project or not req.kind:
                raise HTTPException(
                    status_code=400,
                    detail="project and kind are required (or pass test_name).",
                )
            from opp_ci import platforms
            try:
                resolved_os, resolved_distro, resolved_flavor = platforms.resolve_platform(
                    os=req.os, distro=req.distro, flavor=req.flavor,
                )
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
            os_canon = platforms._os_canonical(resolved_os) if resolved_os else None
            os_ver = req.os_version if os_canon and os_canon != "Linux" else None
            distro_ver = req.distro_version if resolved_distro else None
            flavor_ver = req.flavor_version if resolved_flavor else None

            if req.pins:
                # Resolve pins server-side so --remote works without the
                # opp_env catalog on the client. resolve_dependencies validates
                # against the project's compatible versions when registered;
                # the setdefault loop still honours an explicit pin for custom
                # projects (e.g. mm1k) that have no registered dep metadata.
                # Resolved before get_or_create_test: deps are part of Test
                # identity, so they must be in the coord that keys the hash.
                from opp_ci.dependency import resolve_dependencies, parse_pins
                try:
                    pin_dict = parse_pins(req.pins)
                    resolved_deps = dict(resolve_dependencies(req.project, pins=pin_dict) or {})
                except ValueError as e:
                    raise HTTPException(status_code=400, detail=str(e))
                for name, ver in pin_dict.items():
                    resolved_deps.setdefault(name, ver)

            coord = {
                "project": req.project,
                "kind": req.kind,
                "mode": req.mode,
                "os": os_canon,
                "os_version": os_ver,
                "distro": resolved_distro,
                "distro_version": distro_ver,
                "flavor": resolved_flavor,
                "flavor_version": flavor_ver,
                "arch": req.arch,
                "compiler": req.compiler,
                "compiler_version": req.compiler_version,
                "isolation": req.isolation,
                "toolchain": req.toolchain,
                "opp_file": None,
                "resolved_deps": resolved_deps,
            }
            test = get_or_create_test(
                session, coord,
                default_expectation=default_expectation,
                expectation_set_by=identity.get("name"),
            )
            if req.name:
                try:
                    set_test_name(session, test, req.name)
                except ValueError as e:
                    raise HTTPException(status_code=409, detail=str(e))
        run = create_test_run(
            session,
            test_id=test.id,
            git_ref=req.git_ref,
            version=req.version,
            resolved_deps=resolved_deps,
        )
        session.commit()
        _logger.info("Run #%d submitted by %s", run.id, identity.get("name"))
        return {"id": run.id, "lifecycle": run.lifecycle.value}
    finally:
        session.close()


@router.post("/runs/matrix")
async def submit_matrix_run(
    req: SubmitMatrixRequest,
    identity: dict = Depends(require_role("submitter")),
):
    """Expand a matrix and queue all jobs as a single TestMatrixRun."""
    from opp_ci.scheduler import expand_matrix

    session = SessionLocal()
    try:
        try:
            default_expectation = parse_expectation_override(req.expected_result_code)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid expected_result_code: {req.expected_result_code!r}",
            )
        matrix = session.execute(
            select(TestMatrix).where(TestMatrix.name == req.matrix_name)
        ).scalar_one_or_none()
        if matrix is None:
            raise HTTPException(status_code=404, detail=f"Matrix '{req.matrix_name}' not found")

        proj = session.execute(
            select(Project).where(Project.name == matrix.project)
        ).scalar_one_or_none()
        matrix_run = create_matrix_run(
            session,
            matrix_id=matrix.id,
            trigger="remote",
            github_owner=proj.github_owner if proj else None,
            github_repo=proj.github_repo if proj else None,
        )

        from opp_ci.fingerprint import compute_cache_fingerprint

        jobs = expand_matrix(matrix.project, matrix.config)
        run_ids = []
        for job in jobs:
            fp = compute_cache_fingerprint(
                job, project=matrix.project, opp_file=matrix.opp_file,
            )
            run, _ = enqueue_job(
                session,
                job,
                project=matrix.project,
                opp_file=matrix.opp_file,
                matrix_run_id=matrix_run.id,
                use_cache=True,
                cache_fingerprint=fp,
                default_expectation=default_expectation,
                expectation_set_by=identity.get("name"),
            )
            run_ids.append(run.id)
        session.commit()
        _logger.info("Matrix '%s' queued %d jobs (matrix_run=%d) by %s",
                     req.matrix_name, len(run_ids), matrix_run.id, identity.get("name"))
        return {
            "matrix_name": matrix.display_name,
            "matrix_run_id": matrix_run.id,
            "jobs_queued": len(run_ids),
            "run_ids": run_ids,
        }
    finally:
        session.close()


@router.get("/runs")
async def list_runs(
    project: str | None = None,
    kind: str | None = None,
    status: str | None = None,
    lifecycle: str | None = None,
    result_code: str | None = None,
    os: str | None = None,
    os_version: str | None = None,
    distro: str | None = None,
    distro_version: str | None = None,
    flavor: str | None = None,
    flavor_version: str | None = None,
    limit: int = 50,
    _identity: dict = Depends(require_role("readonly")),
):
    """List test runs.

    ``status`` is the convenience union filter (accepts either a lifecycle
    value like ``queued`` or an outcome value like ``PASS``). ``lifecycle``
    and ``result_code`` are strict filters for callers that want to target
    one column. A bad value on any of the three returns HTTP 400.
    """
    session = SessionLocal()
    try:
        query = (
            select(TestRun)
            .join(Test, TestRun.test_id == Test.id)
            .order_by(TestRun.id.desc())
            .limit(limit)
        )
        if project:
            query = query.where(Test.project == project)
        if kind:
            query = query.where(Test.kind == kind)
        if status:
            try:
                query = status_filter(query, status)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
        if lifecycle:
            try:
                query = query.where(TestRun.lifecycle == TestRunLifecycle(lifecycle))
            except ValueError:
                raise HTTPException(
                    status_code=400, detail=f"Invalid lifecycle: {lifecycle!r}")
        if result_code:
            try:
                query = query.where(TestRun.result_code == TestResultCode(result_code))
            except ValueError:
                raise HTTPException(
                    status_code=400, detail=f"Invalid result_code: {result_code!r}")
        if os:
            query = query.where(Test.os == os)
        if os_version:
            query = query.where(Test.os_version == os_version)
        if distro:
            query = query.where(Test.distro == distro.lower())
        if distro_version:
            query = query.where(Test.distro_version == distro_version)
        if flavor:
            query = query.where(Test.flavor == flavor.lower())
        if flavor_version:
            query = query.where(Test.flavor_version == flavor_version)

        runs = session.execute(query).scalars().all()
        return [_run_to_dict(r) for r in runs]
    finally:
        session.close()


@router.get("/runs/{run_id}")
async def get_run(
    run_id: int,
    _identity: dict = Depends(require_role("readonly")),
):
    """Get details of a specific test run including its outcome."""
    session = SessionLocal()
    try:
        run = session.execute(
            select(TestRun).where(TestRun.id == run_id)
        ).scalar_one_or_none()
        if run is None:
            raise HTTPException(status_code=404, detail=f"Run #{run_id} not found")
        d = _run_to_dict(run)
        d["stdout"] = run.stdout
        d["stderr"] = run.stderr
        d["details"] = run.details
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


def _worker_to_dict(w):
    return {
        "id": w.id,
        "name": w.name,
        "tags": w.tags or [],
        "concurrency": w.concurrency,
        "status": w.status,
        "enabled": bool(w.enabled),
        "current_job_count": w.current_job_count,
    }


@router.patch("/workers/{worker_id}")
async def patch_worker(
    worker_id: int,
    req: WorkerUpdateRequest,
    _identity: dict = Depends(require_role("admin")),
):
    """Update a worker's concurrency, tags, and/or enabled flag (admin).

    Disabling drains rather than kills: the worker keeps heartbeating but is
    assigned no new jobs, and its in-flight jobs run to completion.
    """
    session = SessionLocal()
    try:
        try:
            worker = update_worker(
                session, worker_id,
                concurrency=req.concurrency, tags=req.tags, enabled=req.enabled,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        if worker is None:
            raise HTTPException(status_code=404, detail=f"Worker #{worker_id} not found")
        session.commit()
        _logger.info("Worker '%s' (id=%d) updated", worker.name, worker.id)
        return _worker_to_dict(worker)
    finally:
        session.close()


@router.delete("/workers/{worker_id}", status_code=204)
async def remove_worker(
    worker_id: int,
    _identity: dict = Depends(require_role("admin")),
):
    """Hard-delete a worker (admin). In-flight jobs are re-queued for others."""
    from opp_ci.config import MAX_RECLAIMS
    session = SessionLocal()
    try:
        result = delete_worker(
            session, worker_id, datetime.datetime.utcnow(), MAX_RECLAIMS)
        if result is None:
            raise HTTPException(status_code=404, detail=f"Worker #{worker_id} not found")
        requeued, retired = result
        session.commit()
        _logger.info("Worker #%d deleted (requeued=%d, retired=%d)",
                     worker_id, requeued, retired)
        return None
    finally:
        session.close()


@router.get("/workers/me")
async def worker_me(
    worker_info: dict = Depends(require_worker_token()),
):
    """Return the worker's own registered config (name, tags, concurrency)."""
    session = SessionLocal()
    try:
        worker = session.execute(
            select(Worker).where(Worker.id == worker_info["worker_id"])
        ).scalar_one_or_none()
        if worker is None:
            raise HTTPException(status_code=401, detail="Worker not found")
        return {
            "id": worker.id,
            "name": worker.name,
            "tags": worker.tags or [],
            "concurrency": worker.concurrency,
        }
    finally:
        session.close()


@router.post("/workers/heartbeat")
async def worker_heartbeat(
    payload: dict = Body(default=None),
    worker_info: dict = Depends(require_worker_token()),
):
    """Worker heartbeat — keeps the worker marked as online and reconciles job count.

    The optional body may carry shipped log lines
    (``{"logs": {"entries": [...]}}``) for the per-worker log view; older
    workers send no body, so the field is optional.
    """
    session = SessionLocal()
    try:
        worker = session.execute(
            select(Worker).where(Worker.id == worker_info["worker_id"])
        ).scalar_one_or_none()
        if worker:
            actual = session.execute(
                select(func.count(TestRun.id)).where(
                    TestRun.worker_id == worker.id,
                    TestRun.lifecycle == TestRunLifecycle.running,
                )
            ).scalar() or 0
            if worker.current_job_count != actual:
                _logger.info("Reconciling worker '%s' job count: %d -> %d",
                             worker.name, worker.current_job_count, actual)
                worker.current_job_count = actual
                worker.status = "busy" if actual >= worker.concurrency else "online"
                session.commit()
    finally:
        session.close()

    # Ingest shipped logs (best-effort; keyed by the authenticated worker id).
    logs = payload.get("logs") if isinstance(payload, dict) else None
    if isinstance(logs, dict) and logs.get("entries"):
        from opp_ci.worker_logs import STORE
        STORE.append(worker_info["worker_id"], logs["entries"])

    return {"status": "ok", "worker_id": worker_info["worker_id"]}


@router.post("/workers/poll")
async def worker_poll(
    worker_info: dict = Depends(require_worker_token()),
):
    """
    Worker polls for the next available job.

    Assigns a queued TestRun to this worker, sets lifecycle=running, and
    returns the job spec. Returns null job if nothing is queued.
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

        worker_tags = set(worker.tags or [])
        claimed_run = None
        for candidate in session.execute(
            select(TestRun)
            .where(TestRun.lifecycle == TestRunLifecycle.queued)
            .order_by(TestRun.id)
        ).scalars():
            if _worker_can_run(worker_tags, candidate.test):
                claimed_run = candidate
                break

        if claimed_run is None:
            return {"job": None, "reason": "no queued jobs"}

        claimed_run.lifecycle = TestRunLifecycle.running
        claimed_run.worker_id = worker.id
        claimed_run.started_at = datetime.datetime.utcnow()
        worker.current_job_count += 1
        if worker.current_job_count >= worker.concurrency:
            worker.status = "busy"
        session.commit()

        test = claimed_run.test
        _logger.info("Assigned run #%d to worker '%s'", claimed_run.id, worker.name)
        return {
            "job": {
                "run_id": claimed_run.id,
                "project": test.project,
                "version": claimed_run.version,
                "kind": test.kind,
                "mode": test.mode,
                "git_ref": claimed_run.git_ref,
                "os": test.os,
                "os_version": test.os_version,
                "distro": test.distro,
                "distro_version": test.distro_version,
                "flavor": test.flavor,
                "flavor_version": test.flavor_version,
                "arch": test.arch,
                "compiler": test.compiler,
                "compiler_version": test.compiler_version,
                "isolation": test.isolation,
                "toolchain": test.toolchain,
                "opp_file": test.opp_file,
                "resolved_deps": claimed_run.resolved_deps,
            }
        }
    finally:
        session.close()


def _platform_required_tag(test):
    """Return the most-specific platform capability tag a worker must
    advertise to claim a TestRun targeting *test*, or None when the test
    doesn't pin a platform.

    Rules:
      - test names a flavor   →  flavor:<flavor>-<flavor_version-or-distro_version>
      - test names a distro   →  distro:<distro>-<distro_version>
      - test names Windows/MacOS with a version → os:<os>-<ver>
      - test names just an OS family → os:<os>
    """
    if test.flavor:
        ver = test.flavor_version or test.distro_version
        return f"flavor:{test.flavor.lower()}-{ver}" if ver else f"flavor:{test.flavor.lower()}"
    if test.distro:
        return (f"distro:{test.distro.lower()}-{test.distro_version}"
                if test.distro_version else f"distro:{test.distro.lower()}")
    if test.os:
        os_lower = test.os.lower()
        if os_lower != "linux" and test.os_version:
            return f"os:{os_lower}-{test.os_version}"
        return f"os:{os_lower}"
    return None


def _worker_can_run(worker_tags, test):
    """Return True if a worker with *worker_tags* may claim a TestRun
    targeting *test*.

    Required tags by execution environment:
      - isolation=podman             →  {"podman"}
      - isolation=none, toolchain=nix → {"nix", "<platform>", "compiler:<c>-<cv>"}
      - isolation=none, toolchain=none → {"<platform>", "compiler:<c>-<cv>"}
    """
    isolation = test.isolation or "none"
    toolchain = test.toolchain or "none"
    required = set()
    if isolation == "podman":
        required.add("podman")
    else:
        if toolchain == "nix":
            required.add("nix")
        platform_tag = _platform_required_tag(test)
        if platform_tag:
            required.add(platform_tag)
        if test.compiler and test.compiler_version:
            required.add(f"compiler:{test.compiler.lower()}-{test.compiler_version}")
    if test.arch:
        required.add(f"arch:{test.arch.lower()}")
    return required.issubset(worker_tags)


@router.post("/workers/snapshot")
async def worker_report_snapshot(
    req: WorkerSnapshotRequest,
    worker_info: dict = Depends(require_worker_token()),
):
    """Worker reports the system snapshot captured at run start.

    Optional — workers that don't capture a snapshot simply skip this call.
    """
    session = SessionLocal()
    try:
        run = session.execute(
            select(TestRun).where(TestRun.id == req.run_id)
        ).scalar_one_or_none()
        if run is None:
            raise HTTPException(status_code=404, detail=f"Run #{req.run_id} not found")
        if run.worker_id != worker_info["worker_id"]:
            # Reclaimed/reassigned while this worker was out of contact —
            # drop the stale snapshot quietly (benign 200, see the result
            # endpoint for the full rationale).
            _logger.warning(
                "Dropping stale snapshot for run #%d from worker '%s' "
                "(current worker_id=%s)",
                req.run_id, worker_info["worker_name"], run.worker_id)
            return {"status": "dropped", "reason": "reclaimed"}
        run.system_snapshot = req.snapshot
        session.commit()
        return {"status": "ok"}
    finally:
        session.close()


@router.post("/workers/result")
async def worker_report_result(
    req: WorkerResultRequest,
    worker_info: dict = Depends(require_worker_token()),
):
    """Worker reports the result of a completed job.

    Writes the outcome columns directly onto the same `TestRun` row and
    flips lifecycle to `finished`. Cancelled runs (lifecycle already set
    to `cancelled`) are not overwritten — the worker is finishing a run
    that the coordinator gave up on, and the outcome is recorded but the
    cancel takes precedence at the lifecycle level.
    """
    session = SessionLocal()
    try:
        run = session.execute(
            select(TestRun).where(TestRun.id == req.run_id)
        ).scalar_one_or_none()
        if run is None:
            raise HTTPException(status_code=404, detail=f"Run #{req.run_id} not found")
        if run.worker_id != worker_info["worker_id"]:
            # The run was reclaimed (worker_id cleared, then possibly
            # re-queued, reassigned to another worker, or retired as a
            # poison pill) while this worker was out of contact. Drop the
            # now-stale result rather than clobbering the run's current
            # state or its new owner's work. Benign 200 so the worker just
            # moves on instead of logging an error / retrying.
            _logger.warning(
                "Dropping stale result for run #%d from worker '%s': no "
                "longer assigned to it (current worker_id=%s, lifecycle=%s)",
                req.run_id, worker_info["worker_name"], run.worker_id,
                run.lifecycle.value if run.lifecycle else None)
            return {"status": "dropped", "run_id": run.id, "reason": "reclaimed"}

        try:
            result_code = TestResultCode(req.result_code)
        except ValueError:
            raise HTTPException(status_code=400,
                                detail=f"Invalid result_code: {req.result_code!r}")

        if run.lifecycle != TestRunLifecycle.cancelled:
            run.lifecycle = TestRunLifecycle.finished
        run.finished_at = datetime.datetime.utcnow()
        run.test_exec_seconds = req.test_exec_seconds
        run.result_code = result_code
        run.stdout = req.stdout
        run.stderr = req.stderr
        run.details = req.details
        if req.commit_sha:
            run.commit_sha = req.commit_sha

        worker = session.execute(
            select(Worker).where(Worker.id == worker_info["worker_id"])
        ).scalar_one_or_none()
        if worker:
            worker.current_job_count = max(0, worker.current_job_count - 1)
            if worker.status == "busy" and worker.current_job_count < worker.concurrency:
                worker.status = "online"

        finalize_verdict_for_run(session, run.id)

        session.commit()
        _logger.info("Run #%d result: %s (worker '%s')", run.id, req.result_code,
                     worker_info["worker_name"])

        try:
            from opp_ci.github.status import update_github_status
            update_github_status(run.id)
        except Exception as e:
            _logger.warning("GitHub status update failed for run #%d: %s", run.id, e)

        # Trigger git notes sync — once per matrix run when all its children
        # finish, or immediately for ad-hoc (matrix_run=None) runs.
        should_sync = True
        gh_owner = gh_repo = None
        if run.matrix_run_id:
            pending = session.execute(
                select(TestRun).where(
                    TestRun.matrix_run_id == run.matrix_run_id,
                    TestRun.lifecycle.in_(
                        [TestRunLifecycle.queued, TestRunLifecycle.running]),
                )
            ).first()
            should_sync = pending is None
            mr = run.matrix_run
            if mr:
                gh_owner = mr.github_owner
                gh_repo = mr.github_repo

        if should_sync:
            if not gh_owner or not gh_repo:
                proj = session.execute(
                    select(Project).where(Project.name == run.test.project)
                ).scalar_one_or_none()
                if proj:
                    gh_owner = gh_owner or proj.github_owner
                    gh_repo = gh_repo or proj.github_repo
            if gh_owner and gh_repo:
                try:
                    from opp_ci.notes import trigger_notes_sync
                    trigger_notes_sync(gh_owner, gh_repo)
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
                "enabled": bool(w.enabled),
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


# ── Matrix management ──────────────────────────────────────────────────

class CreateMatrixRequest(BaseModel):
    name: str
    project: str
    config: dict = Field(default_factory=dict)
    opp_file: str | None = None
    ref_range: dict | None = Field(
        default=None,
        description='Optional {"base": "...", "head": "..."} to populate refs from GitHub commit range',
    )


@router.post("/matrices")
async def create_matrix(
    req: CreateMatrixRequest,
    identity: dict = Depends(require_role("submitter")),
):
    """Create a test matrix, optionally with a ref range for lazy commit resolution."""
    session = SessionLocal()
    try:
        existing = session.execute(
            select(TestMatrix).where(TestMatrix.name == req.name)
        ).scalar_one_or_none()
        if existing is not None:
            raise HTTPException(status_code=409, detail=f"Matrix '{req.name}' already exists")

        config = dict(req.config) if req.config else {}

        if req.ref_range:
            base = req.ref_range.get("base", "")
            head = req.ref_range.get("head", "")
            if not base or not head:
                raise HTTPException(status_code=400, detail="ref_range requires both 'base' and 'head'")
            config["ref_range"] = {"base": base, "head": head}

        matrix = TestMatrix(name=req.name, project=req.project, opp_file=req.opp_file, config=config)
        session.add(matrix)
        session.commit()

        from opp_ci.scheduler import expand_matrix
        jobs = expand_matrix(matrix.project, config)

        _logger.info("Matrix '%s' created by %s (%d jobs)", req.name, identity.get("name"), len(jobs))
        return {
            "id": matrix.id,
            "name": matrix.name,
            "project": matrix.project,
            "jobs_count": len(jobs),
        }
    finally:
        session.close()


@router.get("/matrices")
async def list_matrices(
    _identity: dict = Depends(require_role("readonly")),
):
    """List all test matrices."""
    session = SessionLocal()
    try:
        matrices = session.execute(
            select(TestMatrix).order_by(TestMatrix.name)
        ).scalars().all()
        return [
            {
                "id": m.id,
                "name": m.name,
                "project": m.project,
                "opp_file": m.opp_file,
                "config": m.config,
                "refs_count": len(m.config.get("refs", [])) if m.config else 0,
            }
            for m in matrices
        ]
    finally:
        session.close()


# ── Matrix-runs (rollup view + anonymous launcher) ─────────────────────


class InlineMatrixRunRequest(BaseModel):
    """Body for POST /api/matrix-runs.

    Either `matrix_name` (an existing matrix) OR `project` plus axis
    fields (anonymous matrix). When axis fields are present, a synthetic
    `TestMatrix` row is persisted with a generated name. `no_cache`
    forces a fresh TestRun per cell.
    """
    matrix_name: str | None = None
    project: str | None = None
    name: str | None = None
    opp_file: str | None = None
    kinds: list[str] | None = None
    modes: list[str] | None = None
    refs: list[str] | None = None
    versions: list[str] | None = None
    os: list[str] | None = None
    os_version: list[str] | None = None
    distro: list[str] | None = None
    distro_version: list[str] | None = None
    flavor: list[str] | None = None
    flavor_version: list[str] | None = None
    compiler: list[str] | None = None
    compiler_version: list[str] | None = None
    arch: list[str] | None = None
    isolation: list[str] | None = None
    toolchain: list[str] | None = None
    deps: dict | None = None
    no_cache: bool = False
    # Inline expected-result override for Tests the matrix freshly creates:
    # PASS/FAIL/ERROR, or null/"" to use the global default.
    expected_result_code: str | None = None


def _spec_to_config(req: "InlineMatrixRunRequest"):
    """Strip empty axes from the spec body and return a plain config dict."""
    config = {}
    for key in ("kinds", "modes", "refs", "versions", "os", "os_version",
                "distro", "distro_version", "flavor", "flavor_version",
                "compiler", "compiler_version", "arch", "isolation",
                "toolchain"):
        v = getattr(req, key)
        if v:
            config[key] = v
    if req.deps:
        config["deps"] = req.deps
    return config


@router.post("/matrix-runs")
async def submit_matrix_run(
    req: InlineMatrixRunRequest,
    identity: dict = Depends(require_role("submitter")),
):
    """Launch a matrix run from a named matrix or an inline spec.

    On an inline spec (no `matrix_name`), an anonymous `TestMatrix` row
    is persisted with a generated name so the resulting `TestMatrixRun`
    has a stable parent.
    """
    from opp_ci.scheduler import expand_matrix

    session = SessionLocal()
    try:
        try:
            default_expectation = parse_expectation_override(req.expected_result_code)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid expected_result_code: {req.expected_result_code!r}",
            )
        if req.matrix_name:
            matrix = session.execute(
                select(TestMatrix).where(TestMatrix.name == req.matrix_name)
            ).scalar_one_or_none()
            if matrix is None:
                raise HTTPException(status_code=404,
                                    detail=f"Matrix '{req.matrix_name}' not found")
        else:
            if not req.project:
                raise HTTPException(
                    status_code=400,
                    detail="Inline spec must include either 'matrix_name' "
                           "or 'project' plus axis fields.",
                )
            # An inline spec with no name stays anonymous (name = NULL);
            # pass `name` to make it reusable.
            try:
                matrix = create_matrix_from_axes(
                    session, project=req.project, config=_spec_to_config(req),
                    name=req.name, opp_file=req.opp_file,
                )
            except ValueError as e:
                raise HTTPException(status_code=409, detail=str(e))

        proj = session.execute(
            select(Project).where(Project.name == matrix.project)
        ).scalar_one_or_none()
        matrix_run = create_matrix_run(
            session,
            matrix_id=matrix.id,
            trigger="remote",
            github_owner=proj.github_owner if proj else None,
            github_repo=proj.github_repo if proj else None,
        )

        from opp_ci.fingerprint import compute_cache_fingerprint

        jobs = expand_matrix(matrix.project, matrix.config)
        run_ids = []
        for job in jobs:
            fp = None if req.no_cache else compute_cache_fingerprint(
                job, project=matrix.project, opp_file=matrix.opp_file,
            )
            run, _ = enqueue_job(
                session,
                job,
                project=matrix.project,
                opp_file=matrix.opp_file,
                matrix_run_id=matrix_run.id,
                use_cache=not req.no_cache,
                cache_fingerprint=fp,
                default_expectation=default_expectation,
                expectation_set_by=identity.get("name"),
            )
            run_ids.append(run.id)
        session.commit()
        _logger.info(
            "Matrix '%s' queued %d jobs (matrix_run=%d) by %s%s",
            matrix.name, len(run_ids), matrix_run.id, identity.get("name"),
            " (cache disabled)" if req.no_cache else "",
        )
        return {
            "matrix_name": matrix.display_name,
            "matrix_run_id": matrix_run.id,
            "jobs_queued": len(run_ids),
            "run_ids": run_ids,
            "status": "queued",
        }
    finally:
        session.close()




def _matrix_run_to_dict(mr, matrix=None):
    return {
        "id": mr.id,
        "matrix_id": mr.matrix_id,
        "matrix_name": matrix.name if matrix else None,
        "matrix_project": matrix.project if matrix else None,
        "trigger": mr.trigger,
        "ref": mr.ref,
        "verdict": mr.verdict.value if mr.verdict else None,
        "actual_summary": mr.actual_summary.value if mr.actual_summary else None,
        "pass_count": mr.pass_count,
        "fail_count": mr.fail_count,
        "error_count": mr.error_count,
        "expected_count": mr.expected_count,
        "unexpected_count": mr.unexpected_count,
        "unknown_count": mr.unknown_count,
        "cache_hit_count": mr.cache_hit_count,
        "total_count": mr.total_count,
        "github_owner": mr.github_owner,
        "github_repo": mr.github_repo,
        "github_commit_sha": mr.github_commit_sha,
        "github_pr_number": mr.github_pr_number,
        "created_at": mr.created_at.isoformat() if mr.created_at else None,
        "completed_at": mr.completed_at.isoformat() if mr.completed_at else None,
    }


@router.get("/matrix-runs")
async def list_matrix_runs_api(
    project: str | None = None,
    verdict: str | None = None,
    since: str | None = None,
    limit: int = 50,
    _identity: dict = Depends(require_role("readonly")),
):
    """List recent TestMatrixRun rows with their rollup verdict."""
    session = SessionLocal()
    try:
        query = (
            select(TestMatrixRun, TestMatrix)
            .join(TestMatrix, TestMatrixRun.matrix_id == TestMatrix.id)
            .order_by(TestMatrixRun.id.desc())
            .limit(limit)
        )
        if project:
            query = query.where(TestMatrix.project == project)
        if verdict:
            try:
                query = query.where(TestMatrixRun.verdict == TestVerdictKind(verdict))
            except ValueError:
                raise HTTPException(status_code=400,
                                    detail=f"Invalid verdict: {verdict!r}")
        if since:
            try:
                cutoff = datetime.datetime.fromisoformat(since)
            except ValueError:
                raise HTTPException(status_code=400,
                                    detail=f"Invalid since: {since!r}")
            query = query.where(TestMatrixRun.created_at >= cutoff)

        rows = session.execute(query).all()
        return [_matrix_run_to_dict(mr, m) for mr, m in rows]
    finally:
        session.close()


@router.get("/matrix-runs/{matrix_run_id}")
async def get_matrix_run_api(
    matrix_run_id: int,
    _identity: dict = Depends(require_role("readonly")),
):
    """Get rollup plus the per-cell TestVerdict list for one matrix run."""
    session = SessionLocal()
    try:
        mr = session.execute(
            select(TestMatrixRun).where(TestMatrixRun.id == matrix_run_id)
        ).scalar_one_or_none()
        if mr is None:
            raise HTTPException(status_code=404,
                                detail=f"Matrix run #{matrix_run_id} not found")
        matrix = session.execute(
            select(TestMatrix).where(TestMatrix.id == mr.matrix_id)
        ).scalar_one_or_none()
        d = _matrix_run_to_dict(mr, matrix)

        rows = session.execute(
            select(TestVerdict, TestRun, Test)
            .join(TestRun, TestVerdict.test_run_id == TestRun.id)
            .join(Test, TestVerdict.test_id == Test.id)
            .where(TestVerdict.matrix_run_id == matrix_run_id)
            .order_by(TestVerdict.id)
        ).all()
        cells = []
        for verdict, run, test in rows:
            expected = None
            if verdict.expectation_id is not None:
                exp = session.get(ExpectedTestResult, verdict.expectation_id)
                if exp:
                    expected = exp.expected_result_code.value if exp.expected_result_code else None
            cells.append({
                "verdict_id": verdict.id,
                "test_id": test.id,
                "test_run_id": run.id,
                "kind": test.kind,
                "mode": test.mode,
                "os": test.os,
                "os_version": test.os_version,
                "distro": test.distro,
                "distro_version": test.distro_version,
                "flavor": test.flavor,
                "flavor_version": test.flavor_version,
                "arch": test.arch,
                "compiler": test.compiler,
                "compiler_version": test.compiler_version,
                "isolation": test.isolation,
                "toolchain": test.toolchain,
                "actual": run.result_code.value if run.result_code else run.lifecycle.value,
                "expected": expected,
                "expectation_id": verdict.expectation_id,
                "verdict": verdict.verdict.value if verdict.verdict else None,
                "cache_hit": verdict.cache_hit,
                "recorded_at": verdict.recorded_at.isoformat() if verdict.recorded_at else None,
                "test_run_finished_at": run.finished_at.isoformat() if run.finished_at else None,
            })
        d["cells"] = cells
        return d
    finally:
        session.close()


# ── Expectations ───────────────────────────────────────────────────────


class ExpectationRequest(BaseModel):
    expected_result_code: str | None = None  # PASS/FAIL/ERROR/SKIPPED, or null = retract
    expected_result_description: str | None = None
    reason: str | None = None


def _expectation_to_dict(row):
    return {
        "id": row.id,
        "test_id": row.test_id,
        "expected_result_code": row.expected_result_code.value if row.expected_result_code else None,
        "expected_result_description": row.expected_result_description,
        "reason": row.reason,
        "set_by": row.set_by,
        "set_at": row.set_at.isoformat() if row.set_at else None,
    }


@router.post("/tests/{test_id}/expectations")
async def post_expectation(
    test_id: int,
    req: ExpectationRequest,
    identity: dict = Depends(require_role("submitter")),
):
    """Append a new ExpectedTestResult row for `test_id`.

    `expected_result_code: null` records an explicit retraction —
    distinguishable from never-set and itself audited.
    """
    code = None
    if req.expected_result_code is not None:
        try:
            code = TestResultCode(req.expected_result_code)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid expected_result_code: {req.expected_result_code!r}",
            )

    session = SessionLocal()
    try:
        test = session.get(Test, test_id)
        if test is None:
            raise HTTPException(status_code=404, detail=f"Test #{test_id} not found")
        row = insert_expectation(
            session, test_id=test_id,
            expected_result_code=code,
            expected_result_description=req.expected_result_description,
            reason=req.reason,
            set_by=identity.get("name"),
        )
        session.commit()
        _logger.info(
            "Expectation for Test #%d set to %s by %s",
            test_id, code.value if code else "(retract)", identity.get("name"),
        )
        return _expectation_to_dict(row)
    finally:
        session.close()


@router.get("/tests/{test_id}/expectations")
async def list_expectations(
    test_id: int,
    limit: int = 50,
    _identity: dict = Depends(require_role("readonly")),
):
    """Return the expectation edit log for one Test, newest first."""
    session = SessionLocal()
    try:
        rows = session.execute(
            select(ExpectedTestResult)
            .where(ExpectedTestResult.test_id == test_id)
            .order_by(ExpectedTestResult.set_at.desc(),
                      ExpectedTestResult.id.desc())
            .limit(limit)
        ).scalars().all()
        return [_expectation_to_dict(r) for r in rows]
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


class AckNotesRequest(BaseModel):
    shas: list[str] = Field(default_factory=list)


@router.post("/notes/{owner}/{repo}/ack", status_code=204)
async def ack_notes(
    owner: str,
    repo: str,
    req: AckNotesRequest,
    _identity: dict = Depends(require_role("readonly")),
):
    """
    Acknowledge that the ci-notes.yml workflow has written git notes for
    the listed commit SHAs. Currently a no-op (logged only).
    """
    _logger.info("Notes ack from %s/%s: %d sha(s)", owner, repo, len(req.shas))
    return None


# ── Projects ───────────────────────────────────────────────────────────


class CreateProjectRequest(BaseModel):
    name: str
    github: str | None = None        # "owner/repo"
    git_url: str | None = None
    opp_env_name: str | None = None
    deps: list[str] = Field(default_factory=list)


def _project_to_dict(p):
    return {
        "id": p.id,
        "name": p.name,
        "opp_env_name": p.opp_env_name,
        "github": f"{p.github_owner}/{p.github_repo}" if p.github_owner else None,
        "github_owner": p.github_owner,
        "github_repo": p.github_repo,
        "git_url": p.git_url,
        "deps": p.dependency_names or [],
    }


@router.get("/projects")
async def list_projects(
    _identity: dict = Depends(require_role("readonly")),
):
    """List known projects (readonly)."""
    session = SessionLocal()
    try:
        projects = session.execute(
            select(Project).order_by(Project.name)
        ).scalars().all()
        return [_project_to_dict(p) for p in projects]
    finally:
        session.close()


@router.post("/projects")
async def add_project(
    req: CreateProjectRequest,
    identity: dict = Depends(require_role("submitter")),
):
    """Register a new project (submitter)."""
    session = SessionLocal()
    try:
        existing = session.execute(
            select(Project).where(Project.name == req.name)
        ).scalar_one_or_none()
        if existing is not None:
            raise HTTPException(status_code=409,
                                detail=f"Project '{req.name}' already exists")

        github_owner = github_repo = None
        if req.github:
            parts = req.github.split("/", 1)
            if len(parts) != 2 or not parts[0] or not parts[1]:
                raise HTTPException(status_code=400,
                                    detail="github must be 'owner/repo'")
            github_owner, github_repo = parts

        project = Project(
            name=req.name,
            opp_env_name=req.opp_env_name or req.name,
            github_owner=github_owner,
            github_repo=github_repo,
            git_url=req.git_url,
            dependency_names=req.deps or [],
        )
        session.add(project)
        session.commit()
        _logger.info("Project '%s' added by %s", project.name, identity.get("name"))
        return _project_to_dict(project)
    finally:
        session.close()


@router.post("/projects/sync-catalog")
async def sync_catalog_endpoint(
    identity: dict = Depends(require_role("admin")),
):
    """Refresh the project catalog from opp_env, server-side (admin).

    Synchronous; can take 30+ seconds while opp_env is queried. Returns
    the count of newly-added projects and versions.
    """
    from opp_ci.opp_env_adapter import sync_catalog
    session = SessionLocal()
    try:
        new_projects, new_versions = sync_catalog(session)
        _logger.info("Catalog sync by %s: %d new projects, %d new versions",
                     identity.get("name"), new_projects, new_versions)
        return {"new_projects": new_projects, "new_versions": new_versions}
    finally:
        session.close()


# ── Versions ───────────────────────────────────────────────────────────


class AddVersionRequest(BaseModel):
    label: str
    git_ref: str | None = None
    opp_env_version: str | None = None
    deps: dict | None = None


def _version_to_dict(v, project_name):
    return {
        "id": v.id,
        "project": project_name,
        "label": v.label,
        "git_ref": v.git_ref,
        "opp_env_version": v.opp_env_version,
        "deps": v.resolved_dependencies,
    }


@router.get("/versions")
async def list_all_versions(
    _identity: dict = Depends(require_role("readonly")),
):
    """List every registered version across all projects (readonly)."""
    session = SessionLocal()
    try:
        names = dict(session.execute(select(Project.id, Project.name)).all())
        versions = session.execute(select(Version)).scalars().all()
        return [_version_to_dict(v, names.get(v.project_id, "?")) for v in versions]
    finally:
        session.close()


@router.get("/projects/{name}/versions")
async def list_project_versions(
    name: str,
    _identity: dict = Depends(require_role("readonly")),
):
    """List registered versions for one project (readonly)."""
    session = SessionLocal()
    try:
        proj = session.execute(
            select(Project).where(Project.name == name)
        ).scalar_one_or_none()
        if proj is None:
            raise HTTPException(status_code=404, detail=f"Project '{name}' not found")
        versions = session.execute(
            select(Version).where(Version.project_id == proj.id)
        ).scalars().all()
        return [_version_to_dict(v, proj.name) for v in versions]
    finally:
        session.close()


@router.post("/projects/{name}/versions")
async def add_version(
    name: str,
    req: AddVersionRequest,
    identity: dict = Depends(require_role("submitter")),
):
    """Register a version for a project (submitter)."""
    session = SessionLocal()
    try:
        proj = session.execute(
            select(Project).where(Project.name == name)
        ).scalar_one_or_none()
        if proj is None:
            raise HTTPException(status_code=404, detail=f"Project '{name}' not found")
        version = Version(
            project_id=proj.id,
            label=req.label,
            git_ref=req.git_ref or req.label,
            opp_env_version=req.opp_env_version,
            resolved_dependencies=req.deps,
        )
        session.add(version)
        session.commit()
        _logger.info("Version '%s' added to '%s' by %s",
                     req.label, name, identity.get("name"))
        return _version_to_dict(version, proj.name)
    finally:
        session.close()


# ── Admin seed ─────────────────────────────────────────────────────────


@router.post("/admin/seed/projects")
async def seed_projects_endpoint(
    _identity: dict = Depends(require_role("admin")),
):
    """Seed the core projects from the catalog (admin)."""
    from opp_ci.catalog import seed_projects
    session = SessionLocal()
    try:
        before = session.execute(select(func.count(Project.id))).scalar() or 0
        seed_projects(session)
        after = session.execute(select(func.count(Project.id))).scalar() or 0
        return {"inserted": after - before, "total": after}
    finally:
        session.close()


@router.post("/admin/seed/platforms")
async def seed_platforms_endpoint(
    _identity: dict = Depends(require_role("admin")),
):
    """Seed OS and Compiler rows from platforms.yml (admin)."""
    from opp_ci.catalog import seed_platforms
    session = SessionLocal()
    try:
        os_n, comp_n = seed_platforms(session)
        return {"os_inserted": os_n, "compilers_inserted": comp_n}
    finally:
        session.close()


@router.post("/admin/seed/matrices")
async def seed_matrices_endpoint(
    _identity: dict = Depends(require_role("admin")),
):
    """Seed the default matrix definitions (admin)."""
    from opp_ci.scheduler import DEFAULT_MATRICES
    session = SessionLocal()
    try:
        inserted = 0
        for name, mdef in DEFAULT_MATRICES.items():
            existing = session.execute(
                select(TestMatrix).where(TestMatrix.name == name)
            ).scalar_one_or_none()
            if existing is None:
                session.add(TestMatrix(name=name, project=mdef["project"],
                                       config=mdef["config"]))
                inserted += 1
        session.commit()
        return {"inserted": inserted, "total": len(DEFAULT_MATRICES)}
    finally:
        session.close()


# ── Token revocation ───────────────────────────────────────────────────


@router.delete("/tokens/{token_id}", status_code=204)
async def revoke_token(
    token_id: int,
    _identity: dict = Depends(require_role("admin")),
):
    """Disable an API token by id (admin). Does not hard-delete the row."""
    session = SessionLocal()
    try:
        token = session.execute(
            select(ApiToken).where(ApiToken.id == token_id)
        ).scalar_one_or_none()
        if token is None:
            raise HTTPException(status_code=404, detail=f"Token #{token_id} not found")
        token.enabled = False
        session.commit()
        _logger.info("API token #%d (%s) revoked", token.id, token.name)
        return None
    finally:
        session.close()


# ── Users ──────────────────────────────────────────────────────────────


class CreateUserRequest(BaseModel):
    username: str
    password: str
    role: str = "admin"
    update_password: bool = False


class UpdateUserRequest(BaseModel):
    enabled: bool | None = None
    role: str | None = None
    password: str | None = None


_USER_ROLES = ("readonly", "submitter", "admin")


def _user_to_dict(u):
    return {
        "id": u.id,
        "username": u.username,
        "github_username": u.github_username,
        "role": u.role,
        "role_locked": bool(u.role_locked),
        "enabled": bool(u.enabled),
        "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None,
    }


@router.post("/users")
async def create_user(
    req: CreateUserRequest,
    _identity: dict = Depends(require_role("admin")),
):
    """Create (or update) a local-login user (admin).

    Plaintext password arrives over TLS and is hashed before storage.
    """
    from opp_ci.passwords import hash_password

    if req.role not in _USER_ROLES:
        raise HTTPException(status_code=400, detail=f"Invalid role: {req.role!r}")

    session = SessionLocal()
    try:
        existing = session.execute(
            select(User).where(User.username == req.username)
        ).scalar_one_or_none()
        if existing is not None:
            if not req.update_password:
                raise HTTPException(
                    status_code=409,
                    detail=f"User '{req.username}' already exists "
                           "(pass update_password=true to reset)",
                )
            existing.password_hash = hash_password(req.password)
            existing.role = req.role
            existing.role_locked = True
            existing.enabled = True
            session.commit()
            return _user_to_dict(existing)

        user = User(
            username=req.username,
            password_hash=hash_password(req.password),
            role=req.role,
            role_locked=True,
            enabled=True,
        )
        session.add(user)
        session.commit()
        _logger.info("User '%s' created (role=%s)", user.username, user.role)
        return _user_to_dict(user)
    finally:
        session.close()


@router.get("/users")
async def list_users(
    _identity: dict = Depends(require_role("admin")),
):
    """List web UI users (admin)."""
    session = SessionLocal()
    try:
        users = session.execute(select(User).order_by(User.id)).scalars().all()
        return [_user_to_dict(u) for u in users]
    finally:
        session.close()


@router.patch("/users/{username}")
async def update_user(
    username: str,
    req: UpdateUserRequest,
    _identity: dict = Depends(require_role("admin")),
):
    """Patch a user's enabled flag, role, and/or password (admin)."""
    from opp_ci.passwords import hash_password

    if req.role is not None and req.role not in _USER_ROLES:
        raise HTTPException(status_code=400, detail=f"Invalid role: {req.role!r}")

    session = SessionLocal()
    try:
        user = session.execute(
            select(User).where(User.username == username)
        ).scalar_one_or_none()
        if user is None:
            raise HTTPException(status_code=404, detail=f"User '{username}' not found")
        if req.enabled is not None:
            user.enabled = req.enabled
        if req.role is not None:
            user.role = req.role
            user.role_locked = True
        if req.password is not None:
            user.password_hash = hash_password(req.password)
        session.commit()
        _logger.info("User '%s' updated", username)
        return _user_to_dict(user)
    finally:
        session.close()


# ── Webhook simulation ─────────────────────────────────────────────────


class TestWebhookRequest(BaseModel):
    project: str
    ref: str
    event_type: str = "push"  # "push" | "pr"
    sha: str | None = None
    pr_number: int | None = None


@router.post("/github/rules/test-webhook")
async def test_webhook(
    req: TestWebhookRequest,
    _identity: dict = Depends(require_role("admin")),
):
    """Drive the webhook handler with a synthesized payload (admin).

    Same code path as `opp_ci rule test-webhook`. Returns the handler's
    result dict so the operator sees which rules matched.
    """
    from opp_ci.github.webhook import handle_webhook_event

    session = SessionLocal()
    try:
        proj = session.execute(
            select(Project).where(Project.name == req.project)
        ).scalar_one_or_none()
        if proj is None:
            raise HTTPException(status_code=404,
                                detail=f"Project '{req.project}' not found")
        if not proj.github_owner or not proj.github_repo:
            raise HTTPException(
                status_code=400,
                detail=f"Project '{req.project}' has no GitHub owner/repo configured",
            )
        owner, repo = proj.github_owner, proj.github_repo
    finally:
        session.close()

    sha = req.sha or "0" * 40
    if req.event_type == "push":
        payload = {
            "ref": f"refs/heads/{req.ref}",
            "after": sha,
            "repository": {"name": repo, "owner": {"login": owner}},
        }
        result = handle_webhook_event("push", payload)
    elif req.event_type == "pr":
        payload = {
            "action": "synchronize",
            "pull_request": {
                "number": req.pr_number or 1,
                "head": {"sha": sha, "ref": req.ref},
            },
            "repository": {"name": repo, "owner": {"login": owner}},
        }
        result = handle_webhook_event("pull_request", payload)
    else:
        raise HTTPException(status_code=400,
                            detail=f"Invalid event_type: {req.event_type!r}")
    return result


# ── Helpers ────────────────────────────────────────────────────────────

def _run_to_dict(run):
    """Render a TestRun (with its joined Test row) into a serialisable dict."""
    test = run.test
    matrix_run = run.matrix_run
    return {
        "id": run.id,
        "test_id": run.test_id,
        "project": test.project,
        "kind": test.kind,
        "mode": test.mode,
        "os": test.os,
        "os_version": test.os_version,
        "distro": test.distro,
        "distro_version": test.distro_version,
        "flavor": test.flavor,
        "flavor_version": test.flavor_version,
        "arch": test.arch,
        "compiler": test.compiler,
        "compiler_version": test.compiler_version,
        "isolation": test.isolation,
        "toolchain": test.toolchain,
        "opp_file": test.opp_file,
        "resolved_deps": test.resolved_deps,
        "git_ref": run.git_ref,
        "version": run.version,
        "commit_sha": run.commit_sha,
        "lifecycle": run.lifecycle.value,
        "result_code": run.result_code.value if run.result_code else None,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "duration_seconds": run.duration_seconds,
        "test_exec_seconds": run.test_exec_seconds,
        "worker_id": run.worker_id,
        "matrix_run_id": run.matrix_run_id,
        "matrix_id": matrix_run.matrix_id if matrix_run else None,
        "github_owner": matrix_run.github_owner if matrix_run else None,
        "github_repo": matrix_run.github_repo if matrix_run else None,
        "github_commit_sha": matrix_run.github_commit_sha if matrix_run else None,
        "github_pr_number": matrix_run.github_pr_number if matrix_run else None,
    }
