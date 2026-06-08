"""
Tests for the remote-CLI-control feature.

Three layers, mirroring the implementation:

1. REST endpoints — driven with FastAPI's TestClient against a fresh
   sqlite DB, asserting response shape and role enforcement.
2. OppCiClient methods — `_session.request` is mocked so each method's
   verb / URL / payload is checked without a live server.
3. The `@remoteable` dispatch shim — asserts the remote handler runs
   under --remote and the local body otherwise.

Run with: python -m unittest tests.test_remote_cli   (no pytest needed)

The DB url must be set before importing opp_ci.db, so this module pokes
os.environ at import time.
"""

import os
import tempfile
import unittest
from unittest import mock

# A throwaway on-disk sqlite DB, shared by every test in this process.
_DB_FD, _DB_PATH = tempfile.mkstemp(suffix=".db", prefix="opp_ci_test_")
os.close(_DB_FD)
os.environ["OPP_CI_DATABASE_URL"] = f"sqlite:///{_DB_PATH}"
os.environ.pop("OPP_CI_API_TOKEN", None)
os.environ["OPP_CI_REMOTE"] = "0"

from fastapi import FastAPI                                    # noqa: E402
from fastapi.testclient import TestClient                     # noqa: E402

from opp_ci.db.connection import engine, SessionLocal         # noqa: E402
from opp_ci.db.models import ApiToken, Base, Project          # noqa: E402
from opp_ci.web.api import router                             # noqa: E402


def _make_app():
    app = FastAPI()
    app.include_router(router)
    return app


def _mint_token(role, name=None):
    session = SessionLocal()
    try:
        tok = ApiToken(name=name or f"{role}-tok", role=role)
        session.add(tok)
        session.commit()
        return tok.token
    finally:
        session.close()


class RestEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        cls.client = TestClient(_make_app())
        cls.admin = _mint_token("admin")
        cls.submitter = _mint_token("submitter")
        cls.readonly = _mint_token("readonly")

    def _h(self, token):
        return {"Authorization": f"Bearer {token}"}

    # ── Projects ────────────────────────────────────────────────────

    def test_project_create_list_and_role_enforcement(self):
        # readonly cannot create
        r = self.client.post("/api/projects", json={"name": "p1"},
                             headers=self._h(self.readonly))
        self.assertEqual(r.status_code, 403)

        # submitter can
        r = self.client.post(
            "/api/projects",
            json={"name": "mm1k", "github": "levy/mm1k", "deps": ["omnetpp"]},
            headers=self._h(self.submitter),
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["name"], "mm1k")
        self.assertEqual(body["github"], "levy/mm1k")
        self.assertEqual(body["deps"], ["omnetpp"])

        # duplicate → 409
        r = self.client.post("/api/projects", json={"name": "mm1k"},
                             headers=self._h(self.submitter))
        self.assertEqual(r.status_code, 409)

        # bad github shape → 400
        r = self.client.post("/api/projects",
                             json={"name": "bad", "github": "noslash"},
                             headers=self._h(self.submitter))
        self.assertEqual(r.status_code, 400)

        # list (readonly ok)
        r = self.client.get("/api/projects", headers=self._h(self.readonly))
        self.assertEqual(r.status_code, 200)
        names = [p["name"] for p in r.json()]
        self.assertIn("mm1k", names)

    def test_versions(self):
        self.client.post("/api/projects", json={"name": "verproj"},
                         headers=self._h(self.submitter))
        r = self.client.post(
            "/api/projects/verproj/versions",
            json={"label": "v1.0", "git_ref": "main"},
            headers=self._h(self.submitter),
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["label"], "v1.0")

        r = self.client.get("/api/projects/verproj/versions",
                            headers=self._h(self.readonly))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()), 1)

        # unknown project → 404
        r = self.client.get("/api/projects/nope/versions",
                            headers=self._h(self.readonly))
        self.assertEqual(r.status_code, 404)

        # global /versions
        r = self.client.get("/api/versions", headers=self._h(self.readonly))
        self.assertEqual(r.status_code, 200)
        self.assertTrue(any(v["label"] == "v1.0" for v in r.json()))

    # ── Runs lifecycle (submit → list → get → delete) ────────────────

    def test_run_submit_get_delete(self):
        r = self.client.post("/api/runs",
                             json={"project": "mm1k", "kind": "smoke"},
                             headers=self._h(self.submitter))
        self.assertEqual(r.status_code, 200, r.text)
        run_id = r.json()["id"]

        r = self.client.get(f"/api/runs/{run_id}", headers=self._h(self.readonly))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["project"], "mm1k")

        # readonly cannot delete
        r = self.client.delete(f"/api/runs/{run_id}", headers=self._h(self.readonly))
        self.assertEqual(r.status_code, 403)

        # admin can
        r = self.client.delete(f"/api/runs/{run_id}", headers=self._h(self.admin))
        self.assertEqual(r.status_code, 204)

        # gone now
        r = self.client.get(f"/api/runs/{run_id}", headers=self._h(self.readonly))
        self.assertEqual(r.status_code, 404)

    def test_bulk_delete_guards(self):
        # seed two runs
        for _ in range(2):
            self.client.post("/api/runs",
                             json={"project": "mm1k", "kind": "build"},
                             headers=self._h(self.submitter))

        # missing confirm → 400
        r = self.client.delete("/api/runs?project=mm1k", headers=self._h(self.admin))
        self.assertEqual(r.status_code, 400)

        # confirm but no filter and no all → 400
        r = self.client.delete("/api/runs?confirm=true", headers=self._h(self.admin))
        self.assertEqual(r.status_code, 400)

        # filtered + confirm → deletes
        r = self.client.delete("/api/runs?kind=build&confirm=true",
                              headers=self._h(self.admin))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertGreaterEqual(r.json()["deleted"], 2)

    # ── Matrix create (config dict, Option A) ────────────────────────

    def test_create_matrix_from_config(self):
        # The CLI composes config via _build_matrix_config and posts it.
        from opp_ci.scheduler import _build_matrix_config
        config = _build_matrix_config(project="mm1k", kinds="smoke",
                                      modes="release,debug", compilers="gcc-14")
        r = self.client.post(
            "/api/matrices",
            json={"name": "m1", "project": "mm1k", "config": config},
            headers=self._h(self.submitter),
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["name"], "m1")
        self.assertGreaterEqual(r.json()["jobs_count"], 2)

        # duplicate → 409 (no server-side replace; matrices are immutable)
        r = self.client.post(
            "/api/matrices",
            json={"name": "m1", "project": "mm1k", "config": config},
            headers=self._h(self.submitter),
        )
        self.assertEqual(r.status_code, 409)

        # readonly cannot create
        r = self.client.post(
            "/api/matrices",
            json={"name": "m9", "project": "mm1k", "config": config},
            headers=self._h(self.readonly),
        )
        self.assertEqual(r.status_code, 403)

    # ── Seed (admin) ─────────────────────────────────────────────────

    def test_seed_endpoints(self):
        for path in ("projects", "platforms", "matrices"):
            r = self.client.post(f"/api/admin/seed/{path}",
                                headers=self._h(self.admin))
            self.assertEqual(r.status_code, 200, r.text)
            self.assertIn("inserted", " ".join(r.json().keys()) + " inserted")
        # readonly refused
        r = self.client.post("/api/admin/seed/projects",
                            headers=self._h(self.readonly))
        self.assertEqual(r.status_code, 403)

    # ── Tokens ───────────────────────────────────────────────────────

    def test_token_revoke(self):
        r = self.client.post("/api/tokens",
                             json={"name": "throwaway", "role": "readonly"},
                             headers=self._h(self.admin))
        self.assertEqual(r.status_code, 200, r.text)
        tid = r.json()["id"]

        r = self.client.delete(f"/api/tokens/{tid}", headers=self._h(self.admin))
        self.assertEqual(r.status_code, 204)

        # revoked token no longer authenticates
        revoked = r  # noqa
        listing = self.client.get("/api/tokens", headers=self._h(self.admin)).json()
        match = [t for t in listing if t["id"] == tid][0]
        self.assertFalse(match["enabled"])

        # 404 on unknown
        r = self.client.delete("/api/tokens/999999", headers=self._h(self.admin))
        self.assertEqual(r.status_code, 404)

    # ── Users ────────────────────────────────────────────────────────

    def test_user_crud(self):
        r = self.client.post(
            "/api/users",
            json={"username": "alice", "password": "secret123", "role": "admin"},
            headers=self._h(self.admin),
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["username"], "alice")
        self.assertEqual(r.json()["role"], "admin")

        # password is not echoed back
        self.assertNotIn("password", r.json())

        # duplicate without update_password → 409
        r = self.client.post(
            "/api/users",
            json={"username": "alice", "password": "x", "role": "admin"},
            headers=self._h(self.admin),
        )
        self.assertEqual(r.status_code, 409)

        # list
        r = self.client.get("/api/users", headers=self._h(self.admin))
        self.assertTrue(any(u["username"] == "alice" for u in r.json()))

        # disable via PATCH
        r = self.client.patch("/api/users/alice", json={"enabled": False},
                             headers=self._h(self.admin))
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["enabled"])

        # invalid role → 400
        r = self.client.post(
            "/api/users",
            json={"username": "bob", "password": "x", "role": "superuser"},
            headers=self._h(self.admin),
        )
        self.assertEqual(r.status_code, 400)

    # ── Rules ────────────────────────────────────────────────────────

    def test_rule_create_list_delete(self):
        self.client.post("/api/projects",
                         json={"name": "ruleproj", "github": "org/ruleproj"},
                         headers=self._h(self.submitter))
        r = self.client.post(
            "/api/github/rules",
            json={"project_name": "ruleproj", "rule_type": "tag", "pattern": "*"},
            headers=self._h(self.admin),
        )
        self.assertEqual(r.status_code, 200, r.text)
        rid = r.json()["id"]

        r = self.client.get("/api/github/rules", headers=self._h(self.readonly))
        self.assertTrue(any(x["id"] == rid for x in r.json()))

        r = self.client.delete(f"/api/github/rules/{rid}", headers=self._h(self.admin))
        self.assertEqual(r.status_code, 200)


