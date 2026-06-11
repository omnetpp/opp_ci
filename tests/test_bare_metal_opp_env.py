"""The bare-metal host path (isolation=none, toolchain=none) must provision the
*pinned* omnetpp via an opp_env --nixless-workspace — never anything found on
the host. These tests assert the exact opp_env command lines, mocking
run_external so nothing actually builds.

Run with: python -m unittest tests.test_bare_metal_opp_env
"""

import contextlib
import os
import subprocess
import unittest
from unittest import mock

os.environ.setdefault("OPP_CI_REMOTE", "0")

from opp_ci import executor                                    # noqa: E402


def _ok(args):
    return subprocess.CompletedProcess(args, 0, stdout="", stderr="")


class BareMetalOppEnvTests(unittest.TestCase):
    def setUp(self):
        self.calls = []

        def _fake_run_external(args, **kwargs):
            self.calls.append(list(args))
            return _ok(args)

        patches = [
            mock.patch.object(executor, "run_external", _fake_run_external),
            mock.patch.object(executor, "_opp_env_workspace",
                              lambda **kw: "/tmp/ws-test"),
            mock.patch.object(executor, "_gc_workspaces", lambda: None),
            mock.patch.object(executor, "_workspace_lock",
                              lambda ws: contextlib.nullcontext()),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    # ── install ────────────────────────────────────────────────────────
    def test_install_none_is_nixless_with_pinned_omnetpp(self):
        executor.install_project(
            "mm1k", isolation="none", toolchain="none",
            resolved_deps={"omnetpp": "6.4.0"})
        self.assertEqual(self.calls, [[
            "opp_env", "install", "--init", "--nixless-workspace",
            "omnetpp-6.4.0", "mm1k-latest",
        ]])

    def test_install_nix_unchanged_no_nixless_no_pins(self):
        # The Nix path keeps its existing argv (regression guard).
        executor.install_project(
            "mm1k", isolation="none", toolchain="nix", resolved_deps=None)
        self.assertEqual(self.calls, [["opp_env", "install", "--init", "mm1k-latest"]])

    def test_install_podman_is_noop(self):
        executor.install_project(
            "mm1k", isolation="podman", toolchain="none",
            resolved_deps={"omnetpp": "6.4.0"})
        self.assertEqual(self.calls, [])

    # ── run ────────────────────────────────────────────────────────────
    def test_run_none_names_pinned_omnetpp(self):
        executor.run_test(
            "mm1k", "build", isolation="none", toolchain="none",
            resolved_deps={"omnetpp": "6.4.0"})
        # build stage is the first opp_env run; it must name the pinned omnetpp.
        self.assertTrue(self.calls, "no opp_env run invoked")
        build = self.calls[0]
        self.assertEqual(build[:4], ["opp_env", "run", "omnetpp-6.4.0", "mm1k-latest"])
        self.assertEqual(build[4], "-c")
        self.assertIn("opp_build_project", build[5])


if __name__ == "__main__":
    unittest.main()
