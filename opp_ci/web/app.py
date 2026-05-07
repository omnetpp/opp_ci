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
from opp_ci.db.models import Project, Version, Platform, TestMatrix, TestRun, TestRunStatus, TestResult

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


@app.get("/compare", response_class=HTMLResponse)
def compare_page(
    request: Request,
    run_a: int = Query(default=None, description="First run ID"),
    run_b: int = Query(default=None, description="Second run ID"),
    project: str = Query(default=None),
    ref_a: str = Query(default=None, description="Git ref for side A"),
    ref_b: str = Query(default=None, description="Git ref for side B"),
    test_type: str = Query(default=None),
):
    """Compare two runs or two refs side-by-side."""
    session = SessionLocal()
    try:
        left_runs = []
        right_runs = []
        left_label = ""
        right_label = ""
        left_results = []
        right_results = []

        if run_a and run_b:
            # Compare two specific runs
            left_run = session.execute(select(TestRun).where(TestRun.id == run_a)).scalar_one_or_none()
            right_run = session.execute(select(TestRun).where(TestRun.id == run_b)).scalar_one_or_none()
            if left_run:
                left_runs = [left_run]
                left_label = f"Run #{left_run.id} ({left_run.project}{'@' + left_run.git_ref if left_run.git_ref else ''})"
                left_results = session.execute(
                    select(TestResult).where(TestResult.test_run_id == left_run.id)
                ).scalars().all()
            if right_run:
                right_runs = [right_run]
                right_label = f"Run #{right_run.id} ({right_run.project}{'@' + right_run.git_ref if right_run.git_ref else ''})"
                right_results = session.execute(
                    select(TestResult).where(TestResult.test_run_id == right_run.id)
                ).scalars().all()

        elif project and ref_a and ref_b:
            # Compare by branch/ref — find most recent runs for each ref
            query_base = select(TestRun).where(TestRun.project == project).order_by(TestRun.id.desc())
            if test_type:
                query_base = query_base.where(TestRun.test_type == test_type)

            left_runs = session.execute(
                query_base.where(TestRun.git_ref == ref_a).limit(20)
            ).scalars().all()
            right_runs = session.execute(
                query_base.where(TestRun.git_ref == ref_b).limit(20)
            ).scalars().all()
            left_label = f"{project}@{ref_a}"
            right_label = f"{project}@{ref_b}"

            if left_runs:
                left_results = session.execute(
                    select(TestResult).where(TestResult.test_run_id.in_([r.id for r in left_runs]))
                ).scalars().all()
            if right_runs:
                right_results = session.execute(
                    select(TestResult).where(TestResult.test_run_id.in_([r.id for r in right_runs]))
                ).scalars().all()

        # Build comparison data: match tests by name/type
        diff = _build_comparison_diff(left_runs, left_results, right_runs, right_results)

        return templates.TemplateResponse(request, "compare.html", {
            "left_label": left_label,
            "right_label": right_label,
            "left_runs": left_runs,
            "right_runs": right_runs,
            "diff": diff,
            "run_a": run_a or "",
            "run_b": run_b or "",
            "filter_project": project or "",
            "ref_a": ref_a or "",
            "ref_b": ref_b or "",
            "filter_test_type": test_type or "",
        })
    finally:
        session.close()


def _build_comparison_diff(left_runs, left_results, right_runs, right_results):
    """
    Build a list of comparison rows.

    Each row: {test_key, left_status, right_status, changed, left_duration, right_duration}
    """
    def _result_key(result):
        # Use test_run's test_type + result parameters as key
        return result.result_code  # fallback

    def _results_by_run(runs, results):
        """Map run_id → results list."""
        by_run = {}
        for r in results:
            by_run.setdefault(r.test_run_id, []).append(r)
        return by_run

    # For simple two-run comparison
    if len(left_runs) == 1 and len(right_runs) == 1:
        left_detail = left_results[0].details if left_results else None
        right_detail = right_results[0].details if right_results else None

        left_tests = {}
        right_tests = {}

        if left_detail and isinstance(left_detail, dict) and "results" in left_detail:
            for t in left_detail["results"]:
                key = t.get("parameters") or t.get("working_directory", "")
                left_tests[key] = t
        if right_detail and isinstance(right_detail, dict) and "results" in right_detail:
            for t in right_detail["results"]:
                key = t.get("parameters") or t.get("working_directory", "")
                right_tests[key] = t

        all_keys = list(dict.fromkeys(list(left_tests.keys()) + list(right_tests.keys())))

        rows = []
        for key in all_keys:
            lt = left_tests.get(key)
            rt = right_tests.get(key)
            left_status = lt.get("result", "-") if lt else "-"
            right_status = rt.get("result", "-") if rt else "-"
            left_dur = lt.get("elapsed_wall_time") if lt else None
            right_dur = rt.get("elapsed_wall_time") if rt else None
            rows.append({
                "test_key": key or "(unnamed)",
                "left_status": left_status,
                "right_status": right_status,
                "changed": left_status != right_status,
                "left_duration": f"{left_dur:.3f}s" if left_dur else "-",
                "right_duration": f"{right_dur:.3f}s" if right_dur else "-",
            })
        return rows

    # For multi-run (branch) comparison: compare by test_type
    left_by_type = {}
    right_by_type = {}
    for r in left_runs:
        left_by_type.setdefault(r.test_type, []).append(r)
    for r in right_runs:
        right_by_type.setdefault(r.test_type, []).append(r)

    all_types = list(dict.fromkeys(list(left_by_type.keys()) + list(right_by_type.keys())))
    rows = []
    for tt in all_types:
        l_runs = left_by_type.get(tt, [])
        r_runs = right_by_type.get(tt, [])
        l_statuses = set(r.status.value for r in l_runs)
        r_statuses = set(r.status.value for r in r_runs)
        left_status = l_statuses.pop() if len(l_statuses) == 1 else "/".join(sorted(l_statuses)) if l_statuses else "-"
        right_status = r_statuses.pop() if len(r_statuses) == 1 else "/".join(sorted(r_statuses)) if r_statuses else "-"
        rows.append({
            "test_key": tt,
            "left_status": left_status,
            "right_status": right_status,
            "changed": left_status != right_status,
            "left_duration": "-",
            "right_duration": "-",
        })
    return rows


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


