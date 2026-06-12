"""opp_env_project_id: build the full opp_env id from (project, version).

Regression for "opp_env install … git-latest: Unknown project 'git'": opp_env's
version *field* for mm1k is the bare "git", so the worker must combine it with
the project into "mm1k-git" rather than use "git" alone.

Run with: python -m pytest tests/test_opp_env_id.py
"""

import unittest

from opp_ci.executor import opp_env_project_id


class OppEnvProjectIdTests(unittest.TestCase):
    def test_bare_version_combined_with_project(self):
        self.assertEqual(opp_env_project_id("mm1k", "git"), "mm1k-git")
        self.assertEqual(opp_env_project_id("inet", "4.5"), "inet-4.5")

    def test_already_full_id_kept(self):
        self.assertEqual(opp_env_project_id("mm1k", "mm1k-git"), "mm1k-git")
        self.assertEqual(opp_env_project_id("inet", "inet-4.5.0"), "inet-4.5.0")

    def test_no_version_uses_bare_project(self):
        self.assertEqual(opp_env_project_id("mm1k", None), "mm1k")
        self.assertEqual(opp_env_project_id("mm1k", ""), "mm1k")


if __name__ == "__main__":
    unittest.main()
