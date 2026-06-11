"""Tests for persistence.validate_test_coord — strict test-coordinate
specification.

A Test identity must fully specify its execution environment so that all runs
sharing the identity are comparable. validate_test_coord enforces this at
submit time. These are pure-function tests (no DB).

Run with: python -m unittest tests.test_coord_validation
"""

import os
import unittest

os.environ.setdefault("OPP_CI_REMOTE", "0")

from opp_ci.persistence import validate_test_coord            # noqa: E402


def _coord(**over):
    """A fully-specified Linux coord; override fields per test."""
    base = {
        "project": "mm1k", "kind": "smoke", "mode": "release",
        "os": "Linux", "os_version": None,
        "distro": "ubuntu", "distro_version": "24.04",
        "flavor": None, "flavor_version": None,
        "arch": "amd64", "compiler": "gcc", "compiler_version": "14",
        "isolation": "none", "toolchain": "none", "opp_file": None,
    }
    base.update(over)
    return base


class CoordValidationTests(unittest.TestCase):
    def test_full_linux_coord_ok(self):
        validate_test_coord(_coord())  # must not raise

    def test_full_windows_coord_ok(self):
        validate_test_coord(_coord(
            os="Windows", os_version="11", distro=None, distro_version=None))

    def test_full_flavored_linux_coord_ok(self):
        # flavor with the version coming from distro_version is complete.
        validate_test_coord(_coord(flavor="kubuntu"))

    def test_missing_arch_rejected(self):
        with self.assertRaises(ValueError) as cm:
            validate_test_coord(_coord(arch=None))
        self.assertIn("arch", str(cm.exception))

    def test_missing_mode_rejected(self):
        with self.assertRaises(ValueError) as cm:
            validate_test_coord(_coord(mode=None))
        self.assertIn("mode", str(cm.exception))

    def test_missing_compiler_version_rejected(self):
        with self.assertRaises(ValueError) as cm:
            validate_test_coord(_coord(compiler_version=None))
        self.assertIn("compiler_version", str(cm.exception))

    def test_linux_distro_without_version_rejected(self):
        # The original run #32 case.
        with self.assertRaises(ValueError) as cm:
            validate_test_coord(_coord(distro_version=None))
        self.assertIn("distro_version", str(cm.exception))

    def test_linux_without_distro_rejected(self):
        with self.assertRaises(ValueError) as cm:
            validate_test_coord(_coord(distro=None, distro_version=None))
        self.assertIn("distro", str(cm.exception))

    def test_linux_os_version_rejected(self):
        with self.assertRaises(ValueError) as cm:
            validate_test_coord(_coord(os_version="24.04"))
        self.assertIn("os_version", str(cm.exception))

    def test_windows_without_os_version_rejected(self):
        with self.assertRaises(ValueError) as cm:
            validate_test_coord(_coord(
                os="Windows", os_version=None, distro=None, distro_version=None))
        self.assertIn("os_version", str(cm.exception))

    def test_distro_on_windows_rejected(self):
        with self.assertRaises(ValueError) as cm:
            validate_test_coord(_coord(os="Windows", os_version="11"))
        self.assertIn("only valid when os=Linux", str(cm.exception))

    def test_missing_project_and_kind_rejected(self):
        with self.assertRaises(ValueError) as cm:
            validate_test_coord(_coord(project=None, kind=None))
        msg = str(cm.exception)
        self.assertIn("project", msg)
        self.assertIn("kind", msg)


if __name__ == "__main__":
    unittest.main()
