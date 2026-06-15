"""Tests for plan/pending/pin-opp-repl-and-opp-env-to-opp-ci-branch.md:

  * opp_repl/opp_env refs are centralised in config and default to the
    `opp_ci` branch; service re-exports them;
  * the coordinator dependency resolver shells out via the configured opp_env
    command (OPP_CI_OPP_ENV_CMD), not a hard-coded `opp_env`;
  * the podman runner images no longer install/clone opp_ci — both entry
    scripts clone opp_repl only (at its opp_ci branch) and the host path drives
    opp_repl's console scripts directly;
  * each podman + host-nix run captures opp_repl's --result-file JSON into
    TestRun.details.

No DB needed — run with: python -m unittest tests.test_opp_ci_branch_pins
"""

import json
import os
import tempfile
import unittest
from unittest import mock

from opp_ci import config, executor, service
from opp_ci.stages import Stage


class CentralRefsTest(unittest.TestCase):
    def test_refs_default_to_opp_ci_branch(self):
        self.assertEqual(config.OPP_REPL_REF, "opp_ci")
        self.assertEqual(config.OPP_ENV_REF, "opp_ci")
        self.assertTrue(config.OPP_ENV_GIT.startswith("git+https://"))
        self.assertTrue(config.OPP_REPL_GIT.startswith("git+https://"))

    def test_service_reexports_config_refs(self):
        self.assertEqual(service.OPP_ENV_REF, config.OPP_ENV_REF)
        self.assertEqual(service.OPP_ENV_GIT, config.OPP_ENV_GIT)
        self.assertEqual(service.OPP_REPL_GIT, config.OPP_REPL_GIT)


class OppEnvArgvTest(unittest.TestCase):
    def test_default_is_bare_opp_env(self):
        with mock.patch.object(config, "OPP_ENV_CMD", "opp_env"):
            self.assertEqual(config.opp_env_argv(), ["opp_env"])

    def test_shlex_split_of_override(self):
        with mock.patch.object(config, "OPP_ENV_CMD", "uvx --from opp-env opp_env"):
            self.assertEqual(config.opp_env_argv(),
                             ["uvx", "--from", "opp-env", "opp_env"])


