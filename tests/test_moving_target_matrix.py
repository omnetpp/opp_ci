"""Tests for moving-target matrices (Phase 3 of
plan/pending/repeatable-tests-and-moving-target-matrices.md).

The refs axis carries the source spec (decision #5): a `base..topic` range
fans out (expand) into one pinned Test per commit; a branch/tag passes
through unpinned without touching the network; a full SHA is already pinned.
GitHub is mocked so these stay hermetic.

Run with: python -m pytest tests/test_moving_target_matrix.py
"""

import os
import tempfile
import unittest
from unittest import mock

_DB_FD, _DB_PATH = tempfile.mkstemp(suffix=".db", prefix="opp_ci_mtm_")
os.close(_DB_FD)
os.environ["OPP_CI_DATABASE_URL"] = f"sqlite:///{_DB_PATH}"

from opp_ci import scheduler                                    # noqa: E402
from opp_ci.persistence import job_to_coord                    # noqa: E402
from opp_ci.db.models import compute_test_coord_hash           # noqa: E402

_BASE_CONFIG = {"kinds": ["smoke"], "modes": ["release"],
                "distro": ["Ubuntu 24.04"], "arch": ["amd64"],
                "compiler": ["gcc-14"]}


def _config(**over):
    cfg = dict(_BASE_CONFIG)
    cfg.update(over)
    return cfg


class ExpandRefsAxisTests(unittest.TestCase):
    def _expand(self, range_to_shas, **cfg_over):
        with mock.patch.object(scheduler, "_resolve_ref_range",
                               side_effect=lambda proj, rng: range_to_shas[rng]):
            return scheduler.expand_matrix("inet", _config(**cfg_over))

    def test_range_fans_out_into_pinned_commits(self):
        shas = ["a" * 40, "b" * 40, "c" * 40]
        jobs = self._expand({"v1..v2": shas}, refs=["v1..v2"])
        self.assertEqual(len(jobs), 3)
        self.assertEqual([j["commit_sha"] for j in jobs], shas)
        # Each pinned commit becomes a distinct Test identity.
        hashes = {compute_test_coord_hash(job_to_coord(j, project="inet"))
                  for j in jobs}
        self.assertEqual(len(hashes), 3)

    def test_single_ref_unpinned_no_network(self):
        # No "_resolve_ref_range" patch: a branch must not hit the network.
        jobs = scheduler.expand_matrix("inet", _config(refs=["main"]))
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["git_ref"], "main")
        self.assertIsNone(jobs[0]["commit_sha"])

    def test_full_sha_is_already_pinned(self):
        sha = "d" * 40
        jobs = scheduler.expand_matrix("inet", _config(refs=[sha]))
        self.assertEqual(jobs[0]["commit_sha"], sha)
        self.assertEqual(jobs[0]["git_ref"], sha)

    def test_mixed_refs(self):
        shas = ["e" * 40, "f" * 40]
        jobs = self._expand({"x..y": shas}, refs=["main", "x..y"])
        self.assertEqual(len(jobs), 3)  # main + 2 range commits
        commits = [j["commit_sha"] for j in jobs]
        self.assertIn(None, commits)          # the branch, unpinned
        self.assertIn("e" * 40, commits)
        self.assertIn("f" * 40, commits)

    def test_no_refs_axis_unpinned(self):
        jobs = scheduler.expand_matrix("inet", _config())
        self.assertEqual(len(jobs), 1)
        self.assertIsNone(jobs[0]["commit_sha"])
        self.assertIsNone(jobs[0]["git_ref"])


if __name__ == "__main__":
    unittest.main()