class ClientMethodTests(unittest.TestCase):
    """Each OppCiClient method hits the right verb/URL/payload."""

    def setUp(self):
        from opp_ci.client import OppCiClient
        self.c = OppCiClient(url="https://ci.example/api", token="t")

    def _patch(self, status=200, json_body=None, content=b"{}"):
        resp = mock.Mock()
        resp.ok = 200 <= status < 300
        resp.status_code = status
        resp.content = content
        resp.json.return_value = json_body if json_body is not None else {}
        resp.text = ""
        return mock.patch.object(self.c._session, "request", return_value=resp)

    def test_get_and_post_shapes(self):
        with self._patch(json_body=[{"id": 1}]) as req:
            self.c.list_projects()
            req.assert_called_once()
            args, kwargs = req.call_args
            self.assertEqual(args[0], "GET")
            self.assertEqual(args[1], "https://ci.example/api/projects")

        with self._patch(json_body={"id": 5}) as req:
            self.c.add_project("p", github="o/r", deps=["omnetpp"])
            args, kwargs = req.call_args
            self.assertEqual(args[0], "POST")
            self.assertEqual(args[1], "https://ci.example/api/projects")
            self.assertEqual(kwargs["json"]["github"], "o/r")
            self.assertEqual(kwargs["json"]["deps"], ["omnetpp"])

    def test_delete_and_patch_verbs(self):
        with self._patch(status=204, content=b"") as req:
            self.assertIsNone(self.c.delete_run(7))
            self.assertEqual(req.call_args[0][0], "DELETE")
            self.assertEqual(req.call_args[0][1], "https://ci.example/api/runs/7")

        with self._patch(json_body={"username": "alice"}) as req:
            self.c.update_user("alice", enabled=False, role="submitter")
            self.assertEqual(req.call_args[0][0], "PATCH")
            self.assertEqual(req.call_args[0][1],
                             "https://ci.example/api/users/alice")
            self.assertEqual(req.call_args[1]["json"],
                             {"enabled": False, "role": "submitter"})

    def test_bulk_delete_passes_confirm(self):
        with self._patch(json_body={"deleted": 3}) as req:
            out = self.c.delete_runs(project="mm1k", confirm=True)
            self.assertEqual(out["deleted"], 3)
            params = req.call_args[1]["params"]
            self.assertEqual(params["project"], "mm1k")
            self.assertEqual(params["confirm"], "true")

    def test_error_surface(self):
        from opp_ci.client import OppCiClientError
        resp = mock.Mock()
        resp.ok = False
        resp.status_code = 403
        resp.reason = "Forbidden"
        resp.content = b'{"detail":"Requires role \'admin\'"}'
        resp.text = '{"detail":"Requires role \'admin\'"}'
        resp.json.return_value = {"detail": "Requires role 'admin'"}
        with mock.patch.object(self.c._session, "request", return_value=resp):
            with self.assertRaises(OppCiClientError) as ctx:
                self.c.list_users()
            self.assertEqual(ctx.exception.status_code, 403)
            self.assertIn("admin", ctx.exception.detail)

    def test_transport_error_surface(self):
        import requests
        from opp_ci.client import OppCiClientError
        with mock.patch.object(self.c._session, "request",
                               side_effect=requests.ConnectionError("boom")):
            with self.assertRaises(OppCiClientError) as ctx:
                self.c.list_runs()
            self.assertIn("boom", ctx.exception.detail)
            self.assertIsNone(ctx.exception.status_code)


class DispatchShimTests(unittest.TestCase):
    """@remoteable runs the remote handler iff --remote is set."""

    def _build(self):
        import click
        from opp_ci.cli import remoteable
        calls = []

        def handler(**kwargs):
            calls.append(("remote", kwargs))

        @click.command()
        @click.option("--x", default="0")
        @remoteable(handler)
        def cmd(x):
            calls.append(("local", {"x": x}))

        return cmd, calls

    def test_local_path(self):
        from click.testing import CliRunner
        cmd, calls = self._build()
        # ctx.obj must exist; emulate the group setting obj["remote"].
        res = CliRunner().invoke(cmd, ["--x", "5"], obj={"remote": False})
        self.assertEqual(res.exit_code, 0, res.output)
        self.assertEqual(calls, [("local", {"x": "5"})])

    def test_remote_path(self):
        from click.testing import CliRunner
        cmd, calls = self._build()
        res = CliRunner().invoke(cmd, ["--x", "5"], obj={"remote": True})
        self.assertEqual(res.exit_code, 0, res.output)
        self.assertEqual(calls, [("remote", {"x": "5"})])


