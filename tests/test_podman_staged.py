"""Tests for the staged podman lifecycle — detached container + per-stage
exec + guaranteed teardown (plan/pending/staged-execution-capture.md, phase 2).

These mock run_external (there's no podman in CI); they pin the command
sequence, stage recording, bootstrap-failure handling, and that the container
is always torn down. Real podman validation happens on a podman host.

Run with: python -m unittest tests.test_podman_staged
"""

import subprocess
import unittest
from unittest import mock

from opp_ci import executor
from opp_ci.stages import Stage, StageRecorder, FAILED, SKIPPED, PASSED


def _ok(args, **kw):
    return subprocess.CompletedProcess(args, 0, stdout="out\n", stderr="")


def _call(recorder, fake_run, run_stages=None):
    if run_stages is None:
        run_stages = [(Stage.TEST_RUN, ["run", "-c", "x"], "bare-x")]
    with mock.patch("opp_ci.executor.run_external", side_effect=fake_run):
        return executor._run_podman_staged(
            image="img:tag", run_stages=run_stages, entry_script="/opt/e.sh",
            run_flags=["-v", "/m:/work:Z", "-w", "/work"],
            recorder=recorder, git_ref=None, worktree_path=None, scratch_dir=None)


class PodmanStagedTests(unittest.TestCase):
    def test_lifecycle_sequence_and_stages(self):
        rec = StageRecorder()
        calls = []

        def fake(args, **kw):
            calls.append(args)
            return _ok(args)

        outcome = _call(rec, fake)
        # detached run → bootstrap exec → test exec → rm -f
        self.assertEqual(calls[0][:4], ["podman", "run", "-d", "--name"])
        self.assertIn("--entrypoint", calls[0])
        self.assertEqual(calls[1][:2], ["podman", "exec"])
        self.assertIn("--bootstrap-only", calls[1])
        self.assertIn("--skip-bootstrap", calls[2])
        self.assertEqual(calls[-1][:3], ["podman", "rm", "-f"])
        self.assertEqual([s["name"] for s in rec.stages],
                         [Stage.RUNNER_BOOTSTRAP, Stage.TEST_RUN, Stage.CLEANUP])
        self.assertEqual([s["status"] for s in rec.stages], [PASSED, PASSED, PASSED])
        self.assertEqual(outcome["result_code"], "PASS")

    def test_bootstrap_failure_skips_test_still_tears_down(self):
        rec = StageRecorder()
        calls = []

        def fake(args, **kw):
            calls.append(args)
            rc = 1 if "--bootstrap-only" in args else 0
            return subprocess.CompletedProcess(args, rc, stdout="", stderr="boom")

        outcome = _call(rec, fake)
        self.assertFalse(any("--skip-bootstrap" in c for c in calls))  # test skipped
        self.assertTrue(any(c[:3] == ["podman", "rm", "-f"] for c in calls))
        self.assertEqual(rec.stages[0]["status"], FAILED)
        self.assertEqual(rec.stages[1]["name"], Stage.TEST_RUN)
        self.assertEqual(rec.stages[1]["status"], SKIPPED)
        self.assertEqual(outcome["result_code"], "FAIL")

    def test_teardown_runs_even_if_a_stage_raises(self):
        calls = []

        def fake(args, **kw):
            calls.append(args)
            if "--skip-bootstrap" in args:
                raise RuntimeError("exec blew up")
            return _ok(args)

        with self.assertRaises(RuntimeError):
            _call(StageRecorder(), fake)
        self.assertTrue(any(c[:3] == ["podman", "rm", "-f"] for c in calls))

    def test_two_run_stages_build_then_test(self):
        rec = StageRecorder()
        calls = []

        def fake(args, **kw):
            calls.append(args)
            return _ok(args)

        out = _call(rec, fake, run_stages=[
            (Stage.PROJECT_BUILD, ["run", "-c", "build"], "opp_build_project"),
            (Stage.TEST_RUN, ["run", "-c", "test"], "opp_run_smoke_tests")])
        self.assertEqual([s["name"] for s in rec.stages],
                         [Stage.RUNNER_BOOTSTRAP, Stage.PROJECT_BUILD,
                          Stage.TEST_RUN, Stage.CLEANUP])
        # the stage view shows the bare command, not the executed `run -c …` argv
        self.assertEqual(
            [s["command"] for s in rec.stages
             if s["name"] not in (Stage.RUNNER_BOOTSTRAP, Stage.CLEANUP)],
            ["opp_build_project", "opp_run_smoke_tests"])
        self.assertEqual(len([c for c in calls if "--skip-bootstrap" in c]), 2)
        self.assertEqual(out["result_code"], "PASS")

    def test_build_failure_skips_test_stage(self):
        rec = StageRecorder()

        def fake(args, **kw):
            if "--skip-bootstrap" in args and "build" in args:
                return subprocess.CompletedProcess(args, 1, stdout="", stderr="boom")
            return _ok(args)

        out = _call(rec, fake, run_stages=[
            (Stage.PROJECT_BUILD, ["run", "-c", "build"], "opp_build_project"),
            (Stage.TEST_RUN, ["run", "-c", "test"], "opp_run_smoke_tests")])
        self.assertEqual([(s["name"], s["status"]) for s in rec.stages],
                         [(Stage.RUNNER_BOOTSTRAP, PASSED),
                          (Stage.PROJECT_BUILD, FAILED),
                          (Stage.TEST_RUN, SKIPPED),
                          (Stage.CLEANUP, PASSED)])
        self.assertEqual(out["result_code"], "FAIL")

    def test_failed_run_d_raises_and_tears_down(self):
        calls = []

        def fake(args, **kw):
            calls.append(args)
            if args[:3] == ["podman", "run", "-d"]:
                return subprocess.CompletedProcess(args, 125, stdout="", stderr="no image")
            return _ok(args)

        with self.assertRaises(RuntimeError):
            _call(StageRecorder(), fake)
        # even though startup failed, the finally still issues an rm -f
        self.assertTrue(any(c[:3] == ["podman", "rm", "-f"] for c in calls))


