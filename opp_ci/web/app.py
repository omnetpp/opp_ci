import datetime
import os
import re
from html import escape as html_escape
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, FastAPI, Form, Request, Query
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from sqlalchemy import and_, exists, false, func, select
from sqlalchemy.orm import aliased, selectinload
from starlette.middleware.sessions import SessionMiddleware

from opp_ci import config as cfg
from opp_ci.auth import get_csrf_token, require_csrf, require_user
from opp_ci.db.connection import SessionLocal
from opp_ci.db.models import (
    ApiToken, AutoTestRule, Compiler, ExpectedTestResult, OS, Project, Test,
    TestMatrix, TestMatrixRun, TestResultCode, TestRun, TestRunLifecycle,
    TestVerdict, TestVerdictKind, User, Version, Worker,
)
from opp_ci.persistence import (
    CannotDeleteRunningRun, create_matrix_from_axes, create_matrix_run,
    create_test_run, delete_matrix_run, delete_test_run, enqueue_job,
    get_current_expectation, get_matrix_by_name, get_or_create_test,
    get_test_by_name, insert_expectation, set_matrix_name, set_test_name,
    status_filter,
)

_ANSI_RE = re.compile(r'\x1b\[([0-9;]*)m')

_ANSI_COLORS = {
    "30": "#000", "31": "#c00", "32": "#0a0", "33": "#aa0",
    "34": "#00a", "35": "#a0a", "36": "#0aa", "37": "#aaa",
    "90": "#555", "91": "#f55", "92": "#5f5", "93": "#ff5",
    "94": "#55f", "95": "#f5f", "96": "#5ff", "97": "#fff",
}


# Suggested values for the arch axis. omnetpp itself supports amd64 and
# aarch64; the OS table may register additional/alternative names (e.g.
# x86_64). _arch_suggestions() merges both so users see what is configured
# in their own deployment too.
_DEFAULT_ARCH_SUGGESTIONS = ("amd64", "aarch64")


def _arch_suggestions(os_entries):
    """Return a sorted, de-duplicated list of arch values for datalist hints."""
    values = set(_DEFAULT_ARCH_SUGGESTIONS)
    for entry in os_entries or ():
        if entry.arch:
            values.add(entry.arch)
    return sorted(values)


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

# Session middleware signs cookies with OPP_CI_SESSION_SECRET. We fail
# closed if it's unset: a random per-process secret would silently log
# everyone out on every restart and let anyone forge cookies on a
# misconfigured deploy.
if not cfg.SESSION_SECRET:
    raise RuntimeError(
        "OPP_CI_SESSION_SECRET is required for `opp_ci serve`. "
        "Generate one with `python -c 'import secrets; print(secrets.token_urlsafe(32))'` "
        "and set it in /etc/opp_ci/serve.env or the environment."
    )
if cfg.GITHUB_OAUTH_CLIENT_ID and not cfg.PUBLIC_URL:
    # Behind a reverse proxy, deriving the callback URL from request
    # headers is brittle. Demand an explicit value.
    raise RuntimeError(
        "OPP_CI_GITHUB_OAUTH_CLIENT_ID is set but OPP_CI_PUBLIC_URL is empty. "
        "Set OPP_CI_PUBLIC_URL to the base URL the browser sees (e.g. "
        "https://opp-ci.example.com) so the OAuth callback URL is stable."
    )
app.add_middleware(
    SessionMiddleware,
    secret_key=cfg.SESSION_SECRET,
    same_site="lax",
    https_only=cfg.SESSION_COOKIE_SECURE,
    session_cookie="opp_ci_session",
)

from opp_ci.web.api import router as api_router
from opp_ci.web.login import router as login_router
app.include_router(api_router)
app.include_router(login_router)


_FAVICON_SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">'
    b'<rect width="16" height="16" rx="3" fill="#1f6feb"/>'
    b'<text x="8" y="12" font-family="monospace" font-size="10" '
    b'font-weight="bold" text-anchor="middle" fill="#fff">CI</text>'
    b'</svg>'
)


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(content=_FAVICON_SVG, media_type="image/svg+xml")

templates_dir = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(templates_dir))
templates.env.filters["ansi_to_html"] = _ansi_to_html


def _template_globals(request, current_user):
    """Common context for gated HTML templates."""
    return {
        "current_user": current_user,
        "csrf_token": get_csrf_token(request),
    }


# Every route on `web_router` requires a logged-in user. Routes that need
# `submitter` or `admin` add a stricter `require_user(...)` dependency
# locally. POST routes additionally depend on `require_csrf`.
web_router = APIRouter(dependencies=[Depends(require_user())])


@web_router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, current_user: User = Depends(require_user())):
    session = SessionLocal()
    try:
        recent_runs = session.execute(
            select(TestRun).order_by(TestRun.id.desc()).limit(20)
        ).scalars().all()

        total_runs = session.execute(select(func.count(TestRun.id))).scalar()
        passed = session.execute(
            select(func.count(TestRun.id)).where(TestRun.result_code == TestResultCode.PASS)
        ).scalar()
        failed = session.execute(
            select(func.count(TestRun.id)).where(TestRun.result_code == TestResultCode.FAIL)
        ).scalar()
        errored = session.execute(
            select(func.count(TestRun.id)).where(TestRun.result_code == TestResultCode.ERROR)
        ).scalar()

        return templates.TemplateResponse(request, "dashboard.html", {
            "recent_runs": recent_runs,
            "total_runs": total_runs,
            "passed": passed,
            "failed": failed,
            "errored": errored,
            **_template_globals(request, current_user),
        })
    finally:
        session.close()


@web_router.get("/queue", response_class=HTMLResponse)
def queue_page(request: Request, current_user: User = Depends(require_user()),
               message: str = Query(default=None), message_type: str = Query(default=None)):
    session = SessionLocal()
    try:
        running = session.execute(
            select(TestRun)
            .where(TestRun.lifecycle == TestRunLifecycle.running)
            .order_by(TestRun.started_at.desc())
        ).scalars().all()
        queued = session.execute(
            select(TestRun)
            .where(TestRun.lifecycle == TestRunLifecycle.queued)
            .order_by(TestRun.id)
        ).scalars().all()

        return templates.TemplateResponse(request, "queue.html", {
            "running": running,
            "queued": queued,
            "message": message,
            "message_type": message_type,
            **_template_globals(request, current_user),
        })
    finally:
        session.close()


def _distinct_options(session, *columns):
    """Sorted distinct non-empty values for each given model column, keyed by
    the column's attribute name. Powers the filter dropdowns so every value
    actually present in the data is offered as an explicit choice."""
    opts = {}
    for col in columns:
        vals = session.execute(
            select(col).distinct().where(col.isnot(None)).order_by(col)
        ).scalars().all()
        opts[col.key] = [v for v in vals if v not in (None, "")]
    return opts


# Status vocabulary shared by the run filters (lifecycle + outcome values,
# matching opp_ci.persistence.status_filter).
_RUN_STATUS_OPTIONS = [
    "PASS", "FAIL", "ERROR", "SKIPPED",
    "queued", "running", "finished", "cancelled", "timed_out",
]


@web_router.get("/test-runs", response_class=HTMLResponse)
def runs_list(
    request: Request,
    current_user: User = Depends(require_user()),
    project: str = Query(default=None),
    kind: str = Query(default=None),
    mode: str = Query(default=None),
    os: str = Query(default=None, alias="os"),
    distro: str = Query(default=None),
    compiler: str = Query(default=None),
    git_ref: str = Query(default=None),
    version: str = Query(default=None),
    worker: str = Query(default=None),
    status: str = Query(default=None),
    limit: int = Query(default=50),
):
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
        if mode:
            query = query.where(Test.mode == mode)
        if os:
            query = query.where(Test.os == os)
        if distro:
            query = query.where(Test.distro == distro)
        if compiler:
            query = query.where(Test.compiler == compiler)
        if git_ref:
            query = query.where(
                (TestRun.git_ref == git_ref) | (TestRun.commit_sha.startswith(git_ref))
            )
        if version:
            query = query.where(TestRun.version == version)
        if worker and worker.isdigit():
            query = query.where(TestRun.worker_id == int(worker))
        if status:
            try:
                query = status_filter(query, status)
            except ValueError:
                # Forgiving on URL params: a bad ?status= just shows no rows
                # and the user corrects the filter.
                query = query.where(false())

        runs = session.execute(query).scalars().all()
        options = _distinct_options(
            session, Test.project, Test.kind, Test.mode, Test.os, Test.distro,
            Test.compiler, TestRun.version,
        )
        options["status"] = _RUN_STATUS_OPTIONS
        workers = session.execute(select(Worker).order_by(Worker.name)).scalars().all()
        return templates.TemplateResponse(request, "runs.html", {
            "runs": runs,
            "options": options,
            "workers": workers,
            "filters": {
                "project": project or "", "kind": kind or "", "mode": mode or "",
                "os": os or "", "distro": distro or "", "compiler": compiler or "",
                "git_ref": git_ref or "", "version": version or "",
                "worker": worker or "", "status": status or "",
            },
            **_template_globals(request, current_user),
        })
    finally:
        session.close()


