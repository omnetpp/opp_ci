import os
from pathlib import Path

from fastapi import FastAPI, Request, Query
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func

from opp_ci.db.connection import SessionLocal
from opp_ci.db.models import TestRun, TestRunStatus, TestResult

app = FastAPI(title="opp_ci")

templates_dir = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(templates_dir))


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    session = SessionLocal()
    try:
        recent_runs = session.execute(
            select(TestRun).order_by(TestRun.id.desc()).limit(20)
        ).scalars().all()

        total_runs = session.execute(select(func.count(TestRun.id))).scalar()
        passed = session.execute(select(func.count(TestRun.id)).where(TestRun.status == TestRunStatus.passed)).scalar()
        failed = session.execute(select(func.count(TestRun.id)).where(TestRun.status == TestRunStatus.failed)).scalar()
        errored = session.execute(select(func.count(TestRun.id)).where(TestRun.status == TestRunStatus.error)).scalar()

        return templates.TemplateResponse(request, "dashboard.html", {
            "recent_runs": recent_runs,
            "total_runs": total_runs,
            "passed": passed,
            "failed": failed,
            "errored": errored,
        })
    finally:
        session.close()


@app.get("/runs", response_class=HTMLResponse)
def runs_list(
    request: Request,
    project: str = Query(default=None),
    test_type: str = Query(default=None),
    status: str = Query(default=None),
    limit: int = Query(default=50),
):
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
        return templates.TemplateResponse(request, "runs.html", {
            "runs": runs,
            "filter_project": project or "",
            "filter_test_type": test_type or "",
            "filter_status": status or "",
        })
    finally:
        session.close()


@app.get("/results", response_class=HTMLResponse)
def results_page(
    request: Request,
    project: str = Query(default=None),
    test_type: str = Query(default=None),
    status: str = Query(default=None),
    view: str = Query(default="summary"),
    limit: int = Query(default=200),
):
    from opp_ci.web.rollup import rollup_runs

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

        summaries = rollup_runs(runs) if view == "summary" else None

        return templates.TemplateResponse(request, "results.html", {
            "runs": runs,
            "summaries": summaries,
            "view": view,
            "filter_project": project or "",
            "filter_test_type": test_type or "",
            "filter_status": status or "",
        })
    finally:
        session.close()


@app.get("/runs/{run_id}", response_class=HTMLResponse)
def run_detail(request: Request, run_id: int):
    session = SessionLocal()
    try:
        run = session.execute(
            select(TestRun).where(TestRun.id == run_id)
        ).scalar_one_or_none()
        if run is None:
            return HTMLResponse("<h1>Not found</h1>", status_code=404)

        results = session.execute(
            select(TestResult).where(TestResult.test_run_id == run_id)
        ).scalars().all()

        return templates.TemplateResponse(request, "run_detail.html", {
            "run": run,
            "results": results,
        })
    finally:
        session.close()
