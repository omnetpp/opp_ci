import os
import re
from html import escape as html_escape
from pathlib import Path

from fastapi import FastAPI, Form, Request, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from sqlalchemy import select, func

from opp_ci.db.connection import SessionLocal
from opp_ci.db.models import Project, Version, OS, Compiler, TestMatrix, AutoTestRule, TestRun, TestRunStatus, TestResult, Worker, ApiToken

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

from opp_ci.web.api import router as api_router
app.include_router(api_router)

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
        passed = session.execute(select(func.count(TestRun.id)).where(TestRun.status == TestRunStatus.PASS)).scalar()
        failed = session.execute(select(func.count(TestRun.id)).where(TestRun.status == TestRunStatus.FAIL)).scalar()
        errored = session.execute(select(func.count(TestRun.id)).where(TestRun.status == TestRunStatus.ERROR)).scalar()

        return templates.TemplateResponse(request, "dashboard.html", {
            "recent_runs": recent_runs,
            "total_runs": total_runs,
            "passed": passed,
            "failed": failed,
            "errored": errored,
        })
    finally:
        session.close()


@app.get("/queue", response_class=HTMLResponse)
def queue_page(request: Request, message: str = Query(default=None), message_type: str = Query(default=None)):
    session = SessionLocal()
    try:
        running = session.execute(
            select(TestRun).where(TestRun.status == TestRunStatus.running).order_by(TestRun.started_at.desc())
        ).scalars().all()
        queued = session.execute(
            select(TestRun).where(TestRun.status == TestRunStatus.queued).order_by(TestRun.id)
        ).scalars().all()

        return templates.TemplateResponse(request, "queue.html", {
            "running": running,
            "queued": queued,
            "message": message,
            "message_type": message_type,
        })
    finally:
        session.close()


