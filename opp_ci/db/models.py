import datetime
import enum
import hashlib
import json
import secrets

from sqlalchemy import Column, Integer, String, DateTime, Float, ForeignKey, Text, Enum, JSON, Boolean, Index, text
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


class Project(Base):
    __tablename__ = "projects"

    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True, nullable=False)
    opp_env_name = Column(String, nullable=True)
    github_owner = Column(String, nullable=True)
    github_repo = Column(String, nullable=True)
    git_url = Column(String, nullable=True)
    dependency_names = Column(JSON, default=list)

    def __repr__(self):
        return f"<Project(name={self.name!r})>"


class Version(Base):
    __tablename__ = "versions"

    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False)
    opp_env_version = Column(String, nullable=True)
    git_ref = Column(String, nullable=True)
    label = Column(String, nullable=True)
    resolved_dependencies = Column(JSON, nullable=True)

    project_rel = relationship("Project", backref="versions")

    def __repr__(self):
        return f"<Version(project_id={self.project_id}, label={self.label!r}, git_ref={self.git_ref!r})>"


class OS(Base):
    __tablename__ = "os_entries"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    version = Column(String, nullable=True)
    arch = Column(String, default="amd64")

    def __repr__(self):
        return f"<OS({self.name} {self.version or ''} {self.arch})>"

    @property
    def label(self):
        parts = [self.name]
        if self.version:
            parts.append(self.version)
        return " ".join(parts)


class Compiler(Base):
    __tablename__ = "compilers"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    version = Column(String, nullable=True)

    def __repr__(self):
        return f"<Compiler({self.name}-{self.version or '?'})>"

    @property
    def label(self):
        return f"{self.name}-{self.version}" if self.version else self.name


class TestMatrix(Base):
    __tablename__ = "test_matrices"

    id = Column(Integer, primary_key=True)
    # Optional, editable label. NULL = an anonymous matrix (run once,
    # never named). A UNIQUE constraint keeps named matrices distinct;
    # NULLs are distinct under SQL UNIQUE, so any number may be anonymous.
    name = Column(String, unique=True, nullable=True)
    project = Column(String, nullable=False)
    opp_file = Column(String, nullable=True)
    config = Column(JSON, nullable=False)
    # Content hash over (project, opp_file, canonical config). A resolved matrix
    # is content-addressed by this — re-resolving a recipe to the same pinned
    # content reuses the snapshot instead of minting a duplicate (mirrors
    # Test.coord_hash). NULL allowed on legacy rows.
    matrix_hash = Column(String(64), nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    # Resolve-in-place state (Phase 2/3), mirroring Test. An unresolved matrix
    # (recipe) carries moving refs / ranges / loose axes; resolve() pins them
    # and expand() fans the resolved matrix into resolved Tests. Defaults True
    # so existing matrices stay runnable until the recipe flow lands.
    is_resolved = Column(Boolean, nullable=False, default=True)
    resolved_from = Column(Integer, ForeignKey("test_matrices.id"), nullable=True)

    recipe = relationship("TestMatrix", remote_side=[id],
                          backref="resolved_instances")

    @property
    def display_name(self):
        """Human label that never renders blank for anonymous matrices."""
        return self.name or f"(anonymous #{self.id})"

    @property
    def is_recipe(self):
        """A recipe matrix carries moving refs/ranges; resolve+expand to run."""
        return not self.is_resolved

    @property
    def state_label(self):
        return "recipe" if self.is_recipe else "resolved"


class Worker(Base):
    __tablename__ = "workers"

    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True, nullable=False)
    token = Column(String, unique=True, nullable=False, default=lambda: secrets.token_urlsafe(32))
    tags = Column(JSON, default=list)  # ["linux", "amd64", "perf-counters"]
    concurrency = Column(Integer, default=1)
    status = Column(String, default="offline")  # online, offline, busy
    # status is auto-managed by heartbeat/poll/reaper; `enabled` is the
    # independent, admin-controlled on/off switch (mirrors ApiToken/User).
    enabled = Column(Boolean, default=True, nullable=False)
    last_heartbeat = Column(DateTime, nullable=True)
    registered_at = Column(DateTime, default=datetime.datetime.utcnow)
    current_job_count = Column(Integer, default=0)
    software_version = Column(String, nullable=True)  # opp_ci version last reported by the worker

    def __repr__(self):
        return f"<Worker(name={self.name!r}, status={self.status!r}, tags={self.tags})>"

    @property
    def is_available(self):
        # A disabled worker still heartbeats (so we see it's alive) but is
        # handed no jobs — disable is a drain, not a kill.
        return (self.enabled
                and self.status == "online"
                and self.current_job_count < self.concurrency)


