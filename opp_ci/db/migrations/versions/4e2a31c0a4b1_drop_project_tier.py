"""drop tier column from projects

Revision ID: 4e2a31c0a4b1
Revises: 11b0cd9aa9a6
Create Date: 2026-05-14
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "4e2a31c0a4b1"
down_revision: Union[str, None] = "11b0cd9aa9a6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("projects") as batch_op:
        batch_op.drop_column("tier")


def downgrade() -> None:
    with op.batch_alter_table("projects") as batch_op:
        batch_op.add_column(sa.Column("tier", sa.Integer(), nullable=True))
