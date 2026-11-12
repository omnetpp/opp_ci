"""Regression tests for test-baseline resolution in opp_ci.executor.

A run is dispatched under its *versioned* opp_env id (e.g. ``inet-git``), but a
project's per-kind ``test_parameters`` — including the chart/statistical baseline
repo — live under the *bare* name in the bundled ``@opp`` registry. So
``_resolve_test_baseline`` must look up the bare name; otherwise
``_load_workspace`` fails to find the project, silently falls back to a
programmatic project with no ``test_parameters``, and the baseline checkout never
happens (chart/statistical then fail with "Stored statistical results are not
found" / "Baseline chart not found").

A baseline can declare a ``ref``. A full commit hash names one fixed commit, which
can be on no branch: the head of a pull request in the baseline repository. The
checkout must fetch such a commit by its hash.
"""
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from opp_ci import executor


def _git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=test", "-c", "user.email=test@example.com", *args],
                          cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


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


class BaselineCheckoutRefTests(unittest.TestCase):
    """Both checkout paths, with real git repositories in a temporary folder."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = os.path.realpath(tmp.name)
        self.remote = os.path.join(root, "statistics.git")
        os.makedirs(self.remote)
        _git(self.remote, "init", "-q", "-b", "master", ".")
        # GitHub serves a commit that a ref can reach to a fetch by its hash;
        # a plain repository needs this option for the same
        _git(self.remote, "config", "uploadpack.allowAnySHA1InWant", "true")
        self.first = self._commit("first", "1\n")
        self.second = self._commit("second", "2\n")
        _git(self.remote, "checkout", "-q", "--detach")
        self.pull_request = self._commit("pull request", "3\n")
        _git(self.remote, "update-ref", "refs/pull/1/head", self.pull_request)
        _git(self.remote, "checkout", "-q", "master")
        self.project_root = os.path.join(root, "project")
        os.makedirs(self.project_root)
        self.dest = os.path.join(self.project_root, "statistics")

    def _commit(self, message, content):
        with open(os.path.join(self.remote, "a.sca"), "w") as f:
            f.write(content)
        _git(self.remote, "add", "-A")
        _git(self.remote, "commit", "-q", "-m", message)
        return _git(self.remote, "rev-parse", "HEAD")

    def _baseline(self, ref=None):
        baseline = {"repository": "file://" + self.remote, "folder": "statistics"}
        if ref:
            baseline["ref"] = ref
        return baseline

    def _checkout(self, path, ref):
        if path == "shell":
            subprocess.run(["bash", "-c", executor._baseline_checkout_shell(self._baseline(ref))],
                           cwd=self.project_root, check=True, capture_output=True, text=True)
        else:
            executor._checkout_baseline_on_host(self._baseline(ref), "statistical", self.project_root)
        return _git(self.dest, "rev-parse", "HEAD")

    def _content(self):
        with open(os.path.join(self.dest, "a.sca")) as f:
            return f.read()

    def test_commit_hash_in_a_new_clone(self):
        for path in ("shell", "host"):
            with self.subTest(path=path):
                self.assertEqual(self._checkout(path, self.first), self.first)
                self.assertEqual(self._content(), "1\n")
                subprocess.run(["rm", "-rf", self.dest], check=True)

    def test_commit_only_on_a_pull_request_head(self):
        for path in ("shell", "host"):
            with self.subTest(path=path):
                # a clone and a plain fetch bring the branches only
                self._checkout(path, self.first)
                self.assertNotEqual(subprocess.run(["git", "cat-file", "-e", self.pull_request + "^{commit}"],
                                                   cwd=self.dest, capture_output=True).returncode, 0)
                self.assertEqual(self._checkout(path, self.pull_request), self.pull_request)
                self.assertEqual(self._content(), "3\n")
                subprocess.run(["rm", "-rf", self.dest], check=True)

    def test_branch_ref_follows_the_remote(self):
        for path in ("shell", "host"):
            with self.subTest(path=path):
                self.assertEqual(self._checkout(path, "master"), _git(self.remote, "rev-parse", "master"))
                newer = self._commit("newer " + path, path + "\n")
                self.assertEqual(self._checkout(path, "master"), newer)
                self.assertEqual(self._content(), path + "\n")
                subprocess.run(["rm", "-rf", self.dest], check=True)

    def test_no_ref_keeps_the_default_branch(self):
        for path in ("shell", "host"):
            with self.subTest(path=path):
                self.assertEqual(self._checkout(path, None), _git(self.remote, "rev-parse", "master"))
                subprocess.run(["rm", "-rf", self.dest], check=True)

    def test_unknown_commit_fails(self):
        missing = "0" * 40
        with self.assertRaises(subprocess.CalledProcessError):
            self._checkout("shell", missing)
        subprocess.run(["rm", "-rf", self.dest], check=True)
        with self.assertRaises(RuntimeError):
            self._checkout("host", missing)

    def test_full_commit_hash(self):
        self.assertTrue(executor._is_full_commit_hash("e8f63b9c81e3a12d114898f00e6178151fa6c033"))
        self.assertFalse(executor._is_full_commit_hash("e8f63b9"))
        self.assertFalse(executor._is_full_commit_hash("master"))


if __name__ == "__main__":
    unittest.main()