class ApiToken(Base):
    __tablename__ = "api_tokens"

    id = Column(Integer, primary_key=True)
    token = Column(String, unique=True, nullable=False, default=lambda: secrets.token_urlsafe(32))
    name = Column(String, nullable=False)
    role = Column(String, nullable=False, default="readonly")  # admin, submitter, worker, readonly
    enabled = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    def __repr__(self):
        return f"<ApiToken(name={self.name!r}, role={self.role!r})>"


class User(Base):
    """A human user of the web UI.

    Either `github_user_id` is set (GitHub OAuth login) or `username` is set
    (local password login), or both (a local account that has linked a
    GitHub identity). `role_locked` means an admin has pinned the role —
    subsequent OAuth logins won't recompute it from GitHub team membership.
    """
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    github_user_id = Column(Integer, unique=True, nullable=True)
    github_username = Column(String, nullable=True)
    username = Column(String, unique=True, nullable=True)
    password_hash = Column(String, nullable=True)
    role = Column(String, nullable=False, default="readonly")
    role_locked = Column(Boolean, default=False, nullable=False)
    enabled = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    last_login_at = Column(DateTime, nullable=True)
    last_role_sync_at = Column(DateTime, nullable=True)

    def __repr__(self):
        ident = self.username or f"@{self.github_username}" or f"id={self.id}"
        return f"<User({ident}, role={self.role!r})>"

    @property
    def display_name(self):
        if self.github_username:
            return f"@{self.github_username}"
        return self.username or f"user#{self.id}"


class AutoTestRule(Base):
    __tablename__ = "auto_test_rules"

    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False)
    rule_type = Column(String, nullable=False)  # branch, pr, tag
    pattern = Column(String, nullable=False)    # glob pattern, e.g. "master", "topic/*", "*"
    matrix_id = Column(Integer, ForeignKey("test_matrices.id"), nullable=True)
    enabled = Column(Integer, default=1)

    project_rel = relationship("Project", backref="auto_test_rules")
    matrix_rel = relationship("TestMatrix", backref="auto_test_rules")

    def __repr__(self):
        return f"<AutoTestRule(id={self.id}, rule_type={self.rule_type!r}, pattern={self.pattern!r})>"


# ── Test data model (phase 1) ──────────────────────────────────────────
#
# See plan/pending/test-data-model-redesign.md and
# plan/pending/test-data-model-phase-1-schema.md.
#
# Test       — deduped coordinate row + editable metadata
# TestMatrix — matrix definition (above)
# TestRun    — per-attempt lifecycle + outcome row
# TestMatrixRun — first-class grouping of TestRuns from one matrix submission


# Closed field list that goes into Test.coord_hash. Order matters only
# in that the JSON serializer sorts keys; what matters is the set of
# fields. Adding or removing one re-keys every Test.
TEST_COORD_FIELDS = (
    "project", "commit_sha", "kind", "mode",
    "os", "os_version",
    "distro", "distro_version",
    "flavor", "flavor_version",
    "arch",
    "compiler", "compiler_version",
    "isolation", "toolchain",
    "opp_file",
)


def normalise_deps(resolved_deps):
    """Canonicalise a resolved_deps mapping into a sorted-keys dict.

    `None` and the empty dict are equivalent (no pinned dependencies), so
    they hash identically. Defined here (the lowest-level module) so both
    Test identity and the cache fingerprint share one canonicalisation;
    `fingerprint.py` imports this rather than redefining it.
    """
    if not resolved_deps:
        return {}
    if isinstance(resolved_deps, dict):
        return {k: resolved_deps[k] for k in sorted(resolved_deps)}
    # Defensive: stringify whatever it is so callers don't crash.
    return {"_raw": str(resolved_deps)}


