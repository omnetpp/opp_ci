"""Tests for resolving loose coordinate axes against the worker fleet
(Phase 4 of plan/pending/repeatable-tests-and-moving-target-matrices.md).

Pure resolver: a loose axis is pinned to a fleet-advertised value by the
per-axis preference order, deterministically; an axis the fleet can't satisfy
is rejected. compiler family prefers clang→gcc→msvc and the newest version
within the family; arch prefers amd64→aarch64; mode defaults release.

Run with: python -m pytest tests/test_fleet_resolve.py
"""

import unittest

from opp_ci.fleet import (
    candidate_axes, resolve_loose_axes, _version_key, _newest_version,
)


class TagParsingTests(unittest.TestCase):
    def test_parse_compiler_and_arch(self):
        tags = {"compiler:gcc-14", "compiler:clang-18", "arch:amd64",
                "podman", "distro:ubuntu-24.04"}
        cand = candidate_axes(tags)
        self.assertEqual(cand["compiler"], {("gcc", "14"), ("clang", "18")})
        self.assertEqual(cand["arch"], {"amd64"})

    def test_compiler_without_version(self):
        self.assertEqual(candidate_axes({"compiler:clang"})["compiler"],
                         {("clang", None)})

    def test_version_key_orders_numerically(self):
        self.assertGreater(_version_key("14"), _version_key("9"))
        self.assertGreater(_version_key("24.04"), _version_key("6.1"))
        self.assertEqual(_version_key(None), (0, ()))

    def test_newest_version_within_family(self):
        cand = {("gcc", "13"), ("gcc", "14"), ("clang", "18")}
        self.assertEqual(_newest_version(cand, "gcc"), "14")
        self.assertEqual(_newest_version(cand, "clang"), "18")
        self.assertIsNone(_newest_version({("gcc", None)}, "gcc"))


def _loose(**over):
    # A specified platform by default, so these cases stay focused on
    # compiler/arch/mode; platform resolution has its own test class.
    c = {"compiler": None, "compiler_version": None, "arch": None, "mode": None,
         "os": "Linux", "distro": "ubuntu", "distro_version": "24.04"}
    c.update(over)
    return c


class ResolveTests(unittest.TestCase):
    FLEET = {"compiler:gcc-14", "compiler:gcc-13", "compiler:clang-18",
             "arch:amd64", "arch:aarch64"}

    def test_compiler_family_preference_clang_first(self):
        c = resolve_loose_axes(_loose(), self.FLEET)
        self.assertEqual(c["compiler"], "clang")
        self.assertEqual(c["compiler_version"], "18")

    def test_compiler_falls_back_to_gcc_when_no_clang(self):
        c = resolve_loose_axes(_loose(), {"compiler:gcc-14", "compiler:gcc-13",
                                          "arch:amd64"})
        self.assertEqual(c["compiler"], "gcc")
        self.assertEqual(c["compiler_version"], "14")  # newest

    def test_arch_preference_amd64_first(self):
        c = resolve_loose_axes(_loose(), self.FLEET)
        self.assertEqual(c["arch"], "amd64")

    def test_arch_falls_back_when_only_aarch64(self):
        c = resolve_loose_axes(_loose(compiler="gcc", compiler_version="14"),
                               {"arch:aarch64"})
        self.assertEqual(c["arch"], "aarch64")

    def test_mode_defaults_release(self):
        self.assertEqual(resolve_loose_axes(_loose(), self.FLEET)["mode"],
                         "release")

    def test_specified_axes_untouched(self):
        c = resolve_loose_axes(
            _loose(compiler="gcc", compiler_version="11", arch="aarch64",
                   mode="debug"),
            self.FLEET)
        self.assertEqual((c["compiler"], c["compiler_version"], c["arch"],
                          c["mode"]), ("gcc", "11", "aarch64", "debug"))

    def test_fill_version_for_specified_family(self):
        c = resolve_loose_axes(_loose(compiler="gcc"), self.FLEET)
        self.assertEqual(c["compiler_version"], "14")  # newest gcc in fleet

    def test_deterministic(self):
        a = resolve_loose_axes(_loose(), self.FLEET)
        b = resolve_loose_axes(_loose(), set(self.FLEET))
        self.assertEqual(a, b)

    def test_reject_no_compiler_in_fleet(self):
        with self.assertRaises(ValueError):
            resolve_loose_axes(_loose(), {"arch:amd64"})

    def test_reject_no_arch_in_fleet(self):
        with self.assertRaises(ValueError):
            resolve_loose_axes(_loose(), {"compiler:gcc-14"})

    def test_unknown_compiler_family_deterministic_fallback(self):
        # A family not in the preference list is still pinned deterministically.
        c = resolve_loose_axes(_loose(), {"compiler:icc-2024", "arch:amd64"})
        self.assertEqual(c["compiler"], "icc")
        self.assertEqual(c["compiler_version"], "2024")


def _loose_platform(**over):
    c = {"compiler": None, "compiler_version": None, "arch": None, "mode": None,
         "os": None, "os_version": None, "distro": None, "distro_version": None,
         "flavor": None}
    c.update(over)
    return c


class PlatformResolveTests(unittest.TestCase):
    FLEET = {"compiler:gcc-14", "arch:amd64", "distro:ubuntu-24.04"}

    def test_fills_loose_platform_from_distro(self):
        c = resolve_loose_axes(_loose_platform(), self.FLEET)
        self.assertEqual(c["os"], "Linux")
        self.assertEqual(c["distro"], "ubuntu")
        self.assertEqual(c["distro_version"], "24.04")
        # and the other axes are pinned too
        self.assertEqual(c["compiler"], "gcc")
        self.assertEqual(c["arch"], "amd64")

    def test_specified_platform_untouched(self):
        c = resolve_loose_axes(
            _loose_platform(compiler="gcc", compiler_version="14", arch="amd64",
                            mode="release", os="Linux", distro="fedora"),
            self.FLEET)
        self.assertEqual(c["distro"], "fedora")

    def test_reject_no_platform_in_fleet(self):
        with self.assertRaises(ValueError):
            resolve_loose_axes(_loose_platform(),
                               {"compiler:gcc-14", "arch:amd64"})  # no distro/os

    def test_resolves_os_when_no_distro(self):
        c = resolve_loose_axes(_loose_platform(),
                               {"compiler:gcc-14", "arch:amd64", "os:windows-11"})
        self.assertEqual(c["os"], "Windows")
        self.assertEqual(c["os_version"], "11")
        self.assertIsNone(c["distro"])


if __name__ == "__main__":
    unittest.main()
