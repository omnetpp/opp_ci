"""Tests for dependency versions being part of Test identity
(plan/pending/test-identity-includes-deps.md).

A Test fully defines what/how/where is tested, including the resolved
dependency versions. So mm1k against omnetpp 6.4.0 and 6.3.0 are distinct
Tests, while a pinned and an auto-resolved identical version collapse to
one Test (identity tracks resolved versions, not pin intent).

Run with: python -m unittest tests.test_test_identity_deps  (no pytest needed)
"""

import os
import tempfile
import unittest

_DB_FD, _DB_PATH = tempfile.mkstemp(suffix=".db", prefix="opp_ci_tid_")
os.close(_DB_FD)
os.environ["OPP_CI_DATABASE_URL"] = f"sqlite:///{_DB_PATH}"

from opp_ci.db.connection import engine, SessionLocal         # noqa: E402
from opp_ci.db.models import (                                 # noqa: E402
    Base, compute_test_coord_hash, normalise_deps,
)
from opp_ci.persistence import get_or_create_test             # noqa: E402


def _coord(**over):
    base = {"project": "mm1k", "kind": "build", "mode": None, "os": "Linux",
            "os_version": None, "distro": "Ubuntu 24.04", "distro_version": None,
            "flavor": None, "flavor_version": None, "arch": None,
            "compiler": "gcc", "compiler_version": "13", "isolation": "podman",
            "toolchain": "none", "opp_file": None, "resolved_deps": None}
    base.update(over)
    return base


class CoordHashTests(unittest.TestCase):
    def test_different_dep_versions_differ(self):
        h640 = compute_test_coord_hash(_coord(resolved_deps={"omnetpp": "6.4.0"}))
        h630 = compute_test_coord_hash(_coord(resolved_deps={"omnetpp": "6.3.0"}))
        self.assertNotEqual(h640, h630)

    def test_none_empty_equivalent(self):
        hnone = compute_test_coord_hash(_coord(resolved_deps=None))
        hempty = compute_test_coord_hash(_coord(resolved_deps={}))
        hmissing = compute_test_coord_hash({k: v for k, v in _coord().items()
                                            if k != "resolved_deps"})
        self.assertEqual(hnone, hempty)
        self.assertEqual(hnone, hmissing)

    def test_key_order_irrelevant(self):
        a = compute_test_coord_hash(_coord(resolved_deps={"inet": "4.5", "omnetpp": "6.4.0"}))
        b = compute_test_coord_hash(_coord(resolved_deps={"omnetpp": "6.4.0", "inet": "4.5"}))
        self.assertEqual(a, b)

    def test_normalise_deps(self):
        self.assertEqual(normalise_deps(None), {})
        self.assertEqual(normalise_deps({}), {})
        self.assertEqual(list(normalise_deps({"b": "2", "a": "1"})), ["a", "b"])


class GetOrCreateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)

    def test_distinct_deps_create_distinct_tests(self):
        s = SessionLocal()
        try:
            t640 = get_or_create_test(s, _coord(resolved_deps={"omnetpp": "6.4.0"}))
            t630 = get_or_create_test(s, _coord(resolved_deps={"omnetpp": "6.3.0"}))
            s.commit()
            self.assertNotEqual(t640.id, t630.id)
            self.assertEqual(t640.resolved_deps, {"omnetpp": "6.4.0"})
            self.assertEqual(t630.resolved_deps, {"omnetpp": "6.3.0"})
        finally:
            s.close()

    def test_same_deps_reuse_test(self):
        s = SessionLocal()
        try:
            first = get_or_create_test(s, _coord(resolved_deps={"omnetpp": "6.4.0"}))
            s.commit()
            # Re-resolving the same version (e.g. pinned vs auto) reuses the Test.
            again = get_or_create_test(s, _coord(resolved_deps={"omnetpp": "6.4.0"}))
            s.commit()
            self.assertEqual(first.id, again.id)
        finally:
            s.close()


if __name__ == "__main__":
    unittest.main()