def compute_test_coord_hash(coord):
    """SHA-256 hex of sorted-keys canonical JSON over the Test coordinate.

    The coordinate is `TEST_COORD_FIELDS` plus the resolved dependency
    versions (`resolved_deps`): the dependency environment is part of what
    a Test *is*, so e.g. mm1k against omnetpp 6.4.0 and 6.3.0 are distinct
    Tests. `resolved_deps` is normalised (`None` == `{}`, key order
    irrelevant) so a pinned and an auto-resolved identical version collapse
    to the same Test — identity tracks resolved versions, not pin intent.

    The project **source commit** (`commit_sha`) is part of the coordinate
    too (Phase 2 "resolve in place"): a resolved Test is pinned to one
    commit, so two commits are distinct Tests, and re-running a Test rebuilds
    the same source. An unresolved recipe leaves it None.

    Unknown keys in `coord` are ignored; missing keys are treated as None
    so the hash is stable regardless of whether the caller passed every
    field explicitly. The mutable columns (`name`,
    `expected_result_code`, `expected_result_description`) are
    deliberately excluded.
    """
    payload = {field: coord.get(field) for field in TEST_COORD_FIELDS}
    payload["resolved_deps"] = normalise_deps(coord.get("resolved_deps"))
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _canonical_config(config):
    """Canonicalise a matrix config for hashing: sort dict keys and sort each
    axis's value list (a matrix axis is an unordered set — its cartesian
    product doesn't depend on order), so configs that differ only in ordering
    hash identically."""
    def canon(v):
        if isinstance(v, dict):
            return {k: canon(v[k]) for k in sorted(v)}
        if isinstance(v, list):
            items = [canon(x) for x in v]
            try:
                return sorted(items)
            except TypeError:      # non-scalar items — keep order
                return items
        return v
    return canon(config or {})