class DependencyUsesConfiguredOppEnvTest(unittest.TestCase):
    def test_query_opp_env_info_uses_opp_env_argv(self):
        from opp_ci import dependency
        with mock.patch.object(config, "OPP_ENV_CMD", "myoppenv --flag"), \
             mock.patch("opp_ci.dependency.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=1, stderr="", stdout="")
            dependency.query_opp_env_info("inet-4.5")
            argv = run.call_args.args[0]
            self.assertEqual(argv[:4], ["myoppenv", "--flag", "info", "inet-4.5"])


class RenderNoOppCiTest(unittest.TestCase):
    def _render(self, toolchain):
        with mock.patch.object(executor, "_resolve_remote_head", return_value="deadbeef"):
            return executor.render_containerfile(
                toolchain, "ubuntu", "24.04", "gcc", "13", omnetpp_version="6.1")

    def test_host_image_drops_opp_ci_and_pins_opp_repl(self):
        files = self._render("none")
        entry = files["opp_ci_entry.sh"]
        self.assertNotIn("opp_ci_src", entry)                 # opp_ci not cloned
        self.assertNotIn('-c "opp_ci', entry)                 # opp_ci not exec'd
        self.assertIn("sync_repo opp_repl", entry)
        self.assertIn("opp_repl.git opp_ci", entry)           # cloned at opp_ci branch
        cf = files["Containerfile"]
        self.assertIn("opp_repl.git@opp_ci", cf)              # baked deps pinned
        self.assertIn("opp_env.git@deadbeef", cf)             # opp_env at opp_ci-branch SHA
        self.assertNotIn("opp_ci.git", cf)

    def test_nix_image_clones_opp_repl_only(self):
        files = self._render("nix")
        entry = files["opp_env_entry.sh"]
        self.assertNotIn("opp_ci_src", entry)
        self.assertIn("opp_repl.git opp_ci", entry)
        self.assertIn("opp_env.git@deadbeef", files["Containerfile"])

    def test_resolve_remote_head_called_for_opp_ci_branch(self):
        with mock.patch.object(executor, "_resolve_remote_head",
                               return_value="sha") as rh:
            executor.render_containerfile("nix", "ubuntu", "24.04", "gcc", "13",
                                          omnetpp_version="6.1")
            # resolves the opp_ci branch head, not the default-branch HEAD
            self.assertEqual(rh.call_args.args[1], config.OPP_ENV_REF)


class ReadResultFileTest(unittest.TestCase):
    def test_reads_json(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "r.json")
            with open(p, "w") as f:
                json.dump({"results": [{"name": "x"}], "elapsed_wall_time": 1.5}, f)
            self.assertEqual(executor._read_result_file(p)["elapsed_wall_time"], 1.5)

    def test_missing_returns_none(self):
        self.assertIsNone(executor._read_result_file("/no/such/result.json"))

    def test_corrupt_returns_none(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write("not json {")
            p = f.name
        self.addCleanup(os.remove, p)
        self.assertIsNone(executor._read_result_file(p))


class PodmanResultFilePlumbingTest(unittest.TestCase):
    _OK = {"result_code": "PASS", "test_exec_seconds": 1.0, "stdout": "",
           "stderr": "", "details": None, "commit_sha": None}

    def _staged_kwargs(self, toolchain, **kw):
        with mock.patch("opp_ci.executor._podman_image_tag", return_value="img"), \
             mock.patch("opp_ci.executor._ensure_runner_image"), \
             mock.patch("opp_ci.executor._resolve_project_dir", return_value="/proj"), \
             mock.patch("opp_ci.executor.resolve_opp_env_id",
                        return_value=("mm1k-latest", None)), \
             mock.patch("opp_ci.executor._run_podman_staged",
                        return_value=self._OK) as staged:
            executor._run_test_in_podman("mm1k", "smoke", toolchain=toolchain, **kw)
        return staged.call_args.kwargs

    @staticmethod
    def _test_stage(kwargs):
        return [s for s in kwargs["run_stages"] if s[0] == Stage.TEST_RUN][0][1]

    def test_host_path_mounts_result_dir_and_passes_result_file(self):
        kw = self._staged_kwargs("none", opp_file="mm1k.opp", resolved_deps={})
        self.assertTrue(kw["result_file"].endswith("result.json"))
        self.assertTrue(any("/opp_ci_result" in f for f in kw["run_flags"]))
        test_args = self._test_stage(kw)
        self.assertIn("--result-file", test_args)
        self.assertIn("/opp_ci_result/result.json", test_args)

    def test_nix_path_test_command_carries_result_file(self):
        kw = self._staged_kwargs("nix", resolved_deps={"omnetpp": "6.2.0"})
        joined = " ".join(self._test_stage(kw))
        self.assertIn("--result-file /opp_ci_result/result.json", joined)


class PodmanStagedReadsDetailsTest(unittest.TestCase):
    """_run_podman_staged reads result_file into details (a missing file → None)."""

    def test_details_from_result_file(self):
        with tempfile.TemporaryDirectory() as d:
            rf = os.path.join(d, "result.json")
            with open(rf, "w") as f:
                json.dump({"results": [1, 2]}, f)
            ok = mock.Mock(returncode=0, stdout="", stderr="")
            with mock.patch("opp_ci.executor.run_external", return_value=ok):
                out = executor._run_podman_staged(
                    image="img",
                    run_stages=[(Stage.TEST_RUN, ["opp_run_smoke_tests"], "smoke")],
                    entry_script="/e.sh", run_flags=[], recorder=None,
                    git_ref=None, worktree_path=None, scratch_dir=None,
                    result_file=rf)
            self.assertEqual(out["details"], {"results": [1, 2]})

    def test_details_none_when_no_result_file(self):
        ok = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch("opp_ci.executor.run_external", return_value=ok):
            out = executor._run_podman_staged(
                image="img",
                run_stages=[(Stage.TEST_RUN, ["opp_run_smoke_tests"], "smoke")],
                entry_script="/e.sh", run_flags=[], recorder=None,
                git_ref=None, worktree_path=None, scratch_dir=None,
                result_file=None)
        self.assertIsNone(out["details"])


if __name__ == "__main__":
    unittest.main()