@web_router.get("/results", response_class=HTMLResponse)
def results_page(
    request: Request,
    current_user: User = Depends(require_user()),
    project: str = Query(default=None),
    kind: str = Query(default=None),
    mode: str = Query(default=None),
    os: str = Query(default=None, alias="os"),
    os_version: str = Query(default=None),
    distro: str = Query(default=None),
    distro_version: str = Query(default=None),
    flavor: str = Query(default=None),
    flavor_version: str = Query(default=None),
    compiler: str = Query(default=None),
    compiler_version: str = Query(default=None),
    status: str = Query(default=None),
    run_ids: str = Query(default=None),
    view: str = Query(default="summary"),
    grouping: str = Query(default="any"),
    hide_obsolete: bool = Query(default=False),
    limit: int = Query(default=200),
):
    from opp_ci.web.rollup import rollup_runs, visible_extra_dims

    session = SessionLocal()
    try:
        query = (
            select(TestRun)
            .join(Test, TestRun.test_id == Test.id)
            .options(selectinload(TestRun.verdicts))
            .order_by(TestRun.id.desc())
            .limit(limit)
        )
        if run_ids:
            ids = [int(x) for x in run_ids.split(",") if x.strip().isdigit()]
            query = query.where(TestRun.id.in_(ids))
        if project:
            query = query.where(Test.project == project)
        if kind:
            query = query.where(Test.kind == kind)
        if mode:
            query = query.where(Test.mode == mode)
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
        if compiler:
            query = query.where(Test.compiler == compiler)
        if compiler_version:
            query = query.where(Test.compiler_version == compiler_version)
        if status:
            try:
                query = status_filter(query, status)
            except ValueError:
                # Forgiving on URL params: a bad ?status= just shows no rows
                # and the user corrects the filter.
                query = query.where(false())
        if hide_obsolete:
            # Drop runs overridden by a newer finished run at the same
            # (test_id, commit_sha). is_not_distinct_from makes NULL
            # commit_shas (legacy rows) compare equal to each other.
            newer = aliased(TestRun)
            query = query.where(~exists().where(and_(
                newer.test_id == TestRun.test_id,
                newer.commit_sha.is_not_distinct_from(TestRun.commit_sha),
                newer.lifecycle == TestRunLifecycle.finished,
                newer.id > TestRun.id,
            )))

        runs = session.execute(query).scalars().all()

        summaries = rollup_runs(runs, grouping=grouping) if view == "summary" else None
        extra_dims = visible_extra_dims(summaries) if summaries else []

        return templates.TemplateResponse(request, "results.html", {
            "runs": runs,
            "summaries": summaries,
            "extra_dims": extra_dims,
            "view": view,
            "grouping": grouping,
            "hide_obsolete": hide_obsolete,
            "filter_project": project or "",
            "filter_kind": kind or "",
            "filter_mode": mode or "",
            "filter_os": os or "",
            "filter_os_version": os_version or "",
            "filter_distro": distro or "",
            "filter_distro_version": distro_version or "",
            "filter_flavor": flavor or "",
            "filter_flavor_version": flavor_version or "",
            "filter_compiler": compiler or "",
            "filter_compiler_version": compiler_version or "",
            "filter_status": status or "",
            **_template_globals(request, current_user),
        })
    finally:
        session.close()


def _test_form_context(session):
    """Shared template context for the single-test coordinate form
    (project list, datalist suggestions, per-project versions with the
    omnetpp-compat hint). Used by `test_new_form`."""
    from opp_ci import platforms
    projects = session.execute(select(Project).order_by(Project.name)).scalars().all()
    os_entries = session.execute(select(OS).order_by(OS.name, OS.version)).scalars().all()
    compilers = session.execute(select(Compiler).order_by(Compiler.name, Compiler.version)).scalars().all()

    project_by_id = {p.id: p.name for p in projects}
    versions_by_project = {p.name: [] for p in projects}
    for v in session.execute(select(Version)).scalars().all():
        pname = project_by_id.get(v.project_id)
        if pname is None:
            continue
        deps = v.resolved_dependencies or {}
        omnetpp_dep = deps.get("omnetpp") if isinstance(deps, dict) else None
        if isinstance(omnetpp_dep, str):
            omnetpp_compat = [omnetpp_dep]
        elif isinstance(omnetpp_dep, list):
            omnetpp_compat = list(omnetpp_dep)
        else:
            omnetpp_compat = []
        versions_by_project[pname].append({
            "opp_env_version": v.opp_env_version or "",
            "git_ref": v.git_ref or "",
            "label": v.label or v.opp_env_version or v.git_ref or "",
            "omnetpp_compat": omnetpp_compat,
        })
    for pname in versions_by_project:
        versions_by_project[pname].sort(key=lambda d: d["label"])

    omnetpp_versions = sorted({
        v["opp_env_version"] for v in versions_by_project.get("omnetpp", []) if v["opp_env_version"]
    })

    return {
        "projects": projects,
        "os_entries": os_entries,
        "compilers": compilers,
        "os_suggestions": list(platforms.OS_NAMES),
        "os_version_suggestions": sorted({o.version for o in os_entries if o.version}),
        "distro_suggestions": sorted({platforms.display_name(n) for n in platforms.DISTROS}),
        "flavor_suggestions": sorted({platforms.display_name(n) for n in platforms.FLAVORS}),
        "arch_suggestions": _arch_suggestions(os_entries),
        "compiler_suggestions": sorted({c.name for c in compilers if c.name}),
        "compiler_version_suggestions": sorted({c.version for c in compilers if c.version}),
        "versions_by_project": versions_by_project,
        "omnetpp_versions": omnetpp_versions,
    }


@web_router.get("/tests", response_class=HTMLResponse)
def tests_list(
    request: Request,
    current_user: User = Depends(require_user()),
    name: str = Query(default=None),
    project: str = Query(default=None),
    kind: str = Query(default=None),
    mode: str = Query(default=None),
    os: str = Query(default=None, alias="os"),
    os_version: str = Query(default=None),
    distro: str = Query(default=None),
    distro_version: str = Query(default=None),
    flavor: str = Query(default=None),
    flavor_version: str = Query(default=None),
    arch: str = Query(default=None),
    compiler: str = Query(default=None),
    compiler_version: str = Query(default=None),
    isolation: str = Query(default=None),
    toolchain: str = Query(default=None),
    status: str = Query(default=None),
    include_anonymous: bool = Query(default=False),
    limit: int = Query(default=200),
):
    """Catalog of Test definitions. Named tests only by default; the
    anonymous matrix-cell tests are pulled in with ?include_anonymous=1."""
    session = SessionLocal()
    try:
        # Named first (name NULL sorts last), then by name / newest id.
        query = select(Test).order_by(Test.name.is_(None), Test.name, Test.id.desc())
        if not include_anonymous:
            query = query.where(Test.name.isnot(None))
        if name:
            query = query.where(Test.name.ilike(f"%{name}%"))
        if project:
            query = query.where(Test.project == project)
        if kind:
            query = query.where(Test.kind == kind)
        if mode:
            query = query.where(Test.mode == mode)
        if os:
            query = query.where(Test.os == os)
        if os_version:
            query = query.where(Test.os_version == os_version)
        if distro:
            query = query.where(Test.distro == distro)
        if distro_version:
            query = query.where(Test.distro_version == distro_version)
        if flavor:
            query = query.where(Test.flavor == flavor)
        if flavor_version:
            query = query.where(Test.flavor_version == flavor_version)
        if arch:
            query = query.where(Test.arch == arch)
        if compiler:
            query = query.where(Test.compiler == compiler)
        if compiler_version:
            query = query.where(Test.compiler_version == compiler_version)
        if isolation:
            query = query.where(Test.isolation == isolation)
        if toolchain:
            query = query.where(Test.toolchain == toolchain)
        tests = session.execute(query.limit(limit)).scalars().all()

        # Last-run status per test (N+1, mirrors projects_list; the page is
        # capped by `limit`). The status filter is applied on this value.
        last_status = {}
        run_counts = {}
        for t in tests:
            last_run = session.execute(
                select(TestRun).where(TestRun.test_id == t.id)
                .order_by(TestRun.id.desc()).limit(1)
            ).scalar_one_or_none()
            last_status[t.id] = last_run.effective_status if last_run else None
            run_counts[t.id] = session.execute(
                select(func.count(TestRun.id)).where(TestRun.test_id == t.id)
            ).scalar()
        if status:
            tests = [t for t in tests if last_status.get(t.id) == status]

        options = _distinct_options(
            session, Test.project, Test.kind, Test.mode, Test.os, Test.os_version,
            Test.distro, Test.distro_version, Test.flavor, Test.flavor_version,
            Test.arch, Test.compiler, Test.compiler_version, Test.isolation,
            Test.toolchain,
        )
        options["status"] = [
            "PASS", "FAIL", "ERROR", "SKIPPED",
            "queued", "running", "cancelled", "timed_out",
        ]
        return templates.TemplateResponse(request, "tests.html", {
            "tests": tests,
            "last_status": last_status,
            "run_counts": run_counts,
            "options": options,
            "filters": {
                "name": name or "", "project": project or "", "kind": kind or "",
                "mode": mode or "", "os": os or "", "os_version": os_version or "",
                "distro": distro or "", "distro_version": distro_version or "",
                "flavor": flavor or "", "flavor_version": flavor_version or "",
                "arch": arch or "", "compiler": compiler or "",
                "compiler_version": compiler_version or "", "isolation": isolation or "",
                "toolchain": toolchain or "", "status": status or "",
            },
            "include_anonymous": include_anonymous,
            **_template_globals(request, current_user),
        })
    finally:
        session.close()