def compute_matrix_hash(project, opp_file, config):
    """SHA-256 hex over a TestMatrix's content (project, opp_file, canonical
    config). A resolved matrix is content-addressed by this, so re-resolving a
    recipe to the same pinned content reuses the snapshot (mirrors
    `compute_test_coord_hash` for Tests)."""
    payload = {
        "project": project,
        "opp_file": opp_file,
        "config": _canonical_config(config),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class TestResultCode(enum.Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    ERROR = "ERROR"
    SKIPPED = "SKIPPED"


class TestRunLifecycle(enum.Enum):
    queued = "queued"
    running = "running"
    finished = "finished"
    cancelled = "cancelled"
    timed_out = "timed_out"


class TestVerdictKind(enum.Enum):
    EXPECTED = "EXPECTED"
    UNEXPECTED = "UNEXPECTED"
    UNKNOWN = "UNKNOWN"


class Test(Base):
    __tablename__ = "tests"

    # Named tests must be unique so "run by name" is unambiguous; unnamed
    # (NULL) tests are unconstrained. Partial index — NULLs excluded.
    __table_args__ = (
        Index(
            "uq_tests_name", "name", unique=True,
            sqlite_where=text("name IS NOT NULL"),
            postgresql_where=text("name IS NOT NULL"),
        ),
    )

    id = Column(Integer, primary_key=True)

    # The only mutable column on a Test row. Excluded from coord_hash so
    # renaming never affects dedup. Expectations live on
    # ExpectedTestResult — append-only edit log keyed by test_id.
    name = Column(String, nullable=True)

    # Coordinate fields (immutable after creation; see TEST_COORD_FIELDS).
    project = Column(String, nullable=False)
    # Resolved project source commit (Phase 2). Part of the coordinate /
    # identity: a resolved Test is pinned to one commit, so two commits are
    # distinct Tests. NULL on an unresolved recipe (and on legacy rows).
    commit_sha = Column(String, nullable=True)
    kind = Column(String, nullable=False)             # was "test" in legacy schema
    mode = Column(String, nullable=True)
    os = Column(String, nullable=True)                # "Linux" | "Windows" | "MacOS"
    os_version = Column(String, nullable=True)        # only Windows/MacOS
    distro = Column(String, nullable=True)            # Linux only
    distro_version = Column(String, nullable=True)
    flavor = Column(String, nullable=True)            # Linux only
    flavor_version = Column(String, nullable=True)
    arch = Column(String, nullable=True)
    compiler = Column(String, nullable=True)
    compiler_version = Column(String, nullable=True)
    isolation = Column(String, nullable=True)         # "none" | "podman"
    toolchain = Column(String, nullable=True)         # "none" | "nix"
    opp_file = Column(String, nullable=True)

    # Resolved dependency versions (e.g. {"omnetpp": "6.4.0"}). Part of the
    # coordinate / identity — folded into coord_hash via normalise_deps —
    # not a per-run knob. NULL/{} means no pinned dependencies.
    resolved_deps = Column(JSON, nullable=True)

    coord_hash = Column(String(64), unique=True, nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    # Resolve-in-place state (Phase 2). A recipe (is_resolved=False) carries
    # loose / moving inputs (a branch, "latest", an underspecified axis) and
    # may not be run; resolve() mints a *new* resolved Test (is_resolved=True)
    # with everything pinned and points resolved_from back at the recipe. The
    # recipe is preserved, so re-resolving it later mints another snapshot —
    # that lineage is the moving-target history. Defaults True so a directly
    # submitted, already-concrete Test is runnable without an extra step.
    is_resolved = Column(Boolean, nullable=False, default=True)
    resolved_from = Column(Integer, ForeignKey("tests.id"), nullable=True)

    runs = relationship("TestRun", back_populates="test", cascade="all, delete-orphan")
    recipe = relationship("Test", remote_side=[id], backref="resolved_instances")

    def __repr__(self):
        return f"<Test(id={self.id}, project={self.project!r}, kind={self.kind!r})>"

    # ── Resolve-in-place view helpers (Phase 5) ───────────────────────
    @property
    def is_recipe(self):
        """A recipe (unresolved) carries loose/moving inputs and can't run."""
        return not self.is_resolved

    @property
    def state_label(self):
        """'recipe' or 'resolved' — drives the UI state badge / action."""
        return "recipe" if self.is_recipe else "resolved"

    @property
    def short_commit(self):
        """First 8 chars of the pinned source commit, or None on a recipe."""
        return self.commit_sha[:8] if self.commit_sha else None


class TestMatrixRun(Base):
    """One row per submission of a TestMatrix.

    Groups the per-Test `TestRun`s spawned from one expansion so they can
    be tracked, cancelled, or queried as a unit. Counter columns
    (pass/fail/error/expected/unexpected/unknown/cache_hit/total) and the
    derived `actual_summary` / `verdict` are updated eagerly each time a
    child `TestVerdict` finalizes; the UI / API never has to fan out
    across cells to render a rollup.
    """
    __tablename__ = "test_matrix_runs"

    id = Column(Integer, primary_key=True)
    matrix_id = Column(Integer, ForeignKey("test_matrices.id"), nullable=False)
    trigger = Column(String, default="manual")  # manual, web, remote, webhook, tag, schedule, rerun, cli
    ref = Column(String, nullable=True)  # git ref / tag for which the run was triggered
    github_owner = Column(String, nullable=True)
    github_repo = Column(String, nullable=True)
    github_commit_sha = Column(String, nullable=True)
    github_pr_number = Column(Integer, nullable=True)
    github_status_url = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)

    # Rollup counters — updated atomically as each child TestVerdict finalizes.
    pass_count = Column(Integer, nullable=False, default=0)
    fail_count = Column(Integer, nullable=False, default=0)
    error_count = Column(Integer, nullable=False, default=0)
    expected_count = Column(Integer, nullable=False, default=0)
    unexpected_count = Column(Integer, nullable=False, default=0)
    unknown_count = Column(Integer, nullable=False, default=0)
    cache_hit_count = Column(Integer, nullable=False, default=0)
    total_count = Column(Integer, nullable=False, default=0)

    # Derived summaries; recomputed in lockstep with the counters.
    actual_summary = Column(Enum(TestResultCode), nullable=True)
    verdict = Column(Enum(TestVerdictKind), nullable=True)

    matrix = relationship("TestMatrix", backref="matrix_runs")
    test_runs = relationship("TestRun", back_populates="matrix_run")
    verdicts = relationship("TestVerdict", back_populates="matrix_run",
                            cascade="all, delete-orphan")

    def __repr__(self):
        return f"<TestMatrixRun(id={self.id}, matrix_id={self.matrix_id}, trigger={self.trigger!r})>"


class ExpectedTestResult(Base):
    """Append-only edit log for the expected outcome of a Test.

    "Current expectation for a Test" = most recent row by `set_at`. No row
    at all = "no expectation ever declared." A row with
    `expected_result_code IS NULL` is an explicit retraction
    (distinguishable from never-set, and itself an audited event). Edits
    are inserts; nothing is ever updated.
    """
    __tablename__ = "expected_test_results"

    id = Column(Integer, primary_key=True)
    test_id = Column(Integer, ForeignKey("tests.id"), nullable=False, index=True)
    expected_result_code = Column(Enum(TestResultCode), nullable=True)
    expected_result_description = Column(Text, nullable=True)
    reason = Column(Text, nullable=True)
    set_by = Column(String, nullable=True)
    set_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False, index=True)

    test = relationship("Test", backref="expected_results")

    def __repr__(self):
        code = self.expected_result_code.value if self.expected_result_code else "RETRACT"
        return f"<ExpectedTestResult(test_id={self.test_id}, code={code}, set_at={self.set_at!s})>"


class TestVerdict(Base):
    """One recorded verdict of a TestRun, optionally within a TestMatrixRun.

    Pins a Test to a specific TestRun (cache hits reuse a prior row;
    cache misses point at a freshly-queued row) plus the
    `ExpectedTestResult` row in force at recording time. Has at most one
    promotion event (verdict written) and is then frozen. Its lifecycle
    is derived from the underlying TestRun.lifecycle — not stored — to
    avoid two sources of truth.

    `matrix_run_id` is set when the row is a cell of a TestMatrixRun;
    NULL when it is a standalone run's own verdict (created in
    `finalize_verdict_for_run`). Standalone rows are skipped by the
    matrix rollup, which keys on a non-NULL `matrix_run_id`.
    """
    __tablename__ = "test_verdicts"

    id = Column(Integer, primary_key=True)
    matrix_run_id = Column(Integer, ForeignKey("test_matrix_runs.id"), nullable=True, index=True)
    test_id = Column(Integer, ForeignKey("tests.id"), nullable=False, index=True)
    test_run_id = Column(Integer, ForeignKey("test_runs.id"), nullable=False, index=True)
    expectation_id = Column(Integer, ForeignKey("expected_test_results.id"), nullable=True)
    verdict = Column(Enum(TestVerdictKind), nullable=True)
    recorded_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)
    # True iff this cell reused a pre-existing TestRun (cache hit).
    cache_hit = Column(Boolean, default=False, nullable=False)

    matrix_run = relationship("TestMatrixRun", back_populates="verdicts")
    test = relationship("Test")
    test_run = relationship("TestRun", backref="verdicts")
    expectation = relationship("ExpectedTestResult")

    def __repr__(self):
        v = self.verdict.value if self.verdict else "pending"
        return f"<TestVerdict(id={self.id}, matrix_run={self.matrix_run_id}, test={self.test_id}, verdict={v})>"


