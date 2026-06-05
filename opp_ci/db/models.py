import datetime
import enum
import hashlib
import json
import secrets

from sqlalchemy import Column, Integer, String, DateTime, Float, ForeignKey, Text, Enum, JSON, Boolean
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
    arch = Column(String, default="x86_64")

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
    name = Column(String, unique=True, nullable=False)
    project = Column(String, nullable=False)
    opp_file = Column(String, nullable=True)
    config = Column(JSON, nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class Worker(Base):
    __tablename__ = "workers"

    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True, nullable=False)
    token = Column(String, unique=True, nullable=False, default=lambda: secrets.token_urlsafe(32))
    tags = Column(JSON, default=list)  # ["linux", "amd64", "perf-counters"]
    concurrency = Column(Integer, default=1)
    status = Column(String, default="offline")  # online, offline, busy
    last_heartbeat = Column(DateTime, nullable=True)
    registered_at = Column(DateTime, default=datetime.datetime.utcnow)
    current_job_count = Column(Integer, default=0)

    def __repr__(self):
        return f"<Worker(name={self.name!r}, status={self.status!r}, tags={self.tags})>"

    @property
    def is_available(self):
        return self.status == "online" and self.current_job_count < self.concurrency


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
    "project", "kind", "mode",
    "os", "os_version",
    "distro", "distro_version",
    "flavor", "flavor_version",
    "arch",
    "compiler", "compiler_version",
    "isolation", "toolchain",
    "opp_file",
)


def compute_test_coord_hash(coord):
    """SHA-256 hex of sorted-keys canonical JSON over `TEST_COORD_FIELDS`.

    Unknown keys in `coord` are ignored; missing keys are treated as None
    so the hash is stable regardless of whether the caller passed every
    field explicitly. The mutable columns (`name`,
    `expected_result_code`, `expected_result_description`) are
    deliberately excluded.
    """
    payload = {field: coord.get(field) for field in TEST_COORD_FIELDS}
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

    id = Column(Integer, primary_key=True)

    # The only mutable column on a Test row. Excluded from coord_hash so
    # renaming never affects dedup. Expectations live on
    # ExpectedTestResult — append-only edit log keyed by test_id.
    name = Column(String, nullable=True)

    # Coordinate fields (immutable after creation; see TEST_COORD_FIELDS).
    project = Column(String, nullable=False)
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

    coord_hash = Column(String(64), unique=True, nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    runs = relationship("TestRun", back_populates="test", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Test(id={self.id}, project={self.project!r}, kind={self.kind!r})>"


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
    """One row per cell of a TestMatrixRun.

    A cell pins a Test to a specific TestRun (cache hits reuse a prior
    row; cache misses point at a freshly-queued row) plus the
    `ExpectedTestResult` row in force at recording time. The cell has at
    most one promotion event (verdict written) and is then frozen. The
    cell's lifecycle is derived from the underlying TestRun.lifecycle —
    not stored — to avoid two sources of truth.
    """
    __tablename__ = "test_verdicts"

    id = Column(Integer, primary_key=True)
    matrix_run_id = Column(Integer, ForeignKey("test_matrix_runs.id"), nullable=False, index=True)
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
    duration_seconds = Column(Float, nullable=True)

    # Outcome (populated iff lifecycle == finished)
    result_code = Column(Enum(TestResultCode), nullable=True)
    stdout = Column(Text, nullable=True)
    stderr = Column(Text, nullable=True)
    details = Column(JSON, nullable=True)

    # Best-effort system facts captured at run start.
    system_snapshot = Column(JSON, nullable=True)

    # Content-addressable cache key (Phase 4). NULL on legacy rows.
    cache_fingerprint = Column(String, nullable=True, index=True)

    test = relationship("Test", back_populates="runs")
    matrix_run = relationship("TestMatrixRun", back_populates="test_runs")
    worker = relationship("Worker", backref="test_runs")

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
