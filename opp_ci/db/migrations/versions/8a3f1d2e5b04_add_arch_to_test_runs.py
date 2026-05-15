"""add arch column to test_runs

Revision ID: 8a3f1d2e5b04
Revises: 4e2a31c0a4b1
Create Date: 2026-05-15
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "8a3f1d2e5b04"
down_revision: Union[str, None] = "4e2a31c0a4b1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("test_runs", sa.Column("arch", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("test_runs", "arch")