@app.get("/projects", response_class=HTMLResponse)
def projects_list(request: Request):
    session = SessionLocal()
    try:
        projects = session.execute(
            select(Project).order_by(Project.tier, Project.name)
        ).scalars().all()

        # Collect run counts per project
        run_counts = {}
        for p in projects:
            count = session.execute(
                select(func.count(TestRun.id)).where(TestRun.project == p.name)
            ).scalar()
            run_counts[p.name] = count

        return templates.TemplateResponse(request, "projects.html", {
            "projects": projects,
            "run_counts": run_counts,
        })
    finally:
        session.close()


@app.get("/projects/{name}", response_class=HTMLResponse)
def project_detail(request: Request, name: str):
    session = SessionLocal()
    try:
        project = session.execute(
            select(Project).where(Project.name == name)
        ).scalar_one_or_none()
        if project is None:
            return HTMLResponse("<h1>Project not found</h1>", status_code=404)

        versions = session.execute(
            select(Version).where(Version.project_id == project.id)
        ).scalars().all()

        recent_runs = session.execute(
            select(TestRun).where(TestRun.project == name).order_by(TestRun.id.desc()).limit(30)
        ).scalars().all()

        # Stats
        total = session.execute(select(func.count(TestRun.id)).where(TestRun.project == name)).scalar()
        passed = session.execute(select(func.count(TestRun.id)).where(TestRun.project == name, TestRun.status == TestRunStatus.passed)).scalar()
        failed = session.execute(select(func.count(TestRun.id)).where(TestRun.project == name, TestRun.status == TestRunStatus.failed)).scalar()

        return templates.TemplateResponse(request, "project_detail.html", {
            "project": project,
            "versions": versions,
            "recent_runs": recent_runs,
            "total": total,
            "passed": passed,
            "failed": failed,
        })
    finally:
        session.close()


@app.get("/matrices", response_class=HTMLResponse)
def matrices_list(request: Request):
    session = SessionLocal()
    try:
        matrices = session.execute(
            select(TestMatrix).order_by(TestMatrix.id)
        ).scalars().all()

        # Count runs per matrix
        run_counts = {}
        for m in matrices:
            count = session.execute(
                select(func.count(TestRun.id)).where(TestRun.matrix_id == m.id)
            ).scalar()
            run_counts[m.id] = count

        return templates.TemplateResponse(request, "matrices.html", {
            "matrices": matrices,
            "run_counts": run_counts,
        })
    finally:
        session.close()


@app.get("/matrices/{matrix_id}", response_class=HTMLResponse)
def matrix_detail(request: Request, matrix_id: int):
    from opp_ci.scheduler import expand_matrix

    session = SessionLocal()
    try:
        matrix = session.execute(
            select(TestMatrix).where(TestMatrix.id == matrix_id)
        ).scalar_one_or_none()
        if matrix is None:
            return HTMLResponse("<h1>Matrix not found</h1>", status_code=404)

        jobs = expand_matrix(matrix.project, matrix.config)

        recent_runs = session.execute(
            select(TestRun).where(TestRun.matrix_id == matrix_id).order_by(TestRun.id.desc()).limit(50)
        ).scalars().all()

        return templates.TemplateResponse(request, "matrix_detail.html", {
            "matrix": matrix,
            "jobs": jobs,
            "recent_runs": recent_runs,
        })
    finally:
        session.close()


@app.get("/platforms", response_class=HTMLResponse)
def platforms_list(request: Request):
    session = SessionLocal()
    try:
        platforms = session.execute(
            select(Platform).order_by(Platform.os_type, Platform.os_version)
        ).scalars().all()

        return templates.TemplateResponse(request, "platforms.html", {
            "platforms": platforms,
        })
    finally:
        session.close()


@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request):
    session = SessionLocal()
    try:
        stats = {
            "projects": session.execute(select(func.count(Project.id))).scalar(),
            "versions": session.execute(select(func.count(Version.id))).scalar(),
            "platforms": session.execute(select(func.count(Platform.id))).scalar(),
            "matrices": session.execute(select(func.count(TestMatrix.id))).scalar(),
            "runs_total": session.execute(select(func.count(TestRun.id))).scalar(),
            "runs_passed": session.execute(select(func.count(TestRun.id)).where(TestRun.status == TestRunStatus.passed)).scalar(),
            "runs_failed": session.execute(select(func.count(TestRun.id)).where(TestRun.status == TestRunStatus.failed)).scalar(),
            "runs_error": session.execute(select(func.count(TestRun.id)).where(TestRun.status == TestRunStatus.error)).scalar(),
            "runs_running": session.execute(select(func.count(TestRun.id)).where(TestRun.status == TestRunStatus.running)).scalar(),
            "results": session.execute(select(func.count(TestResult.id))).scalar(),
        }

        recent_errors = session.execute(
            select(TestRun).where(TestRun.status == TestRunStatus.error).order_by(TestRun.id.desc()).limit(10)
        ).scalars().all()

        return templates.TemplateResponse(request, "admin.html", {
            "stats": stats,
            "recent_errors": recent_errors,
        })
    finally:
        session.close()