@web_router.get("/tests/new", response_class=HTMLResponse)
def test_new_form(request: Request,
                  current_user: User = Depends(require_user("submitter")),
                  message: str = Query(default=None), message_type: str = Query(default=None)):
    session = SessionLocal()
    try:
        return templates.TemplateResponse(request, "test_new.html", {
            **_test_form_context(session),
            "message": message,
            "message_type": message_type,
            **_template_globals(request, current_user),
        })
    finally:
        session.close()


@web_router.post("/tests/new", dependencies=[Depends(require_csrf)])
def test_new_submit(
    request: Request,
    current_user: User = Depends(require_user("submitter")),
    action: str = Form(default="save"),
    project: str = Form(...),
    kind: str = Form(...),
    name: str = Form(default=""),
    mode: str = Form(default=""),
    git_ref: str = Form(default=""),
    version: str = Form(default=""),
    omnetpp_version: str = Form(default=""),
    os: str = Form(default="", alias="os"),
    os_version: str = Form(default=""),
    distro: str = Form(default=""),
    distro_version: str = Form(default=""),
    flavor: str = Form(default=""),
    flavor_version: str = Form(default=""),
    arch: str = Form(default=""),
    compiler: str = Form(default=""),
    compiler_version: str = Form(default=""),
    isolation: str = Form(default="none"),
    toolchain: str = Form(default="none"),
):
    """Save a Test definition (get-or-create + optional name). With
    action=run, also queue a TestRun and land on the run; otherwise land
    on the Test detail page."""
    from opp_ci import platforms

    session = SessionLocal()
    try:
        resolved_deps = None
        if omnetpp_version and project != "omnetpp":
            resolved_deps = {"omnetpp": omnetpp_version}
        try:
            r_os, r_distro, r_flavor = platforms.resolve_platform(
                os=os or None, distro=distro or None, flavor=flavor or None,
            )
        except ValueError as e:
            return RedirectResponse(
                url=f"/tests/new?message={e}&message_type=error",
                status_code=303,
            )
        os_canon = platforms._os_canonical(r_os) if r_os else None
        os_ver_clean = (os_version or None) if os_canon and os_canon != "Linux" else None
        distro_ver_clean = (distro_version or None) if r_distro else None
        flavor_ver_clean = (flavor_version or None) if r_flavor else None
        coord = {
            "project": project,
            "kind": kind,
            "mode": mode or None,
            "os": os_canon,
            "os_version": os_ver_clean,
            "distro": r_distro,
            "distro_version": distro_ver_clean,
            "flavor": r_flavor,
            "flavor_version": flavor_ver_clean,
            "arch": arch or None,
            "compiler": compiler or None,
            "compiler_version": compiler_version or None,
            "isolation": isolation or None,
            "toolchain": toolchain or None,
            "opp_file": None,
        }
        test = get_or_create_test(session, coord)
        if name.strip():
            try:
                set_test_name(session, test, name)
            except ValueError as e:
                session.rollback()
                return RedirectResponse(
                    url=f"/tests/new?message={e}&message_type=error",
                    status_code=303,
                )
        if action == "run":
            run = create_test_run(
                session,
                test_id=test.id,
                git_ref=git_ref or None,
                version=version or None,
                resolved_deps=resolved_deps,
            )
            session.commit()
            return RedirectResponse(url=f"/test-runs/{run.id}", status_code=303)
        session.commit()
        return RedirectResponse(url=f"/tests/{test.id}", status_code=303)
    finally:
        session.close()


@web_router.get("/tests/{test_id}", response_class=HTMLResponse)
def test_detail(request: Request, test_id: int,
                current_user: User = Depends(require_user()),
                message: str = Query(default=None), message_type: str = Query(default=None)):
    """Test definition detail: coordinate, run history, expectation
    history + editor, rename, and a Run button."""
    session = SessionLocal()
    try:
        test = session.get(Test, test_id)
        if test is None:
            return HTMLResponse("<h1>Test not found</h1>", status_code=404)
        runs = session.execute(
            select(TestRun).where(TestRun.test_id == test_id)
            .order_by(TestRun.id.desc()).limit(50)
        ).scalars().all()
        expectations = session.execute(
            select(ExpectedTestResult).where(ExpectedTestResult.test_id == test_id)
            .order_by(ExpectedTestResult.set_at.desc(), ExpectedTestResult.id.desc())
        ).scalars().all()
        return templates.TemplateResponse(request, "test_detail.html", {
            "test": test,
            "runs": runs,
            "expectations": expectations,
            "message": message,
            "message_type": message_type,
            **_template_globals(request, current_user),
        })
    finally:
        session.close()


@web_router.post("/tests/{test_id}/run", dependencies=[Depends(require_csrf)])
def test_run(test_id: int,
             current_user: User = Depends(require_user("submitter")),
             git_ref: str = Form(default=""),
             version: str = Form(default="")):
    """Queue a fresh TestRun for an existing Test (from the Tests list or
    the Test detail page)."""
    session = SessionLocal()
    try:
        test = session.get(Test, test_id)
        if test is None:
            return RedirectResponse(
                url="/tests?message=Test+not+found&message_type=error",
                status_code=303,
            )
        run = create_test_run(
            session,
            test_id=test.id,
            git_ref=git_ref or None,
            version=version or None,
        )
        session.commit()
        return RedirectResponse(url=f"/test-runs/{run.id}", status_code=303)
    finally:
        session.close()


@web_router.post("/test-runs/{run_id}/rerun", dependencies=[Depends(require_csrf)])
def run_rerun(run_id: int, current_user: User = Depends(require_user("submitter"))):
    session = SessionLocal()
    try:
        original = session.execute(
            select(TestRun).where(TestRun.id == run_id)
        ).scalar_one_or_none()
        if original is None:
            return RedirectResponse(url="/test-runs", status_code=303)

        # Same Test (coord), fresh TestRun row. matrix_run_id stays NULL —
        # an ad-hoc rerun is not part of any matrix run.
        new_run = create_test_run(
            session,
            test_id=original.test_id,
            git_ref=original.git_ref,
            version=original.version,
            resolved_deps=original.resolved_deps,
        )
        session.commit()
        return RedirectResponse(url=f"/test-runs/{new_run.id}", status_code=303)
    finally:
        session.close()


@web_router.post("/test-runs/{run_id}/cancel", dependencies=[Depends(require_csrf)])
def run_cancel(run_id: int, current_user: User = Depends(require_user("submitter"))):
    """Cancel a queued run. Running runs are left to finish — see the
    locked decision in plan/pending/test-data-model-redesign.md."""
    import datetime
    session = SessionLocal()
    try:
        run = session.execute(
            select(TestRun).where(TestRun.id == run_id)
        ).scalar_one_or_none()
        if run and run.lifecycle == TestRunLifecycle.queued:
            run.lifecycle = TestRunLifecycle.cancelled
            run.finished_at = datetime.datetime.utcnow()
            session.commit()
        return RedirectResponse(url=f"/test-runs/{run_id}", status_code=303)
    finally:
        session.close()


