"""Branch-tracking: a recipe matrix auto-resolves on a push (Phase 3 /
plan/pending/repeatable-tests-and-moving-target-matrices.md).

A push to a branch with an AutoTestRule pointing at a *recipe* matrix resolves
the recipe — loose axes pinned against the fleet, source pinned to the pushed
commit — minting a runnable snapshot (resolved_from → recipe) and running it.

Run with: python -m pytest tests/test_webhook_recipe.py
"""

import os
import tempfile
import unittest

_DB_FD, _DB_PATH = tempfile.mkstemp(suffix=".db", prefix="opp_ci_whrec_")
os.close(_DB_FD)
os.environ["OPP_CI_DATABASE_URL"] = f"sqlite:///{_DB_PATH}"

from sqlalchemy import select                                   # noqa: E402

from opp_ci.db.connection import engine, SessionLocal           # noqa: E402
from opp_ci.db.models import (                                  # noqa: E402
    Base, AutoTestRule, Project, TestMatrix, TestMatrixRun, Worker,
)
from opp_ci.github.webhook import handle_webhook_event          # noqa: E402

_SHA = "a" * 40


class WebhookRecipeTests(unittest.TestCase):
    def setUp(self):
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        s = SessionLocal()
        try:
            proj = Project(name="inet", github_owner="inet-framework",
                           github_repo="inet")
            s.add(proj)
            s.add(Worker(name="w", tags=["compiler:clang-18", "arch:amd64",
                                         "distro:ubuntu-24.04"]))
            s.flush()
            # An underspecified matrix = a recipe.
            recipe = TestMatrix(project="inet", config={"kinds": ["smoke"]},
                                is_resolved=False)
            s.add(recipe)
            s.flush()
            s.add(AutoTestRule(project_id=proj.id, rule_type="branch",
                               pattern="main", matrix_id=recipe.id, enabled=1))
            s.commit()
            self.recipe_id = recipe.id
        finally:
            s.close()

    def _push(self, sha=_SHA):
        return handle_webhook_event("push", {
            "ref": "refs/heads/main",
            "after": sha,
            "repository": {"name": "inet",
                           "owner": {"login": "inet-framework"}},
        })

    def test_push_resolves_recipe_and_runs_snapshot(self):
        result = self._push()
        self.assertEqual(result["action"], "queued", result)
        self.assertGreaterEqual(result["jobs_queued"], 1)

        s = SessionLocal()
        try:
            snap = s.execute(
                select(TestMatrix).where(TestMatrix.resolved_from == self.recipe_id)
            ).scalar_one()
            # Source pinned to the pushed commit; loose axes pinned from fleet.
            self.assertTrue(snap.is_resolved)
            self.assertEqual(snap.config["refs"], [_SHA])
            self.assertEqual(snap.config["compiler"], ["clang-18"])
            self.assertEqual(snap.config["arch"], ["amd64"])
            # The run is grouped under the snapshot, not the recipe.
            mr = s.execute(
                select(TestMatrixRun).where(TestMatrixRun.matrix_id == snap.id)
            ).scalar_one()
            self.assertIsNotNone(mr.id)
        finally:
            s.close()

    def test_content_addressed_snapshots(self):
        # Resolved matrices are content-addressed: pushing the SAME commit twice
        # reuses one snapshot; a DIFFERENT commit mints a new one.
        self._push("a" * 40)
        self._push("a" * 40)        # same commit → dedup
        self._push("b" * 40)        # new commit → new snapshot
        s = SessionLocal()
        try:
            snaps = s.execute(
                select(TestMatrix).where(TestMatrix.resolved_from == self.recipe_id)
            ).scalars().all()
            self.assertEqual(len(snaps), 2)   # {a..., b...}, not 3
            self.assertEqual({tuple(sn.config["refs"]) for sn in snaps},
                             {("a" * 40,), ("b" * 40,)})
        finally:
            s.close()


if __name__ == "__main__":
    unittest.main()
