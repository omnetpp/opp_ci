"""Tests for "run by name or anonymously" (plan/pending/run-by-name-or-anonymous-web-ui.md).

Covers:
  * persistence helpers — naming, collisions, anonymous matrices
  * REST `/api/runs` — name on first run, run-by-name, collisions
  * REST `/api/matrix-runs` — named vs anonymous (NULL name) inline spec

Run with: python -m unittest tests.test_run_by_name   (no pytest needed)
"""

import os
import tempfile
import unittest

_DB_FD, _DB_PATH = tempfile.mkstemp(suffix=".db", prefix="opp_ci_rbn_")
os.close(_DB_FD)
os.environ["OPP_CI_DATABASE_URL"] = f"sqlite:///{_DB_PATH}"
os.environ.pop("OPP_CI_API_TOKEN", None)
os.environ["OPP_CI_REMOTE"] = "0"

from fastapi import FastAPI                                    # noqa: E402
from fastapi.testclient import TestClient                     # noqa: E402

from opp_ci.db.connection import engine, SessionLocal         # noqa: E402
from opp_ci.db.models import ApiToken, Base, TestMatrix       # noqa: E402
from opp_ci.persistence import (                              # noqa: E402
    create_matrix_from_axes, get_matrix_by_name, get_or_create_test,
    get_test_by_name, set_matrix_name, set_test_name,
)
from opp_ci.web.api import router                             # noqa: E402


def _coord(**over):
    base = {"project": "inet", "kind": "smoke", "mode": None, "os": "Linux",
            "os_version": None, "distro": None, "distro_version": None,
            "flavor": None, "flavor_version": None, "arch": None,
            "compiler": None, "compiler_version": None, "isolation": "none",
            "toolchain": "none", "opp_file": None}
    base.update(over)
    return base


class PersistenceNamingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)

    def test_set_test_name_and_lookup(self):
        s = SessionLocal()
        try:
            t = get_or_create_test(s, _coord(kind="smoke"))
            set_test_name(s, t, "my-smoke")
            s.commit()
            self.assertEqual(get_test_by_name(s, "my-smoke").id, t.id)
            # blank clears
            set_test_name(s, t, "")
            s.commit()
            self.assertIsNone(t.name)
            self.assertIsNone(get_test_by_name(s, "my-smoke"))
        finally:
            s.close()

    def test_test_name_collision(self):
        s = SessionLocal()
        try:
            a = get_or_create_test(s, _coord(kind="fingerprint"))
            b = get_or_create_test(s, _coord(kind="statistical"))
            set_test_name(s, a, "dup")
            s.commit()
            with self.assertRaises(ValueError):
                set_test_name(s, b, "dup")
            s.rollback()
        finally:
            s.close()

    def test_dedup_preserves_name(self):
        s = SessionLocal()
        try:
            t1 = get_or_create_test(s, _coord(kind="build"))
            set_test_name(s, t1, "keep")
            s.commit()
            # same coordinate → same row, name preserved
            t2 = get_or_create_test(s, _coord(kind="build"))
            self.assertEqual(t1.id, t2.id)
            self.assertEqual(t2.name, "keep")
        finally:
            s.close()

    def test_anonymous_matrix_has_null_name(self):
        s = SessionLocal()
        try:
            m1 = create_matrix_from_axes(s, project="inet", config={"kinds": ["smoke"]})
            m2 = create_matrix_from_axes(s, project="inet", config={"kinds": ["build"]})
            s.commit()
            # multiple NULL names allowed; display_name never blank
            self.assertIsNone(m1.name)
            self.assertIsNone(m2.name)
            self.assertEqual(m1.display_name, f"(anonymous #{m1.id})")
        finally:
            s.close()

    def test_named_matrix_collision(self):
        s = SessionLocal()
        try:
            create_matrix_from_axes(s, project="inet", config={"kinds": ["smoke"]}, name="nm")
            s.commit()
            with self.assertRaises(ValueError):
                create_matrix_from_axes(s, project="inet", config={"kinds": ["x"]}, name="nm")
            s.rollback()
            self.assertIsNotNone(get_matrix_by_name(s, "nm"))
        finally:
            s.close()

    def test_set_matrix_name_clears(self):
        s = SessionLocal()
        try:
            m = create_matrix_from_axes(s, project="inet", config={"kinds": ["smoke"]}, name="tmp")
            s.commit()
            set_matrix_name(s, m, "")
            s.commit()
            self.assertIsNone(m.name)
        finally:
            s.close()


