"""Regression: kind=build on the split build/test path (podman) must not crash.

When the build and test run as two `internal run-direct` execs, the *test* exec
has skip_build=True. For kind=build there is no test function (func is None), so
the build-only case must finish PASS before the test stage rather than call
None → "'NoneType' object is not callable".

Run with: python -m pytest tests/test_run_direct_build.py
"""

import unittest
from unittest import mock

from opp_ci import executor


class RunDirectBuildOnlyTests(unittest.TestCase):
    def _run(self, **kw):
        with mock.patch.object(executor, "_load_workspace",
                               return_value=(None, mock.Mock())), \
             mock.patch.object(executor, "_get_test_functions", return_value={}), \
             mock.patch.object(executor, "resolve_commit_sha",
                               return_value="deadbeef"):
            return executor._run_test_direct("mm1k", "build", mode="release", **kw)

    def test_build_only_skip_build_returns_pass(self):
        # The podman test exec: build already done in the separate build exec.
        result = self._run(skip_build=True)
        self.assertEqual(result["result_code"], "PASS")
        self.assertEqual(result["commit_sha"], "deadbeef")


if __name__ == "__main__":
    unittest.main()