@web_router.post("/test-runs/{run_id}/delete", dependencies=[Depends(require_csrf)])
def run_delete(run_id: int, current_user: User = Depends(require_user("submitter"))):
    """Delete a run (submitter). Running runs are refused — let them finish
    first. On success the run is gone, so redirect to the list."""
    session = SessionLocal()
    try:
        try:
            delete_test_run(session, run_id)
        except CannotDeleteRunningRun as e:
            return RedirectResponse(
                url=f"/test-runs/{run_id}?message={quote(str(e))}&message_type=error",
                status_code=303,
            )
        session.commit()
        return RedirectResponse(url="/test-runs", status_code=303)
    finally:
        session.close()


@web_router.get("/test-runs/{run_id}", response_class=HTMLResponse)
def run_detail(request: Request, run_id: int, current_user: User = Depends(require_user()),
               message: str = Query(default=None), message_type: str = Query(default=None)):
    session = SessionLocal()
    try:
        run = session.execute(
            select(TestRun).where(TestRun.id == run_id)
        ).scalar_one_or_none()
        if run is None:
            return HTMLResponse("<h1>Not found</h1>", status_code=404)

        return templates.TemplateResponse(request, "run_detail.html", {
            "run": run,
            "message": message,
            "message_type": message_type,
            **_template_globals(request, current_user),
        })
    finally:
        session.close()


@web_router.post("/tests/{test_id}/rename", dependencies=[Depends(require_csrf)])
def test_rename(test_id: int,
                current_user: User = Depends(require_user("submitter")),
                name: str = Form(default=""),
                return_to: str = Form(default="/test-runs")):
    """Set or clear a Test's name. Blank clears it (back to anonymous)."""
    session = SessionLocal()
    try:
        test = session.get(Test, test_id)
        if test is None:
            return RedirectResponse(url=return_to, status_code=303)
        try:
            set_test_name(session, test, name)
            session.commit()
        except ValueError as e:
            session.rollback()
            sep = "&" if "?" in return_to else "?"
            return RedirectResponse(
                url=f"{return_to}{sep}message={e}&message_type=error",
                status_code=303,
            )
        return RedirectResponse(url=return_to, status_code=303)
    finally:
        session.close()


@web_router.get("/projects", response_class=HTMLResponse)
def projects_list(
    request: Request,
    current_user: User = Depends(require_user()),
    name: str = Query(default=None),
):
    session = SessionLocal()
    try:
        query = select(Project).order_by(Project.name)
        if name:
            query = query.where(Project.name.ilike(f"%{name}%"))
        projects = session.execute(query).scalars().all()

        run_counts = {}
        last_status = {}
        for p in projects:
            count = session.execute(
                select(func.count(TestRun.id))
                .join(Test, TestRun.test_id == Test.id)
                .where(Test.project == p.name)
            ).scalar()
            run_counts[p.name] = count
            last_run = session.execute(
                select(TestRun)
                .join(Test, TestRun.test_id == Test.id)
                .where(
                    Test.project == p.name,
                    TestRun.lifecycle == TestRunLifecycle.finished,
                ).order_by(TestRun.id.desc()).limit(1)
            ).scalar_one_or_none()
            last_status[p.name] = last_run.effective_status if last_run else None

        return templates.TemplateResponse(request, "projects.html", {
            "projects": projects,
            "run_counts": run_counts,
            "last_status": last_status,
            "filter_name": name or "",
            **_template_globals(request, current_user),
        })
    finally:
        session.close()


@web_router.get("/projects/new", response_class=HTMLResponse)
def project_new_form(request: Request,
                     current_user: User = Depends(require_user("submitter")),
                     error: str = Query(default=None)):
    return templates.TemplateResponse(request, "project_new.html", {
        "error": error,
        **_template_globals(request, current_user),
    })


@web_router.post("/projects/new", dependencies=[Depends(require_csrf)])
def project_new_submit(
    current_user: User = Depends(require_user("submitter")),
    name: str = Form(...),
    opp_env_name: str = Form(default=""),
    github_owner: str = Form(default=""),
    github_repo: str = Form(default=""),
    git_url: str = Form(default=""),
):
    session = SessionLocal()
    try:
        existing = session.execute(
            select(Project).where(Project.name == name)
        ).scalar_one_or_none()
        if existing:
            return RedirectResponse(url="/projects/new?error=Project+already+exists", status_code=303)

        project = Project(
            name=name,
            opp_env_name=opp_env_name or None,
            github_owner=github_owner or None,
            github_repo=github_repo or None,
            git_url=git_url or None,
        )
        session.add(project)
        session.commit()
        return RedirectResponse(url=f"/projects/{project.name}", status_code=303)
    finally:
        session.close()


@web_router.post("/projects/{name}/delete", dependencies=[Depends(require_csrf)])
def project_delete(name: str, current_user: User = Depends(require_user("submitter"))):
    session = SessionLocal()
    try:
        project = session.execute(
            select(Project).where(Project.name == name)
        ).scalar_one_or_none()
        if project:
            for v in session.execute(select(Version).where(Version.project_id == project.id)).scalars().all():
                session.delete(v)
            for r in session.execute(select(AutoTestRule).where(AutoTestRule.project_id == project.id)).scalars().all():
                session.delete(r)
            session.delete(project)
            session.commit()
        return RedirectResponse(url="/projects", status_code=303)
    finally:
        session.close()


@web_router.get("/projects/{name}", response_class=HTMLResponse)
def project_detail(request: Request, name: str, current_user: User = Depends(require_user())):
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
            select(TestRun)
            .join(Test, TestRun.test_id == Test.id)
            .where(Test.project == name)
            .order_by(TestRun.id.desc()).limit(30)
        ).scalars().all()

        # The "Latest release run" card answers Q1 — release readiness.
        latest_release = session.execute(
            select(TestMatrixRun, TestMatrix)
            .join(TestMatrix, TestMatrixRun.matrix_id == TestMatrix.id)
            .where(
                TestMatrix.project == name,
                TestMatrixRun.trigger == "tag",
            )
            .order_by(TestMatrixRun.id.desc())
            .limit(1)
        ).first()

        # Stats
        total = session.execute(
            select(func.count(TestRun.id))
            .join(Test, TestRun.test_id == Test.id)
            .where(Test.project == name)
        ).scalar()
        passed = session.execute(
            select(func.count(TestRun.id))
            .join(Test, TestRun.test_id == Test.id)
            .where(Test.project == name, TestRun.result_code == TestResultCode.PASS)
        ).scalar()
        failed = session.execute(
            select(func.count(TestRun.id))
            .join(Test, TestRun.test_id == Test.id)
            .where(Test.project == name, TestRun.result_code == TestResultCode.FAIL)
        ).scalar()

        return templates.TemplateResponse(request, "project_detail.html", {
            "project": project,
            "versions": versions,
            "recent_runs": recent_runs,
            "total": total,
            "passed": passed,
            "failed": failed,
            "latest_release": latest_release,
            **_template_globals(request, current_user),
        })
    finally:
        session.close()


@web_router.get("/commits/{project}/{sha}", response_class=HTMLResponse)
def commit_detail(request: Request, project: str, sha: str,
                  current_user: User = Depends(require_user())):
    session = SessionLocal()
    try:
        runs = session.execute(
            select(TestRun)
            .join(Test, TestRun.test_id == Test.id)
            .where(Test.project == project, TestRun.commit_sha == sha)
            .order_by(TestRun.id.desc())
        ).scalars().all()
        return templates.TemplateResponse(request, "commit_detail.html", {
            "project": project,
            "sha": sha,
            "runs": runs,
            **_template_globals(request, current_user),
        })
    finally:
        session.close()


@web_router.get("/compatibility", response_class=HTMLResponse)
def compatibility_index(request: Request, current_user: User = Depends(require_user())):
    session = SessionLocal()
    try:
        projects = session.execute(
            select(Project).where(Project.dependency_names.isnot(None)).order_by(Project.name)
        ).scalars().all()
        # Filter to only those with non-empty dependency lists
        projects = [p for p in projects if p.dependency_names]
        return templates.TemplateResponse(request, "compatibility_index.html", {
            "projects": projects,
            **_template_globals(request, current_user),
        })
    finally:
        session.close()


@web_router.get("/compatibility/{project_name}", response_class=HTMLResponse)
def compatibility_page(request: Request, project_name: str,
                       current_user: User = Depends(require_user())):
    from opp_ci.compatibility import get_compatibility_matrix
    session = SessionLocal()
    try:
        matrices = get_compatibility_matrix(session, project_name)
        return templates.TemplateResponse(request, "compatibility.html", {
            "project_name": project_name,
            "matrices": matrices,
            **_template_globals(request, current_user),
        })
    finally:
        session.close()


