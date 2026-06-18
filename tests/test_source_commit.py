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


def _fake_github(resolve_ref_result, *, configured=True, remote_refs=None):
    client = mock.Mock()
    client.is_configured = configured
    client.resolve_ref.return_value = resolve_ref_result
    client.list_remote_refs.return_value = remote_refs or {}
    # Patch yields the class-mock; its `.return_value` is this client.
    return mock.patch("opp_ci.github.client.GitHubClient", return_value=client)


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
        # API configured but resolves nothing, and the ref-advertisement
        # fallback (remote_refs) has no matching ref → strict rejection.
        with _fake_github(None, remote_refs={}):
            with self.assertRaises(ValueError):
                scheduler.resolve_source_commit("inet", "no-such-branch")

    def test_falls_back_to_smart_http_when_api_unconfigured(self):
        # No GitHub token → resolve the public ref over the git smart-HTTP ref
        # advertisement (no credentials, no REST rate limit), not a failure.
        refs = {"refs/heads/omnetpp-6.x": "c" * 40}
        with _fake_github(None, configured=False, remote_refs=refs) as gh:
            self.assertEqual(
                scheduler.resolve_source_commit("inet", "omnetpp-6.x"), "c" * 40)
            client = gh.return_value
        client.resolve_ref.assert_not_called()        # REST skipped: no token
        client.list_remote_refs.assert_called_once_with("inet-framework", "inet")

    def test_annotated_tag_resolves_to_peeled_commit(self):
        refs = {"refs/tags/v1": "a" * 40, "refs/tags/v1^{}": "d" * 40}
        with _fake_github(None, configured=False, remote_refs=refs):
            self.assertEqual(scheduler.resolve_source_commit("inet", "v1"), "d" * 40)

    def test_smart_http_miss_when_unconfigured_rejects(self):
        with _fake_github(None, configured=False, remote_refs={}):
            with self.assertRaises(ValueError):
                scheduler.resolve_source_commit("inet", "no-such-branch")


if __name__ == "__main__":
    unittest.main()
