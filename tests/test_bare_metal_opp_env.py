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
        # build stage is the first opp_env run; it must name the pinned omnetpp,
        # pass -w <ws>, and end with the bare build command.
        self.assertTrue(self.calls, "no opp_env run invoked")
        build = self.calls[0]
        self.assertEqual(build[:2], ["opp_env", "run"])
        self.assertIn("-w", build)
        self.assertIn("omnetpp-6.4.0", build)
        self.assertIn("mm1k-latest", build)
        self.assertEqual(build[-2], "-c")
        self.assertIn("opp_build_project", build[-1])

    def test_build_kind_runs_opp_env_once(self):
        # kind=build: the build is the test — one opp_env run, and the build
        # command must not carry --no-build (opp_build_project rejects it).
        executor.run_test(
            "mm1k", "build", isolation="none", toolchain="none",
            resolved_deps={"omnetpp": "6.4.0"})
        self.assertEqual(len(self.calls), 1, "build kind must run opp_env once")
        self.assertNotIn("--no-build", self.calls[0][-1])

    def test_test_kind_builds_then_runs_with_no_build(self):
        # A real test kind splits into build + test, the test reusing the build.
        executor.run_test(
            "mm1k", "smoke", isolation="none", toolchain="none",
            resolved_deps={"omnetpp": "6.4.0"})
        self.assertEqual(len(self.calls), 2)
        self.assertIn("opp_build_project", self.calls[0][-1])
        self.assertNotIn("--no-build", self.calls[0][-1])
        self.assertIn("opp_run_smoke_tests", self.calls[1][-1])
        self.assertIn("--no-build", self.calls[1][-1])


class ProjectInstallDirTests(unittest.TestCase):
    def test_globs_resolved_install_dir(self):
        import tempfile
        ws = tempfile.mkdtemp(prefix="opp_ci_pid_")
        self.addCleanup(lambda: __import__("shutil").rmtree(ws, ignore_errors=True))
        os.makedirs(os.path.join(ws, "mm1k-git"))
        os.makedirs(os.path.join(ws, "omnetpp-6.4.0"))
        # A -latest alias the matrix passed resolves to the on-disk -git dir,
        # and the omnetpp dep dir is never picked for the mm1k project.
        self.assertEqual(executor._project_install_dir(ws, "mm1k-latest"),
                         os.path.join(ws, "mm1k-git"))
        self.assertEqual(executor._project_install_dir(ws, "mm1k"),
                         os.path.join(ws, "mm1k-git"))

    def test_falls_back_to_ws_when_absent(self):
        import tempfile
        ws = tempfile.mkdtemp(prefix="opp_ci_pid_")
        self.addCleanup(lambda: __import__("shutil").rmtree(ws, ignore_errors=True))
        self.assertEqual(executor._project_install_dir(ws, "mm1k"), ws)


if __name__ == "__main__":
    unittest.main()
