import asyncio
import datetime
import logging
import os
import re
from contextlib import asynccontextmanager
from html import escape as html_escape
from pathlib import Path
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, FastAPI, Form, HTTPException, Request, Query
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from sqlalchemy import and_, cast, exists, false, func, select, String, text
from sqlalchemy.orm import aliased, selectinload
from starlette.middleware.sessions import SessionMiddleware

from opp_ci import config as cfg
from opp_ci.auth import get_csrf_token, require_csrf, require_user

try:
    from opp_ci._version import __version__ as _APP_VERSION
except ImportError:
    _APP_VERSION = "0.0.0"
from opp_ci.db.connection import SessionLocal
from opp_ci.db.models import (
    ApiToken, AutoTestRule, Compiler, ExpectedTestResult, OS, Project, Test,
    TestMatrix, TestMatrixRun, TestResultCode, TestRun, TestRunLifecycle,
    TestVerdict, TestVerdictKind, User, Version, Worker,
)
from opp_ci.persistence import (
    create_matrix_from_axes, create_matrix_run, create_test_run, delete_worker,
    enqueue_job, expire_unserviceable_queued_runs, format_run_filters,
    get_current_expectation,
    get_matrix_by_name, get_or_create_test,
    get_test_by_name, insert_expectation, mark_stale_workers_offline,
    parse_expectation_override, read_default_expectation_code,
    set_default_expectation_code, set_matrix_name, set_test_name, update_worker,
    validate_test_coord,
    USE_GLOBAL_DEFAULT,
)

_logger = logging.getLogger(__name__)

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
    """Return a sorted, de-duplicated list of arch values for datalist hints.

    Values are folded to the canonical matrix vocabulary (amd64/aarch64) so an
    OS row carrying an alias spelling (e.g. x86_64) doesn't surface a second,
    unmatchable choice alongside its canonical name.
    """
    from opp_ci import platforms
    values = set(_DEFAULT_ARCH_SUGGESTIONS)
    for entry in os_entries or ():
        if entry.arch:
            values.add(platforms.canonical_arch(entry.arch))
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


def _reap_stale_workers():
    """Sweep once for workers silent past WORKER_HEARTBEAT_TIMEOUT (mark them
    offline, reclaim their orphaned `running` runs) and for `queued` runs no
    enabled worker can ever serve (retire them). Sync DB work, meant to be
    called via asyncio.to_thread off the event loop."""
    session = SessionLocal()
    now = datetime.datetime.utcnow()
    try:
        results = mark_stale_workers_offline(
            session,
            now,
            cfg.WORKER_HEARTBEAT_TIMEOUT,
            cfg.MAX_RECLAIMS,
        )
        # Same tick, same transaction: serviceability counts enabled workers
        # of any status, so flipping a stale worker `offline` just above does
        # not make the runs only it can serve look unserviceable.
        workers = session.execute(select(Worker)).scalars().all()
        expired = expire_unserviceable_queued_runs(
            session, now, workers, cfg.QUEUE_UNSERVICEABLE_TIMEOUT)
        session.commit()
    finally:
        session.close()
    for name, requeued, retired in results:
        _logger.warning(
            "Reaped stale worker '%s': %d run(s) re-queued, %d retired "
            "(poison pill)", name, requeued, retired)
    if expired:
        _logger.warning(
            "Expired %d queued run(s) no enabled worker can serve", expired)
    return results


async def _reaper_loop():
    """Periodically reap stale workers until cancelled."""
    while True:
        await asyncio.sleep(cfg.WORKER_REAP_INTERVAL)
        try:
            await asyncio.to_thread(_reap_stale_workers)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # never let a transient DB error kill the loop
            _logger.warning("Stale-worker reaper sweep failed: %s", e)


@asynccontextmanager
async def lifespan(app):
    # Startup reconciliation: a coordinator restart leaves no one to mark
    # crashed workers offline, so sweep once immediately. This also covers
    # workers that went stale while the coordinator was down.
    try:
        await asyncio.to_thread(_reap_stale_workers)
    except Exception as e:
        _logger.warning("Startup stale-worker reconciliation failed: %s", e)
    task = asyncio.create_task(_reaper_loop())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="opp_ci", lifespan=lifespan)

# Session middleware signs cookies with OPP_CI_SESSION_SECRET. We fail
# closed if it's unset: a random per-process secret would silently log
# everyone out on every restart and let anyone forge cookies on a
# misconfigured deploy.
if not cfg.SESSION_SECRET:
    raise RuntimeError(
        "OPP_CI_SESSION_SECRET is required for `opp_ci coordinator start`. "
        "Generate one with `python -c 'import secrets; print(secrets.token_urlsafe(32))'` "
        "and set it in /etc/opp_ci/coordinator.env or the environment."
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
        "app_version": _APP_VERSION,
    }


# Every route on `web_router` requires a logged-in user. Routes that need
# `submitter` or `admin` add a stricter `require_user(...)` dependency
# locally. POST routes additionally depend on `require_csrf`.
web_router = APIRouter(dependencies=[Depends(require_user())])


