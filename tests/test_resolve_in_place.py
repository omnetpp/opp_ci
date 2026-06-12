"""Tests for resolve-in-place schema + identity (Phase 2 of
plan/pending/repeatable-tests-and-moving-target-matrices.md).

A Test/TestMatrix is one entity in two states (recipe vs resolved):
`is_resolved` + `resolved_from`. The resolved project source commit is part
of Test identity, so two commits are distinct Tests; an unresolved recipe
leaves it None.

Run with: python -m pytest tests/test_resolve_in_place.py
"""

import os
import tempfile
import unittest

_DB_FD, _DB_PATH = tempfile.mkstemp(suffix=".db", prefix="opp_ci_rip_")
os.close(_DB_FD)
os.environ["OPP_CI_DATABASE_URL"] = f"sqlite:///{_DB_PATH}"

from opp_ci.db.connection import engine, SessionLocal          # noqa: E402
from opp_ci.db.models import (                                  # noqa: E402
    Base, Test, TestMatrix, compute_test_coord_hash,
)
from opp_ci.persistence import get_or_create_test              # noqa: E402


def _coord(**over):
    base = {"project": "inet", "commit_sha": None, "kind": "smoke",
            "mode": "release", "os": "Linux", "os_version": None,
            "distro": "Ubuntu 24.04", "distro_version": None, "flavor": None,
            "flavor_version": None, "arch": "amd64", "compiler": "gcc",
            "compiler_version": "13", "isolation": "none", "toolchain": "none",
            "opp_file": None, "resolved_deps": None}
    base.update(over)
    return base


class CommitInIdentityTests(unittest.TestCase):
    def test_distinct_commits_distinct_hash(self):
        a = compute_test_coord_hash(_coord(commit_sha="a" * 40))
        b = compute_test_coord_hash(_coord(commit_sha="b" * 40))
        self.assertNotEqual(a, b)

    def test_missing_and_none_commit_equivalent(self):
        none = compute_test_coord_hash(_coord(commit_sha=None))
        missing = compute_test_coord_hash(
            {k: v for k, v in _coord().items() if k != "commit_sha"})
        self.assertEqual(none, missing)

    def test_commit_independent_of_deps(self):
        # commit and resolved_deps are independent identity axes.
        a = compute_test_coord_hash(_coord(commit_sha="a" * 40,
                                           resolved_deps={"omnetpp": "6.4.0"}))
        b = compute_test_coord_hash(_coord(commit_sha="a" * 40,
                                           resolved_deps={"omnetpp": "6.3.0"}))
        self.assertNotEqual(a, b)


class ResolveStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)

    def test_default_is_resolved_true(self):
        s = SessionLocal()
        try:
            t = get_or_create_test(s, _coord(commit_sha="c" * 40))
            s.commit()
            self.assertTrue(t.is_resolved)
            self.assertIsNone(t.resolved_from)
            # View helpers used by the badges / templates.
            self.assertFalse(t.is_recipe)
            self.assertEqual(t.state_label, "resolved")
            self.assertEqual(t.short_commit, "c" * 8)
        finally:
            s.close()

    def test_recipe_view_helpers(self):
        recipe = Test(project="inet", kind="smoke", coord_hash="recipe-helpers",
                      is_resolved=False)
        self.assertTrue(recipe.is_recipe)
        self.assertEqual(recipe.state_label, "recipe")
        self.assertIsNone(recipe.short_commit)
        m = TestMatrix(project="inet", config={}, is_resolved=False)
        self.assertEqual(m.state_label, "recipe")

    def test_resolved_from_lineage(self):
        s = SessionLocal()
        try:
            recipe = Test(project="inet", kind="smoke", coord_hash="recipe-hash",
                          is_resolved=False)
            s.add(recipe)
            s.flush()
            resolved = get_or_create_test(s, _coord(commit_sha="d" * 40))
            resolved.resolved_from = recipe.id
            s.commit()
            # The recipe sees its resolved snapshots via the backref.
            self.assertIn(resolved, recipe.resolved_instances)
            self.assertEqual(resolved.recipe.id, recipe.id)
            self.assertFalse(recipe.is_resolved)
        finally:
            s.close()

    def test_matrix_resolve_state_columns(self):
        s = SessionLocal()
        try:
            recipe = TestMatrix(project="inet", config={"refs": ["main"]},
                                is_resolved=False)
            s.add(recipe)
            s.flush()
            snap = TestMatrix(project="inet", config={"refs": ["abc123"]},
                              is_resolved=True, resolved_from=recipe.id)
            s.add(snap)
            s.commit()
            self.assertIn(snap, recipe.resolved_instances)
            self.assertEqual(snap.recipe.id, recipe.id)
        finally:
            s.close()


