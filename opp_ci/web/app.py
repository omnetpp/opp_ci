import os
import re
from html import escape as html_escape
from pathlib import Path

from fastapi import FastAPI, Request, Query
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from sqlalchemy import select, func

from opp_ci.db.connection import SessionLocal
from opp_ci.db.models import TestRun, TestRunStatus, TestResult

_ANSI_RE = re.compile(r'\x1b\[([0-9;]*)m')

_ANSI_COLORS = {
    "30": "#000", "31": "#c00", "32": "#0a0", "33": "#aa0",
    "34": "#00a", "35": "#a0a", "36": "#0aa", "37": "#aaa",
    "90": "#555", "91": "#f55", "92": "#5f5", "93": "#ff5",
    "94": "#55f", "95": "#f5f", "96": "#5ff", "97": "#fff",
}


def _ansi_to_html(text):
    """Convert ANSI escape codes in text to HTML spans."""
    if not text:
        return ""
    result = []
    pos = 0
    open_span = False
    for m in _ANSI_RE.finditer(text):
        result.append(html_escape(text[pos:m.start()]))
        pos = m.end()
        codes = m.group(1).split(";") if m.group(1) else ["0"]
        if open_span:
            result.append("</span>")
            open_span = False
        if codes == ["0"] or codes == [""]:
            pass
        else:
            effective = [c for c in codes if c != "0" and c != ""]
            if effective:
                style = _resolve_ansi_style(effective)
                if style:
                    result.append(f'<span style="{style}">')
                    open_span = True
    result.append(html_escape(text[pos:]))
    if open_span:
        result.append("</span>")
    return Markup("".join(result))


def _resolve_ansi_style(codes):
    parts = []
    i = 0
    while i < len(codes):
        c = codes[i]
        if c in _ANSI_COLORS:
            parts.append(f"color:{_ANSI_COLORS[c]}")
        elif c == "1":
            parts.append("font-weight:bold")
        elif c == "38" and i + 4 < len(codes) and codes[i+1] == "2":
            r, g, b = codes[i+2], codes[i+3], codes[i+4]
            parts.append(f"color:rgb({r},{g},{b})")
            i += 4
        i += 1
    return ";".join(parts)


app = FastAPI(title="opp_ci")

templates_dir = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(templates_dir))
templates.env.filters["ansi_to_html"] = _ansi_to_html


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
    mode: str = Query(default=None),
    os: str = Query(default=None, alias="os"),
    os_version: str = Query(default=None),
    compiler: str = Query(default=None),
    compiler_version: str = Query(default=None),
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
        if mode:
            query = query.where(TestRun.mode == mode)
        if os:
            query = query.where(TestRun.os == os)
        if os_version:
            query = query.where(TestRun.os_version == os_version)
        if compiler:
            query = query.where(TestRun.compiler == compiler)
        if compiler_version:
            query = query.where(TestRun.compiler_version == compiler_version)
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
            "filter_mode": mode or "",
            "filter_os": os or "",
            "filter_os_version": os_version or "",
            "filter_compiler": compiler or "",
            "filter_compiler_version": compiler_version or "",
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
