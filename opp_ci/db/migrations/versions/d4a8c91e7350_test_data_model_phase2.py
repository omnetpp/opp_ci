"""test data model phase 2 — expectations, verdicts, rollup, cache key

Revision ID: d4a8c91e7350
Revises: a7f2e9d4b15c
Create Date: 2026-06-05

Phase 2 of the test data model (plan/pending/test-data-model-redesign.md):
  * New `expected_test_results` table — append-only edit log for the
    expected outcome of a Test, keyed by `test_id`.
  * New `test_verdicts` table — one row per cell of a matrix run, with
    `verdict` (EXPECTED / UNEXPECTED / UNKNOWN), pinned `expectation_id`,
    and an FK to the underlying `TestRun`. Cache hits share a TestRun
    across many TestVerdict rows.
  * Rollup / counter / verdict columns on `test_matrix_runs`, plus a
    `ref` snapshot and `completed_at`.
  * `cache_fingerprint` on `test_runs` (used by Phase 4).
  * Existing Test.expected_result_* values are migrated into one initial
    ExpectedTestResult row per non-null expectation; existing TestRuns
    that are children of a TestMatrixRun get one TestVerdict each, with
    counters and verdicts backfilled from the current observation.
"""
from typing import Sequence, Union
import datetime

from alembic import op
import sqlalchemy as sa


revision: str = "d4a8c91e7350"
down_revision: Union[str, None] = "b3e7d915c042"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_RESULT_CODE = sa.Enum("PASS", "FAIL", "ERROR", "SKIPPED", name="testresultcode")
_VERDICT_KIND = sa.Enum("EXPECTED", "UNEXPECTED", "UNKNOWN", name="testverdictkind")


def _worst(actual, candidate):
    """Pick the more severe of two result codes (PASS < FAIL < ERROR)."""
    rank = {"PASS": 0, "SKIPPED": 0, "FAIL": 1, "ERROR": 2}
    if actual is None:
        return candidate
    if candidate is None:
        return actual
    return candidate if rank.get(candidate, 0) > rank.get(actual, 0) else actual


