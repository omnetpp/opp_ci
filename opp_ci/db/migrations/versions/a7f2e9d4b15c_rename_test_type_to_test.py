"""rename test_type column to test, and matrix-config key test_types to tests

Revision ID: a7f2e9d4b15c
Revises: c5d8a4f12b30
Create Date: 2026-06-04
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a7f2e9d4b15c"
down_revision: Union[str, None] = "c5d8a4f12b30"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _rewrite_matrix_configs(old_key: str, new_key: str) -> None:
    bind = op.get_bind()
    matrices = sa.table(
        "test_matrices",
        sa.column("id", sa.Integer),
        sa.column("config", sa.JSON),
    )
    for row in bind.execute(sa.select(matrices.c.id, matrices.c.config)).all():
        if not isinstance(row.config, dict) or old_key not in row.config:
            continue
        new_config = dict(row.config)
        new_config[new_key] = new_config.pop(old_key)
        bind.execute(
            sa.update(matrices).where(matrices.c.id == row.id).values(config=new_config)
        )


def upgrade() -> None:
    op.alter_column("test_runs", "test_type", new_column_name="test")
    _rewrite_matrix_configs("test_types", "tests")


def downgrade() -> None:
    op.alter_column("test_runs", "test", new_column_name="test_type")
    _rewrite_matrix_configs("tests", "test_types")
