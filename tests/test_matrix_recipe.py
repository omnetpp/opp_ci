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
from unittest import mock

_DB_FD, _DB_PATH = tempfile.mkstemp(suffix=".db", prefix="opp_ci_mrec_")
os.close(_DB_FD)
os.environ["OPP_CI_DATABASE_URL"] = f"sqlite:///{_DB_PATH}"

from opp_ci.db.connection import engine, SessionLocal           # noqa: E402
from opp_ci.db.models import Base, TestMatrix, Worker           # noqa: E402
from opp_ci.scheduler import matrix_is_recipe, describe_expansion  # noqa: E402
from opp_ci.fleet import resolve_loose_matrix_axes              # noqa: E402
from opp_ci.persistence import (                                # noqa: E402
    create_matrix_from_axes, create_matrix_run, resolve_matrix_recipe,
)


class RecipeDetectionTests(unittest.TestCase):
    def test_missing_compiler_is_recipe(self):
        self.assertTrue(matrix_is_recipe({"arch": ["amd64"], "distro": ["ubuntu"]}))

    def test_missing_arch_is_recipe(self):
        self.assertTrue(matrix_is_recipe({"compiler": ["gcc-14"], "distro": ["ubuntu"]}))

    def test_missing_platform_is_recipe(self):
        self.assertTrue(matrix_is_recipe({"compiler": ["gcc-14"], "arch": ["amd64"]}))

    def test_moving_ref_is_recipe(self):
        full = {"compiler": ["gcc-14"], "arch": ["amd64"], "distro": ["ubuntu"]}
        self.assertTrue(matrix_is_recipe({**full, "refs": ["main"]}))          # branch
        self.assertTrue(matrix_is_recipe({**full, "refs": ["v1..v2"]}))        # range
        self.assertTrue(matrix_is_recipe({**full, "ref_range": {"base": "a", "head": "b"}}))

    def test_fully_specified_is_resolved(self):
        full = {"compiler": ["gcc-14"], "arch": ["amd64"], "distro": ["ubuntu"]}
        self.assertFalse(matrix_is_recipe(full))
        # full coordinate + a pinned-SHA ref is resolved (no moving source)
        self.assertFalse(matrix_is_recipe({**full, "refs": ["a" * 40]}))


class DescribeExpansionTests(unittest.TestCase):
    def test_empty_config_is_one(self):
        self.assertEqual(describe_expansion({}), "1 Test")

    def test_cartesian_product(self):
        self.assertEqual(
            describe_expansion({"kinds": ["a", "b"], "modes": ["x", "y"]}),
            "4 Tests")

    def test_static_refs_multiply(self):
        self.assertEqual(
            describe_expansion({"kinds": ["a"], "refs": ["main", "v1.0"]}),
            "2 Tests")

    def test_range_counted_per_commit(self):
        out = describe_expansion({"kinds": ["a", "b"], "refs": ["base..topic"]})
        self.assertIn("2 Tests per commit in base..topic", out)
        self.assertIn("resolved at run time", out)

    def test_recipe_loose_axes_count_as_one(self):
        # No compiler/arch/platform → each resolves to one value, so the count
        # matches what the snapshot will produce.
        self.assertEqual(describe_expansion({"kinds": ["a", "b", "c"]}), "3 Tests")


class MatrixHashTests(unittest.TestCase):
    def test_hash_order_independent(self):
        from opp_ci.db.models import compute_matrix_hash
        a = compute_matrix_hash("inet", None,
                                {"kinds": ["a", "b"], "arch": ["amd64"]})
        b = compute_matrix_hash("inet", None,
                                {"arch": ["amd64"], "kinds": ["b", "a"]})
        self.assertEqual(a, b)                       # axis order irrelevant
        c = compute_matrix_hash("inet", None, {"kinds": ["a"]})
        self.assertNotEqual(a, c)                    # different content differs


