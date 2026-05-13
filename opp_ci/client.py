"""
Python client for the opp_ci REST API.

Allows remote job submission and result querying from any machine.

Usage:
    from opp_ci.client import OppCiClient

    ci = OppCiClient(url="https://ci.omnetpp.org/api", token="...")
    run = ci.submit_run(project="inet", test_type="smoke")
    ci.get_run(run["id"])
    results = ci.list_runs(project="inet", status="FAIL")
"""

import logging

import requests

_logger = logging.getLogger(__name__)


class OppCiClient:
    """Client for the opp_ci coordinator REST API."""

    def __init__(self, url, token):
        """
        Args:
            url: Coordinator API base URL (e.g. "https://ci.omnetpp.org/api")
            token: API token (submitter, admin, or readonly)
        """
        self.url = url.rstrip("/")
        self.token = token
        self._session = requests.Session()
        self._session.headers["Authorization"] = f"Bearer {token}"

    def submit_run(self, project, test_type, mode=None, git_ref=None,
                   os=None, os_version=None, compiler=None, compiler_version=None):
        """Submit a single test run. Returns {"id": ..., "status": "queued"}."""
        payload = {"project": project, "test_type": test_type}
        if mode:
            payload["mode"] = mode
        if git_ref:
            payload["git_ref"] = git_ref
        if os:
            payload["os"] = os
        if os_version:
            payload["os_version"] = os_version
        if compiler:
            payload["compiler"] = compiler
        if compiler_version:
            payload["compiler_version"] = compiler_version
        return self._post("/runs", payload)

    def submit_matrix(self, matrix_name):
        """Submit all jobs from a named matrix. Returns {"matrix": ..., "jobs_queued": ..., "run_ids": [...]}."""
        return self._post("/runs/matrix", {"matrix_name": matrix_name})

    def get_run(self, run_id):
        """Get full details of a run including results."""
        return self._get(f"/runs/{run_id}")

    def list_runs(self, project=None, test_type=None, status=None, limit=50):
        """List test runs with optional filters."""
        params = {"limit": limit}
        if project:
            params["project"] = project
        if test_type:
            params["test_type"] = test_type
        if status:
            params["status"] = status
        return self._get("/runs", params=params)

    def list_workers(self):
        """List registered workers."""
        return self._get("/workers")

    def register_worker(self, name, tags=None, concurrency=1):
        """Register a new worker (admin only). Returns {"id": ..., "token": ...}."""
        payload = {"name": name, "concurrency": concurrency}
        if tags:
            payload["tags"] = tags
        return self._post("/workers/register", payload)

    def create_token(self, name, role="readonly"):
        """Create an API token (admin only). Returns {"token": ...}."""
        return self._post("/tokens", {"name": name, "role": role})

    def list_tokens(self):
        """List API tokens (admin only)."""
        return self._get("/tokens")

    def _get(self, path, params=None):
        resp = self._session.get(f"{self.url}{path}", params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path, payload):
        resp = self._session.post(f"{self.url}{path}", json=payload, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def __repr__(self):
        return f"OppCiClient(url={self.url!r})"