@app.get("/runs", response_class=HTMLResponse)
def runs_list(
    request: Request,
    project: str = Query(default=None),
    test_type: str = Query(default=None),
    git_ref: str = Query(default=None),
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
        if git_ref:
            query = query.where(
                (TestRun.git_ref == git_ref) | (TestRun.commit_sha.startswith(git_ref))
            )
        if status:
            query = query.where(TestRun.status == TestRunStatus(status))

        runs = session.execute(query).scalars().all()
        return templates.TemplateResponse(request, "runs.html", {
            "runs": runs,
            "filter_project": project or "",
            "filter_test_type": test_type or "",
            "filter_git_ref": git_ref or "",
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
    cartesian_only: bool = Query(default=False),
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

        summaries = rollup_runs(runs, cartesian_only=cartesian_only) if view == "summary" else None

        return templates.TemplateResponse(request, "results.html", {
            "runs": runs,
            "summaries": summaries,
            "view": view,
            "cartesian_only": cartesian_only,
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


@app.get("/runs/new", response_class=HTMLResponse)
def run_new_form(request: Request, message: str = Query(default=None), message_type: str = Query(default=None)):
    session = SessionLocal()
    try:
        projects = session.execute(select(Project).order_by(Project.tier, Project.name)).scalars().all()
        matrices = session.execute(select(TestMatrix).order_by(TestMatrix.name)).scalars().all()
        os_entries = session.execute(select(OS).order_by(OS.name, OS.version)).scalars().all()
        compilers = session.execute(select(Compiler).order_by(Compiler.name, Compiler.version)).scalars().all()
        return templates.TemplateResponse(request, "run_new.html", {
            "projects": projects,
            "matrices": matrices,
            "os_entries": os_entries,
            "compilers": compilers,
            "message": message,
            "message_type": message_type,
        })
    finally:
        session.close()


@app.post("/runs/new")
def run_new_submit(
    request: Request,
    project: str = Form(...),
    test_type: str = Form(...),
    mode: str = Form(default=""),
    git_ref: str = Form(default=""),
    os: str = Form(default="", alias="os"),
    compiler: str = Form(default=""),
):
    import datetime
    session = SessionLocal()
    try:
        run = TestRun(
            project=project,
            test_type=test_type,
            mode=mode or None,
            git_ref=git_ref or None,
            os=os or None,
            compiler=compiler or None,
            status=TestRunStatus.queued,
            trigger="web",
            started_at=datetime.datetime.utcnow(),
        )
        session.add(run)
        session.commit()
        return RedirectResponse(url=f"/runs/{run.id}", status_code=303)
    finally:
        session.close()


@app.post("/runs/new/matrix")
def run_new_matrix(request: Request, matrix_name: str = Form(...)):
    import datetime
    from opp_ci.scheduler import expand_matrix
    from opp_ci.executor import find_existing_run

    session = SessionLocal()
    try:
        matrix = session.execute(
            select(TestMatrix).where(TestMatrix.name == matrix_name)
        ).scalar_one_or_none()
        if matrix is None:
            return RedirectResponse(
                url=f"/runs/new?message=Matrix+not+found&message_type=error",
                status_code=303,
            )

        jobs = expand_matrix(matrix.project, matrix.config)

        # Resolve GitHub info from the project record
        proj = session.execute(
            select(Project).where(Project.name == matrix.project)
        ).scalar_one_or_none()
        gh_owner = proj.github_owner if proj else None
        gh_repo = proj.github_repo if proj else None

        queued = 0
        skipped = 0
        for job in jobs:
            existing = find_existing_run(
                session,
                project=job.get("project", matrix.project),
                test_type=job.get("test_type", "smoke"),
                mode=job.get("mode"),
                git_ref=job.get("git_ref"),
                os=job.get("os"),
                os_version=job.get("os_version"),
                compiler=job.get("compiler"),
                compiler_version=job.get("compiler_version"),
            )
            if existing:
                skipped += 1
                continue

            run = TestRun(
                project=job.get("project", matrix.project),
                test_type=job.get("test_type", "smoke"),
                mode=job.get("mode"),
                git_ref=job.get("git_ref"),
                os=job.get("os"),
                os_version=job.get("os_version"),
                compiler=job.get("compiler"),
                compiler_version=job.get("compiler_version"),
                platform_desc=job.get("platform_desc"),
                resolved_deps=job.get("resolved_deps"),
                opp_file=matrix.opp_file,
                matrix_id=matrix.id,
                github_owner=gh_owner,
                github_repo=gh_repo,
                status=TestRunStatus.queued,
                trigger="web",
            )
            session.add(run)
            queued += 1
        session.commit()
        msg = f"Queued+{queued}+jobs+from+matrix+{matrix_name}"
        if skipped:
            msg += f"+(skipped+{skipped}+already+completed)"
        return RedirectResponse(
            url=f"/queue?message={msg}&message_type=success",
            status_code=303,
        )
    finally:
        session.close()


@app.post("/runs/{run_id}/rerun")
def run_rerun(run_id: int):
    import datetime
    session = SessionLocal()
    try:
        original = session.execute(
            select(TestRun).where(TestRun.id == run_id)
        ).scalar_one_or_none()
        if original is None:
            return RedirectResponse(url="/runs", status_code=303)

        new_run = TestRun(
            project=original.project,
            test_type=original.test_type,
            mode=original.mode,
            git_ref=original.git_ref,
            os=original.os,
            os_version=original.os_version,
            compiler=original.compiler,
            compiler_version=original.compiler_version,
            platform_desc=original.platform_desc,
            opp_file=original.opp_file,
            matrix_id=original.matrix_id,
            github_owner=original.github_owner,
            github_repo=original.github_repo,
            status=TestRunStatus.queued,
            trigger="rerun",
        )
        session.add(new_run)
        session.commit()
        return RedirectResponse(url=f"/runs/{new_run.id}", status_code=303)
    finally:
        session.close()


@app.post("/runs/{run_id}/cancel")
def run_cancel(run_id: int):
    import datetime
    session = SessionLocal()
    try:
        run = session.execute(
            select(TestRun).where(TestRun.id == run_id)
        ).scalar_one_or_none()
        if run and run.status in (TestRunStatus.queued, TestRunStatus.running):
            run.status = TestRunStatus.ERROR
            run.finished_at = datetime.datetime.utcnow()
            session.add(TestResult(
                test_run_id=run.id,
                result_code="CANCELLED",
                stderr="Cancelled from web UI",
            ))
            session.commit()
        return RedirectResponse(url=f"/runs/{run_id}", status_code=303)
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


@app.get("/projects", response_class=HTMLResponse)
def projects_list(request: Request):
    session = SessionLocal()
    try:
        projects = session.execute(
            select(Project).order_by(Project.tier, Project.name)
        ).scalars().all()

        # Collect run counts and last status per project
        run_counts = {}
        last_status = {}
        for p in projects:
            count = session.execute(
                select(func.count(TestRun.id)).where(TestRun.project == p.name)
            ).scalar()
            run_counts[p.name] = count
            last_run = session.execute(
                select(TestRun).where(
                    TestRun.project == p.name,
                    TestRun.status.in_([TestRunStatus.PASS, TestRunStatus.FAIL, TestRunStatus.ERROR]),
                ).order_by(TestRun.id.desc()).limit(1)
            ).scalar_one_or_none()
            last_status[p.name] = last_run.status.value if last_run else None

        return templates.TemplateResponse(request, "projects.html", {
            "projects": projects,
            "run_counts": run_counts,
            "last_status": last_status,
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
        passed = session.execute(select(func.count(TestRun.id)).where(TestRun.project == name, TestRun.status == TestRunStatus.PASS)).scalar()
        failed = session.execute(select(func.count(TestRun.id)).where(TestRun.project == name, TestRun.status == TestRunStatus.FAIL)).scalar()

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


@app.get("/commits/{project}/{sha}", response_class=HTMLResponse)
def commit_detail(request: Request, project: str, sha: str):
    session = SessionLocal()
    try:
        runs = session.execute(
            select(TestRun).where(
                TestRun.project == project,
                TestRun.commit_sha == sha,
            ).order_by(TestRun.id.desc())
        ).scalars().all()
        return templates.TemplateResponse(request, "commit_detail.html", {
            "project": project,
            "sha": sha,
            "runs": runs,
        })
    finally:
        session.close()


@app.get("/compatibility", response_class=HTMLResponse)
def compatibility_index(request: Request):
    session = SessionLocal()
    try:
        projects = session.execute(
            select(Project).where(Project.dependency_names.isnot(None)).order_by(Project.tier, Project.name)
        ).scalars().all()
        # Filter to only those with non-empty dependency lists
        projects = [p for p in projects if p.dependency_names]
        return templates.TemplateResponse(request, "compatibility_index.html", {
            "projects": projects,
        })
    finally:
        session.close()


@app.get("/compatibility/{project_name}", response_class=HTMLResponse)
def compatibility_page(request: Request, project_name: str):
    from opp_ci.compatibility import get_compatibility_matrix
    session = SessionLocal()
    try:
        matrices = get_compatibility_matrix(session, project_name)
        return templates.TemplateResponse(request, "compatibility.html", {
            "project_name": project_name,
            "matrices": matrices,
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


@app.post("/matrices/create")
def matrix_create(
    name: str = Form(...),
    project: str = Form(...),
    config_json: str = Form(default="{}"),
    ref_range_base: str = Form(default=""),
    ref_range_head: str = Form(default=""),
):
    import json
    session = SessionLocal()
    try:
        try:
            config = json.loads(config_json) if config_json.strip() else {}
        except json.JSONDecodeError:
            return RedirectResponse(
                url="/matrices?error=Invalid+JSON",
                status_code=303,
            )

        existing = session.execute(
            select(TestMatrix).where(TestMatrix.name == name)
        ).scalar_one_or_none()
        if existing:
            return RedirectResponse(
                url="/matrices?error=Matrix+already+exists",
                status_code=303,
            )

        if ref_range_base.strip() and ref_range_head.strip():
            config["ref_range"] = {"base": ref_range_base.strip(), "head": ref_range_head.strip()}

        matrix = TestMatrix(name=name, project=project, config=config)
        session.add(matrix)
        session.commit()
        return RedirectResponse(url=f"/matrices/{matrix.id}", status_code=303)
    finally:
        session.close()


@app.post("/matrices/{matrix_id}/delete")
def matrix_delete(matrix_id: int):
    session = SessionLocal()
    try:
        matrix = session.execute(
            select(TestMatrix).where(TestMatrix.id == matrix_id)
        ).scalar_one_or_none()
        if matrix:
            session.delete(matrix)
            session.commit()
        return RedirectResponse(url="/matrices", status_code=303)
    finally:
        session.close()


@app.get("/os", response_class=HTMLResponse)
def os_list(request: Request):
    session = SessionLocal()
    try:
        os_entries = session.execute(
            select(OS).order_by(OS.name, OS.version)
        ).scalars().all()

        return templates.TemplateResponse(request, "os.html", {
            "os_entries": os_entries,
        })
    finally:
        session.close()


@app.post("/os/create")
def os_create(name: str = Form(...), version: str = Form(default=""), arch: str = Form(default="x86_64")):
    session = SessionLocal()
    try:
        session.add(OS(name=name, version=version or None, arch=arch or "x86_64"))
        session.commit()
        return RedirectResponse(url="/os", status_code=303)
    finally:
        session.close()


@app.post("/os/{os_id}/delete")
def os_delete(os_id: int):
    session = SessionLocal()
    try:
        entry = session.execute(select(OS).where(OS.id == os_id)).scalar_one_or_none()
        if entry:
            session.delete(entry)
            session.commit()
        return RedirectResponse(url="/os", status_code=303)
    finally:
        session.close()


@app.get("/compilers", response_class=HTMLResponse)
def compilers_list(request: Request):
    session = SessionLocal()
    try:
        compilers = session.execute(
            select(Compiler).order_by(Compiler.name, Compiler.version)
        ).scalars().all()

        return templates.TemplateResponse(request, "compilers.html", {
            "compilers": compilers,
        })
    finally:
        session.close()


@app.post("/compilers/create")
def compiler_create(name: str = Form(...), version: str = Form(default="")):
    session = SessionLocal()
    try:
        session.add(Compiler(name=name, version=version or None))
        session.commit()
        return RedirectResponse(url="/compilers", status_code=303)
    finally:
        session.close()


@app.post("/compilers/{compiler_id}/delete")
def compiler_delete(compiler_id: int):
    session = SessionLocal()
    try:
        entry = session.execute(select(Compiler).where(Compiler.id == compiler_id)).scalar_one_or_none()
        if entry:
            session.delete(entry)
            session.commit()
        return RedirectResponse(url="/compilers", status_code=303)
    finally:
        session.close()


@app.get("/rules", response_class=HTMLResponse)
def rules_list(request: Request):
    session = SessionLocal()
    try:
        rules = session.execute(
            select(AutoTestRule).order_by(AutoTestRule.project_id, AutoTestRule.rule_type)
        ).scalars().all()

        # Resolve project names and matrix names
        rule_data = []
        for rule in rules:
            proj_name = session.execute(
                select(Project.name).where(Project.id == rule.project_id)
            ).scalar_one_or_none() or "?"
            matrix_name = None
            if rule.matrix_id:
                matrix_name = session.execute(
                    select(TestMatrix.name).where(TestMatrix.id == rule.matrix_id)
                ).scalar_one_or_none()
            rule_data.append({
                "id": rule.id,
                "project_name": proj_name,
                "project_id": rule.project_id,
                "rule_type": rule.rule_type,
                "pattern": rule.pattern,
                "matrix_id": rule.matrix_id,
                "matrix_name": matrix_name,
                "enabled": rule.enabled,
            })

        projects = session.execute(select(Project).order_by(Project.name)).scalars().all()
        matrices = session.execute(select(TestMatrix).order_by(TestMatrix.name)).scalars().all()

        return templates.TemplateResponse(request, "rules.html", {
            "rules": rule_data,
            "projects": projects,
            "matrices": matrices,
        })
    finally:
        session.close()


@app.get("/rules/{rule_id}", response_class=HTMLResponse)
def rule_detail(request: Request, rule_id: int):
    session = SessionLocal()
    try:
        rule = session.execute(
            select(AutoTestRule).where(AutoTestRule.id == rule_id)
        ).scalar_one_or_none()
        if rule is None:
            return HTMLResponse("<h1>Rule not found</h1>", status_code=404)

        proj_name = session.execute(
            select(Project.name).where(Project.id == rule.project_id)
        ).scalar_one_or_none() or "?"
        matrix_name = None
        if rule.matrix_id:
            matrix_name = session.execute(
                select(TestMatrix.name).where(TestMatrix.id == rule.matrix_id)
            ).scalar_one_or_none()

        projects = session.execute(select(Project).order_by(Project.name)).scalars().all()
        matrices = session.execute(select(TestMatrix).order_by(TestMatrix.name)).scalars().all()

        return templates.TemplateResponse(request, "rule_detail.html", {
            "rule": rule,
            "project_name": proj_name,
            "matrix_name": matrix_name,
            "projects": projects,
            "matrices": matrices,
        })
    finally:
        session.close()


@app.post("/rules/{rule_id}/edit")
def rule_edit_web(
    rule_id: int,
    rule_type: str = Form(...),
    pattern: str = Form(...),
    matrix_name: str = Form(default=""),
    enabled: int = Form(default=0),
):
    session = SessionLocal()
    try:
        rule = session.execute(
            select(AutoTestRule).where(AutoTestRule.id == rule_id)
        ).scalar_one_or_none()
        if rule is None:
            return RedirectResponse(url="/rules", status_code=303)

        rule.rule_type = rule_type
        rule.pattern = pattern
        rule.enabled = enabled

        if matrix_name:
            matrix = session.execute(
                select(TestMatrix).where(TestMatrix.name == matrix_name)
            ).scalar_one_or_none()
            rule.matrix_id = matrix.id if matrix else None
        else:
            rule.matrix_id = None

        session.commit()
        return RedirectResponse(url=f"/rules/{rule_id}", status_code=303)
    finally:
        session.close()


@app.post("/rules/create")
def rule_create_web(
    project_name: str = Form(...),
    rule_type: str = Form(...),
    pattern: str = Form(...),
    matrix_name: str = Form(default=""),
):
    session = SessionLocal()
    try:
        proj = session.execute(
            select(Project).where(Project.name == project_name)
        ).scalar_one_or_none()
        if proj is None:
            return RedirectResponse(url="/rules", status_code=303)

        matrix_id = None
        if matrix_name:
            matrix = session.execute(
                select(TestMatrix).where(TestMatrix.name == matrix_name)
            ).scalar_one_or_none()
            if matrix:
                matrix_id = matrix.id

        rule = AutoTestRule(
            project_id=proj.id,
            rule_type=rule_type,
            pattern=pattern,
            matrix_id=matrix_id,
            enabled=1,
        )
        session.add(rule)
        session.commit()
        return RedirectResponse(url="/rules", status_code=303)
    finally:
        session.close()


@app.post("/rules/{rule_id}/delete")
def rule_delete_web(rule_id: int):
    session = SessionLocal()
    try:
        rule = session.execute(
            select(AutoTestRule).where(AutoTestRule.id == rule_id)
        ).scalar_one_or_none()
        if rule:
            session.delete(rule)
            session.commit()
        return RedirectResponse(url="/rules", status_code=303)
    finally:
        session.close()


@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request):
    session = SessionLocal()
    try:
        stats = {
            "projects": session.execute(select(func.count(Project.id))).scalar(),
            "versions": session.execute(select(func.count(Version.id))).scalar(),
            "os_entries": session.execute(select(func.count(OS.id))).scalar(),
            "compilers": session.execute(select(func.count(Compiler.id))).scalar(),
            "matrices": session.execute(select(func.count(TestMatrix.id))).scalar(),
            "runs_total": session.execute(select(func.count(TestRun.id))).scalar(),
            "runs_passed": session.execute(select(func.count(TestRun.id)).where(TestRun.status == TestRunStatus.PASS)).scalar(),
            "runs_failed": session.execute(select(func.count(TestRun.id)).where(TestRun.status == TestRunStatus.FAIL)).scalar(),
            "runs_error": session.execute(select(func.count(TestRun.id)).where(TestRun.status == TestRunStatus.ERROR)).scalar(),
            "runs_running": session.execute(select(func.count(TestRun.id)).where(TestRun.status == TestRunStatus.running)).scalar(),
            "runs_queued": session.execute(select(func.count(TestRun.id)).where(TestRun.status == TestRunStatus.queued)).scalar(),
            "results": session.execute(select(func.count(TestResult.id))).scalar(),
            "workers_total": session.execute(select(func.count(Worker.id))).scalar(),
            "workers_online": session.execute(select(func.count(Worker.id)).where(Worker.status == "online")).scalar(),
            "tokens": session.execute(select(func.count(ApiToken.id))).scalar(),
        }

        workers = session.execute(
            select(Worker).order_by(Worker.name)
        ).scalars().all()

        recent_errors = session.execute(
            select(TestRun).where(TestRun.status == TestRunStatus.ERROR).order_by(TestRun.id.desc()).limit(10)
        ).scalars().all()

        tokens = session.execute(
            select(ApiToken).order_by(ApiToken.id)
        ).scalars().all()

        rules = session.execute(
            select(AutoTestRule).order_by(AutoTestRule.id)
        ).scalars().all()

        return templates.TemplateResponse(request, "admin.html", {
            "stats": stats,
            "workers": workers,
            "tokens": tokens,
            "rules": rules,
            "recent_errors": recent_errors,
        })
    finally:
        session.close()


@app.post("/admin/workers/register")
def admin_register_worker(
    name: str = Form(...),
    tags: str = Form(default=""),
    concurrency: int = Form(default=1),
):
    import secrets
    session = SessionLocal()
    try:
        existing = session.execute(
            select(Worker).where(Worker.name == name)
        ).scalar_one_or_none()
        if existing:
            return RedirectResponse(url="/admin?error=Worker+already+exists", status_code=303)

        worker = Worker(
            name=name,
            token=secrets.token_urlsafe(32),
            tags=[t.strip() for t in tags.split(",") if t.strip()] if tags else [],
            concurrency=concurrency,
            status="offline",
        )
        session.add(worker)
        session.commit()
        return RedirectResponse(url=f"/admin?worker_token={worker.token}", status_code=303)
    finally:
        session.close()


@app.post("/admin/tokens/create")
def admin_create_token(
    name: str = Form(...),
    role: str = Form(default="readonly"),
):
    import secrets
    session = SessionLocal()
    try:
        token = ApiToken(
            token=secrets.token_urlsafe(32),
            name=name,
            role=role,
        )
        session.add(token)
        session.commit()
        return RedirectResponse(url=f"/admin?new_token={token.token}", status_code=303)
    finally:
        session.close()


@app.post("/admin/tokens/{token_id}/revoke")
def admin_revoke_token(token_id: int):
    session = SessionLocal()
    try:
        token = session.execute(
            select(ApiToken).where(ApiToken.id == token_id)
        ).scalar_one_or_none()
        if token:
            token.enabled = False
            session.commit()
        return RedirectResponse(url="/admin", status_code=303)
    finally:
        session.close()


@app.post("/admin/projects/register")
def admin_register_project(
    name: str = Form(...),
    opp_env_name: str = Form(default=""),
    github_owner: str = Form(default=""),
    github_repo: str = Form(default=""),
    tier: int = Form(default=2),
):
    session = SessionLocal()
    try:
        existing = session.execute(
            select(Project).where(Project.name == name)
        ).scalar_one_or_none()
        if existing:
            return RedirectResponse(url="/admin?error=Project+already+exists", status_code=303)

        project = Project(
            name=name,
            opp_env_name=opp_env_name or None,
            github_owner=github_owner or None,
            github_repo=github_repo or None,
            tier=tier,
        )
        session.add(project)
        session.commit()
        return RedirectResponse(url="/admin", status_code=303)
    finally:
        session.close()