def _make_app():
    app = FastAPI()
    app.include_router(router)
    return app


def _mint(role):
    s = SessionLocal()
    try:
        tok = ApiToken(name=f"{role}-rbn", role=role)
        s.add(tok)
        s.commit()
        return tok.token
    finally:
        s.close()


class RestRunByNameTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        cls.client = TestClient(_make_app())
        cls.sub = _mint("submitter")

    def _h(self):
        return {"Authorization": f"Bearer {self.sub}"}

    # Environment dimensions every submit now requires (strict specification).
    _ENV = {
        "mode": "release", "os": "Linux",
        "distro": "Ubuntu", "distro_version": "24.04",
        "arch": "amd64", "compiler": "gcc", "compiler_version": "14",
    }

    def test_submit_run_with_name_then_run_by_name(self):
        r = self.client.post("/api/runs", headers=self._h(), json={
            "project": "inet", "kind": "smoke", "name": "rest-smoke", **self._ENV,
        })
        self.assertEqual(r.status_code, 200, r.text)

        # run by name — no coordinate needed
        r2 = self.client.post("/api/runs", headers=self._h(), json={
            "test_name": "rest-smoke",
        })
        self.assertEqual(r2.status_code, 200, r2.text)
        self.assertIn("id", r2.json())

        # unknown name → 404
        r3 = self.client.post("/api/runs", headers=self._h(), json={"test_name": "nope"})
        self.assertEqual(r3.status_code, 404)

        # missing project/kind and no test_name → 400
        r4 = self.client.post("/api/runs", headers=self._h(), json={"os": "Linux"})
        self.assertEqual(r4.status_code, 400)

    def test_submit_run_name_collision_409(self):
        self.client.post("/api/runs", headers=self._h(), json={
            "project": "inet", "kind": "build", "name": "taken", **self._ENV,
        })
        # different coordinate, same name → 409
        r = self.client.post("/api/runs", headers=self._h(), json={
            "project": "inet", "kind": "feature", "name": "taken", **self._ENV,
        })
        self.assertEqual(r.status_code, 409, r.text)

    def test_inline_matrix_run_anonymous_and_named(self):
        # anonymous inline spec → matrix row with NULL name
        r = self.client.post("/api/matrix-runs", headers=self._h(), json={
            "project": "inet", "kinds": ["smoke"], "modes": ["release"],
            "distro": ["Ubuntu 24.04"], "arch": ["amd64"], "compiler": ["gcc-14"],
        })
        self.assertEqual(r.status_code, 200, r.text)
        mr_id = r.json()["matrix_run_id"]

        s = SessionLocal()
        try:
            from opp_ci.db.models import TestMatrixRun
            mr = s.get(TestMatrixRun, mr_id)
            matrix = s.get(TestMatrix, mr.matrix_id)
            self.assertIsNone(matrix.name)  # anonymous
        finally:
            s.close()

        # named inline spec → reusable
        r2 = self.client.post("/api/matrix-runs", headers=self._h(), json={
            "project": "inet", "kinds": ["smoke"], "name": "rest-matrix",
            "modes": ["release"], "distro": ["Ubuntu 24.04"], "arch": ["amd64"],
            "compiler": ["gcc-14"],
        })
        self.assertEqual(r2.status_code, 200, r2.text)
        s = SessionLocal()
        try:
            self.assertIsNotNone(get_matrix_by_name(s, "rest-matrix"))
        finally:
            s.close()


if __name__ == "__main__":
    unittest.main()
