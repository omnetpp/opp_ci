"""add isolation and toolchain to test_runs

Revision ID: 11b0cd9aa9a6
Revises:
Create Date: 2026-05-14
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "11b0cd9aa9a6"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("test_runs", sa.Column("isolation", sa.String(), nullable=True))
    op.add_column("test_runs", sa.Column("toolchain", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("test_runs", "toolchain")
    op.drop_column("test_runs", "isolation")
