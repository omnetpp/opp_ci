"""
Python client for the opp_ci REST API.

Allows remote job submission, result querying, and full coordinator
control (projects, versions, matrices, workers, tokens, users, rules)
from any machine. Every `opp_ci --remote <command>` is one-to-one with
a method here.

Usage:
    from opp_ci.client import OppCiClient

    ci = OppCiClient(url="https://ci.omnetpp.org/api", token="...")
    run = ci.submit_run(project="inet", kind="smoke")
    ci.get_run(run["id"])
    results = ci.list_runs(project="inet", status="FAIL")

All methods raise `OppCiClientError` on failure, with a tidy `.detail`
(the server's `detail:` field on a 4xx/5xx, or the transport error
message) so callers can print `ERROR: {e.detail}` without leaking a
traceback.
"""

import logging

import requests

_logger = logging.getLogger(__name__)


class OppCiClientError(Exception):
    """A remote API call failed.

    `.detail` carries a human-readable message: the server-side
    `detail:` field on a 4xx/5xx JSON body when present, otherwise the
    underlying transport error message. `.status_code` is the HTTP
    status when the failure came from a response (None for connection
    errors / timeouts).
    """

    def __init__(self, detail, *, status_code=None):
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


def _quiet_urllib3_debug():
    """Keep urllib3 at INFO even when our top-level logger is DEBUG.

    `opp_ci --verbose --remote …` flips the root logger to DEBUG, which
    would make urllib3 echo every request — including the
    `Authorization: Bearer …` header — into shell scrollback. Opt back
    in explicitly with OPP_CI_HTTP_DEBUG=1.
    """
    import os
    if os.environ.get("OPP_CI_HTTP_DEBUG", "0") == "1":
        return
    logging.getLogger("urllib3").setLevel(logging.INFO)


