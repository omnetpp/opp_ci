import datetime

from sqlalchemy import Column, Integer, String, DateTime, Float, ForeignKey, Text, Enum, JSON
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
    tier = Column(Integer, default=2)
    dependency_names = Column(JSON, default=list)

    def __repr__(self):
        return f"<Project(name={self.name!r}, tier={self.tier})>"


class TestRunStatus(enum.Enum):
    queued = "queued"
    running = "running"
    passed = "passed"
    failed = "failed"
    error = "error"


class TestRun(Base):
    __tablename__ = "test_runs"

    id = Column(Integer, primary_key=True)
    project = Column(String, nullable=False)
    test_type = Column(String, nullable=False)
    status = Column(Enum(TestRunStatus), nullable=False, default=TestRunStatus.queued)
    started_at = Column(DateTime, default=datetime.datetime.utcnow)
    finished_at = Column(DateTime, nullable=True)
    duration_seconds = Column(Float, nullable=True)
    trigger = Column(String, default="manual")

    results = relationship("TestResult", back_populates="test_run", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<TestRun(id={self.id}, project={self.project!r}, test_type={self.test_type!r}, status={self.status.value!r})>"


class TestResult(Base):
    __tablename__ = "test_results"

    id = Column(Integer, primary_key=True)
    test_run_id = Column(Integer, ForeignKey("test_runs.id"), nullable=False)
    result_code = Column(String, nullable=False)
    stdout = Column(Text, nullable=True)
    stderr = Column(Text, nullable=True)

    test_run = relationship("TestRun", back_populates="results")

    def __repr__(self):
        return f"<TestResult(id={self.id}, test_run_id={self.test_run_id}, result_code={self.result_code!r})>"