class TestRun(Base):
    """One row per attempt to run a Test.

    Carries the per-attempt context (commit_sha, git_ref, version,
    resolved_deps, worker_id, timing), the lifecycle state, and, once the
    lifecycle reaches `finished`, the outcome columns
    (`result_code`/`stdout`/`stderr`/`details`). `system_snapshot` is a
    nullable JSON blob captured at run start — Postgres TOAST keeps it
    out-of-line and lazy-loaded.
    """
    __tablename__ = "test_runs"

    id = Column(Integer, primary_key=True)
    test_id = Column(Integer, ForeignKey("tests.id"), nullable=False)
    matrix_run_id = Column(Integer, ForeignKey("test_matrix_runs.id"), nullable=True)
    worker_id = Column(Integer, ForeignKey("workers.id"), nullable=True)

    # Per-attempt context
    commit_sha = Column(String, nullable=True)
    git_ref = Column(String, nullable=True)
    version = Column(String, nullable=True)
    resolved_deps = Column(JSON, nullable=True)

    # Lifecycle (always set)
    lifecycle = Column(Enum(TestRunLifecycle), nullable=False, default=TestRunLifecycle.queued)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    test_exec_seconds = Column(Float, nullable=True)

    # Outcome (populated iff lifecycle == finished)
    result_code = Column(Enum(TestResultCode), nullable=True)
    stdout = Column(Text, nullable=True)
    stderr = Column(Text, nullable=True)
    details = Column(JSON, nullable=True)

    # Best-effort system facts captured at run start.
    system_snapshot = Column(JSON, nullable=True)

    # Content-addressable cache key (Phase 4). NULL on legacy rows.
    cache_fingerprint = Column(String, nullable=True, index=True)

    # How many times this run has been reclaimed (re-queued after its
    # worker went dark mid-run). Bounds poison-pill loops: once it exceeds
    # config.MAX_RECLAIMS the run is retired to timed_out. See
    # opp_ci.persistence.reclaim_orphaned_runs / retire_poison_run.
    reclaim_count = Column(Integer, nullable=False, default=0, server_default="0")

    test = relationship("Test", back_populates="runs")
    matrix_run = relationship("TestMatrixRun", back_populates="test_runs")
    worker = relationship("Worker", backref="test_runs")
    stages = relationship(
        "TestRunStage", back_populates="test_run",
        cascade="all, delete-orphan", order_by="TestRunStage.ordinal")

    def __repr__(self):
        lc = self.lifecycle.value if self.lifecycle else "?"
        rc = self.result_code.value if self.result_code else "-"
        return f"<TestRun(id={self.id}, test_id={self.test_id}, lifecycle={lc!r}, result={rc!r})>"

    # ── View-side accessors ───────────────────────────────────────────
    #
    # Coordinate fields live on the joined `Test` row; templates, the
    # rollup module, and helpers read them off the TestRun directly
    # (`run.project`, `run.kind`, `run.mode`, …). Note `run.test` is
    # the SQLAlchemy relationship returning the Test row, not the test
    # kind — use `run.kind` (or `run.test.kind`) for the kind string.

    @property
    def duration_seconds(self):
        """Total wall-clock the run was worked on: worker-claim (started_at)
        to finish (finished_at). Always available once a run has started —
        including ERROR/timed-out runs that never reached the test command.
        This is the "Duration" shown across the UI.

        Distinct from the stored ``test_exec_seconds`` column, which is the
        test-execution stopwatch (excludes install/setup) and is NULL when
        the run died before the test ran. See run_detail's "Test time".
        """
        if self.started_at and self.finished_at:
            return (self.finished_at - self.started_at).total_seconds()
        return None

    @property
    def project(self):
        return self.test.project if self.test else None

    @property
    def kind(self):
        return self.test.kind if self.test else None

    @property
    def mode(self):
        return self.test.mode if self.test else None

    @property
    def os(self):
        return self.test.os if self.test else None

    @property
    def os_version(self):
        return self.test.os_version if self.test else None

    @property
    def distro(self):
        return self.test.distro if self.test else None

    @property
    def distro_version(self):
        return self.test.distro_version if self.test else None

    @property
    def flavor(self):
        return self.test.flavor if self.test else None

    @property
    def flavor_version(self):
        return self.test.flavor_version if self.test else None

    @property
    def arch(self):
        return self.test.arch if self.test else None

    @property
    def compiler(self):
        return self.test.compiler if self.test else None

    @property
    def compiler_version(self):
        return self.test.compiler_version if self.test else None

    @property
    def isolation(self):
        return self.test.isolation if self.test else None

    @property
    def toolchain(self):
        return self.test.toolchain if self.test else None

    @property
    def opp_file(self):
        return self.test.opp_file if self.test else None

    @property
    def matrix_id(self):
        return self.matrix_run.matrix_id if self.matrix_run else None

    @property
    def github_owner(self):
        return self.matrix_run.github_owner if self.matrix_run else None

    @property
    def github_repo(self):
        return self.matrix_run.github_repo if self.matrix_run else None

    @property
    def github_commit_sha(self):
        return self.matrix_run.github_commit_sha if self.matrix_run else None

    @property
    def github_pr_number(self):
        return self.matrix_run.github_pr_number if self.matrix_run else None

    @property
    def trigger(self):
        return self.matrix_run.trigger if self.matrix_run else None

    @property
    def effective_status(self):
        """Single-string label combining lifecycle and outcome.

        Returns one of: "queued", "running", "cancelled", "timed_out",
        or the outcome value ("PASS"/"FAIL"/"ERROR"/"SKIPPED") when the
        run is finished. Used by templates and rollup that grew up
        around the legacy single-enum status.
        """
        if self.lifecycle == TestRunLifecycle.finished and self.result_code:
            return self.result_code.value
        return self.lifecycle.value if self.lifecycle else None

    @property
    def lifecycle_status(self):
        """Lifecycle state as a string ("queued"/"running"/"finished"/…)."""
        return self.lifecycle.value if self.lifecycle else None

    @property
    def result_status(self):
        """Outcome code ("PASS"/"FAIL"/"ERROR"/"SKIPPED") once the run is
        finished, else None. Unlike `effective_status`, this never carries
        a lifecycle state — so a running run has no result, not "running".
        """
        if self.lifecycle == TestRunLifecycle.finished and self.result_code:
            return self.result_code.value
        return None

    @property
    def recorded_verdict(self):
        """Verdict recorded for this run by its most recent `TestVerdict`.

        Every finished run has at least its own standalone verdict
        (matrix_run_id NULL); a run reused across matrix cells may have
        several. We return the latest promoted one (by `recorded_at`,
        falling back to `created_at`). Returns
        "EXPECTED"/"UNEXPECTED"/"UNKNOWN", or None if no verdict has been
        recorded yet (e.g. the run never finished with a result).
        """
        promoted = [v for v in self.verdicts if v.verdict is not None]
        if not promoted:
            return None
        latest = max(promoted, key=lambda v: v.recorded_at or v.created_at)
        return latest.verdict.value

    @property
    def deps_label(self):
        """Resolved dependencies as a stable "name=version, …" string
        (sorted by name) for display and roll-up grouping; None when the run
        pins no dependencies. Matches the run/test detail rendering, and is
        where the omnetpp version lives — distinct from `version`, which is
        the project's own version."""
        deps = self.resolved_deps or {}
        if not deps:
            return None
        return ", ".join(f"{k}={v}" for k, v in sorted(deps.items()))