class OppCiClient:
    """Client for the opp_ci coordinator REST API."""

    def __init__(self, url, token, *, verify=None, insecure=None):
        """
        Args:
            url: Coordinator API base URL (e.g. "https://ci.omnetpp.org/api")
            token: API token (submitter, admin, or readonly)
            verify: Path to a CA bundle PEM (e.g. Cloudflare's Origin CA
                root) used to verify the coordinator's certificate. If
                None, falls back to `$OPP_CI_TLS_CA_BUNDLE`, otherwise
                the system CA store.
            insecure: If True, skip TLS verification entirely. Dev only.
                If None, falls back to `$OPP_CI_TLS_INSECURE`.
        """
        from opp_ci.http import configure_session
        _quiet_urllib3_debug()
        self.url = url.rstrip("/")
        self.token = token
        self._session = requests.Session()
        self._session.headers["Authorization"] = f"Bearer {token}"
        configure_session(self._session, ca_bundle=verify, insecure=insecure)

    # ── Runs ────────────────────────────────────────────────────────────

    def submit_run(self, project, kind, mode=None, git_ref=None,
                   os=None, os_version=None,
                   distro=None, distro_version=None,
                   flavor=None, flavor_version=None,
                   arch=None,
                   compiler=None, compiler_version=None,
                   isolation=None, toolchain=None, pins=None,
                   expected_result_code=None):
        """Submit a single test run. Returns {"id": ..., "lifecycle": "queued"}.

        ``pins`` is a list of ``"dep=version"`` strings (e.g.
        ``["omnetpp=6.4.0"]``); the coordinator resolves them into the run's
        ``resolved_deps`` (required for ``isolation=podman`` image selection).
        ``expected_result_code`` (PASS/FAIL/ERROR) overrides the global default
        expectation stamped on a freshly-created Test.
        """
        payload = {"project": project, "kind": kind}
        for key, value in (
            ("mode", mode), ("git_ref", git_ref),
            ("os", os), ("os_version", os_version),
            ("distro", distro), ("distro_version", distro_version),
            ("flavor", flavor), ("flavor_version", flavor_version),
            ("arch", arch),
            ("compiler", compiler), ("compiler_version", compiler_version),
            ("isolation", isolation), ("toolchain", toolchain),
            ("expected_result_code", expected_result_code),
        ):
            if value:
                payload[key] = value
        if pins:
            payload["pins"] = list(pins)
        return self._post("/runs", payload)

    def submit_matrix(self, matrix_name):
        """Submit all jobs from a named matrix. Returns {"matrix_name": ..., "jobs_queued": ..., "run_ids": [...]}."""
        return self._post("/runs/matrix", {"matrix_name": matrix_name})

    def get_run(self, run_id):
        """Get full details of a run including results."""
        return self._get(f"/runs/{run_id}")

    def list_runs(self, *, limit=50, **filters):
        """List test runs with optional filters.

        Accepts any GET /runs query param as a keyword: ``project``, ``kind``,
        ``status`` (the lifecycle ∪ outcome union — e.g. ``"FAIL"`` or
        ``"queued"``), ``lifecycle`` and ``result_code`` (strict
        single-column filters), ``os``, ``distro``, ``flavor``. Falsy values
        are dropped; forwarded as query params as-is.
        """
        params = {"limit": limit}
        params.update({key: value for key, value in filters.items() if value})
        return self._get("/runs", params=params)

    # ── Projects / versions ─────────────────────────────────────────────

    def list_projects(self):
        """List known projects (readonly)."""
        return self._get("/projects")

    def add_project(self, name, *, github=None, git_url=None,
                    opp_env_name=None, deps=None):
        """Create a project (submitter). `deps` is a list of dependency names."""
        payload = {"name": name}
        for key, value in (
            ("github", github), ("git_url", git_url),
            ("opp_env_name", opp_env_name),
        ):
            if value:
                payload[key] = value
        if deps is not None:
            payload["deps"] = deps
        return self._post("/projects", payload)

    def sync_catalog(self):
        """Sync the project catalog from opp_env server-side (admin).

        Can take 30+ seconds; uses an extended timeout.
        """
        return self._post("/projects/sync-catalog", {}, timeout=120)

    def list_versions(self, project=None):
        """List registered versions, optionally scoped to one project (readonly)."""
        if project:
            return self._get(f"/projects/{project}/versions")
        return self._get("/versions")

    def add_version(self, project, label, *, git_ref=None,
                    opp_env_version=None, deps=None):
        """Register a version for a project (submitter)."""
        payload = {"label": label}
        for key, value in (
            ("git_ref", git_ref), ("opp_env_version", opp_env_version),
        ):
            if value:
                payload[key] = value
        if deps is not None:
            payload["deps"] = deps
        return self._post(f"/projects/{project}/versions", payload)

    # ── Matrices ────────────────────────────────────────────────────────

    def list_matrices(self):
        """List defined matrices (readonly)."""
        return self._get("/matrices")

    def create_matrix(self, name, project, config, *, opp_file=None,
                      ref_range=None):
        """Create a matrix from an already-composed config dict (submitter)."""
        payload = {"name": name, "project": project, "config": config}
        if opp_file:
            payload["opp_file"] = opp_file
        if ref_range:
            payload["ref_range"] = ref_range
        return self._post("/matrices", payload)

    def run_matrix(self, matrix_name):
        """Queue all jobs from a named matrix (submitter). Alias for submit_matrix."""
        return self._post("/runs/matrix", {"matrix_name": matrix_name})

    # ── Seed (admin) ────────────────────────────────────────────────────

    def seed_projects(self):
        return self._post("/admin/seed/projects", {})

    def seed_platforms(self):
        return self._post("/admin/seed/platforms", {})

    def seed_matrices(self):
        return self._post("/admin/seed/matrices", {})

    # ── Workers ─────────────────────────────────────────────────────────

    def list_workers(self):
        """List registered workers."""
        return self._get("/workers")

    def register_worker(self, name, tags=None, concurrency=1):
        """Register a new worker (admin only). Returns {"id": ..., "token": ...}."""
        payload = {"name": name, "concurrency": concurrency}
        if tags:
            payload["tags"] = tags
        return self._post("/workers/register", payload)

    # ── Tokens ──────────────────────────────────────────────────────────

    def create_token(self, name, role="readonly"):
        """Create an API token (admin only). Returns {"token": ...}."""
        return self._post("/tokens", {"name": name, "role": role})

    def list_tokens(self):
        """List API tokens (admin only)."""
        return self._get("/tokens")

    def revoke_token(self, token_id):
        """Disable an API token by id (admin). Returns None on success."""
        return self._delete(f"/tokens/{token_id}")

    # ── Users (admin) ───────────────────────────────────────────────────

    def create_user(self, username, password, role="admin",
                    update_password=False):
        """Create (or update) a local-login user (admin)."""
        return self._post("/users", {
            "username": username,
            "password": password,
            "role": role,
            "update_password": update_password,
        })

    def list_users(self):
        """List web UI users (admin)."""
        return self._get("/users")

    def update_user(self, username, *, enabled=None, role=None, password=None):
        """Patch a user's enabled/role/password (admin)."""
        payload = {}
        if enabled is not None:
            payload["enabled"] = enabled
        if role is not None:
            payload["role"] = role
        if password is not None:
            payload["password"] = password
        return self._patch(f"/users/{username}", payload)

    # ── GitHub rules ────────────────────────────────────────────────────

    def list_rules(self):
        """List auto-test rules (readonly)."""
        return self._get("/github/rules")

    def create_rule(self, project, rule_type, pattern, *,
                    matrix_name=None, enabled=True):
        """Create an auto-test rule (admin)."""
        payload = {
            "project_name": project,
            "rule_type": rule_type,
            "pattern": pattern,
            "enabled": enabled,
        }
        if matrix_name:
            payload["matrix_name"] = matrix_name
        return self._post("/github/rules", payload)

    def delete_rule(self, rule_id):
        """Delete an auto-test rule by id (admin)."""
        return self._delete(f"/github/rules/{rule_id}")

    def test_webhook(self, project, ref, event_type, *, sha=None,
                     pr_number=None):
        """Drive the webhook handler with a synthesized payload (admin)."""
        payload = {"project": project, "ref": ref, "event_type": event_type}
        if sha:
            payload["sha"] = sha
        if pr_number is not None:
            payload["pr_number"] = pr_number
        return self._post("/github/rules/test-webhook", payload)

    # ── HTTP verb helpers ───────────────────────────────────────────────

    def _get(self, path, params=None, timeout=30):
        return self._request("GET", path, params=params, timeout=timeout)

    def _post(self, path, payload, timeout=30):
        return self._request("POST", path, json=payload, timeout=timeout)

    def _delete(self, path, params=None, timeout=30):
        return self._request("DELETE", path, params=params, timeout=timeout)

    def _patch(self, path, payload, timeout=30):
        return self._request("PATCH", path, json=payload, timeout=timeout)

    def _request(self, method, path, *, params=None, json=None, timeout=30):
        url = f"{self.url}{path}"
        try:
            resp = self._session.request(
                method, url, params=params, json=json, timeout=timeout)
        except requests.RequestException as e:
            raise OppCiClientError(str(e)) from e
        if not resp.ok:
            raise OppCiClientError(_detail_from(resp), status_code=resp.status_code)
        if resp.status_code == 204 or not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return resp.text

    def __repr__(self):
        return f"OppCiClient(url={self.url!r})"


def _detail_from(resp):
    """Pull a tidy error message out of a non-2xx response.

    Prefers the FastAPI `{"detail": ...}` field; falls back to the raw
    body, then to a generic status line.
    """
    try:
        body = resp.json()
    except ValueError:
        body = None
    if isinstance(body, dict) and "detail" in body:
        detail = body["detail"]
        # FastAPI validation errors put a list under detail.
        if isinstance(detail, list):
            return "; ".join(
                str(d.get("msg", d)) if isinstance(d, dict) else str(d)
                for d in detail
            )
        return str(detail)
    text = (resp.text or "").strip()
    if text:
        return text
    return f"HTTP {resp.status_code} {resp.reason}"