@web_router.get("/test-matrices", response_class=HTMLResponse)
def matrices_list(
    request: Request,
    current_user: User = Depends(require_user()),
    name: str = Query(default=None),
    project: str = Query(default=None),
    test: str = Query(default=None),
    mode: str = Query(default=None),
    error: str = Query(default=None),
):
    session = SessionLocal()
    try:
        # Options are drawn from all matrices (config holds the test/mode axes
        # as JSON, so those are filtered in Python below).
        all_matrices = session.execute(select(TestMatrix)).scalars().all()
        options = {
            "project": sorted({m.project for m in all_matrices if m.project}),
            "test": sorted({t for m in all_matrices for t in (m.config.get("tests") or [])}),
            "mode": sorted({md for m in all_matrices for md in (m.config.get("modes") or [])}),
        }

        query = select(TestMatrix).order_by(TestMatrix.id)
        if name:
            query = query.where(TestMatrix.name.ilike(f"%{name}%"))
        if project:
            query = query.where(TestMatrix.project == project)
        matrices = session.execute(query).scalars().all()
        if test:
            matrices = [m for m in matrices if test in (m.config.get("tests") or [])]
        if mode:
            matrices = [m for m in matrices if mode in (m.config.get("modes") or [])]

        run_counts = {}
        for m in matrices:
            count = session.execute(
                select(func.count(TestRun.id))
                .join(TestMatrixRun, TestRun.matrix_run_id == TestMatrixRun.id)
                .where(TestMatrixRun.matrix_id == m.id)
            ).scalar()
            run_counts[m.id] = count

        return templates.TemplateResponse(request, "matrices.html", {
            "matrices": matrices,
            "run_counts": run_counts,
            "options": options,
            "filters": {
                "name": name or "", "project": project or "",
                "test": test or "", "mode": mode or "",
            },
            "error": error,
            **_template_globals(request, current_user),
        })
    finally:
        session.close()


def _matrix_form_context(session):
    """Shared template context for the matrix axis form (suggestions +
    per-project versions). Used by `matrix_new_form`."""
    from opp_ci import platforms
    projects = session.execute(select(Project).order_by(Project.name)).scalars().all()
    os_entries = session.execute(select(OS).order_by(OS.name, OS.version)).scalars().all()
    compilers = session.execute(select(Compiler).order_by(Compiler.name, Compiler.version)).scalars().all()

    project_by_id = {p.id: p.name for p in projects}
    versions_by_project = {p.name: [] for p in projects}
    for v in session.execute(select(Version)).scalars().all():
        pname = project_by_id.get(v.project_id)
        if pname is None:
            continue
        versions_by_project[pname].append({
            "opp_env_version": v.opp_env_version or "",
            "git_ref": v.git_ref or "",
            "label": v.label or v.opp_env_version or v.git_ref or "",
        })
    for pname in versions_by_project:
        versions_by_project[pname].sort(key=lambda d: d["label"])

    omnetpp_versions = sorted({
        v["opp_env_version"] for v in versions_by_project.get("omnetpp", []) if v["opp_env_version"]
    })

    return {
        "projects": projects,
        "os_suggestions": list(platforms.OS_NAMES),
        "os_version_suggestions": sorted({o.version for o in os_entries if o.version}),
        "distro_suggestions": sorted({platforms.display_name(n) for n in platforms.DISTROS}),
        "flavor_suggestions": sorted({platforms.display_name(n) for n in platforms.FLAVORS}),
        "arch_suggestions": _arch_suggestions(os_entries),
        "compiler_suggestions": sorted({c.name for c in compilers if c.name}),
        "compiler_version_suggestions": sorted({c.version for c in compilers if c.version}),
        "versions_by_project": versions_by_project,
        "omnetpp_versions": omnetpp_versions,
    }


@web_router.get("/test-matrices/new", response_class=HTMLResponse)
def matrix_new_form(request: Request,
                    current_user: User = Depends(require_user("submitter")),
                    error: str = Query(default=None)):
    session = SessionLocal()
    try:
        return templates.TemplateResponse(request, "matrix_new.html", {
            **_matrix_form_context(session),
            "error": error,
            **_template_globals(request, current_user),
        })
    finally:
        session.close()


@web_router.get("/test-matrices/{matrix_id}", response_class=HTMLResponse)
def matrix_detail(request: Request, matrix_id: int,
                  current_user: User = Depends(require_user()),
                  error: str = Query(default=None)):
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
            select(TestRun)
            .join(TestMatrixRun, TestRun.matrix_run_id == TestMatrixRun.id)
            .where(TestMatrixRun.matrix_id == matrix_id)
            .order_by(TestRun.id.desc()).limit(50)
        ).scalars().all()

        return templates.TemplateResponse(request, "matrix_detail.html", {
            "matrix": matrix,
            "jobs": jobs,
            "recent_runs": recent_runs,
            "error": error,
            **_template_globals(request, current_user),
        })
    finally:
        session.close()


def _split_csv(value):
    """Split a comma-separated string into a list of stripped, non-empty values."""
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _build_matrix_config_from_form(*, project, kinds, modes, versions,
                                   omnetpp_versions, refs, os, os_version,
                                   distro, distro_version, flavor, flavor_version,
                                   arch, compiler, compiler_version, isolation,
                                   toolchain, ref_range_base, ref_range_head):
    """Assemble a matrix `config` dict from the axis form fields.

    Used by `matrix_create` for both Save and Save & run so the CSV axes,
    omnetpp deps, and ref-range are interpreted the same way."""
    config = {}
    axes = {
        "kinds": _split_csv(kinds),
        "modes": _split_csv(modes),
        "versions": _split_csv(versions),
        "refs": _split_csv(refs),
        "os": _split_csv(os),
        "os_version": _split_csv(os_version),
        "distro": _split_csv(distro),
        "distro_version": _split_csv(distro_version),
        "flavor": _split_csv(flavor),
        "flavor_version": _split_csv(flavor_version),
        "arch": _split_csv(arch),
        "compiler": _split_csv(compiler),
        "compiler_version": _split_csv(compiler_version),
        "isolation": _split_csv(isolation),
        "toolchain": _split_csv(toolchain),
    }
    for key, values in axes.items():
        if values:
            config[key] = values

    omnetpp_values = _split_csv(omnetpp_versions)
    if omnetpp_values and project != "omnetpp":
        config["deps"] = {"omnetpp": omnetpp_values}

    if ref_range_base.strip() and ref_range_head.strip():
        config["ref_range"] = {"base": ref_range_base.strip(), "head": ref_range_head.strip()}
        config.pop("refs", None)

    return config


@web_router.post("/test-matrices/create", dependencies=[Depends(require_csrf)])
def matrix_create(
    current_user: User = Depends(require_user("submitter")),
    action: str = Form(default="save"),
    name: str = Form(default=""),
    project: str = Form(...),
    kinds: str = Form(default=""),
    modes: str = Form(default=""),
    versions: str = Form(default=""),
    omnetpp_versions: str = Form(default=""),
    refs: str = Form(default=""),
    os: str = Form(default=""),
    os_version: str = Form(default=""),
    distro: str = Form(default=""),
    distro_version: str = Form(default=""),
    flavor: str = Form(default=""),
    flavor_version: str = Form(default=""),
    arch: str = Form(default=""),
    compiler: str = Form(default=""),
    compiler_version: str = Form(default=""),
    isolation: str = Form(default=""),
    toolchain: str = Form(default=""),
    ref_range_base: str = Form(default=""),
    ref_range_head: str = Form(default=""),
):
    session = SessionLocal()
    try:
        config = _build_matrix_config_from_form(
            project=project, kinds=kinds, modes=modes, versions=versions,
            omnetpp_versions=omnetpp_versions, refs=refs, os=os,
            os_version=os_version, distro=distro, distro_version=distro_version,
            flavor=flavor, flavor_version=flavor_version, arch=arch,
            compiler=compiler, compiler_version=compiler_version,
            isolation=isolation, toolchain=toolchain,
            ref_range_base=ref_range_base, ref_range_head=ref_range_head,
        )
        try:
            matrix = create_matrix_from_axes(
                session, project=project, config=config, name=name or None,
            )
        except ValueError:
            return RedirectResponse(
                url="/test-matrices/new?error=Matrix+already+exists",
                status_code=303,
            )
        if action == "run":
            matrix_run = _queue_matrix_run(session, matrix, trigger="web")
            session.commit()
            return RedirectResponse(url=f"/test-matrix-runs/{matrix_run.id}", status_code=303)
        session.commit()
        return RedirectResponse(url=f"/test-matrices/{matrix.id}", status_code=303)
    finally:
        session.close()