def _compute_verdict(actual, expected):
    if expected is None:
        return "UNKNOWN"
    if actual is None:
        return None
    return "EXPECTED" if actual == expected else "UNEXPECTED"


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    now = datetime.datetime.utcnow()

    # ── 1) new tables ─────────────────────────────────────────────────
    op.create_table(
        "expected_test_results",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("test_id", sa.Integer(), sa.ForeignKey("tests.id"), nullable=False),
        sa.Column("expected_result_code", _RESULT_CODE, nullable=True),
        sa.Column("expected_result_description", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("set_by", sa.String(), nullable=True),
        sa.Column("set_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_expected_test_results_test_id",
        "expected_test_results", ["test_id"],
    )
    op.create_index(
        "ix_expected_test_results_set_at",
        "expected_test_results", ["set_at"],
    )

    op.create_table(
        "test_verdicts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("matrix_run_id", sa.Integer(),
                  sa.ForeignKey("test_matrix_runs.id"), nullable=False),
        sa.Column("test_id", sa.Integer(), sa.ForeignKey("tests.id"), nullable=False),
        sa.Column("test_run_id", sa.Integer(),
                  sa.ForeignKey("test_runs.id"), nullable=False),
        sa.Column("expectation_id", sa.Integer(),
                  sa.ForeignKey("expected_test_results.id"), nullable=True),
        sa.Column("verdict", _VERDICT_KIND, nullable=True),
        sa.Column("recorded_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("cache_hit", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
    )
    op.create_index("ix_test_verdicts_matrix_run_id", "test_verdicts", ["matrix_run_id"])
    op.create_index("ix_test_verdicts_test_id", "test_verdicts", ["test_id"])
    op.create_index("ix_test_verdicts_test_run_id", "test_verdicts", ["test_run_id"])

    # ── 2) columns on test_matrix_runs ────────────────────────────────
    with op.batch_alter_table("test_matrix_runs") as batch:
        batch.add_column(sa.Column("ref", sa.String(), nullable=True))
        batch.add_column(sa.Column("completed_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("pass_count", sa.Integer(), nullable=False,
                                   server_default="0"))
        batch.add_column(sa.Column("fail_count", sa.Integer(), nullable=False,
                                   server_default="0"))
        batch.add_column(sa.Column("error_count", sa.Integer(), nullable=False,
                                   server_default="0"))
        batch.add_column(sa.Column("expected_count", sa.Integer(), nullable=False,
                                   server_default="0"))
        batch.add_column(sa.Column("unexpected_count", sa.Integer(), nullable=False,
                                   server_default="0"))
        batch.add_column(sa.Column("unknown_count", sa.Integer(), nullable=False,
                                   server_default="0"))
        batch.add_column(sa.Column("cache_hit_count", sa.Integer(), nullable=False,
                                   server_default="0"))
        batch.add_column(sa.Column("total_count", sa.Integer(), nullable=False,
                                   server_default="0"))
        batch.add_column(sa.Column("actual_summary", _RESULT_CODE, nullable=True))
        batch.add_column(sa.Column("verdict", _VERDICT_KIND, nullable=True))

    # ── 3) cache_fingerprint on test_runs ─────────────────────────────
    with op.batch_alter_table("test_runs") as batch:
        batch.add_column(sa.Column("cache_fingerprint", sa.String(), nullable=True))
    op.create_index("ix_test_runs_cache_fingerprint", "test_runs", ["cache_fingerprint"])

    # ── 4) migrate Test.expected_* into expected_test_results ─────────
    tests = sa.table(
        "tests",
        sa.column("id", sa.Integer),
        sa.column("expected_result_code", sa.String),
        sa.column("expected_result_description", sa.Text),
    )
    expectations = sa.table(
        "expected_test_results",
        sa.column("test_id", sa.Integer),
        sa.column("expected_result_code", sa.String),
        sa.column("expected_result_description", sa.Text),
        sa.column("reason", sa.Text),
        sa.column("set_by", sa.String),
        sa.column("set_at", sa.DateTime),
    )
    rows = bind.execute(sa.select(
        tests.c.id, tests.c.expected_result_code, tests.c.expected_result_description,
    )).all()
    for row in rows:
        if row.expected_result_code is None and row.expected_result_description is None:
            continue
        bind.execute(expectations.insert().values(
            test_id=row.id,
            expected_result_code=row.expected_result_code,
            expected_result_description=row.expected_result_description,
            reason=None,
            set_by="migration",
            set_at=now,
        ))

    # ── 5) drop expected_* from tests ─────────────────────────────────
    with op.batch_alter_table("tests") as batch:
        batch.drop_column("expected_result_code")
        batch.drop_column("expected_result_description")

    # ── 6) backfill TestVerdict rows + matrix-run rollups ─────────────
    # Existing TestRuns that point at a TestMatrixRun become one
    # TestVerdict each, attributed to themselves (no cache hits in the
    # legacy data). Counters and verdicts are recomputed in-place.
    matrix_runs = sa.table(
        "test_matrix_runs",
        sa.column("id", sa.Integer),
        sa.column("pass_count", sa.Integer),
        sa.column("fail_count", sa.Integer),
        sa.column("error_count", sa.Integer),
        sa.column("expected_count", sa.Integer),
        sa.column("unexpected_count", sa.Integer),
        sa.column("unknown_count", sa.Integer),
        sa.column("cache_hit_count", sa.Integer),
        sa.column("total_count", sa.Integer),
        sa.column("actual_summary", sa.String),
        sa.column("verdict", sa.String),
        sa.column("completed_at", sa.DateTime),
    )
    test_runs = sa.table(
        "test_runs",
        sa.column("id", sa.Integer),
        sa.column("test_id", sa.Integer),
        sa.column("matrix_run_id", sa.Integer),
        sa.column("lifecycle", sa.String),
        sa.column("result_code", sa.String),
        sa.column("finished_at", sa.DateTime),
        sa.column("created_at", sa.DateTime),
    )
    verdicts = sa.table(
        "test_verdicts",
        sa.column("matrix_run_id", sa.Integer),
        sa.column("test_id", sa.Integer),
        sa.column("test_run_id", sa.Integer),
        sa.column("expectation_id", sa.Integer),
        sa.column("verdict", sa.String),
        sa.column("recorded_at", sa.DateTime),
        sa.column("created_at", sa.DateTime),
        sa.column("cache_hit", sa.Boolean),
    )

    runs = bind.execute(sa.select(
        test_runs.c.id, test_runs.c.test_id, test_runs.c.matrix_run_id,
        test_runs.c.lifecycle, test_runs.c.result_code, test_runs.c.finished_at,
        test_runs.c.created_at,
    ).where(test_runs.c.matrix_run_id.isnot(None))).all()

    # Map test_id → (most recent expectation_id, expected_result_code).
    current_expect = {}
    for row in bind.execute(sa.select(
        sa.column("id"), sa.column("test_id"), sa.column("expected_result_code"),
        sa.column("set_at"),
    ).select_from(sa.table("expected_test_results"))).all():
        prev = current_expect.get(row.test_id)
        if prev is None or row.set_at > prev[2]:
            current_expect[row.test_id] = (row.id, row.expected_result_code, row.set_at)

    counters = {}
    for run in runs:
        actual = run.result_code if run.lifecycle == "finished" else None
        exp = current_expect.get(run.test_id)
        expectation_id = exp[0] if exp else None
        expected_code = exp[1] if exp else None
        v = _compute_verdict(actual, expected_code)
        bind.execute(verdicts.insert().values(
            matrix_run_id=run.matrix_run_id,
            test_id=run.test_id,
            test_run_id=run.id,
            expectation_id=expectation_id,
            verdict=v,
            recorded_at=run.finished_at if v is not None else None,
            created_at=run.created_at or now,
            cache_hit=False,
        ))

        c = counters.setdefault(run.matrix_run_id, {
            "PASS": 0, "FAIL": 0, "ERROR": 0,
            "EXPECTED": 0, "UNEXPECTED": 0, "UNKNOWN": 0,
            "total": 0, "actual_summary": None, "all_finished": True,
            "latest_finished": None,
        })
        c["total"] += 1
        if run.lifecycle != "finished":
            c["all_finished"] = False
        if actual:
            c[actual] = c.get(actual, 0) + 1
            c["actual_summary"] = _worst(c["actual_summary"], actual)
        if v == "EXPECTED":
            c["EXPECTED"] += 1
        elif v == "UNEXPECTED":
            c["UNEXPECTED"] += 1
        elif v == "UNKNOWN":
            c["UNKNOWN"] += 1
        if run.finished_at:
            if c["latest_finished"] is None or run.finished_at > c["latest_finished"]:
                c["latest_finished"] = run.finished_at

    for matrix_run_id, c in counters.items():
        if c["UNEXPECTED"] > 0:
            verdict = "UNEXPECTED"
        elif c["UNKNOWN"] > 0:
            verdict = "UNKNOWN"
        elif c["EXPECTED"] > 0:
            verdict = "EXPECTED"
        else:
            verdict = None
        bind.execute(matrix_runs.update().where(matrix_runs.c.id == matrix_run_id).values(
            pass_count=c["PASS"], fail_count=c["FAIL"], error_count=c["ERROR"],
            expected_count=c["EXPECTED"], unexpected_count=c["UNEXPECTED"],
            unknown_count=c["UNKNOWN"], cache_hit_count=0, total_count=c["total"],
            actual_summary=c["actual_summary"], verdict=verdict,
            completed_at=c["latest_finished"] if c["all_finished"] else None,
        ))


def downgrade() -> None:
    with op.batch_alter_table("tests") as batch:
        batch.add_column(sa.Column("expected_result_code", _RESULT_CODE, nullable=True))
        batch.add_column(sa.Column("expected_result_description", sa.Text(), nullable=True))

    # Best-effort restore: copy the most recent expectation per test back
    # onto the Test row. Retraction rows (NULL code) leave the column NULL.
    bind = op.get_bind()
    tests = sa.table(
        "tests",
        sa.column("id", sa.Integer),
        sa.column("expected_result_code", sa.String),
        sa.column("expected_result_description", sa.Text),
    )
    expectations = sa.table(
        "expected_test_results",
        sa.column("id", sa.Integer),
        sa.column("test_id", sa.Integer),
        sa.column("expected_result_code", sa.String),
        sa.column("expected_result_description", sa.Text),
        sa.column("set_at", sa.DateTime),
    )
    latest = {}
    for row in bind.execute(sa.select(
        expectations.c.id, expectations.c.test_id,
        expectations.c.expected_result_code,
        expectations.c.expected_result_description,
        expectations.c.set_at,
    )).all():
        prev = latest.get(row.test_id)
        if prev is None or row.set_at > prev[2]:
            latest[row.test_id] = (row.expected_result_code,
                                   row.expected_result_description, row.set_at)
    for test_id, (code, descr, _) in latest.items():
        bind.execute(tests.update().where(tests.c.id == test_id).values(
            expected_result_code=code,
            expected_result_description=descr,
        ))

    op.drop_index("ix_test_runs_cache_fingerprint", table_name="test_runs")
    with op.batch_alter_table("test_runs") as batch:
        batch.drop_column("cache_fingerprint")

    with op.batch_alter_table("test_matrix_runs") as batch:
        for col in ("ref", "completed_at", "pass_count", "fail_count",
                    "error_count", "expected_count", "unexpected_count",
                    "unknown_count", "cache_hit_count", "total_count",
                    "actual_summary", "verdict"):
            batch.drop_column(col)

    op.drop_index("ix_test_verdicts_test_run_id", table_name="test_verdicts")
    op.drop_index("ix_test_verdicts_test_id", table_name="test_verdicts")
    op.drop_index("ix_test_verdicts_matrix_run_id", table_name="test_verdicts")
    op.drop_table("test_verdicts")
    op.drop_index("ix_expected_test_results_set_at", table_name="expected_test_results")
    op.drop_index("ix_expected_test_results_test_id", table_name="expected_test_results")
    op.drop_table("expected_test_results")
