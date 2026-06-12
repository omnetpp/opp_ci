"""Tests for strict single-run source pinning (Phase 2b of
plan/pending/repeatable-tests-and-moving-target-matrices.md).

resolve_source_commit pins a single ref to a concrete commit and is STRICT:
a ref that can't be resolved (no GitHub repo, unknown ref) is rejected, not
left unpinned (decision #7). GitHub is mocked so these stay hermetic.

Run with: python -m pytest tests/test_source_commit.py
"""

import os
import tempfile
import unittest
from unittest import mock

_DB_FD, _DB_PATH = tempfile.mkstemp(suffix=".db", prefix="opp_ci_src_")
os.close(_DB_FD)
os.environ["OPP_CI_DATABASE_URL"] = f"sqlite:///{_DB_PATH}"

from opp_ci import scheduler                                    # noqa: E402
from opp_ci.db.connection import engine, SessionLocal           # noqa: E402
from opp_ci.db.models import Base, Project                      # noqa: E402


def _fake_github(resolve_ref_result):
    client = mock.Mock()
    client.is_configured = True
    client.resolve_ref.return_value = resolve_ref_result
    return mock.patch("opp_ci.github.client.GitHubClient",
                      return_value=client)


class SourceCommitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        s = SessionLocal()
        try:
            s.add(Project(name="inet", github_owner="inet-framework",
                          github_repo="inet"))
            s.add(Project(name="local", github_owner=None, github_repo=None))
            s.commit()
        finally:
            s.close()

    def test_none_ref_is_unpinned(self):
        self.assertIsNone(scheduler.resolve_source_commit("inet", None))

    def test_full_sha_passes_through(self):
        sha = "A" * 40
        self.assertEqual(scheduler.resolve_source_commit("inet", sha),
                         "a" * 40)

    def test_branch_resolves_via_github(self):
        with _fake_github("b" * 40):
            self.assertEqual(
                scheduler.resolve_source_commit("inet", "main"), "b" * 40)

    def test_branch_without_github_repo_rejected(self):
        with self.assertRaises(ValueError):
            scheduler.resolve_source_commit("local", "main")

    def test_unknown_project_rejected(self):
        with self.assertRaises(ValueError):
            scheduler.resolve_source_commit("ghost", "main")

    def test_unresolvable_ref_rejected(self):
        with _fake_github(None):  # GitHub returns no SHA for any ref path
            with self.assertRaises(ValueError):
                scheduler.resolve_source_commit("inet", "no-such-branch")


if __name__ == "__main__":
    unittest.main()