@web_router.post("/test-matrices/{matrix_id}/delete", dependencies=[Depends(require_csrf)])
def matrix_delete(matrix_id: int, current_user: User = Depends(require_user("submitter"))):
    session = SessionLocal()
    try:
        matrix = session.execute(
            select(TestMatrix).where(TestMatrix.id == matrix_id)
        ).scalar_one_or_none()
        if matrix:
            session.delete(matrix)
            session.commit()
        return RedirectResponse(url="/test-matrices", status_code=303)
    finally:
        session.close()


@web_router.post("/test-matrices/{matrix_id}/rename", dependencies=[Depends(require_csrf)])
def matrix_rename(matrix_id: int,
                  current_user: User = Depends(require_user("submitter")),
                  name: str = Form(default="")):
    """Set or clear a matrix's name. Blank clears it (anonymous)."""
    session = SessionLocal()
    try:
        matrix = session.get(TestMatrix, matrix_id)
        if matrix is None:
            return RedirectResponse(url="/test-matrices", status_code=303)
        try:
            set_matrix_name(session, matrix, name)
            session.commit()
        except ValueError as e:
            session.rollback()
            return RedirectResponse(
                url=f"/test-matrices/{matrix_id}?error={e}", status_code=303,
            )
        return RedirectResponse(url=f"/test-matrices/{matrix_id}", status_code=303)
    finally:
        session.close()


@web_router.post("/test-matrices/{matrix_id}/run", dependencies=[Depends(require_csrf)])
def matrix_run(matrix_id: int,
               current_user: User = Depends(require_user("submitter"))):
    """Queue a TestMatrixRun for a saved matrix (from the Test Matrices
    list or the matrix detail page)."""
    session = SessionLocal()
    try:
        matrix = session.get(TestMatrix, matrix_id)
        if matrix is None:
            return RedirectResponse(url="/test-matrices", status_code=303)
        matrix_run = _queue_matrix_run(session, matrix, trigger="web")
        session.commit()
        return RedirectResponse(url=f"/test-matrix-runs/{matrix_run.id}", status_code=303)
    finally:
        session.close()


@web_router.get("/test-matrix-runs", response_class=HTMLResponse)
def matrix_runs_list(
    request: Request,
    current_user: User = Depends(require_user()),
    project: str = Query(default=None),
    trigger: str = Query(default=None),
    ref: str = Query(default=None),
    verdict: str = Query(default=None),
    actual: str = Query(default=None),
    since: str = Query(default=None),
    limit: int = Query(default=50),
):
    """Index of recent TestMatrixRun rows with their rollup verdict."""
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
        if trigger:
            query = query.where(TestMatrixRun.trigger == trigger)
        if ref:
            query = query.where(TestMatrixRun.ref.ilike(f"%{ref}%"))
        if verdict:
            try:
                query = query.where(TestMatrixRun.verdict == TestVerdictKind(verdict))
            except ValueError:
                pass
        if actual:
            try:
                query = query.where(TestMatrixRun.actual_summary == TestResultCode(actual))
            except ValueError:
                pass
        if since:
            try:
                cutoff = datetime.datetime.fromisoformat(since)
                query = query.where(TestMatrixRun.created_at >= cutoff)
            except ValueError:
                pass

        rows = session.execute(query).all()
        options = _distinct_options(session, TestMatrix.project, TestMatrixRun.trigger)
        options["verdict"] = ["EXPECTED", "UNEXPECTED", "UNKNOWN"]
        options["actual"] = ["PASS", "FAIL", "ERROR", "SKIPPED"]
        return templates.TemplateResponse(request, "matrix_runs.html", {
            "rows": rows,
            "options": options,
            "filters": {
                "project": project or "", "trigger": trigger or "", "ref": ref or "",
                "verdict": verdict or "", "actual": actual or "", "since": since or "",
            },
            **_template_globals(request, current_user),
        })
    finally:
        session.close()


def _queue_matrix_run(session, matrix, *, trigger):
    """Create a TestMatrixRun for `matrix`, expand it, and enqueue every
    cell. Returns the TestMatrixRun. Caller commits. Shared by the
    save-and-run, run-saved-matrix, and rerun handlers."""
    from opp_ci.scheduler import expand_matrix
    from opp_ci.fingerprint import compute_cache_fingerprint

    proj = session.execute(
        select(Project).where(Project.name == matrix.project)
    ).scalar_one_or_none()
    matrix_run = create_matrix_run(
        session,
        matrix_id=matrix.id,
        trigger=trigger,
        github_owner=proj.github_owner if proj else None,
        github_repo=proj.github_repo if proj else None,
    )
    for job in expand_matrix(matrix.project, matrix.config):
        fp = compute_cache_fingerprint(
            job, project=matrix.project, opp_file=matrix.opp_file,
        )
        enqueue_job(
            session, job,
            project=matrix.project,
            opp_file=matrix.opp_file,
            matrix_run_id=matrix_run.id,
            use_cache=True,
            cache_fingerprint=fp,
        )
    return matrix_run


@web_router.post("/test-matrix-runs/{matrix_run_id}/rerun", dependencies=[Depends(require_csrf)])
def matrix_run_rerun(matrix_run_id: int,
                     current_user: User = Depends(require_user("submitter"))):
    """Re-run the same matrix as a fresh TestMatrixRun."""
    session = SessionLocal()
    try:
        mr = session.get(TestMatrixRun, matrix_run_id)
        if mr is None:
            return RedirectResponse(url="/test-matrix-runs", status_code=303)
        matrix = session.get(TestMatrix, mr.matrix_id)
        if matrix is None:
            return RedirectResponse(url="/test-matrix-runs", status_code=303)
        new_run = _queue_matrix_run(session, matrix, trigger="rerun")
        session.commit()
        return RedirectResponse(url=f"/test-matrix-runs/{new_run.id}", status_code=303)
    finally:
        session.close()


@web_router.post("/test-matrix-runs/{matrix_run_id}/cancel", dependencies=[Depends(require_csrf)])
def matrix_run_cancel(matrix_run_id: int,
                      current_user: User = Depends(require_user("submitter"))):
    """Cancel every still-queued child run of a matrix run; running
    children are left to finish (same rule as run_cancel). Refreshes the
    rollup so the counters reflect the cancellations."""
    from opp_ci.persistence import recompute_matrix_run_rollup
    session = SessionLocal()
    try:
        mr = session.get(TestMatrixRun, matrix_run_id)
        if mr is not None:
            queued = session.execute(
                select(TestRun).where(
                    TestRun.matrix_run_id == matrix_run_id,
                    TestRun.lifecycle == TestRunLifecycle.queued,
                )
            ).scalars().all()
            now = datetime.datetime.utcnow()
            for run in queued:
                run.lifecycle = TestRunLifecycle.cancelled
                run.finished_at = now
            session.flush()
            recompute_matrix_run_rollup(session, matrix_run_id)
            session.commit()
        return RedirectResponse(url=f"/test-matrix-runs/{matrix_run_id}", status_code=303)
    finally:
        session.close()


@web_router.post("/test-matrix-runs/{matrix_run_id}/delete",
                 dependencies=[Depends(require_csrf)])
def matrix_run_delete(matrix_run_id: int,
                      current_user: User = Depends(require_user("submitter"))):
    """Delete a matrix run and cascade to its child runs (submitter). If any
    child is still running the whole delete is refused; redirect back with an
    error. On success the run is gone, so redirect to the list."""
    session = SessionLocal()
    try:
        try:
            delete_matrix_run(session, matrix_run_id)
        except CannotDeleteRunningRun as e:
            return RedirectResponse(
                url=f"/test-matrix-runs/{matrix_run_id}"
                    f"?message={quote(str(e))}&message_type=error",
                status_code=303,
            )
        session.commit()
        return RedirectResponse(url="/test-matrix-runs", status_code=303)
    finally:
        session.close()


