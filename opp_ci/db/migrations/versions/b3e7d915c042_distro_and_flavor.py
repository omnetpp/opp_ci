"""add distro/flavor columns and wipe os-dependent rows

Revision ID: b3e7d915c042
Revises: a7f2e9d4b15c
Create Date: 2026-06-04
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b3e7d915c042"
down_revision: Union[str, None] = "a7f2e9d4b15c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Existing test_runs and test_matrices store the old "os holds a distro"
    # encoding. Backward compatibility is out of scope (see plan); drop the
    # data so the new code starts from a clean slate.
    op.execute("DELETE FROM test_runs")
    op.execute("DELETE FROM test_matrices")

    op.add_column("test_runs", sa.Column("distro", sa.String, nullable=True))
    op.add_column("test_runs", sa.Column("distro_version", sa.String, nullable=True))
    op.add_column("test_runs", sa.Column("flavor", sa.String, nullable=True))
    op.add_column("test_runs", sa.Column("flavor_version", sa.String, nullable=True))


def downgrade() -> None:
    op.drop_column("test_runs", "flavor_version")
    op.drop_column("test_runs", "flavor")
    op.drop_column("test_runs", "distro_version")
    op.drop_column("test_runs", "distro")
