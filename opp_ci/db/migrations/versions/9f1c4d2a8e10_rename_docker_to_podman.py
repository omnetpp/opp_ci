"""rename isolation/worker-tag 'docker' to 'podman'

Revision ID: 9f1c4d2a8e10
Revises: 8a3f1d2e5b04
Create Date: 2026-05-19
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "9f1c4d2a8e10"
down_revision: Union[str, None] = "8a3f1d2e5b04"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _rewrite_matrix_config(config, old, new):
    if isinstance(config, dict):
        iso = config.get("isolation")
        if iso == old:
            config["isolation"] = new
        elif isinstance(iso, list):
            config["isolation"] = [new if v == old else v for v in iso]
    return config


def _rewrite_worker_tags(tags, old, new):
    if isinstance(tags, list):
        return [new if t == old else t for t in tags]
    return tags


def _swap(old, new):
    bind = op.get_bind()
    bind.execute(
        sa.text("UPDATE test_runs SET isolation=:new WHERE isolation=:old"),
        {"new": new, "old": old},
    )

    matrices = sa.table(
        "test_matrices",
        sa.column("id", sa.Integer),
        sa.column("config", sa.JSON),
    )
    for row in bind.execute(sa.select(matrices.c.id, matrices.c.config)).all():
        new_config = _rewrite_matrix_config(dict(row.config) if row.config else row.config,
                                            old, new)
        if new_config != row.config:
            bind.execute(
                sa.update(matrices).where(matrices.c.id == row.id).values(config=new_config)
            )

    workers = sa.table(
        "workers",
        sa.column("id", sa.Integer),
        sa.column("tags", sa.JSON),
    )
    for row in bind.execute(sa.select(workers.c.id, workers.c.tags)).all():
        new_tags = _rewrite_worker_tags(list(row.tags) if row.tags else row.tags, old, new)
        if new_tags != row.tags:
            bind.execute(
                sa.update(workers).where(workers.c.id == row.id).values(tags=new_tags)
            )


def upgrade() -> None:
    _swap("docker", "podman")


def downgrade() -> None:
    _swap("podman", "docker")