class PinMatrixRefsTests(unittest.TestCase):
    def test_branch_pinned_to_sha(self):
        from opp_ci.scheduler import pin_matrix_refs
        with mock.patch("opp_ci.scheduler.resolve_source_commit",
                        return_value="c" * 40):
            out = pin_matrix_refs("inet", {"refs": ["main"], "kinds": ["smoke"]})
        self.assertEqual(out["refs"], ["c" * 40])    # no moving branch survives
        self.assertEqual(out["kinds"], ["smoke"])

    def test_full_sha_kept(self):
        from opp_ci.scheduler import pin_matrix_refs
        out = pin_matrix_refs("inet", {"refs": ["D" * 40]})
        self.assertEqual(out["refs"], ["d" * 40])

    def test_no_refs_unchanged(self):
        from opp_ci.scheduler import pin_matrix_refs
        cfg = {"kinds": ["smoke"], "compiler": ["gcc-14"]}
        self.assertEqual(pin_matrix_refs("inet", cfg), cfg)


class ResolveMatrixAxesTests(unittest.TestCase):
    FLEET = {"compiler:clang-18", "compiler:gcc-14", "arch:amd64",
             "arch:aarch64", "distro:ubuntu-24.04"}

    def test_pins_loose_axes(self):
        out = resolve_loose_matrix_axes({"kinds": ["smoke"]}, self.FLEET)
        self.assertEqual(out["compiler"], ["clang-18"])  # preferred + newest
        self.assertEqual(out["arch"], ["amd64"])
        self.assertEqual(out["distro"], ["ubuntu"])       # platform from fleet
        self.assertEqual(out["distro_version"], ["24.04"])
        self.assertEqual(out["modes"], ["release"])
        self.assertEqual(out["kinds"], ["smoke"])         # untouched

    def test_keeps_specified_axes(self):
        out = resolve_loose_matrix_axes(
            {"compiler": ["gcc-13"], "arch": ["aarch64"], "distro": ["fedora"]},
            self.FLEET)
        self.assertEqual(out["compiler"], ["gcc-13"])
        self.assertEqual(out["arch"], ["aarch64"])
        self.assertEqual(out["distro"], ["fedora"])

    def test_reject_when_fleet_lacks_compiler(self):
        with self.assertRaises(ValueError):
            resolve_loose_matrix_axes(
                {"arch": ["amd64"], "distro": ["ubuntu"]},
                {"arch:amd64", "distro:ubuntu-24.04"})  # no compiler

    def test_reject_when_fleet_lacks_platform(self):
        with self.assertRaises(ValueError):
            resolve_loose_matrix_axes({}, {"compiler:gcc-14", "arch:amd64"})


class RecipeLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)

    def setUp(self):
        self.s = SessionLocal()
        self.s.add(Worker(name="w", tags=["compiler:clang-18", "arch:amd64",
                                          "distro:ubuntu-24.04"]))
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
        self.assertEqual(snap.config["distro"], ["ubuntu"])
        # Snapshot is runnable; recipe sees it in its lineage.
        mr = create_matrix_run(self.s, matrix_id=snap.id)
        self.assertIsNotNone(mr.id)
        self.assertIn(snap, recipe.resolved_instances)

    def test_resolve_is_content_addressed(self):
        # Re-resolving a recipe to the same pinned content reuses the snapshot.
        recipe = self._make_recipe({"kinds": ["smoke"]})
        s1 = resolve_matrix_recipe(self.s, recipe)
        s2 = resolve_matrix_recipe(self.s, recipe)
        self.assertEqual(s1.id, s2.id)
        self.assertEqual(len(recipe.resolved_instances), 1)
        self.assertIsNotNone(s1.matrix_hash)

    def test_fully_specified_matrix_is_resolved(self):
        m = create_matrix_from_axes(
            self.s, project="inet",
            config={"kinds": ["smoke"], "compiler": ["gcc-14"],
                    "arch": ["amd64"], "distro": ["ubuntu"]})
        self.assertTrue(m.is_resolved)

    def test_resolving_already_resolved_raises(self):
        m = create_matrix_from_axes(
            self.s, project="inet",
            config={"compiler": ["gcc-14"], "arch": ["amd64"],
                    "distro": ["ubuntu"]})
        with self.assertRaises(ValueError):
            resolve_matrix_recipe(self.s, m)


if __name__ == "__main__":
    unittest.main()