class ContainerPrepareStageTests(unittest.TestCase):
    """_run_test_in_podman records container.prepare around the image build."""

    _STAGED_OK = {"result_code": "PASS", "test_exec_seconds": 1.0,
                  "stdout": "", "stderr": "", "details": None, "commit_sha": None}

    def test_prepare_is_first_stage_and_nix_splits_build_test(self):
        rec = StageRecorder()
        with mock.patch("opp_ci.executor._podman_image_tag", return_value="img"), \
             mock.patch("opp_ci.executor.tempfile.mkdtemp", return_value="/tmp/fake"), \
             mock.patch("opp_ci.executor._ensure_runner_image") as ensure, \
             mock.patch("opp_ci.executor.resolve_opp_env_id", return_value=("mm1k-latest", None)), \
             mock.patch("opp_ci.executor._run_podman_staged",
                        return_value=self._STAGED_OK) as staged:
            executor._run_test_in_podman(
                "mm1k", "smoke", toolchain="nix", recorder=rec,
                resolved_deps={"omnetpp": "6.2.0"})
        ensure.assert_called_once()
        self.assertEqual(rec.stages[0]["name"], Stage.CONTAINER_PREPARE)
        self.assertEqual(rec.stages[0]["status"], PASSED)
        # nix hands three run stages (install, build, test) to the staged runner
        run_stages = staged.call_args.kwargs["run_stages"]
        self.assertEqual([s[0] for s in run_stages],
                         [Stage.DEPS_INSTALL, Stage.PROJECT_BUILD, Stage.TEST_RUN])

    def test_checkout_stage_recorded_for_git_ref(self):
        rec = StageRecorder()
        with mock.patch("opp_ci.executor._podman_image_tag", return_value="img"), \
             mock.patch("opp_ci.executor._ensure_runner_image"), \
             mock.patch("opp_ci.executor.resolve_opp_env_id", return_value=("mm1k", None)), \
             mock.patch("opp_ci.executor._resolve_project_dir", return_value="/proj"), \
             mock.patch("opp_ci.executor._create_git_worktree", return_value="/wt") as wt, \
             mock.patch("opp_ci.executor._run_podman_staged", return_value=self._STAGED_OK):
            executor._run_test_in_podman(
                "mm1k", "smoke", toolchain="nix", recorder=rec,
                opp_file="mm1k.opp", git_ref="abc123",
                resolved_deps={"omnetpp": "6.2.0"})
        self.assertEqual([s["name"] for s in rec.stages][:2],
                         [Stage.CONTAINER_PREPARE, Stage.CHECKOUT])
        wt.assert_called_once()

    def test_prepare_failure_recorded_and_raised(self):
        rec = StageRecorder()
        with mock.patch("opp_ci.executor._podman_image_tag", return_value="img"), \
             mock.patch("opp_ci.executor._ensure_runner_image",
                        side_effect=RuntimeError("build failed")), \
             mock.patch("opp_ci.executor.resolve_opp_env_id", return_value=("mm1k-latest", None)):
            with self.assertRaises(RuntimeError):
                executor._run_test_in_podman(
                    "mm1k", "smoke", toolchain="nix", recorder=rec,
                    resolved_deps={"omnetpp": "6.2.0"})
        self.assertEqual(rec.stages[0]["name"], Stage.CONTAINER_PREPARE)
        self.assertEqual(rec.stages[0]["status"], FAILED)


if __name__ == "__main__":
    unittest.main()