class RecipeGatingTests(unittest.TestCase):
    """A recipe (is_resolved=False) is inert: it cannot be run."""

    @classmethod
    def setUpClass(cls):
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)

    def test_cannot_run_unresolved_test(self):
        from opp_ci.persistence import create_test_run
        s = SessionLocal()
        try:
            recipe = Test(project="inet", kind="smoke",
                          coord_hash="recipe-test-hash", is_resolved=False)
            s.add(recipe)
            s.flush()
            with self.assertRaises(ValueError):
                create_test_run(s, test_id=recipe.id)
        finally:
            s.close()

    def test_can_run_resolved_test(self):
        from opp_ci.persistence import create_test_run
        s = SessionLocal()
        try:
            t = get_or_create_test(s, _coord(commit_sha="e" * 40))
            s.flush()
            run = create_test_run(s, test_id=t.id)  # no raise
            self.assertIsNotNone(run.id)
        finally:
            s.close()

    def test_cannot_run_unresolved_matrix(self):
        from opp_ci.persistence import create_matrix_run
        s = SessionLocal()
        try:
            recipe = TestMatrix(project="inet", config={"refs": ["main"]},
                                is_resolved=False)
            s.add(recipe)
            s.flush()
            with self.assertRaises(ValueError):
                create_matrix_run(s, matrix_id=recipe.id)
        finally:
            s.close()


class TestRecipeObjectTests(unittest.TestCase):
    """Test recipes are first-class separate objects: an underspecified Test is
    a recipe (is_resolved=False, inert) and resolving it mints a separate
    resolved Test with resolved_from lineage."""

    FLEET = {"compiler:clang-18", "arch:amd64", "distro:ubuntu-24.04"}

    @classmethod
    def setUpClass(cls):
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)

    def _loose(self, **over):
        c = {"project": "inet", "kind": "smoke", "commit_sha": None,
             "mode": None, "os": None, "os_version": None, "distro": None,
             "distro_version": None, "flavor": None, "flavor_version": None,
             "arch": None, "compiler": None, "compiler_version": None,
             "isolation": "none", "toolchain": "none", "opp_file": None,
             "resolved_deps": None}
        c.update(over)
        return c

    def test_is_recipe_detection(self):
        from opp_ci.persistence import test_coord_is_recipe
        self.assertTrue(test_coord_is_recipe(self._loose()))
        self.assertTrue(test_coord_is_recipe(
            self._loose(compiler="gcc", arch="amd64")))   # no platform
        self.assertFalse(test_coord_is_recipe(
            {"compiler": "gcc", "arch": "amd64", "distro": "ubuntu"}))

    def test_recipe_created_unresolved_and_inert(self):
        from opp_ci.persistence import get_or_create_test_recipe, create_test_run
        s = SessionLocal()
        try:
            recipe = get_or_create_test_recipe(s, self._loose())
            s.commit()
            self.assertFalse(recipe.is_resolved)
            self.assertIsNone(recipe.compiler)            # loose, no validation
            with self.assertRaises(ValueError):
                create_test_run(s, test_id=recipe.id)     # can't run a recipe
        finally:
            s.close()

    def test_recipe_dedups(self):
        from opp_ci.persistence import get_or_create_test_recipe
        s = SessionLocal()
        try:
            a = get_or_create_test_recipe(s, self._loose(kind="build"))
            s.commit()
            b = get_or_create_test_recipe(s, self._loose(kind="build"))
            s.commit()
            self.assertEqual(a.id, b.id)
        finally:
            s.close()

    def test_resolve_mints_resolved_test_with_lineage(self):
        from opp_ci.persistence import (get_or_create_test_recipe,
                                        resolve_test_recipe, create_test_run)
        s = SessionLocal()
        try:
            recipe = get_or_create_test_recipe(s, self._loose(kind="fingerprint"))
            s.commit()
            resolved = resolve_test_recipe(s, recipe, self.FLEET,
                                           default_expectation=None)
            s.commit()
            self.assertTrue(resolved.is_resolved)
            self.assertEqual((resolved.compiler, resolved.compiler_version),
                             ("clang", "18"))
            self.assertEqual(resolved.arch, "amd64")
            self.assertEqual(resolved.distro, "ubuntu")
            self.assertNotEqual(resolved.id, recipe.id)
            self.assertEqual(resolved.resolved_from, recipe.id)
            self.assertIn(resolved, recipe.resolved_instances)
            run = create_test_run(s, test_id=resolved.id)   # runnable
            self.assertIsNotNone(run.id)
        finally:
            s.close()

    def test_re_resolve_same_fleet_reuses_test(self):
        from opp_ci.persistence import get_or_create_test_recipe, resolve_test_recipe
        s = SessionLocal()
        try:
            recipe = get_or_create_test_recipe(s, self._loose(kind="statistical"))
            s.commit()
            r1 = resolve_test_recipe(s, recipe, self.FLEET, default_expectation=None)
            s.commit()
            r2 = resolve_test_recipe(s, recipe, self.FLEET, default_expectation=None)
            s.commit()
            self.assertEqual(r1.id, r2.id)                  # content-addressed
            self.assertEqual(len(recipe.resolved_instances), 1)
        finally:
            s.close()

    def test_resolve_rejects_already_resolved(self):
        from opp_ci.persistence import resolve_test_recipe
        s = SessionLocal()
        try:
            full = get_or_create_test(s, _coord(commit_sha="f" * 40))
            s.commit()
            self.assertTrue(full.is_resolved)
            with self.assertRaises(ValueError):
                resolve_test_recipe(s, full, self.FLEET)
        finally:
            s.close()


if __name__ == "__main__":
    unittest.main()
