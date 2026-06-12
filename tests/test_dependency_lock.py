"""Tests for the transitive dependency lock (Phase 1 of
plan/pending/repeatable-tests-and-moving-target-matrices.md).

`resolve_dependencies` walks the full closure, not just direct
required_projects, and with `require_complete=True` refuses to return a
partial lock. opp_env is mocked so these stay hermetic.

Run with: python -m pytest tests/test_dependency_lock.py
"""

import unittest
from unittest import mock

from opp_ci import dependency
from opp_ci.dependency import DependencyResolutionError, resolve_dependencies


def _fake_registry(graph):
    """Build a query_opp_env_info stand-in from a {node: required_projects} map.

    A node absent from `graph` returns None (opp_env "doesn't know it"); a
    node present returns an info dict carrying its required_projects.
    """
    def _query(node):
        if node not in graph:
            return None
        return {"version": node, "required_projects": graph[node]}
    return _query


class TransitiveClosureTests(unittest.TestCase):
    def _resolve(self, graph, root, **kw):
        with mock.patch.object(dependency, "query_opp_env_info",
                               _fake_registry(graph)):
            return resolve_dependencies(root, **kw)

    def test_direct_dep_latest_first(self):
        graph = {
            "inet-4.5": {"omnetpp": ["6.1", "6.0"]},
            "omnetpp-6.1": {},
        }
        self.assertEqual(self._resolve(graph, "inet-4.5"), {"omnetpp": "6.1"})

    def test_transitive_closure_includes_indirect(self):
        # A → B → C: C is only reachable through B, but must be in the lock.
        graph = {
            "a-1": {"b": ["b2", "b1"]},
            "b-b2": {"c": ["c1"]},
            "c-c1": {},
        }
        self.assertEqual(self._resolve(graph, "a-1"), {"b": "b2", "c": "c1"})

    def test_pin_overrides_and_recurses(self):
        # Pinning b to b1 must follow b-b1's (different) requirements.
        graph = {
            "a-1": {"b": ["b2", "b1"]},
            "b-b2": {"c": ["c2"]},
            "b-b1": {"c": ["c1"]},
            "c-c1": {}, "c-c2": {},
        }
        self.assertEqual(self._resolve(graph, "a-1", pins={"b": "b1"}),
                         {"b": "b1", "c": "c1"})

    def test_diamond_chooses_once(self):
        # Both b and c require d; d is picked once and reused.
        graph = {
            "a-1": {"b": ["b1"], "c": ["c1"]},
            "b-b1": {"d": ["d2", "d1"]},
            "c-c1": {"d": ["d2", "d1"]},
            "d-d2": {},
        }
        self.assertEqual(self._resolve(graph, "a-1"),
                         {"b": "b1", "c": "c1", "d": "d2"})

    def test_non_transitive_stops_at_direct(self):
        graph = {
            "a-1": {"b": ["b2", "b1"]},
            "b-b2": {"c": ["c1"]},
            "c-c1": {},
        }
        self.assertEqual(self._resolve(graph, "a-1", transitive=False),
                         {"b": "b2"})

    def test_incompatible_pin_raises_valueerror(self):
        graph = {"inet-4.5": {"omnetpp": ["6.1", "6.0"]}}
        with self.assertRaises(ValueError):
            self._resolve(graph, "inet-4.5", pins={"omnetpp": "5.0"})


class RejectIncompleteTests(unittest.TestCase):
    def _resolve(self, graph, root, **kw):
        with mock.patch.object(dependency, "query_opp_env_info",
                               _fake_registry(graph)):
            return resolve_dependencies(root, **kw)

    def test_unknown_root_rejected(self):
        with self.assertRaises(DependencyResolutionError):
            self._resolve({}, "ghost-1", require_complete=True)

    def test_unknown_transitive_node_rejected(self):
        # b is chosen but opp_env can't describe b-b1 → closure unknowable.
        graph = {"a-1": {"b": ["b1"]}}  # no "b-b1" entry
        with self.assertRaises(DependencyResolutionError):
            self._resolve(graph, "a-1", require_complete=True)

    def test_no_compatible_versions_rejected(self):
        graph = {"a-1": {"b": []}}
        with self.assertRaises(DependencyResolutionError):
            self._resolve(graph, "a-1", require_complete=True)

    def test_version_conflict_rejected(self):
        # b and c demand disjoint sets of d → no consistent lock.
        graph = {
            "a-1": {"b": ["b1"], "c": ["c1"]},
            "b-b1": {"d": ["d1"]},
            "c-c1": {"d": ["d2"]},
            "d-d1": {}, "d-d2": {},
        }
        with self.assertRaises(DependencyResolutionError):
            self._resolve(graph, "a-1", require_complete=True)

    def test_non_strict_tolerates_missing_info(self):
        # Without require_complete, an unknowable node yields a partial lock.
        graph = {"a-1": {"b": ["b1"]}}  # no "b-b1"
        self.assertEqual(self._resolve(graph, "a-1"), {"b": "b1"})

    def test_known_no_deps_is_complete(self):
        # A project opp_env knows but with no deps is a valid empty lock.
        graph = {"omnetpp-6.1": {}}
        self.assertEqual(
            self._resolve(graph, "omnetpp-6.1", require_complete=True), {})


if __name__ == "__main__":
    unittest.main()