class AppSetting(Base):
    """Tiny key/value store for global, admin-editable settings.

    One row per setting key. `value` is a string (NULL allowed); callers
    interpret it. Currently holds the default expected result stamped on
    newly-created Tests (key `default_expected_result`).
    """
    __tablename__ = "app_settings"

    key = Column(String, primary_key=True)
    value = Column(String, nullable=True)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow,
                        onupdate=datetime.datetime.utcnow)
    updated_by = Column(String, nullable=True)

    def __repr__(self):
        return f"<AppSetting(key={self.key!r}, value={self.value!r})>"


class TestRunStage(Base):
    """One captured stage of a run's execution (deps.install, project.build,
    test.run, …) — the persisted form of the live stage stream.

    Written at result time from the worker's assembled stage tree, so a
    finished run renders the same staged view the live page showed. ``output``
    holds the stage's captured lines as a JSON list of {"stream", "text"} so
    the finished render can still mark stderr and show the command. Schema
    changes here are handled by recreating the DB, not migrating.
    """
    __tablename__ = "test_run_stages"

    id = Column(Integer, primary_key=True)
    test_run_id = Column(Integer, ForeignKey("test_runs.id"), nullable=False, index=True)
    ordinal = Column(Integer, nullable=False)
    name = Column(String, nullable=False)
    command = Column(Text, nullable=True)
    status = Column(String, nullable=False)        # running/passed/failed/skipped
    exit_code = Column(Integer, nullable=True)
    duration_seconds = Column(Float, nullable=True)
    output = Column(JSON, nullable=True)           # [{"stream": ..., "text": ...}, ...]

    test_run = relationship("TestRun", back_populates="stages")

    def __repr__(self):
        return (f"<TestRunStage(run={self.test_run_id}, ord={self.ordinal}, "
                f"name={self.name!r}, status={self.status!r})>")
