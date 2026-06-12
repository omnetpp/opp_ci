"""Tests for matrix recipes that resolve against the fleet (Phase 3b of
plan/pending/repeatable-tests-and-moving-target-matrices.md).

A matrix created with underspecified coordinates is a recipe (is_resolved=
False) that can't run; resolving it pins the loose axes against the fleet and
mints a runnable snapshot matrix (resolved_from → the recipe).

Run with: python -m pytest tests/test_matrix_recipe.py
"""

import os
import tempfile
import unittest

_DB_FD, _DB_PATH = tempfile.mkstemp(suffix=".db", prefix="opp_ci_mrec_")
os.close(_DB_FD)
os.environ["OPP_CI_DATABASE_URL"] = f"sqlite:///{_DB_PATH}"

from opp_ci.db.connection import engine, SessionLocal           # noqa: E402
from opp_ci.db.models import Base, TestMatrix, Worker           # noqa: E402
from opp_ci.scheduler import matrix_is_recipe                   # noqa: E402
from opp_ci.fleet import resolve_loose_matrix_axes              # noqa: E402
from opp_ci.persistence import (                                # noqa: E402
    create_matrix_from_axes, create_matrix_run, resolve_matrix_recipe,
)


class RecipeDetectionTests(unittest.TestCase):
    def test_missing_compiler_is_recipe(self):
        self.assertTrue(matrix_is_recipe({"arch": ["amd64"], "kinds": ["smoke"]}))

    def test_missing_arch_is_recipe(self):
        self.assertTrue(matrix_is_recipe({"compiler": ["gcc-14"]}))

    def test_fully_specified_is_resolved(self):
        self.assertFalse(matrix_is_recipe({"compiler": ["gcc-14"], "arch": ["amd64"]}))


class ResolveMatrixAxesTests(unittest.TestCase):
    FLEET = {"compiler:clang-18", "compiler:gcc-14", "arch:amd64", "arch:aarch64"}

    def test_pins_loose_compiler_and_arch(self):
        out = resolve_loose_matrix_axes({"kinds": ["smoke"]}, self.FLEET)
        self.assertEqual(out["compiler"], ["clang-18"])  # preferred + newest
        self.assertEqual(out["arch"], ["amd64"])
        self.assertEqual(out["kinds"], ["smoke"])        # untouched

    def test_keeps_specified_axes(self):
        out = resolve_loose_matrix_axes(
            {"compiler": ["gcc-13"], "arch": ["aarch64"]}, self.FLEET)
        self.assertEqual(out["compiler"], ["gcc-13"])
        self.assertEqual(out["arch"], ["aarch64"])

    def test_reject_when_fleet_lacks_axis(self):
        with self.assertRaises(ValueError):
            resolve_loose_matrix_axes({"arch": ["amd64"]}, {"arch:amd64"})  # no compiler


class RecipeLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)

    def setUp(self):
        self.s = SessionLocal()
        self.s.add(Worker(name="w", tags=["compiler:clang-18", "arch:amd64"]))
        self.s.flush()

    def tearDown(self):
        self.s.rollback()
        self.s.close()

    def _make_recipe(self, config):
        # Mirror the web form: an underspecified config is created as a recipe.
        return create_matrix_from_axes(
            self.s, project="inet", config=config,
            is_resolved=not matrix_is_recipe(config))

    def test_underspecified_matrix_is_recipe_and_cannot_run(self):
        recipe = self._make_recipe({"kinds": ["smoke"]})
        self.assertFalse(recipe.is_resolved)
        with self.assertRaises(ValueError):
            create_matrix_run(self.s, matrix_id=recipe.id)

    def test_resolve_mints_runnable_snapshot(self):
        recipe = self._make_recipe({"kinds": ["smoke"]})
        snap = resolve_matrix_recipe(self.s, recipe)
        self.assertTrue(snap.is_resolved)
        self.assertEqual(snap.resolved_from, recipe.id)
        self.assertEqual(snap.config["compiler"], ["clang-18"])
        self.assertEqual(snap.config["arch"], ["amd64"])
        # Snapshot is runnable; recipe sees it in its lineage.
        mr = create_matrix_run(self.s, matrix_id=snap.id)
        self.assertIsNotNone(mr.id)
        self.assertIn(snap, recipe.resolved_instances)

    def test_fully_specified_matrix_is_resolved(self):
        m = create_matrix_from_axes(
            self.s, project="inet",
            config={"kinds": ["smoke"], "compiler": ["gcc-14"], "arch": ["amd64"]})
        self.assertTrue(m.is_resolved)

    def test_resolving_already_resolved_raises(self):
        m = create_matrix_from_axes(
            self.s, project="inet",
            config={"compiler": ["gcc-14"], "arch": ["amd64"]})
        with self.assertRaises(ValueError):
            resolve_matrix_recipe(self.s, m)


if __name__ == "__main__":
    unittest.main()
