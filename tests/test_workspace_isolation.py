"""Tests for the per-coordinate opp_env host workspace
(plan/pending/nix-workspace-isolation.md).

The host-nix path (isolation=none, toolchain=nix) has no container to
isolate it, so each build coordinate gets its own directory under
config.WORKSPACE_ROOT. Identical coordinate → same directory (omnetpp built
once); any axis differing (omnetpp pin, compiler, git ref, project) → a
distinct directory so builds can't clobber each other.

Run with: python -m unittest tests.test_workspace_isolation  (no pytest needed)
"""

import os
import tempfile
import unittest

from opp_ci import config                                     # noqa: E402
from opp_ci.executor import (                                 # noqa: E402
    _opp_env_workspace, _gc_workspaces, _workspace_lock_path,
)


def _ws(root, **over):
    base = {"project": "mm1k", "resolved_deps": {"omnetpp": "6.4.0"},
            "toolchain": "nix", "compiler": "clang", "compiler_version": "21",
            "commit_sha": "3f9a1c2b4d5e6f70"}
    base.update(over)
    orig = config.WORKSPACE_ROOT
    config.WORKSPACE_ROOT = root
    try:
        return _opp_env_workspace(**base)
    finally:
        config.WORKSPACE_ROOT = orig


class CoordinateKeyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="opp_ci_ws_")
        self.root = os.path.join(self._tmp, "workspace")

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_identical_coordinate_same_dir(self):
        self.assertEqual(_ws(self.root), _ws(self.root))

    def test_dir_is_created_under_root(self):
        ws = _ws(self.root)
        self.assertTrue(os.path.isdir(ws))
        self.assertEqual(os.path.dirname(ws), self.root)

    def test_readable_prefix(self):
        ws = os.path.basename(_ws(self.root))
        # <project>-omnetpp<pin>-<compiler><ver>-<ref8>-<hash8>
        self.assertTrue(ws.startswith("mm1k-omnetpp6.4.0-clang21-3f9a1c2b-"))

    def test_omnetpp_pin_differs(self):
        a = _ws(self.root, resolved_deps={"omnetpp": "6.4.0"})
        b = _ws(self.root, resolved_deps={"omnetpp": "6.3.0"})
        self.assertNotEqual(a, b)

    def test_compiler_differs(self):
        self.assertNotEqual(_ws(self.root, compiler="clang"),
                            _ws(self.root, compiler="gcc"))

    def test_compiler_version_differs(self):
        self.assertNotEqual(_ws(self.root, compiler_version="21"),
                            _ws(self.root, compiler_version="20"))

    def test_commit_sha_differs(self):
        # The source axis is the resolved commit SHA.
        self.assertNotEqual(_ws(self.root, commit_sha="aaaaaaaa1111"),
                            _ws(self.root, commit_sha="bbbbbbbb2222"))

    def test_git_ref_does_not_affect_key(self):
        # A moving branch name must never key the workspace — only the resolved
        # commit does. Same commit under different branch labels → same dir
        # (and identical source across branches dedups to one build).
        a = _ws(self.root, git_ref="topic/feature", commit_sha="abc123def456")
        b = _ws(self.root, git_ref="release/9.9", commit_sha="abc123def456")
        self.assertEqual(a, b)

    def test_moving_branch_keyed_by_commit_sha(self):
        # Same branch ref, two commits → distinct workspaces. Without keying on
        # the resolved SHA, a new commit on the branch would reuse a stale tree
        # (opp_env never re-checks-out).
        a = _ws(self.root, git_ref="topic/feature", commit_sha="aaaaaaaa1111")
        b = _ws(self.root, git_ref="topic/feature", commit_sha="bbbbbbbb2222")
        self.assertNotEqual(a, b)

    def test_project_differs(self):
        self.assertNotEqual(_ws(self.root, project="mm1k"),
                            _ws(self.root, project="inet"))

    def test_dep_order_independent(self):
        # Dict iteration order must not change the hash.
        a = _ws(self.root, resolved_deps={"omnetpp": "6.4.0", "inet": "4.6.0"})
        b = _ws(self.root, resolved_deps={"inet": "4.6.0", "omnetpp": "6.4.0"})
        self.assertEqual(a, b)

    def test_missing_axes_normalized(self):
        # None and absent should hash consistently (no crash, stable path).
        a = _ws(self.root, resolved_deps=None, compiler=None,
                compiler_version=None, git_ref=None, commit_sha=None)
        b = _ws(self.root, resolved_deps=None, compiler=None,
                compiler_version=None, git_ref=None, commit_sha=None)
        self.assertEqual(a, b)


class GcTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="opp_ci_ws_gc_")
        self.root = os.path.join(self._tmp, "workspace")
        self._orig_root = config.WORKSPACE_ROOT
        self._orig_max = config.WORKSPACE_MAX
        config.WORKSPACE_ROOT = self.root
        config.WORKSPACE_MAX = 3

    def tearDown(self):
        import shutil
        config.WORKSPACE_ROOT = self._orig_root
        config.WORKSPACE_MAX = self._orig_max
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _make(self, name, mtime):
        ws = os.path.join(self.root, name)
        os.makedirs(ws)
        os.utime(ws, (mtime, mtime))
        return ws

    def test_evicts_lru_beyond_max(self):
        kept = [self._make(f"keep{i}", 2000 + i) for i in range(3)]
        old = [self._make(f"old{i}", 1000 + i) for i in range(2)]
        _gc_workspaces()
        for ws in kept:
            self.assertTrue(os.path.isdir(ws), f"{ws} should survive")
        for ws in old:
            self.assertFalse(os.path.isdir(ws), f"{ws} should be evicted")

    def test_under_cap_keeps_all(self):
        dirs = [self._make(f"d{i}", 1000 + i) for i in range(3)]
        _gc_workspaces()
        for ws in dirs:
            self.assertTrue(os.path.isdir(ws))

    def test_lock_file_is_sibling_not_inside_workspace(self):
        # opp_env install --init refuses a non-empty dir, so the lock must live
        # beside the workspace, leaving the dir empty when opp_env first runs.
        import fcntl
        ws = self._make("fresh", 100)
        path = _workspace_lock_path(ws)
        self.assertEqual(os.path.dirname(path), os.path.dirname(ws),
                         "lock file must be a sibling of the workspace dir")
        self.assertFalse(path.startswith(ws + os.sep),
                         "lock file must not be inside the workspace dir")
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            self.assertEqual(os.listdir(ws), [],
                             "workspace dir must stay empty while the lock is held")
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def test_locked_dir_skipped(self):
        import fcntl
        kept = [self._make(f"keep{i}", 2000 + i) for i in range(3)]
        locked = self._make("locked", 100)  # oldest → would be evicted
        another = self._make("old", 200)
        fd = os.open(_workspace_lock_path(locked), os.O_CREAT | os.O_RDWR, 0o644)
        # The lock file is a sibling of `locked`, not inside it, so creating it
        # doesn't touch the dir's mtime — but reassert oldest mtime for clarity
        # so the LRU sweep targets it (and must skip it because it's held).
        os.utime(locked, (100, 100))
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            _gc_workspaces()
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        self.assertTrue(os.path.isdir(locked), "locked dir must not be evicted")
        self.assertFalse(os.path.isdir(another), "unlocked old dir evicted")
        for ws in kept:
            self.assertTrue(os.path.isdir(ws))


if __name__ == "__main__":
    unittest.main()
