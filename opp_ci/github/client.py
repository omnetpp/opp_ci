"""
GitHub API client for opp_ci.

Posts commit statuses and PR comments to report test results back to GitHub.
Reads the API token from ~/.ssh/github_repo_token or OPP_CI_GITHUB_TOKEN env var.
"""

import logging

import requests

from opp_ci.config import get_github_token, GITHUB_BASE_URL, GITHUB_STATUS_CONTEXT

_logger = logging.getLogger(__name__)


class GitHubClient:
    """Thin wrapper around the GitHub REST API v3."""

    def __init__(self, token=None, base_url=None):
        self.token = token or get_github_token()
        self.base_url = (base_url or GITHUB_BASE_URL).rstrip("/")
        self._session = requests.Session()
        if self.token:
            self._session.headers["Authorization"] = f"token {self.token}"
        self._session.headers["Accept"] = "application/vnd.github+json"
        self._session.headers["X-GitHub-Api-Version"] = "2022-11-28"

    @property
    def is_configured(self):
        return bool(self.token)

    # ── Commit statuses ────────────────────────────────────────────────

    def create_commit_status(self, owner, repo, sha, state, target_url=None,
                             description=None, context=None):
        """
        Create a commit status.

        Args:
            owner: GitHub repo owner (e.g. "inet-framework")
            repo: GitHub repo name (e.g. "inet")
            sha: Full commit SHA
            state: "pending", "success", "failure", or "error"
            target_url: URL to the opp_ci run detail page
            description: Short description (max 140 chars)
            context: Status context string (default: from config)
        """
        url = f"{self.base_url}/repos/{owner}/{repo}/statuses/{sha}"
        payload = {
            "state": state,
            "context": context or GITHUB_STATUS_CONTEXT,
        }
        if target_url:
            payload["target_url"] = target_url
        if description:
            payload["description"] = description[:140]

        resp = self._session.post(url, json=payload, timeout=15)
        if resp.status_code in (201, 200):
            _logger.info("Posted status %s for %s/%s@%s", state, owner, repo, sha[:8])
            return resp.json()
        else:
            _logger.error("Failed to post status: %s %s", resp.status_code, resp.text[:200])
            resp.raise_for_status()

    def set_status_pending(self, owner, repo, sha, run_id, target_url=None):
        """Set commit status to pending when a run is queued."""
        return self.create_commit_status(
            owner, repo, sha,
            state="pending",
            description=f"opp_ci run #{run_id} queued",
            target_url=target_url,
        )

    def set_status_from_run(self, owner, repo, sha, run_id, run_status, target_url=None):
        """Set commit status based on a TestRun's final status."""
        state_map = {
            "passed": "success",
            "failed": "failure",
            "error": "error",
            "running": "pending",
            "queued": "pending",
        }
        state = state_map.get(run_status, "error")
        desc = f"opp_ci run #{run_id}: {run_status}"
        return self.create_commit_status(
            owner, repo, sha,
            state=state,
            description=desc,
            target_url=target_url,
        )

    # ── PR comments ────────────────────────────────────────────────────

    def create_pr_comment(self, owner, repo, pr_number, body):
        """Post a comment on a pull request."""
        url = f"{self.base_url}/repos/{owner}/{repo}/issues/{pr_number}/comments"
        resp = self._session.post(url, json={"body": body}, timeout=15)
        if resp.status_code == 201:
            _logger.info("Posted PR comment on %s/%s#%d", owner, repo, pr_number)
            return resp.json()
        else:
            _logger.error("Failed to post PR comment: %s %s", resp.status_code, resp.text[:200])
            resp.raise_for_status()

    def update_or_create_pr_comment(self, owner, repo, pr_number, body, marker=None):
        """
        Update an existing opp_ci comment on a PR, or create a new one.

        Uses a hidden HTML marker to identify opp_ci comments for updating.
        """
        marker = marker or f"<!-- opp_ci-results -->"
        body_with_marker = f"{marker}\n{body}"

        # Search for existing comment with marker
        existing = self._find_pr_comment(owner, repo, pr_number, marker)
        if existing:
            return self._update_comment(owner, repo, existing["id"], body_with_marker)
        else:
            return self.create_pr_comment(owner, repo, pr_number, body_with_marker)

    def _find_pr_comment(self, owner, repo, pr_number, marker):
        """Find an existing comment containing the marker string."""
        url = f"{self.base_url}/repos/{owner}/{repo}/issues/{pr_number}/comments"
        resp = self._session.get(url, params={"per_page": 100}, timeout=15)
        if resp.status_code != 200:
            return None
        for comment in resp.json():
            if marker in comment.get("body", ""):
                return comment
        return None

    def _update_comment(self, owner, repo, comment_id, body):
        """Update an existing issue/PR comment."""
        url = f"{self.base_url}/repos/{owner}/{repo}/issues/comments/{comment_id}"
        resp = self._session.patch(url, json={"body": body}, timeout=15)
        if resp.status_code == 200:
            _logger.info("Updated comment %d on %s/%s", comment_id, owner, repo)
            return resp.json()
        else:
            _logger.error("Failed to update comment: %s %s", resp.status_code, resp.text[:200])
            resp.raise_for_status()

    # ── Queries ────────────────────────────────────────────────────────

    def get_pr(self, owner, repo, pr_number):
        """Get PR metadata."""
        url = f"{self.base_url}/repos/{owner}/{repo}/pulls/{pr_number}"
        resp = self._session.get(url, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def get_commit(self, owner, repo, sha):
        """Get commit metadata."""
        url = f"{self.base_url}/repos/{owner}/{repo}/commits/{sha}"
        resp = self._session.get(url, timeout=15)
        resp.raise_for_status()
        return resp.json()