class CliRemoteHandlerTests(unittest.TestCase):
    """`opp_ci --remote <cmd>` dispatches to the client and formats output."""

    def _run(self, fake_client, argv):
        from click.testing import CliRunner
        from opp_ci.cli import main
        # config.API_TOKEN / COORDINATOR_URL bind at import, so patch them
        # directly rather than via env (which is read once at startup).
        with mock.patch("opp_ci.config.API_TOKEN", "t"), \
             mock.patch("opp_ci.config.COORDINATOR_URL", "https://x/api"), \
             mock.patch("opp_ci.client.OppCiClient", return_value=fake_client):
            return CliRunner().invoke(main, ["--remote"] + argv)

    def test_list_runs_remote_formats_table(self):
        fake = mock.Mock()
        fake.list_runs.return_value = [
            {"id": 12, "project": "mm1k", "git_ref": "main", "kind": "smoke",
             "lifecycle": "finished", "result_code": "PASS",
             "duration_seconds": 3.2, "started_at": "2026-01-02T10:00:00"},
        ]
        res = self._run(fake, ["list-runs", "--project", "mm1k"])
        self.assertEqual(res.exit_code, 0, res.output)
        self.assertIn("mm1k", res.output)
        self.assertIn("PASS", res.output)
        fake.list_runs.assert_called_once()

    def test_delete_run_remote_confirm_skip(self):
        fake = mock.Mock()
        fake.delete_run.return_value = None
        res = self._run(fake, ["delete-run", "9", "--yes"])
        self.assertEqual(res.exit_code, 0, res.output)
        fake.delete_run.assert_called_once_with(9)
        self.assertIn("deleted", res.output)

    def test_create_matrix_remote_composes_config(self):
        fake = mock.Mock()
        fake.create_matrix.return_value = {"name": "lab", "jobs_count": 2}
        res = self._run(fake, ["create-matrix", "--name", "lab",
                               "--project", "mm1k", "--kinds", "smoke",
                               "--builds", "release,debug"])
        self.assertEqual(res.exit_code, 0, res.output)
        fake.create_matrix.assert_called_once()
        args, kwargs = fake.create_matrix.call_args
        # positional: (name, project, config)
        self.assertEqual(args[0], "lab")
        self.assertEqual(args[1], "mm1k")
        self.assertEqual(args[2]["kinds"], ["smoke"])
        self.assertEqual(args[2]["modes"], ["release", "debug"])

    def test_create_matrix_remote_replace_unsupported(self):
        from opp_ci.client import OppCiClientError
        fake = mock.Mock()
        fake.create_matrix.side_effect = OppCiClientError("exists", status_code=409)
        res = self._run(fake, ["create-matrix", "--name", "lab",
                               "--project", "mm1k", "--kinds", "smoke",
                               "--replace"])
        self.assertEqual(res.exit_code, 1)
        self.assertIn("--replace is not supported", res.output)

    def test_run_matrix_remote_requires_named_matrix(self):
        fake = mock.Mock()
        res = self._run(fake, ["run-matrix", "--project", "mm1k",
                               "--kinds", "smoke"])
        self.assertEqual(res.exit_code, 1)
        self.assertIn("requires --matrix", res.output)


class StatusFilterTests(unittest.TestCase):
    """Unit-cover the shared persistence.status_filter helper directly."""

    def test_lifecycle_value_filters(self):
        from sqlalchemy import select
        from opp_ci.db.models import TestRun
        from opp_ci.persistence import status_filter
        q = select(TestRun)
        out = status_filter(q, "queued")
        self.assertIsNot(out, q)  # a WHERE clause was appended

    def test_result_code_value_filters(self):
        from sqlalchemy import select
        from opp_ci.db.models import TestRun
        from opp_ci.persistence import status_filter
        q = select(TestRun)
        out = status_filter(q, "PASS")
        self.assertIsNot(out, q)

    def test_bad_value_raises_valueerror(self):
        from sqlalchemy import select
        from opp_ci.db.models import TestRun
        from opp_ci.persistence import status_filter
        with self.assertRaises(ValueError):
            status_filter(select(TestRun), "bogus")


def tearDownModule():
    try:
        os.unlink(_DB_PATH)
    except OSError:
        pass


if __name__ == "__main__":
    unittest.main()
