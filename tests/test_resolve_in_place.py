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
        finally:
            s.close()

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


if __name__ == "__main__":
    unittest.main()