@web_router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, current_user: User = Depends(require_user()),
              window: str = Query(default="hour")):
    window = _normalise_window(window)
    cutoff = _window_cutoff(window)
    session = SessionLocal()
    try:
        # Lifecycle + outcome counts are window-scoped (by created_at);
        # computed with two group-by queries rather than one count per bucket.
        def _scoped(q):
            return q.where(TestRun.created_at >= cutoff) if cutoff is not None else q

        lifecycle_rows = session.execute(_scoped(
            select(TestRun.lifecycle, func.count(TestRun.id)).group_by(TestRun.lifecycle)
        )).all()
        lifecycle_counts = {lc.value if lc else None: n for lc, n in lifecycle_rows}
        lifecycle = [
            (state.value, lifecycle_counts.get(state.value, 0))
            for state in TestRunLifecycle
        ]
        total_runs = sum(lifecycle_counts.values())

        outcome_rows = session.execute(_scoped(
            select(TestRun.result_code, func.count(TestRun.id))
            .where(TestRun.lifecycle == TestRunLifecycle.finished)
            .group_by(TestRun.result_code)
        )).all()
        outcome_counts = {rc.value if rc else None: n for rc, n in outcome_rows}
        outcomes = [
            (code.value, outcome_counts.get(code.value, 0))
            for code in TestResultCode
        ]

        workers = worker_summary(session)

        # Inventory counts are absolute (not time-bound).
        def _count(model):
            return session.execute(select(func.count(model.id))).scalar()

        inventory = {
            "tests": _count(Test),
            "matrices": _count(TestMatrix),
            "matrix_runs": _count(TestMatrixRun),
            "projects": _count(Project),
            "rules": _count(AutoTestRule),
            "oses": _count(OS),
            "compilers": _count(Compiler),
        }

        return templates.TemplateResponse(request, "dashboard.html", {
            "window": window,
            "windows": _WINDOWS,
            "total_runs": total_runs,
            "lifecycle": lifecycle,
            "outcomes": outcomes,
            "workers": workers,
            "inventory": inventory,
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
        # Recently finished: the last 20 runs that left the active states
        # (finished / cancelled / timed_out), newest first. Moved here from
        # the dashboard so Queue is the full activity view.
        recent = session.execute(
            select(TestRun)
            .where(TestRun.lifecycle.in_((
                TestRunLifecycle.finished,
                TestRunLifecycle.cancelled,
                TestRunLifecycle.timed_out,
            )))
            .order_by(TestRun.finished_at.desc().nulls_last(), TestRun.id.desc())
            .limit(20)
        ).scalars().all()

        return templates.TemplateResponse(request, "queue.html", {
            "running": running,
            "queued": queued,
            "recent": recent,
            "message": message,
            "message_type": message_type,
            **_template_globals(request, current_user),
        })
    finally:
        session.close()


_WINDOWS = ("hour", "day", "week", "month", "all")


def _normalise_window(window):
    """Clamp an arbitrary `window` query param to one of `_WINDOWS`,
    defaulting unknown/missing values to "all"."""
    return window if window in _WINDOWS else "all"


def _window_cutoff(window):
    """UTC lower bound for a normalised window, or None for "all" (no
    bound). Runs are scoped by `created_at >= cutoff`."""
    now = datetime.datetime.utcnow()
    return {
        "hour": now - datetime.timedelta(hours=1),
        "day": now - datetime.timedelta(days=1),
        "week": now - datetime.timedelta(days=7),
        "month": now - datetime.timedelta(days=30),
    }.get(window)


def worker_summary(session):
    """Current fleet health, independent of any time window. Shared by the
    dashboard summary and the workers page header."""
    from opp_ci.config import WORKER_HEARTBEAT_TIMEOUT

    workers = session.execute(select(Worker)).scalars().all()
    now = datetime.datetime.utcnow()
    threshold = now - datetime.timedelta(seconds=WORKER_HEARTBEAT_TIMEOUT)

    registered = len(workers)
    connected = 0
    busy = 0
    running_jobs = 0
    capacity = 0
    for w in workers:
        is_connected = w.last_heartbeat is not None and w.last_heartbeat > threshold
        if is_connected:
            connected += 1
            capacity += (w.concurrency or 0)
            running_jobs += (w.current_job_count or 0)
            if (w.current_job_count or 0) > 0:
                busy += 1
    return {
        "registered": registered,
        "connected": connected,
        "busy": busy,
        "running_jobs": running_jobs,
        # Free slots across connected workers (never negative).
        "free_capacity": max(0, capacity - running_jobs),
    }


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


def apply_str_filter(query, col, value, mode="eq"):
    """Append one string predicate to `query` per the control taxonomy.

    `mode` selects the match semantics behind each filter control:
      * "eq"       — equality select (bounded categoricals)
      * "contains" — typeable combo / text-contains (substring, ILIKE %v%)
      * "prefix"   — typeable combo for ordered `*_version` fields (ILIKE v%),
                     so `6` matches `6.x` but not `16.x`.
    An empty/None value is a no-op (the filter is simply not applied).
    """
    if not value:
        return query
    if mode == "contains":
        return query.where(col.ilike(f"%{value}%"))
    if mode == "prefix":
        return query.where(col.ilike(f"{value}%"))
    return query.where(col == value)


def apply_dep_filter(query, col, value):
    """Substring filter over a JSON dependency column (`resolved_deps`,
    `dependency_names`). Matches the serialized JSON text, so a bare version
    (`6.4.0`) or a dependency name (`omnetpp`) both hit. First-cut blunt
    instrument — see plan/pending/web-filter-controls-and-completeness.md."""
    if not value:
        return query
    return query.where(cast(col, String).ilike(f"%{value}%"))



# Matrix coordinate axes. A matrix stores its axes inside the `config` JSON
# (as lists) rather than as columns; they're filtered via a correlated
# EXISTS over that JSON (see matrix_axis_sql_filter). Each tuple is
# (filter param, config key, label, control), where control is "sel" (exact
# membership, bounded categorical) or "combo" (substring membership, for the
# open-ended version axes). Shared by the matrices and matrix-runs pages.
_MATRIX_AXES = [
    ("kind", "kinds", "Kind", "sel"),
    ("mode", "modes", "Mode", "sel"),
    ("version", "versions", "Version", "combo"),
    ("os", "os", "OS", "sel"),
    ("os_version", "os_version", "OS version", "combo"),
    ("distro", "distro", "Distro", "sel"),
    ("distro_version", "distro_version", "Distro version", "combo"),
    ("flavor", "flavor", "Flavor", "sel"),
    ("flavor_version", "flavor_version", "Flavor version", "combo"),
    ("compiler", "compiler", "Compiler", "sel"),
    ("compiler_version", "compiler_version", "Compiler version", "combo"),
    ("arch", "arch", "Arch", "sel"),
    ("isolation", "isolation", "Isolation", "sel"),
    ("toolchain", "toolchain", "Toolchain", "sel"),
]


def matrix_axis_options(matrices):
    """Sorted distinct values per matrix axis, gathered across `matrices`'
    `config` lists. Keyed by filter param — feeds the axis dropdowns/combos."""
    opts = {}
    for param, ckey, _label, _control in _MATRIX_AXES:
        opts[param] = sorted({
            v for m in matrices for v in (m.config.get(ckey) or [])
        })
    return opts


def matrix_axis_sql_filter(query, axis_filters, dialect):
    """Add the matrix-axis filters to `query` as correlated EXISTS subqueries
    over the `test_matrices.config` JSON.

    Doing this in SQL (rather than a Python post-filter) is what makes the
    filter correct under LIMIT: SQL evaluates WHERE before LIMIT, so a page
    can't truncate rows out before the axis filter has a chance to look at
    them. Each axis value in `config` is a JSON list, so we test membership;
    "sel" axes match exactly (case-sensitive, like the old `val in list`),
    "combo" axes match a case-insensitive substring. The JSON list is
    expanded per dialect — SQLite `json_each` vs Postgres
    `json_array_elements_text` — since the two have no common operator for
    "is `x` an element of this JSON array". `ckey` comes from the fixed
    `_MATRIX_AXES` table, never user input, so interpolating it into the JSON
    path is safe; the compared value is always a bound parameter."""
    for param, ckey, _label, control in _MATRIX_AXES:
        val = axis_filters.get(param)
        if not val:
            continue
        bind = f"axis_{param}"
        if dialect == "postgresql":
            op = "ILIKE" if control == "combo" else "="
            pat = f"%{val}%" if control == "combo" else val
            clause = text(
                f"EXISTS (SELECT 1 FROM json_array_elements_text("
                f"test_matrices.config -> '{ckey}') AS _ax(v) WHERE _ax.v {op} :{bind})"
            )
        else:  # sqlite (and the default) — LIKE is case-insensitive for ASCII
            op = "LIKE" if control == "combo" else "="
            pat = f"%{val}%" if control == "combo" else val
            clause = text(
                f"EXISTS (SELECT 1 FROM json_each(test_matrices.config, '$.{ckey}') "
                f"WHERE value {op} :{bind})"
            )
        query = query.where(clause.bindparams(**{bind: pat}))
    return query


@web_router.get("/test-runs", response_class=HTMLResponse)
def runs_list(
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
    arch: str = Query(default=None),
    compiler: str = Query(default=None),
    compiler_version: str = Query(default=None),
    isolation: str = Query(default=None),
    toolchain: str = Query(default=None),
    opp_file: str = Query(default=None),
    dep: str = Query(default=None),
    ref: str = Query(default=None),
    commit: str = Query(default=None),
    version: str = Query(default=None),
    worker: str = Query(default=None),
    trigger: str = Query(default=None),
    github_owner: str = Query(default=None),
    github_repo: str = Query(default=None),
    github_pr_number: str = Query(default=None),
    verdict: str = Query(default=None),
    actual: str = Query(default=None),
    state: str = Query(default=None),
    since: str = Query(default=None),
    until: str = Query(default=None),
    view: str = Query(default="flat"),
    show_obsolete: bool = Query(default=False),
    run_ids: str = Query(default=None),
    limit: int = Query(default=None),
):
    from opp_ci.web.rollup import rollup_runs, visible_extra_dims

    # A single View axis: "flat" is the ungrouped run list (an un-merged
    # rollup); "merged"/"cartesian" are the roll-up with that grouping. Flat
    # subsumes the old grouping="none" rollup — same one-row-per-run, but the
    # richer run-list presentation (Ref/Status/Duration + Re-run/Cancel).
    if view not in ("flat", "merged", "cartesian"):
        view = "flat"
    is_rollup = view != "flat"
    grouping = "cartesian" if view == "cartesian" else "any"
    if limit is None:
        limit = 200 if is_rollup else 50

    session = SessionLocal()
    try:
        # Left-join the parent matrix run so its trigger / GitHub context can
        # be filtered; outer so standalone runs (matrix_run_id NULL) still
        # appear when no matrix-run filter is active. Eager-load verdicts so the
        # rollup's recorded_verdict doesn't N+1.
        query = (
            select(TestRun)
            .join(Test, TestRun.test_id == Test.id)
            .outerjoin(TestMatrixRun, TestRun.matrix_run_id == TestMatrixRun.id)
            .options(selectinload(TestRun.verdicts), selectinload(TestRun.test),
                     selectinload(TestRun.worker))
            .order_by(TestRun.id.desc())
            .limit(limit)
        )
        if run_ids:
            ids = [int(x) for x in run_ids.split(",") if x.strip().isdigit()]
            query = query.where(TestRun.id.in_(ids))
        query = apply_str_filter(query, Test.project, project, "contains")
        query = apply_str_filter(query, Test.kind, kind)
        query = apply_str_filter(query, Test.mode, mode)
        query = apply_str_filter(query, Test.os, os)
        query = apply_str_filter(query, Test.os_version, os_version, "prefix")
        query = apply_str_filter(query, Test.distro, distro)
        query = apply_str_filter(query, Test.distro_version, distro_version, "prefix")
        query = apply_str_filter(query, Test.flavor, flavor)
        query = apply_str_filter(query, Test.flavor_version, flavor_version, "prefix")
        query = apply_str_filter(query, Test.arch, arch)
        query = apply_str_filter(query, Test.compiler, compiler)
        query = apply_str_filter(query, Test.compiler_version, compiler_version, "prefix")
        query = apply_str_filter(query, Test.isolation, isolation)
        query = apply_str_filter(query, Test.toolchain, toolchain)
        query = apply_str_filter(query, Test.opp_file, opp_file, "contains")
        query = apply_dep_filter(query, TestRun.resolved_deps, dep)
        query = apply_str_filter(query, TestMatrixRun.trigger, trigger)
        query = apply_str_filter(query, TestMatrixRun.github_owner, github_owner, "contains")
        query = apply_str_filter(query, TestMatrixRun.github_repo, github_repo, "contains")
        query = apply_str_filter(
            query, cast(TestMatrixRun.github_pr_number, String), github_pr_number, "contains")
        query = apply_str_filter(query, TestRun.git_ref, ref, "contains")
        if commit:
            query = query.where(TestRun.commit_sha.startswith(commit))
        query = apply_str_filter(query, TestRun.version, version, "prefix")
        if worker and worker.isdigit():
            query = query.where(TestRun.worker_id == int(worker))
        # Result model mirrors the matrix-runs page: State (lifecycle), Actual
        # (outcome) and Verdict (vs expectation) as three independent filters.
        # Forgiving on URL params: a bad value just shows no rows.
        if state:
            try:
                query = query.where(TestRun.lifecycle == TestRunLifecycle(state))
            except ValueError:
                query = query.where(false())
        if actual:
            try:
                query = query.where(TestRun.result_code == TestResultCode(actual))
            except ValueError:
                query = query.where(false())
        if verdict:
            try:
                verdict_kind = TestVerdictKind(verdict)
            except ValueError:
                query = query.where(false())
            else:
                # `recorded_verdict` is computed from a run's promoted
                # TestVerdict rows; match runs carrying a verdict of this kind.
                query = query.where(exists().where(and_(
                    TestVerdict.test_run_id == TestRun.id,
                    TestVerdict.verdict == verdict_kind,
                )))
        if since:
            try:
                query = query.where(
                    TestRun.created_at >= datetime.datetime.fromisoformat(since)
                )
            except ValueError:
                pass
        if until:
            try:
                # Inclusive upper bound: a bare date covers the whole day.
                end = datetime.datetime.fromisoformat(until) + datetime.timedelta(days=1)
                query = query.where(TestRun.created_at < end)
            except ValueError:
                pass
        if is_rollup and not show_obsolete:
            # Rollup is a current-state view: drop runs overridden by a newer
            # finished run at the same (test_id, commit_sha). is_not_distinct_from
            # makes NULL commit_shas (legacy rows) compare equal to each other.
            # The flat view intentionally keeps showing every attempt.
            newer = aliased(TestRun)
            query = query.where(~exists().where(and_(
                newer.test_id == TestRun.test_id,
                newer.commit_sha.is_not_distinct_from(TestRun.commit_sha),
                newer.lifecycle == TestRunLifecycle.finished,
                newer.id > TestRun.id,
            )))

        runs = session.execute(query).scalars().all()
        summaries = rollup_runs(runs, grouping=grouping) if is_rollup else None
        extra_dims = visible_extra_dims(summaries) if summaries else []
        options = _distinct_options(
            session, Test.project, Test.kind, Test.mode, Test.os, Test.os_version,
            Test.distro, Test.distro_version, Test.flavor, Test.flavor_version,
            Test.arch, Test.compiler, Test.compiler_version, Test.isolation,
            Test.toolchain, Test.opp_file, TestRun.version,
            TestMatrixRun.trigger, TestMatrixRun.github_owner, TestMatrixRun.github_repo,
        )
        options["verdict"] = ["EXPECTED", "UNEXPECTED", "UNKNOWN"]
        options["actual"] = ["PASS", "FAIL", "ERROR", "SKIPPED"]
        options["state"] = ["queued", "running", "finished", "cancelled", "timed_out"]
        workers = session.execute(select(Worker).order_by(Worker.name)).scalars().all()
        return templates.TemplateResponse(request, "runs.html", {
            "runs": runs,
            "summaries": summaries,
            "extra_dims": extra_dims,
            "view": view,
            "show_obsolete": show_obsolete,
            "run_ids": run_ids,
            "options": options,
            "workers": workers,
            "filters": {
                "project": project or "", "kind": kind or "", "mode": mode or "",
                "os": os or "", "os_version": os_version or "",
                "distro": distro or "", "distro_version": distro_version or "",
                "flavor": flavor or "", "flavor_version": flavor_version or "",
                "arch": arch or "", "compiler": compiler or "",
                "compiler_version": compiler_version or "", "isolation": isolation or "",
                "toolchain": toolchain or "", "opp_file": opp_file or "",
                "dep": dep or "", "ref": ref or "", "commit": commit or "",
                "version": version or "",
                "worker": worker or "", "trigger": trigger or "",
                "github_owner": github_owner or "", "github_repo": github_repo or "",
                "github_pr_number": github_pr_number or "",
                "verdict": verdict or "", "actual": actual or "", "state": state or "",
                "since": since or "", "until": until or "",
            },
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

    default_expectation = read_default_expectation_code(session)
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
        "current_default_expectation": default_expectation.value if default_expectation else "",
    }


def _render_test_form(request, session, current_user, *, values=None,
                      message=None, message_type=None, status_code=200):
    """Render the new-test form, preserving the submitted `values` and an
    optional flash message.

    Used by both the GET form (empty values) and every POST validation-error
    path, so a rejected submission re-renders in place — the user keeps what
    they typed and the message names exactly what to fix — instead of being
    redirected to a blank form.
    """
    return templates.TemplateResponse(request, "test_new.html", {
        **_test_form_context(session),
        "values": values or {},
        "message": message,
        "message_type": message_type,
        **_template_globals(request, current_user),
    }, status_code=status_code)


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
    opp_file: str = Query(default=None),
    dep: str = Query(default=None),
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
        query = apply_str_filter(query, Test.name, name, "contains")
        query = apply_str_filter(query, Test.project, project, "contains")
        query = apply_str_filter(query, Test.kind, kind)
        query = apply_str_filter(query, Test.mode, mode)
        query = apply_str_filter(query, Test.os, os)
        query = apply_str_filter(query, Test.os_version, os_version, "prefix")
        query = apply_str_filter(query, Test.distro, distro)
        query = apply_str_filter(query, Test.distro_version, distro_version, "prefix")
        query = apply_str_filter(query, Test.flavor, flavor)
        query = apply_str_filter(query, Test.flavor_version, flavor_version, "prefix")
        query = apply_str_filter(query, Test.arch, arch)
        query = apply_str_filter(query, Test.compiler, compiler)
        query = apply_str_filter(query, Test.compiler_version, compiler_version, "prefix")
        query = apply_str_filter(query, Test.isolation, isolation)
        query = apply_str_filter(query, Test.toolchain, toolchain)
        query = apply_str_filter(query, Test.opp_file, opp_file, "contains")
        query = apply_dep_filter(query, Test.resolved_deps, dep)
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
            Test.toolchain, Test.opp_file,
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
                "toolchain": toolchain or "", "opp_file": opp_file or "",
                "dep": dep or "", "status": status or "",
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
        return _render_test_form(request, session, current_user,
                                 message=message, message_type=message_type)
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
    expected_result_code: str = Form(default=""),
):
    """Save a Test definition (get-or-create + optional name). With
    action=run, also queue a TestRun and land on the run; otherwise land
    on the Test detail page.

    `expected_result_code` is the inline expected-result override for a
    newly-created Test: empty means "use the global default"; PASS/FAIL/
    ERROR stamps that code so the first run already yields a verdict."""
    from opp_ci import platforms

    # Echo back exactly what the user typed so a rejected submission re-renders
    # with state intact (see _render_test_form).
    values = {
        "name": name, "project": project, "kind": kind, "mode": mode,
        "git_ref": git_ref, "version": version, "omnetpp_version": omnetpp_version,
        "os": os, "os_version": os_version, "distro": distro,
        "distro_version": distro_version, "flavor": flavor,
        "flavor_version": flavor_version, "arch": arch,
        "compiler": compiler, "compiler_version": compiler_version,
        "isolation": isolation, "toolchain": toolchain,
        "expected_result_code": expected_result_code,
    }

    session = SessionLocal()
    try:
        try:
            default_expectation = parse_expectation_override(expected_result_code)
        except ValueError:
            return _render_test_form(
                request, session, current_user, values=values, status_code=400,
                message=f"Invalid expected result: {expected_result_code!r}.",
                message_type="error")
        # Always pin the complete transitive lock; the omnetpp form field is a
        # pin into the closure, not the whole lock.
        from opp_ci.dependency import complete_lock_for_submit
        pins = {}
        if omnetpp_version and project != "omnetpp":
            pins["omnetpp"] = omnetpp_version
        try:
            resolved_deps = complete_lock_for_submit(project, pins=pins) or None
        except ValueError as e:
            return _render_test_form(
                request, session, current_user, values=values,
                message=str(e), message_type="error", status_code=400)
        try:
            r_os, r_distro, r_flavor = platforms.resolve_platform(
                os=os or None, distro=distro or None, flavor=flavor or None,
            )
        except ValueError as e:
            return _render_test_form(
                request, session, current_user, values=values,
                message=str(e), message_type="error", status_code=400)
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
            "resolved_deps": resolved_deps,
        }
        # Pin the source ref to a concrete commit so the Test's identity never
        # carries a moving branch (pinned all the way down on its source).
        if git_ref:
            from opp_ci.scheduler import resolve_source_commit
            try:
                coord["commit_sha"] = resolve_source_commit(project, git_ref)
            except ValueError as e:
                return _render_test_form(
                    request, session, current_user, values=values,
                    message=str(e), message_type="error", status_code=400)
        # Underspecified "Save" persists a *recipe* (a separate object) to
        # resolve later or per push; "Run" and fully-specified saves resolve
        # eagerly below.
        from opp_ci.persistence import (test_coord_is_recipe,
                                        get_or_create_test_recipe)
        if action != "run" and test_coord_is_recipe(coord):
            recipe = get_or_create_test_recipe(session, coord)
            if name.strip():
                try:
                    set_test_name(session, recipe, name)
                except ValueError as e:
                    session.rollback()
                    return _render_test_form(
                        request, session, current_user, values=values,
                        message=str(e), message_type="error", status_code=409)
            session.commit()
            return RedirectResponse(url=f"/tests/{recipe.id}", status_code=303)
        # Pin loose coordinate axes against the fleet (the form queues for
        # workers), then validate. If the fleet can't supply a loose axis, the
        # error names the fleet as the cause — not the user.
        from opp_ci.fleet import fleet_tags
        from opp_ci.persistence import resolve_and_validate_coord
        try:
            resolve_and_validate_coord(coord, fleet_tags(session))
        except ValueError as e:
            return _render_test_form(
                request, session, current_user, values=values,
                message=str(e), message_type="error", status_code=400)
        test = get_or_create_test(
            session, coord,
            default_expectation=default_expectation,
            expectation_set_by=current_user.display_name,
        )
        if name.strip():
            try:
                set_test_name(session, test, name)
            except ValueError as e:
                session.rollback()
                return _render_test_form(
                    request, session, current_user, values=values,
                    message=str(e), message_type="error", status_code=409)
        if action == "run":
            run = create_test_run(
                session,
                test_id=test.id,
                commit_sha=test.commit_sha,
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
        try:
            run = create_test_run(
                session,
                test_id=test.id,
                # Inherit the Test's pinned identity — its resolved dependency
                # lock and source commit — so the run builds what the Test *is*.
                commit_sha=test.commit_sha,
                resolved_deps=test.resolved_deps,
                git_ref=git_ref or None,
                version=version or None,
            )
        except ValueError as e:
            # e.g. a recipe Test — resolve it first.
            return RedirectResponse(
                url=f"/tests/{test.id}?message={e}&message_type=error",
                status_code=303)
        session.commit()
        return RedirectResponse(url=f"/test-runs/{run.id}", status_code=303)
    finally:
        session.close()


@web_router.post("/tests/{test_id}/resolve", dependencies=[Depends(require_csrf)])
def test_resolve(test_id: int,
                 current_user: User = Depends(require_user("submitter"))):
    """Resolve a recipe Test: pin its loose coordinate axes against the fleet
    and mint a runnable resolved Test, then go to it."""
    from opp_ci.fleet import fleet_tags
    from opp_ci.persistence import resolve_test_recipe
    session = SessionLocal()
    try:
        recipe = session.get(Test, test_id)
        if recipe is None:
            return RedirectResponse(url="/tests", status_code=303)
        try:
            resolved = resolve_test_recipe(
                session, recipe, fleet_tags(session),
                expectation_set_by=current_user.display_name)
            session.commit()
        except ValueError as e:
            session.rollback()
            return RedirectResponse(
                url=f"/tests/{test_id}?message={e}&message_type=error",
                status_code=303)
        return RedirectResponse(url=f"/tests/{resolved.id}", status_code=303)
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


@web_router.get("/test-runs/{run_id}/output/tail")
def run_output_tail(request: Request, run_id: int, cursor: str = Query(default=None),
                    current_user: User = Depends(require_user())):
    """Live staged-output tail for the run-detail page.

    Returns the run's stages (always in full — small) plus output lines newer
    than `cursor`, each attributed to its stage's ordinal. `done` flips true
    once the run reaches a terminal lifecycle, telling the page to reload and
    show the full stored stdout/stderr instead.
    """
    from opp_ci.run_output import STORE
    session = SessionLocal()
    try:
        run = session.execute(
            select(TestRun).where(TestRun.id == run_id)
        ).scalar_one_or_none()
        if run is None:
            raise HTTPException(status_code=404, detail=f"Run #{run_id} not found")
        lifecycle = run.lifecycle.value if run.lifecycle else None
    finally:
        session.close()
    try:
        after = int(cursor) if cursor else 0
    except (TypeError, ValueError):
        after = 0
    stages, lines, last_seq = STORE.snapshot(run_id, after)
    return JSONResponse({
        "available": True, "reason": None,
        "done": lifecycle not in ("queued", "running"),
        "stages": [_render_stage(s) for s in stages],
        "lines": _render_output_lines(lines),
        "cursor": str(last_seq) if last_seq else "",
    })


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
    opp_env_name: str = Query(default=None),
    github_owner: str = Query(default=None),
    github_repo: str = Query(default=None),
    git_url: str = Query(default=None),
    dep: str = Query(default=None),
    status: str = Query(default=None),
):
    session = SessionLocal()
    try:
        query = select(Project).order_by(Project.name)
        query = apply_str_filter(query, Project.name, name, "contains")
        query = apply_str_filter(query, Project.opp_env_name, opp_env_name, "contains")
        query = apply_str_filter(query, Project.github_owner, github_owner, "contains")
        query = apply_str_filter(query, Project.github_repo, github_repo, "contains")
        query = apply_str_filter(query, Project.git_url, git_url, "contains")
        query = apply_dep_filter(query, Project.dependency_names, dep)
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
        if status:
            projects = [p for p in projects if last_status.get(p.name) == status]

        options = _distinct_options(
            session, Project.opp_env_name, Project.github_owner, Project.github_repo,
        )
        options["status"] = ["PASS", "FAIL", "ERROR", "SKIPPED"]
        return templates.TemplateResponse(request, "projects.html", {
            "projects": projects,
            "run_counts": run_counts,
            "last_status": last_status,
            "options": options,
            "filters": {
                "name": name or "", "opp_env_name": opp_env_name or "",
                "github_owner": github_owner or "", "github_repo": github_repo or "",
                "git_url": git_url or "", "dep": dep or "", "status": status or "",
            },
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


@web_router.get("/compatibility/{project_name}", response_class=HTMLResponse)
def compatibility_page(request: Request, project_name: str,
                       current_user: User = Depends(require_user()),
                       os: str = Query(default=None, alias="os"),
                       os_version: str = Query(default=None),
                       distro: str = Query(default=None),
                       distro_version: str = Query(default=None),
                       flavor: str = Query(default=None),
                       flavor_version: str = Query(default=None),
                       compiler: str = Query(default=None),
                       compiler_version: str = Query(default=None),
                       mode: str = Query(default=None),
                       kind: str = Query(default=None),
                       toolchain: str = Query(default=None),
                       isolation: str = Query(default=None),
                       arch: str = Query(default=None),
                       show_obsolete: bool = Query(default=False)):
    from opp_ci.compatibility import _DIMENSIONS, get_compatibility_matrix
    session = SessionLocal()
    try:
        # One dropdown per execution dimension. An unset dropdown is dropped
        # from `filters` (no-op), so the bare URL matches the unfiltered view.
        dims = {
            "os": os, "os_version": os_version,
            "distro": distro, "distro_version": distro_version,
            "flavor": flavor, "flavor_version": flavor_version,
            "compiler": compiler, "compiler_version": compiler_version,
            "mode": mode, "kind": kind,
            "toolchain": toolchain, "isolation": isolation, "arch": arch,
        }
        filters = {dim: dims[dim] for dim in _DIMENSIONS if dims.get(dim)}
        result = get_compatibility_matrix(session, project_name, filters,
                                          show_obsolete=show_obsolete)
        return templates.TemplateResponse(request, "compatibility.html", {
            "project_name": project_name,
            "matrices": result["matrices"],
            "options": result["options"],
            "filters": {dim: dims.get(dim) or "" for dim in _DIMENSIONS},
            "show_obsolete": show_obsolete,
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
    opp_file: str = Query(default=None),
    status: str = Query(default=None),
    include_anonymous: bool = Query(default=False),
    error: str = Query(default=None),
):
    session = SessionLocal()
    try:
        # Matrix axes live in `config` JSON, not columns, so they're offered
        # as dropdowns/combos built from the values present across all
        # matrices and filtered in Python (see _MATRIX_AXES).
        axis_filters = {p: (request.query_params.get(p) or "")
                        for p, _, _, _ in _MATRIX_AXES}

        all_matrices = session.execute(select(TestMatrix)).scalars().all()
        options = matrix_axis_options(all_matrices)
        options["project"] = sorted({m.project for m in all_matrices if m.project})
        options["opp_file"] = sorted({m.opp_file for m in all_matrices if m.opp_file})

        # Named matrices only by default; the anonymous ad-hoc matrices are
        # pulled in with ?include_anonymous=1 (mirrors the Tests catalog).
        query = select(TestMatrix).order_by(TestMatrix.name.is_(None), TestMatrix.id)
        if not include_anonymous:
            query = query.where(TestMatrix.name.isnot(None))
        query = apply_str_filter(query, TestMatrix.name, name, "contains")
        query = apply_str_filter(query, TestMatrix.project, project, "contains")
        query = apply_str_filter(query, TestMatrix.opp_file, opp_file, "contains")
        query = matrix_axis_sql_filter(query, axis_filters, session.bind.dialect.name)
        matrices = session.execute(query).scalars().all()

        # Status / run-count of each matrix's most recent run (N+1, capped by
        # the named-only default; mirrors the Tests catalog). The status filter
        # is applied on this value.
        last_status = {}
        run_counts = {}
        for m in matrices:
            run_counts[m.id] = session.execute(
                select(func.count(TestRun.id))
                .join(TestMatrixRun, TestRun.matrix_run_id == TestMatrixRun.id)
                .where(TestMatrixRun.matrix_id == m.id)
            ).scalar()
            last = session.execute(
                select(TestMatrixRun).where(TestMatrixRun.matrix_id == m.id)
                .order_by(TestMatrixRun.id.desc()).limit(1)
            ).scalar_one_or_none()
            if last is None:
                last_status[m.id] = None
            elif last.completed_at is None:
                last_status[m.id] = "pending"
            else:
                last_status[m.id] = last.actual_summary.value if last.actual_summary else None
        if status:
            matrices = [m for m in matrices if last_status.get(m.id) == status]

        options["status"] = ["PASS", "FAIL", "ERROR", "SKIPPED", "pending"]

        return templates.TemplateResponse(request, "matrices.html", {
            "matrices": matrices,
            "last_status": last_status,
            "run_counts": run_counts,
            "options": options,
            "axes": [(p, label, control) for p, _, label, control in _MATRIX_AXES],
            "filters": {
                "name": name or "", "project": project or "",
                "opp_file": opp_file or "", "status": status or "",
                **axis_filters,
            },
            "include_anonymous": include_anonymous,
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

    default_expectation = read_default_expectation_code(session)
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
        "current_default_expectation": default_expectation.value if default_expectation else "",
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
    from opp_ci.scheduler import expand_matrix, describe_expansion

    session = SessionLocal()
    try:
        matrix = session.execute(
            select(TestMatrix).where(TestMatrix.id == matrix_id)
        ).scalar_one_or_none()
        if matrix is None:
            return HTMLResponse("<h1>Matrix not found</h1>", status_code=404)

        # Test count is derived offline from the (immutable, for a snapshot)
        # config — never stored, never a GitHub round-trip on render.
        expansion_summary = describe_expansion(matrix.config)
        # Enumerate the per-job table only when it's cheap and meaningful: a
        # resolved matrix with no moving range. expand_matrix would otherwise
        # hit GitHub for a range, or list placeholder all-None rows for a recipe.
        cfg = matrix.config or {}
        has_range = bool(cfg.get("ref_range")) or any(
            isinstance(r, str) and ".." in r for r in (cfg.get("refs") or []))
        jobs = (expand_matrix(matrix.project, matrix.config)
                if matrix.is_resolved and not has_range else [])

        recent_runs = session.execute(
            select(TestRun)
            .join(TestMatrixRun, TestRun.matrix_run_id == TestMatrixRun.id)
            .where(TestMatrixRun.matrix_id == matrix_id)
            .order_by(TestRun.id.desc()).limit(50)
        ).scalars().all()

        default_expectation = read_default_expectation_code(session)
        return templates.TemplateResponse(request, "matrix_detail.html", {
            "matrix": matrix,
            "jobs": jobs,
            "expansion_summary": expansion_summary,
            "recent_runs": recent_runs,
            "error": error,
            "current_default_expectation": default_expectation.value if default_expectation else "",
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
                                   toolchain, ref_range_base, ref_range_head,
                                   workers="", worker_tags=""):
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

    # Routing constraint (not a product axis): worker names → worker:<name>,
    # raw tags verbatim; mirrors scheduler._build_matrix_config.
    selector = [f"worker:{w}" for w in _split_csv(workers)] + _split_csv(worker_tags)
    if selector:
        config["worker_selector"] = sorted(set(selector))

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
    workers: str = Form(default=""),
    worker_tags: str = Form(default=""),
    ref_range_base: str = Form(default=""),
    ref_range_head: str = Form(default=""),
    expected_result_code: str = Form(default=""),
):
    session = SessionLocal()
    try:
        try:
            default_expectation = parse_expectation_override(expected_result_code)
        except ValueError:
            return RedirectResponse(
                url="/test-matrices/new?error=Invalid+expected+result",
                status_code=303,
            )
        config = _build_matrix_config_from_form(
            project=project, kinds=kinds, modes=modes, versions=versions,
            omnetpp_versions=omnetpp_versions, refs=refs, os=os,
            os_version=os_version, distro=distro, distro_version=distro_version,
            flavor=flavor, flavor_version=flavor_version, arch=arch,
            compiler=compiler, compiler_version=compiler_version,
            isolation=isolation, toolchain=toolchain,
            workers=workers, worker_tags=worker_tags,
            ref_range_base=ref_range_base, ref_range_head=ref_range_head,
        )
        # An underspecified matrix (no compiler/arch) is a recipe: it must be
        # resolved against the fleet before it can run.
        from opp_ci.scheduler import matrix_is_recipe
        from opp_ci.persistence import resolve_matrix_recipe
        is_recipe = matrix_is_recipe(config)
        try:
            matrix = create_matrix_from_axes(
                session, project=project, config=config, name=name or None,
                is_resolved=not is_recipe,
            )
        except ValueError:
            return RedirectResponse(
                url="/test-matrices/new?error=Matrix+already+exists",
                status_code=303,
            )
        if action == "run":
            # Save & run on a recipe resolves it first, then runs the snapshot.
            runnable = matrix
            if is_recipe:
                try:
                    runnable = resolve_matrix_recipe(session, matrix)
                except ValueError as e:
                    session.commit()  # keep the saved recipe
                    return RedirectResponse(
                        url=f"/test-matrices/{matrix.id}?error={e}", status_code=303)
            matrix_run = _queue_matrix_run(
                session, runnable, trigger="web",
                default_expectation=default_expectation,
                expectation_set_by=current_user.display_name,
            )
            session.commit()
            return RedirectResponse(url=f"/test-matrix-runs/{matrix_run.id}", status_code=303)
        session.commit()
        return RedirectResponse(url=f"/test-matrices/{matrix.id}", status_code=303)
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


@web_router.post("/test-matrices/{matrix_id}/resolve", dependencies=[Depends(require_csrf)])
def matrix_resolve(matrix_id: int,
                   current_user: User = Depends(require_user("submitter"))):
    """Resolve a recipe matrix: pin its loose coordinate axes against the fleet
    and mint a runnable snapshot matrix, then go to it."""
    from opp_ci.persistence import resolve_matrix_recipe
    session = SessionLocal()
    try:
        recipe = session.get(TestMatrix, matrix_id)
        if recipe is None:
            return RedirectResponse(url="/test-matrices", status_code=303)
        try:
            snapshot = resolve_matrix_recipe(session, recipe)
            session.commit()
        except ValueError as e:
            session.rollback()
            return RedirectResponse(
                url=f"/test-matrices/{matrix_id}?error={e}", status_code=303)
        return RedirectResponse(url=f"/test-matrices/{snapshot.id}", status_code=303)
    finally:
        session.close()


@web_router.post("/test-matrices/{matrix_id}/run", dependencies=[Depends(require_csrf)])
def matrix_run(matrix_id: int,
               current_user: User = Depends(require_user("submitter")),
               expected_result_code: str = Form(default="")):
    """Queue a TestMatrixRun for a saved matrix (from the Test Matrices
    list or the matrix detail page). `expected_result_code` overrides the
    default expectation stamped on any Test the matrix freshly creates."""
    session = SessionLocal()
    try:
        try:
            default_expectation = parse_expectation_override(expected_result_code)
        except ValueError:
            return RedirectResponse(
                url=f"/test-matrices/{matrix_id}?error=Invalid+expected+result",
                status_code=303,
            )
        matrix = session.get(TestMatrix, matrix_id)
        if matrix is None:
            return RedirectResponse(url="/test-matrices", status_code=303)
        matrix_run = _queue_matrix_run(
            session, matrix, trigger="web",
            default_expectation=default_expectation,
            expectation_set_by=current_user.display_name,
        )
        session.commit()
        return RedirectResponse(url=f"/test-matrix-runs/{matrix_run.id}", status_code=303)
    finally:
        session.close()


@web_router.get("/test-matrix-runs", response_class=HTMLResponse)
def matrix_runs_list(
    request: Request,
    current_user: User = Depends(require_user()),
    project: str = Query(default=None),
    matrix: str = Query(default=None),
    trigger: str = Query(default=None),
    ref: str = Query(default=None),
    github_owner: str = Query(default=None),
    github_repo: str = Query(default=None),
    github_commit_sha: str = Query(default=None),
    github_pr_number: str = Query(default=None),
    verdict: str = Query(default=None),
    actual: str = Query(default=None),
    state: str = Query(default=None),
    since: str = Query(default=None),
    until: str = Query(default=None),
    limit: int = Query(default=50),
):
    """Index of recent TestMatrixRun rows with their rollup verdict."""
    session = SessionLocal()
    try:
        # Matrix-axis filters apply to the joined matrix's `config` JSON, so
        # they're collected here and applied in Python after the SQL query.
        axis_filters = {p: (request.query_params.get(p) or "")
                        for p, _, _, _ in _MATRIX_AXES}

        query = (
            select(TestMatrixRun, TestMatrix)
            .join(TestMatrix, TestMatrixRun.matrix_id == TestMatrix.id)
            .order_by(TestMatrixRun.id.desc())
            .limit(limit)
        )
        query = apply_str_filter(query, TestMatrix.project, project, "contains")
        if matrix and matrix.isdigit():
            query = query.where(TestMatrixRun.matrix_id == int(matrix))
        query = apply_str_filter(query, TestMatrixRun.trigger, trigger)
        query = apply_str_filter(query, TestMatrixRun.ref, ref, "contains")
        query = apply_str_filter(query, TestMatrixRun.github_owner, github_owner, "contains")
        query = apply_str_filter(query, TestMatrixRun.github_repo, github_repo, "contains")
        query = apply_str_filter(
            query, TestMatrixRun.github_commit_sha, github_commit_sha, "contains")
        query = apply_str_filter(
            query, cast(TestMatrixRun.github_pr_number, String), github_pr_number, "contains")
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
        if state == "completed":
            query = query.where(TestMatrixRun.completed_at.isnot(None))
        elif state == "pending":
            query = query.where(TestMatrixRun.completed_at.is_(None))
        if since:
            try:
                query = query.where(
                    TestMatrixRun.created_at >= datetime.datetime.fromisoformat(since)
                )
            except ValueError:
                pass
        if until:
            try:
                # Inclusive upper bound: a bare date covers the whole day.
                end = datetime.datetime.fromisoformat(until) + datetime.timedelta(days=1)
                query = query.where(TestMatrixRun.created_at < end)
            except ValueError:
                pass
        # Matrix-axis filtering in SQL (EXISTS over the joined matrix's
        # config JSON), so it composes correctly with the LIMIT above.
        query = matrix_axis_sql_filter(query, axis_filters, session.bind.dialect.name)

        rows = session.execute(query).all()

        all_matrices = session.execute(select(TestMatrix)).scalars().all()
        options = matrix_axis_options(all_matrices)
        options["project"] = sorted({m.project for m in all_matrices if m.project})
        options.update(_distinct_options(
            session, TestMatrixRun.trigger, TestMatrixRun.github_owner,
            TestMatrixRun.github_repo,
        ))
        options["verdict"] = ["EXPECTED", "UNEXPECTED", "UNKNOWN"]
        options["actual"] = ["PASS", "FAIL", "ERROR", "SKIPPED"]
        options["state"] = ["completed", "pending"]
        matrices_opt = session.execute(
            select(TestMatrix.id, TestMatrix.name).order_by(TestMatrix.name, TestMatrix.id)
        ).all()
        return templates.TemplateResponse(request, "matrix_runs.html", {
            "rows": rows,
            "options": options,
            "axes": [(p, label, control) for p, _, label, control in _MATRIX_AXES],
            "matrices_opt": [
                {"id": mid, "name": mname or f"(anonymous #{mid})"}
                for mid, mname in matrices_opt
            ],
            "filters": {
                "project": project or "", "matrix": matrix or "", "trigger": trigger or "",
                "ref": ref or "", "github_owner": github_owner or "",
                "github_repo": github_repo or "", "github_commit_sha": github_commit_sha or "",
                "github_pr_number": github_pr_number or "", "verdict": verdict or "",
                "actual": actual or "", "state": state or "",
                "since": since or "", "until": until or "",
                **axis_filters,
            },
            **_template_globals(request, current_user),
        })
    finally:
        session.close()


def _queue_matrix_run(session, matrix, *, trigger,
                      default_expectation=USE_GLOBAL_DEFAULT,
                      expectation_set_by="system"):
    """Create a TestMatrixRun for `matrix`, expand it, and enqueue every
    cell. Returns the TestMatrixRun. Caller commits. Shared by the
    save-and-run, run-saved-matrix, and rerun handlers.

    `default_expectation` / `expectation_set_by` are forwarded to every
    `enqueue_job` so any Test the matrix freshly creates is stamped with
    the global default (or the per-submission override)."""
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
            default_expectation=default_expectation,
            expectation_set_by=expectation_set_by,
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


@web_router.get("/test-matrix-runs/{matrix_run_id}", response_class=HTMLResponse)
def matrix_run_detail(
    request: Request, matrix_run_id: int,
    current_user: User = Depends(require_user()),
    unexpected_only: int = Query(default=0),
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
        query = apply_str_filter(query, OS.name, name)
        query = apply_str_filter(query, OS.version, version, "prefix")
        query = apply_str_filter(query, OS.arch, arch)
        os_entries = session.execute(query).scalars().all()

        options = _distinct_options(session, OS.name, OS.version, OS.arch)
        return templates.TemplateResponse(request, "os.html", {
            "os_entries": os_entries,
            "options": options,
            "filters": {
                "name": name or "", "version": version or "", "arch": arch or "",
            },
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
        query = apply_str_filter(query, Compiler.name, name)
        query = apply_str_filter(query, Compiler.version, version, "prefix")
        compilers = session.execute(query).scalars().all()

        options = _distinct_options(session, Compiler.name, Compiler.version)
        return templates.TemplateResponse(request, "compilers.html", {
            "compilers": compilers,
            "options": options,
            "filters": {"name": name or "", "version": version or ""},
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


@web_router.get("/workers/{worker_id}", response_class=HTMLResponse)
def worker_detail(request: Request, worker_id: int,
                  current_user: User = Depends(require_user())):
    """Worker detail page. Admins get inline edit/enable/disable/delete controls."""
    import datetime as _dt
    from opp_ci.config import WORKER_HEARTBEAT_TIMEOUT

    session = SessionLocal()
    try:
        w = _worker_or_404(session, worker_id)
        now = _dt.datetime.utcnow()
        threshold = now - _dt.timedelta(seconds=WORKER_HEARTBEAT_TIMEOUT)
        connected = w.last_heartbeat is not None and w.last_heartbeat > threshold
        rf = w.run_filters or {}

        def _rf_form(axis):
            spec = rf.get(axis) or {}
            mode = "allow" if "allow" in spec else "deny" if "deny" in spec else ""
            values = ", ".join(spec.get(mode, [])) if mode else ""
            return {"mode": mode, "values": values}

        return templates.TemplateResponse(request, "worker_detail.html", {
            "w": w,
            "connected": connected,
            "tags_str": ", ".join(w.tags or []),
            "run_filters_str": _format_run_filters(w.run_filters),
            "rf_isolation": _rf_form("isolation"),
            "rf_toolchain": _rf_form("toolchain"),
            "heartbeat_timeout": WORKER_HEARTBEAT_TIMEOUT,
            "is_admin": current_user.role == "admin",
            "message": request.query_params.get("message"),
            "error": request.query_params.get("error"),
            **_template_globals(request, current_user),
        })
    finally:
        session.close()


@web_router.post("/workers/{worker_id}/update", dependencies=[Depends(require_csrf)])
def worker_update_web(worker_id: int,
                      current_user: User = Depends(require_user("admin")),
                      concurrency: int = Form(...), tags: str = Form(default=""),
                      isolation_mode: str = Form(default=""),
                      isolation_values: str = Form(default=""),
                      toolchain_mode: str = Form(default=""),
                      toolchain_values: str = Form(default="")):
    session = SessionLocal()
    try:
        worker = session.execute(
            select(Worker).where(Worker.id == worker_id)).scalar_one_or_none()
        if worker is None:
            return RedirectResponse(url="/workers?error=Worker+not+found", status_code=303)
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
        # Merge the isolation/toolchain form fields into the worker's existing
        # run-filters, leaving any other axes (set via CLI) untouched.
        new_filters = dict(worker.run_filters or {})
        for axis, mode, raw in (("isolation", isolation_mode, isolation_values),
                                ("toolchain", toolchain_mode, toolchain_values)):
            values = [v.strip() for v in raw.split(",") if v.strip()]
            if mode in ("allow", "deny") and values:
                new_filters[axis] = {mode: values}
            else:
                new_filters.pop(axis, None)
        try:
            update_worker(session, worker_id, concurrency=concurrency,
                          tags=tag_list, run_filters=new_filters)
        except ValueError as e:
            return RedirectResponse(
                url=f"/workers/{worker_id}?error={quote_plus(str(e))}", status_code=303)
        session.commit()
        return RedirectResponse(
            url=f"/workers/{worker_id}?message=Worker+updated", status_code=303)
    finally:
        session.close()


@web_router.post("/workers/{worker_id}/toggle", dependencies=[Depends(require_csrf)])
def worker_toggle_web(worker_id: int,
                      current_user: User = Depends(require_user("admin")),
                      enabled: str = Form(...)):
    want = enabled == "true"
    session = SessionLocal()
    try:
        worker = update_worker(session, worker_id, enabled=want)
        if worker is None:
            return RedirectResponse(url="/workers?error=Worker+not+found", status_code=303)
        session.commit()
        state = "enabled" if want else "disabled"
        return RedirectResponse(
            url=f"/workers/{worker_id}?message=Worker+{state}", status_code=303)
    finally:
        session.close()


@web_router.post("/workers/{worker_id}/shutdown", dependencies=[Depends(require_csrf)])
def worker_shutdown_web(worker_id: int,
                        current_user: User = Depends(require_user("admin"))):
    """Ask a worker to terminate; the coordinator relays it on the next
    poll/heartbeat and the service manager restarts the worker."""
    session = SessionLocal()
    try:
        worker = update_worker(session, worker_id, shutdown_requested=True)
        if worker is None:
            return RedirectResponse(url="/workers?error=Worker+not+found", status_code=303)
        session.commit()
        return RedirectResponse(
            url=f"/workers/{worker_id}?message=Shutdown+requested+%E2%80%94+the+worker+will+restart+shortly",
            status_code=303)
    finally:
        session.close()


@web_router.post("/workers/{worker_id}/delete", dependencies=[Depends(require_csrf)])
def worker_delete_web(worker_id: int,
                      current_user: User = Depends(require_user("admin"))):
    import datetime as _dt
    session = SessionLocal()
    try:
        result = delete_worker(
            session, worker_id, _dt.datetime.utcnow(), cfg.MAX_RECLAIMS)
        if result is None:
            return RedirectResponse(url="/workers?error=Worker+not+found", status_code=303)
        session.commit()
        return RedirectResponse(url="/workers?message=Worker+deleted", status_code=303)
    finally:
        session.close()


# ── Logs ───────────────────────────────────────────────────────────────
#
# Viewers for the serve and worker process logs, read from systemd-journald
# (see opp_ci/journal.py). Gated at `submitter` (so readonly users can't
# see logs that may contain tokens/paths, but operators below admin can).
# The pages poll a `…/tail` endpoint with the journal's opaque cursor for
# incremental updates.


def _render_log_entries(entries):
    """journald entries → JSON-able rows with pre-rendered, escaped HTML."""
    rows = []
    for e in entries:
        rows.append({
            "ts": e["ts"].strftime("%H:%M:%S") if e["ts"] else "",
            "priority": e["priority"],
            "html": str(_ansi_to_html(e["message"])),
        })
    return rows


def _tail_response(unit, cursor):
    """Read a unit's journal from `cursor` on and return the tail JSON."""
    from opp_ci.journal import read_unit, JournalUnavailable
    try:
        entries, last_cursor = read_unit(unit, cursor=cursor or None)
    except JournalUnavailable as e:
        return JSONResponse({"available": False, "reason": e.reason,
                             "entries": [], "cursor": cursor})
    return JSONResponse({"available": True, "reason": None,
                         "entries": _render_log_entries(entries),
                         "cursor": last_cursor})


def _level_to_priority(level):
    """Map a Python log level to the syslog-style priority the log_view
    template colours by (<=3 error, 4 warning, else info)."""
    if level is None:
        return 6
    if level >= logging.ERROR:    # ERROR, CRITICAL
        return 3
    if level >= logging.WARNING:  # WARNING
        return 4
    return 6                      # INFO, DEBUG and below


def _render_shipped_entries(entries):
    """Shipped worker-log rows → the same shape `_render_log_entries` emits.

    A shipped entry is {seq, ts (epoch float), level (Python level), msg}.
    """
    rows = []
    for e in entries:
        ts = e.get("ts")
        try:
            hhmmss = (datetime.datetime.fromtimestamp(
                ts, datetime.timezone.utc).strftime("%H:%M:%S")
                if ts is not None else "")
        except (ValueError, OSError, OverflowError):
            hhmmss = ""
        rows.append({
            "ts": hhmmss,
            "priority": _level_to_priority(e.get("level")),
            "html": str(_ansi_to_html(e.get("msg") or "")),
        })
    return rows


def _shipped_tail_response(worker_id, cursor):
    """Tail JSON served from a remote worker's shipped-log store."""
    from opp_ci.worker_logs import STORE
    try:
        after = int(cursor) if cursor else 0
    except (TypeError, ValueError):
        # A leftover journald cursor from before this worker started
        # shipping — start fresh rather than error.
        after = 0
    entries, last_seq = STORE.since(worker_id, after)
    return JSONResponse({"available": True, "reason": None,
                         "entries": _render_shipped_entries(entries),
                         "cursor": str(last_seq) if last_seq else ""})


def _render_output_lines(lines):
    """Live stage output lines → rows with escaped, ANSI-rendered html, kept
    tagged with their stage ordinal and stream so the UI can place + mark them."""
    return [{"ordinal": l.get("ordinal"), "stream": l.get("stream", "out"),
             "html": str(_ansi_to_html(l.get("text") or ""))} for l in lines]


def _render_stage(stage):
    """Stage summary for the run-detail view (command stays plain text — the
    UI sets it via textContent, so no escaping needed here)."""
    return {
        "ordinal": stage.get("ordinal"),
        "name": stage.get("name"),
        "command": stage.get("command"),
        "status": stage.get("status"),
        "exit": stage.get("exit"),
    }


def _worker_or_404(session, worker_id):
    w = session.execute(
        select(Worker).where(Worker.id == worker_id)).scalar_one_or_none()
    if w is None:
        raise HTTPException(status_code=404, detail=f"Worker #{worker_id} not found")
    return w


@web_router.get("/logs", response_class=HTMLResponse)
def logs_hub(request: Request, current_user: User = Depends(require_user("submitter"))):
    """Hub listing every log source: serve plus each registered worker."""
    from opp_ci.config import WORKER_HEARTBEAT_TIMEOUT
    session = SessionLocal()
    try:
        workers = session.execute(select(Worker).order_by(Worker.name)).scalars().all()
        now = datetime.datetime.utcnow()
        threshold = now - datetime.timedelta(seconds=WORKER_HEARTBEAT_TIMEOUT)
        worker_rows = [{
            "worker": w,
            "connected": w.last_heartbeat is not None and w.last_heartbeat > threshold,
        } for w in workers]
        return templates.TemplateResponse(request, "logs.html", {
            "coordinator_unit": cfg.COORDINATOR_UNIT,
            "worker_rows": worker_rows,
            **_template_globals(request, current_user),
        })
    finally:
        session.close()


@web_router.get("/logs/coordinator", response_class=HTMLResponse)
def coordinator_log(request: Request, current_user: User = Depends(require_user("submitter"))):
    return templates.TemplateResponse(request, "log_view.html", {
        "log_title": "Coordinator log",
        "log_subtitle": cfg.COORDINATOR_UNIT,
        "tail_url": "/logs/coordinator/tail",
        "back_url": "/logs",
        **_template_globals(request, current_user),
    })


@web_router.get("/logs/coordinator/tail")
def coordinator_log_tail(request: Request, cursor: str = Query(default=None),
                         current_user: User = Depends(require_user("submitter"))):
    return _tail_response(cfg.COORDINATOR_UNIT, cursor)


@web_router.get("/logs/worker/{worker_id}", response_class=HTMLResponse)
def worker_log(request: Request, worker_id: int,
               current_user: User = Depends(require_user("submitter"))):
    from opp_ci.journal import worker_unit_name, JournalUnavailable
    session = SessionLocal()
    try:
        w = _worker_or_404(session, worker_id)
        try:
            subtitle = worker_unit_name(w.name)
        except JournalUnavailable as e:
            subtitle = e.reason
        return templates.TemplateResponse(request, "log_view.html", {
            "log_title": f"Worker log — {w.name}",
            "log_subtitle": subtitle,
            "tail_url": f"/logs/worker/{w.id}/tail",
            "back_url": "/logs",
            **_template_globals(request, current_user),
        })
    finally:
        session.close()


@web_router.get("/logs/worker/{worker_id}/tail")
def worker_log_tail(request: Request, worker_id: int, cursor: str = Query(default=None),
                    current_user: User = Depends(require_user("submitter"))):
    from opp_ci.journal import worker_unit_name, JournalUnavailable
    from opp_ci.worker_logs import STORE
    session = SessionLocal()
    try:
        name = _worker_or_404(session, worker_id).name
    finally:
        session.close()
    # A worker on another host ships its logs to the coordinator (its journal
    # lives on its own box). Serve those when present; otherwise fall back to
    # the local journal — which covers the co-located `local` worker and a
    # remote worker that hasn't reported its first batch yet.
    if STORE.has(worker_id):
        return _shipped_tail_response(worker_id, cursor)
    try:
        unit = worker_unit_name(name)
    except JournalUnavailable as e:
        return JSONResponse({"available": False, "reason": e.reason,
                             "entries": [], "cursor": cursor})
    return _tail_response(unit, cursor)


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
def admin_page(request: Request, current_user: User = Depends(require_user("admin")),
               message: str = Query(default=None), error: str = Query(default=None),
               new_token: str = Query(default=None), worker_token: str = Query(default=None)):
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

        default_expectation = read_default_expectation_code(session)

        return templates.TemplateResponse(request, "admin.html", {
            "stats": stats,
            "workers": workers,
            "tokens": tokens,
            "rules": rules,
            "recent_errors": recent_errors,
            "default_expectation": default_expectation.value if default_expectation else "",
            "message": message,
            "error": error,
            "new_token": new_token,
            "worker_token": worker_token,
            **_template_globals(request, current_user),
        })
    finally:
        session.close()


@web_router.post("/admin/settings/default-expectation", dependencies=[Depends(require_csrf)])
def admin_set_default_expectation(
    current_user: User = Depends(require_user("admin")),
    expected_result_code: str = Form(default=""),
):
    """Set the global default expected result stamped on newly-created
    Tests. Empty value = "no default" (new Tests start UNKNOWN)."""
    session = SessionLocal()
    try:
        try:
            code = TestResultCode(expected_result_code) if expected_result_code else None
        except ValueError:
            return RedirectResponse(
                url="/admin?error=Invalid+expected+result", status_code=303,
            )
        set_default_expectation_code(session, code, set_by=current_user.display_name)
        session.commit()
        return RedirectResponse(url="/admin?message=Default+expectation+updated", status_code=303)
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


@web_router.post("/admin/coordinator/shutdown", dependencies=[Depends(require_csrf)])
def admin_coordinator_shutdown(current_user: User = Depends(require_user("admin"))):
    """Terminate the coordinator process; the service manager restarts it."""
    import os
    import signal
    import threading
    who = current_user.username or current_user.github_username or f"id={current_user.id}"
    _logger.warning("Coordinator shutdown requested by admin '%s'", who)
    # Send SIGINT — uvicorn shuts down gracefully on both SIGINT and SIGTERM,
    # but only SIGINT exits 0; SIGTERM exits 143. Use SIGINT so a web-requested
    # shutdown returns success, exactly like Ctrl-C at the console. Fire it only
    # after this response is flushed — otherwise the browser sees a dropped
    # connection instead of the redirect.
    threading.Timer(0.5, lambda: os.kill(os.getpid(), signal.SIGINT)).start()
    return RedirectResponse(
        url="/admin?message=Coordinator+shutting+down+%E2%80%94+the+service+will+restart+it",
        status_code=303)


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
