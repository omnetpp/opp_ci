"""add users table for web UI login

Revision ID: c5d8a4f12b30
Revises: 9f1c4d2a8e10
Create Date: 2026-06-04
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c5d8a4f12b30"
down_revision: Union[str, None] = "9f1c4d2a8e10"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("github_user_id", sa.Integer(), nullable=True),
        sa.Column("github_username", sa.String(), nullable=True),
        sa.Column("username", sa.String(), nullable=True),
        sa.Column("password_hash", sa.String(), nullable=True),
        sa.Column("role", sa.String(), nullable=False, server_default="readonly"),
        sa.Column("role_locked", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("last_login_at", sa.DateTime(), nullable=True),
        sa.Column("last_role_sync_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("github_user_id", name="uq_users_github_user_id"),
        sa.UniqueConstraint("username", name="uq_users_username"),
    )


def downgrade() -> None:
    op.drop_table("users")