@web_router.get("/test-matrix-runs/{matrix_run_id}", response_class=HTMLResponse)
def matrix_run_detail(
    request: Request, matrix_run_id: int,
    current_user: User = Depends(require_user()),
    unexpected_only: int = Query(default=0),
    message: str = Query(default=None), message_type: str = Query(default=None),
):
    """Rollup header + per-cell verdict table for one TestMatrixRun."""
    session = SessionLocal()
    try:
        mr = session.execute(
            select(TestMatrixRun).where(TestMatrixRun.id == matrix_run_id)
        ).scalar_one_or_none()
        if mr is None:
            return HTMLResponse("<h1>Matrix run not found</h1>", status_code=404)
        matrix = session.execute(
            select(TestMatrix).where(TestMatrix.id == mr.matrix_id)
        ).scalar_one_or_none()

        rows = session.execute(
            select(TestVerdict, TestRun, Test)
            .join(TestRun, TestVerdict.test_run_id == TestRun.id)
            .join(Test, TestVerdict.test_id == Test.id)
            .where(TestVerdict.matrix_run_id == matrix_run_id)
            .order_by(TestVerdict.id)
        ).all()

        if unexpected_only:
            rows = [r for r in rows if r[0].verdict != TestVerdictKind.EXPECTED]

        # Resolve the expectation in force on each cell (for display).
        cells = []
        for verdict, run, test in rows:
            expected_code = None
            expected_descr = None
            if verdict.expectation_id is not None:
                exp = session.get(ExpectedTestResult, verdict.expectation_id)
                if exp:
                    expected_code = exp.expected_result_code
                    expected_descr = exp.expected_result_description
            cells.append({
                "verdict": verdict,
                "run": run,
                "test": test,
                "expected_code": expected_code,
                "expected_descr": expected_descr,
            })

        return templates.TemplateResponse(request, "matrix_run_detail.html", {
            "mr": mr,
            "matrix": matrix,
            "cells": cells,
            "unexpected_only": bool(unexpected_only),
            "message": message,
            "message_type": message_type,
            **_template_globals(request, current_user),
        })
    finally:
        session.close()


@web_router.post("/tests/{test_id}/expectations", dependencies=[Depends(require_csrf)])
def web_post_expectation(
    test_id: int,
    current_user: User = Depends(require_user("submitter")),
    expected_result_code: str = Form(default=""),
    expected_result_description: str = Form(default=""),
    reason: str = Form(default=""),
    return_to: str = Form(default=""),
):
    """Append an ExpectedTestResult row from the inline editor.

    Empty `expected_result_code` records a retraction (NULL in the
    column). On success redirects back to `return_to` (typically the
    matrix-run detail page that posted the form).
    """
    code = None
    if expected_result_code:
        try:
            code = TestResultCode(expected_result_code)
        except ValueError:
            target = return_to or f"/tests/{test_id}"
            sep = "&" if "?" in target else "?"
            return RedirectResponse(
                url=f"{target}{sep}message=Invalid+result+code&message_type=error",
                status_code=303,
            )

    session = SessionLocal()
    try:
        test = session.get(Test, test_id)
        if test is None:
            return RedirectResponse(url=return_to or f"/tests/{test_id}", status_code=303)
        insert_expectation(
            session, test_id=test_id,
            expected_result_code=code,
            expected_result_description=expected_result_description or None,
            reason=reason or None,
            set_by=current_user.display_name,
        )
        session.commit()
        return RedirectResponse(
            url=return_to or f"/tests/{test_id}",
            status_code=303,
        )
    finally:
        session.close()


@web_router.get("/os", response_class=HTMLResponse)
def os_list(
    request: Request,
    current_user: User = Depends(require_user()),
    name: str = Query(default=None),
    version: str = Query(default=None),
    arch: str = Query(default=None),
):
    session = SessionLocal()
    try:
        query = select(OS).order_by(OS.name, OS.version)
        if name:
            query = query.where(OS.name.ilike(f"%{name}%"))
        if version:
            query = query.where(OS.version.ilike(f"%{version}%"))
        if arch:
            query = query.where(OS.arch.ilike(f"%{arch}%"))
        os_entries = session.execute(query).scalars().all()

        return templates.TemplateResponse(request, "os.html", {
            "os_entries": os_entries,
            "filter_name": name or "",
            "filter_version": version or "",
            "filter_arch": arch or "",
            **_template_globals(request, current_user),
        })
    finally:
        session.close()


@web_router.get("/os/new", response_class=HTMLResponse)
def os_new_form(request: Request, current_user: User = Depends(require_user("submitter"))):
    return templates.TemplateResponse(request, "os_new.html", _template_globals(request, current_user))


@web_router.post("/os/new", dependencies=[Depends(require_csrf)])
def os_new_submit(current_user: User = Depends(require_user("submitter")),
                  name: str = Form(...), version: str = Form(default=""), arch: str = Form(default="x86_64")):
    session = SessionLocal()
    try:
        entry = OS(name=name, version=version or None, arch=arch or "x86_64")
        session.add(entry)
        session.commit()
        return RedirectResponse(url=f"/os/{entry.id}", status_code=303)
    finally:
        session.close()


@web_router.get("/os/{os_id}", response_class=HTMLResponse)
def os_detail(request: Request, os_id: int, current_user: User = Depends(require_user())):
    session = SessionLocal()
    try:
        entry = session.execute(select(OS).where(OS.id == os_id)).scalar_one_or_none()
        if entry is None:
            return HTMLResponse("<h1>OS not found</h1>", status_code=404)
        return templates.TemplateResponse(request, "os_detail.html", {
            "os": entry,
            **_template_globals(request, current_user),
        })
    finally:
        session.close()


@web_router.post("/os/{os_id}/delete", dependencies=[Depends(require_csrf)])
def os_delete(os_id: int, current_user: User = Depends(require_user("submitter"))):
    session = SessionLocal()
    try:
        entry = session.execute(select(OS).where(OS.id == os_id)).scalar_one_or_none()
        if entry:
            session.delete(entry)
            session.commit()
        return RedirectResponse(url="/os", status_code=303)
    finally:
        session.close()


@web_router.get("/compilers", response_class=HTMLResponse)
def compilers_list(
    request: Request,
    current_user: User = Depends(require_user()),
    name: str = Query(default=None),
    version: str = Query(default=None),
):
    session = SessionLocal()
    try:
        query = select(Compiler).order_by(Compiler.name, Compiler.version)
        if name:
            query = query.where(Compiler.name.ilike(f"%{name}%"))
        if version:
            query = query.where(Compiler.version.ilike(f"%{version}%"))
        compilers = session.execute(query).scalars().all()

        return templates.TemplateResponse(request, "compilers.html", {
            "compilers": compilers,
            "filter_name": name or "",
            "filter_version": version or "",
            **_template_globals(request, current_user),
        })
    finally:
        session.close()


@web_router.get("/compilers/new", response_class=HTMLResponse)
def compiler_new_form(request: Request, current_user: User = Depends(require_user("submitter"))):
    return templates.TemplateResponse(request, "compiler_new.html", _template_globals(request, current_user))


@web_router.post("/compilers/new", dependencies=[Depends(require_csrf)])
def compiler_new_submit(current_user: User = Depends(require_user("submitter")),
                         name: str = Form(...), version: str = Form(default="")):
    session = SessionLocal()
    try:
        entry = Compiler(name=name, version=version or None)
        session.add(entry)
        session.commit()
        return RedirectResponse(url=f"/compilers/{entry.id}", status_code=303)
    finally:
        session.close()


@web_router.get("/compilers/{compiler_id}", response_class=HTMLResponse)
def compiler_detail(request: Request, compiler_id: int,
                    current_user: User = Depends(require_user())):
    session = SessionLocal()
    try:
        entry = session.execute(select(Compiler).where(Compiler.id == compiler_id)).scalar_one_or_none()
        if entry is None:
            return HTMLResponse("<h1>Compiler not found</h1>", status_code=404)
        return templates.TemplateResponse(request, "compiler_detail.html", {
            "compiler": entry,
            **_template_globals(request, current_user),
        })
    finally:
        session.close()


@web_router.post("/compilers/{compiler_id}/delete", dependencies=[Depends(require_csrf)])
def compiler_delete(compiler_id: int, current_user: User = Depends(require_user("submitter"))):
    session = SessionLocal()
    try:
        entry = session.execute(select(Compiler).where(Compiler.id == compiler_id)).scalar_one_or_none()
        if entry:
            session.delete(entry)
            session.commit()
        return RedirectResponse(url="/compilers", status_code=303)
    finally:
        session.close()


