import datetime
import secrets

from sqlalchemy import Column, Integer, String, DateTime, Float, ForeignKey, Text, Enum, JSON, Boolean
from sqlalchemy.orm import declarative_base, relationship
import enum

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


class TestRunStatus(enum.Enum):
    queued = "queued"
    running = "running"
    PASS = "PASS"
    FAIL = "FAIL"
    ERROR = "ERROR"


class TestRun(Base):
    __tablename__ = "test_runs"

    id = Column(Integer, primary_key=True)
    project = Column(String, nullable=False)
    test_type = Column(String, nullable=False)
    mode = Column(String, nullable=True)
    os = Column(String, nullable=True)
    os_version = Column(String, nullable=True)
    compiler = Column(String, nullable=True)
    compiler_version = Column(String, nullable=True)
    isolation = Column(String, nullable=True)   # "none" | "docker"; None == "none"
    toolchain = Column(String, nullable=True)   # "none" | "nix";    None == "none"
    platform_desc = Column(String, nullable=True)
    git_ref = Column(String, nullable=True)
    commit_sha = Column(String, nullable=True)
    version = Column(String, nullable=True)
    resolved_deps = Column(JSON, nullable=True)
    opp_file = Column(String, nullable=True)
    matrix_id = Column(Integer, ForeignKey("test_matrices.id"), nullable=True)
    worker_id = Column(Integer, ForeignKey("workers.id"), nullable=True)
    status = Column(Enum(TestRunStatus), nullable=False, default=TestRunStatus.queued)
    started_at = Column(DateTime, default=datetime.datetime.utcnow)
    finished_at = Column(DateTime, nullable=True)
    duration_seconds = Column(Float, nullable=True)
    trigger = Column(String, default="manual")  # manual, webhook, schedule, remote
    github_owner = Column(String, nullable=True)
    github_repo = Column(String, nullable=True)
    github_commit_sha = Column(String, nullable=True)
    github_pr_number = Column(Integer, nullable=True)
    github_status_url = Column(String, nullable=True)

    results = relationship("TestResult", back_populates="test_run", cascade="all, delete-orphan")
    worker = relationship("Worker", backref="test_runs")

    def __repr__(self):
        return f"<TestRun(id={self.id}, project={self.project!r}, test_type={self.test_type!r}, status={self.status.value!r})>"


class TestResult(Base):
    __tablename__ = "test_results"

    id = Column(Integer, primary_key=True)
    test_run_id = Column(Integer, ForeignKey("test_runs.id"), nullable=False)
    result_code = Column(String, nullable=False)
    stdout = Column(Text, nullable=True)
    stderr = Column(Text, nullable=True)
    details = Column(JSON, nullable=True)

    test_run = relationship("TestRun", back_populates="results")

    def __repr__(self):
        return f"<TestResult(id={self.id}, test_run_id={self.test_run_id}, result_code={self.result_code!r})>"
