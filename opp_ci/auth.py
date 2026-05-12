"""
Token-based authentication for the opp_ci REST API.

Supports four roles with increasing privilege:
    readonly   — can view results, runs, workers
    submitter  — can submit runs
    worker     — can poll for jobs, report results, send heartbeats
    admin      — full access (create tokens, register workers, etc.)

Workers authenticate with their per-worker token (stored in the Worker table).
All other API callers authenticate with an ApiToken.
"""

import datetime
import logging

from sqlalchemy import select

from opp_ci.db.connection import SessionLocal
from opp_ci.db.models import ApiToken, Worker

_logger = logging.getLogger(__name__)

ROLE_HIERARCHY = {
    "readonly": 0,
    "submitter": 1,
    "worker": 2,
    "admin": 3,
}


def verify_token(token):
    """
    Verify a bearer token and return (role, identity_dict) or (None, None).

    Checks ApiToken table first, then Worker table.
    """
    if not token:
        return None, None

    session = SessionLocal()
    try:
        # Check ApiToken table
        api_token = session.execute(
            select(ApiToken).where(ApiToken.token == token, ApiToken.enabled == True)
        ).scalar_one_or_none()
        if api_token is not None:
            return api_token.role, {"type": "api_token", "id": api_token.id, "name": api_token.name}

        # Check Worker table
        worker = session.execute(
            select(Worker).where(Worker.token == token)
        ).scalar_one_or_none()
        if worker is not None:
            return "worker", {"type": "worker", "id": worker.id, "name": worker.name}

        return None, None
    finally:
        session.close()


def require_role(minimum_role):
    """
    FastAPI dependency that checks the Authorization header for a bearer token
    with at least the given role.

    Usage in a route:
        @router.post("/api/runs", dependencies=[Depends(require_role("submitter"))])
    """
    from fastapi import Header, HTTPException

    async def _check(authorization: str = Header(default="")):
        token = _extract_bearer(authorization)
        role, identity = verify_token(token)
        if role is None:
            raise HTTPException(status_code=401, detail="Invalid or missing API token")
        if ROLE_HIERARCHY.get(role, -1) < ROLE_HIERARCHY.get(minimum_role, 99):
            raise HTTPException(status_code=403, detail=f"Requires role '{minimum_role}', got '{role}'")
        return identity

    return _check


def require_worker_token():
    """
    FastAPI dependency for worker endpoints.
    Returns the Worker object identified by the bearer token.
    """
    from fastapi import Header, HTTPException

    async def _check(authorization: str = Header(default="")):
        token = _extract_bearer(authorization)
        if not token:
            raise HTTPException(status_code=401, detail="Missing worker token")

        session = SessionLocal()
        try:
            worker = session.execute(
                select(Worker).where(Worker.token == token)
            ).scalar_one_or_none()
            if worker is None:
                raise HTTPException(status_code=401, detail="Invalid worker token")
            # Update heartbeat
            worker.last_heartbeat = datetime.datetime.utcnow()
            if worker.status == "offline":
                worker.status = "online"
                # Re-queue any runs that were left in running state
                from opp_ci.db.models import TestRun, TestRunStatus
                orphans = session.execute(
                    select(TestRun).where(
                        TestRun.worker_id == worker.id,
                        TestRun.status == TestRunStatus.running,
                    )
                ).scalars().all()
                for run in orphans:
                    run.status = TestRunStatus.queued
                    run.worker_id = None
                    run.started_at = None
                worker.current_job_count = 0
            session.commit()
            return {"worker_id": worker.id, "worker_name": worker.name}
        finally:
            session.close()

    return _check


def _extract_bearer(authorization):
    """Extract token from 'Bearer <token>' header value."""
    if not authorization:
        return None
    parts = authorization.split()
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1]
    # Also accept bare token for convenience
    if len(parts) == 1:
        return parts[0]
    return None