@web_router.get("/workers", response_class=HTMLResponse)
def workers_list(request: Request, current_user: User = Depends(require_user())):
    """List registered workers, flagging those whose heartbeat is fresh as connected."""
    import datetime as _dt
    from opp_ci.config import WORKER_HEARTBEAT_TIMEOUT

    session = SessionLocal()
    try:
        workers = session.execute(select(Worker).order_by(Worker.name)).scalars().all()
        now = _dt.datetime.utcnow()
        threshold = now - _dt.timedelta(seconds=WORKER_HEARTBEAT_TIMEOUT)

        rows = []
        for w in workers:
            connected = w.last_heartbeat is not None and w.last_heartbeat > threshold
            age = (now - w.last_heartbeat).total_seconds() if w.last_heartbeat else None
            tags = w.tags or []
            os_tags = [t for t in tags if t.startswith("os:")]
            distro_tags = [t for t in tags if t.startswith("distro:")]
            flavor_tags = [t for t in tags if t.startswith("flavor:")]
            compiler_tags = [t for t in tags if t.startswith("compiler:")]
            has_podman = "podman" in tags
            has_nix = "nix" in tags
            other_tags = [t for t in tags
                          if not (t.startswith("os:") or t.startswith("distro:")
                                  or t.startswith("flavor:") or t.startswith("compiler:")
                                  or t in ("podman", "nix"))]
            rows.append({
                "worker": w,
                "connected": connected,
                "heartbeat_age_seconds": age,
                "os_tags": os_tags,
                "distro_tags": distro_tags,
                "flavor_tags": flavor_tags,
                "compiler_tags": compiler_tags,
                "has_podman": has_podman,
                "has_nix": has_nix,
                "other_tags": other_tags,
            })
        connected_count = sum(1 for r in rows if r["connected"])

        return templates.TemplateResponse(request, "workers.html", {
            "rows": rows,
            "connected_count": connected_count,
            "heartbeat_timeout": WORKER_HEARTBEAT_TIMEOUT,
            **_template_globals(request, current_user),
        })
    finally:
        session.close()


@web_router.get("/rules", response_class=HTMLResponse)
def rules_list(request: Request, current_user: User = Depends(require_user())):
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
            **_template_globals(request, current_user),
        })
    finally:
        session.close()


@web_router.get("/rules/{rule_id}", response_class=HTMLResponse)
def rule_detail(request: Request, rule_id: int, current_user: User = Depends(require_user())):
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
            **_template_globals(request, current_user),
        })
    finally:
        session.close()


@web_router.post("/rules/{rule_id}/edit", dependencies=[Depends(require_csrf)])
def rule_edit_web(
    rule_id: int,
    current_user: User = Depends(require_user("submitter")),
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


@web_router.post("/rules/create", dependencies=[Depends(require_csrf)])
def rule_create_web(
    current_user: User = Depends(require_user("submitter")),
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


@web_router.post("/rules/{rule_id}/delete", dependencies=[Depends(require_csrf)])
def rule_delete_web(rule_id: int, current_user: User = Depends(require_user("submitter"))):
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


@web_router.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request, current_user: User = Depends(require_user("admin"))):
    session = SessionLocal()
    try:
        stats = {
            "projects": session.execute(select(func.count(Project.id))).scalar(),
            "versions": session.execute(select(func.count(Version.id))).scalar(),
            "os_entries": session.execute(select(func.count(OS.id))).scalar(),
            "compilers": session.execute(select(func.count(Compiler.id))).scalar(),
            "matrices": session.execute(select(func.count(TestMatrix.id))).scalar(),
            "tests": session.execute(select(func.count(Test.id))).scalar(),
            "matrix_runs": session.execute(select(func.count(TestMatrixRun.id))).scalar(),
            "runs_total": session.execute(select(func.count(TestRun.id))).scalar(),
            "runs_passed": session.execute(select(func.count(TestRun.id)).where(TestRun.result_code == TestResultCode.PASS)).scalar(),
            "runs_failed": session.execute(select(func.count(TestRun.id)).where(TestRun.result_code == TestResultCode.FAIL)).scalar(),
            "runs_error": session.execute(select(func.count(TestRun.id)).where(TestRun.result_code == TestResultCode.ERROR)).scalar(),
            "runs_running": session.execute(select(func.count(TestRun.id)).where(TestRun.lifecycle == TestRunLifecycle.running)).scalar(),
            "runs_queued": session.execute(select(func.count(TestRun.id)).where(TestRun.lifecycle == TestRunLifecycle.queued)).scalar(),
            "runs_cancelled": session.execute(select(func.count(TestRun.id)).where(TestRun.lifecycle == TestRunLifecycle.cancelled)).scalar(),
            "workers_total": session.execute(select(func.count(Worker.id))).scalar(),
            "workers_online": session.execute(select(func.count(Worker.id)).where(Worker.status == "online")).scalar(),
            "tokens": session.execute(select(func.count(ApiToken.id))).scalar(),
            "users": session.execute(select(func.count(User.id))).scalar(),
        }

        workers = session.execute(
            select(Worker).order_by(Worker.name)
        ).scalars().all()

        recent_errors = session.execute(
            select(TestRun).where(TestRun.result_code == TestResultCode.ERROR).order_by(TestRun.id.desc()).limit(10)
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
            **_template_globals(request, current_user),
        })
    finally:
        session.close()


@web_router.post("/admin/workers/register", dependencies=[Depends(require_csrf)])
def admin_register_worker(
    current_user: User = Depends(require_user("admin")),
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


@web_router.post("/admin/tokens/create", dependencies=[Depends(require_csrf)])
def admin_create_token(
    current_user: User = Depends(require_user("admin")),
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


@web_router.post("/admin/tokens/{token_id}/revoke", dependencies=[Depends(require_csrf)])
def admin_revoke_token(token_id: int, current_user: User = Depends(require_user("admin"))):
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


@web_router.post("/admin/projects/register", dependencies=[Depends(require_csrf)])
def admin_register_project(
    current_user: User = Depends(require_user("admin")),
    name: str = Form(...),
    opp_env_name: str = Form(default=""),
    github_owner: str = Form(default=""),
    github_repo: str = Form(default=""),
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
        )
        session.add(project)
        session.commit()
        return RedirectResponse(url="/admin", status_code=303)
    finally:
        session.close()


# ── Admin: users page ──────────────────────────────────────────────────


@web_router.get("/admin/users", response_class=HTMLResponse)
def admin_users(request: Request, current_user: User = Depends(require_user("admin")),
                message: str = Query(default=None), message_type: str = Query(default=None)):
    session = SessionLocal()
    try:
        users = session.execute(select(User).order_by(User.id)).scalars().all()
        return templates.TemplateResponse(request, "users.html", {
            "users": users,
            "message": message,
            "message_type": message_type,
            **_template_globals(request, current_user),
        })
    finally:
        session.close()


_ROLE_CHOICES = ("readonly", "submitter", "admin")


@web_router.post("/admin/users/{user_id}/role", dependencies=[Depends(require_csrf)])
def admin_set_user_role(
    user_id: int,
    current_user: User = Depends(require_user("admin")),
    role: str = Form(...),
):
    if role not in _ROLE_CHOICES:
        return RedirectResponse(url="/admin/users?message=Invalid+role&message_type=error", status_code=303)
    session = SessionLocal()
    try:
        user = session.execute(select(User).where(User.id == user_id)).scalar_one_or_none()
        if user is None:
            return RedirectResponse(url="/admin/users", status_code=303)
        user.role = role
        user.role_locked = True  # pin: don't recompute from GitHub next login
        session.commit()
        return RedirectResponse(
            url=f"/admin/users?message=Role+for+{user.display_name}+set+to+{role}+(locked)&message_type=success",
            status_code=303,
        )
    finally:
        session.close()


@web_router.post("/admin/users/{user_id}/unlock", dependencies=[Depends(require_csrf)])
def admin_unlock_user(user_id: int, current_user: User = Depends(require_user("admin"))):
    session = SessionLocal()
    try:
        user = session.execute(select(User).where(User.id == user_id)).scalar_one_or_none()
        if user is None:
            return RedirectResponse(url="/admin/users", status_code=303)
        user.role_locked = False
        session.commit()
        return RedirectResponse(
            url=f"/admin/users?message=Role+for+{user.display_name}+unlocked&message_type=success",
            status_code=303,
        )
    finally:
        session.close()


@web_router.post("/admin/users/{user_id}/disable", dependencies=[Depends(require_csrf)])
def admin_disable_user(
    user_id: int,
    current_user: User = Depends(require_user("admin")),
    enabled: int = Form(default=0),
):
    session = SessionLocal()
    try:
        user = session.execute(select(User).where(User.id == user_id)).scalar_one_or_none()
        if user is None:
            return RedirectResponse(url="/admin/users", status_code=303)
        # Don't let an admin lock themselves out of the only admin account.
        if user.id == current_user.id and not enabled:
            return RedirectResponse(
                url="/admin/users?message=Refusing+to+disable+your+own+account&message_type=error",
                status_code=303,
            )
        user.enabled = bool(enabled)
        session.commit()
        state = "enabled" if user.enabled else "disabled"
        return RedirectResponse(
            url=f"/admin/users?message=User+{user.display_name}+{state}&message_type=success",
            status_code=303,
        )
    finally:
        session.close()


app.include_router(web_router)
