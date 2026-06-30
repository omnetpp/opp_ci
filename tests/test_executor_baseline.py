"""Regression tests for test-baseline resolution in opp_ci.executor.

A run is dispatched under its *versioned* opp_env id (e.g. ``inet-git``), but a
project's per-kind ``test_parameters`` — including the chart/statistical baseline
repo — live under the *bare* name in the bundled ``@opp`` registry. So
``_resolve_test_baseline`` must look up the bare name; otherwise
``_load_workspace`` fails to find the project, silently falls back to a
programmatic project with no ``test_parameters``, and the baseline checkout never
happens (chart/statistical then fail with "Stored statistical results are not
found" / "Baseline chart not found").
"""
import unittest
from unittest import mock

from opp_ci import executor


class BareProjectNameTests(unittest.TestCase):
    def test_strips_version_and_git_ref(self):
        self.assertEqual(executor._bare_project_name("inet"), "inet")
        self.assertEqual(executor._bare_project_name("inet-git"), "inet")
        self.assertEqual(executor._bare_project_name("inet-git@deadbeef"), "inet")
        self.assertEqual(executor._bare_project_name("inet-4.5"), "inet")
        self.assertEqual(executor._bare_project_name("mm1k-latest"), "mm1k")


class ResolveTestBaselineUsesBareNameTests(unittest.TestCase):
    def test_resolve_looks_up_bare_name(self):
        captured = {}
        sentinel = {"repository": "inet-framework/statistics", "folder": "statistics"}
        proj = mock.Mock()
        proj.get_test_baseline.return_value = sentinel

        def fake_load(name, opp_file=None):
            captured["name"] = name
            return object(), proj

        with mock.patch.object(executor, "_load_workspace", fake_load):
            out = executor._resolve_test_baseline("inet-git@deadbeef", "statistical")

        self.assertEqual(captured["name"], "inet")   # bare, not 'inet-git@...'
        self.assertIs(out, sentinel)
        proj.get_test_baseline.assert_called_once_with("statistical")

    def test_resolve_returns_none_on_failure(self):
        def boom(name, opp_file=None):
            raise RuntimeError("no such project")

        with mock.patch.object(executor, "_load_workspace", boom):
            self.assertIsNone(executor._resolve_test_baseline("inet-git", "statistical"))


if __name__ == "__main__":
    unittest.main()
