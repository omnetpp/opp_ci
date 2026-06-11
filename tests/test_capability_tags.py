"""Tests for host capability detection and the `--auto-tags` tag refresh.

Two pure-ish units in opp_ci.cli:

1. `_detect_capability_tags()` — probes platform/os-release/PATH. Here the
   probes (platform, shutil.which, subprocess.run, /etc/os-release) are mocked
   so the tag-shaping logic is asserted deterministically, host-independently.
2. `_resolve_tags(..., detected=...)` — the replace/refresh/add/remove layering
   that `worker update --auto-tags` drives.

Run with: python -m unittest tests.test_capability_tags   (no pytest needed)
"""

import unittest
from unittest import mock

from opp_ci import cli


def _fake_run(stdout, returncode=0):
    return mock.Mock(stdout=stdout, returncode=returncode)


class DetectCapabilityTagsTests(unittest.TestCase):
    def _detect(self, *, machine="x86_64", os_release="", which=(),
                compiler_out=None):
        """Run _detect_capability_tags() on a mocked Linux host.

        `which` is the set of binaries on PATH; `compiler_out` maps a compiler
        name to its `--version` stdout.
        """
        compiler_out = compiler_out or {}
        which = set(which)

        def fake_which(name):
            return f"/usr/bin/{name}" if name in which else None

        def fake_subrun(argv, **kw):
            name = argv[0]
            if name in compiler_out:
                return _fake_run(compiler_out[name])
            return _fake_run("", returncode=0)

        m = mock.mock_open(read_data=os_release)
        with mock.patch("platform.system", return_value="Linux"), \
                mock.patch("platform.machine", return_value=machine), \
                mock.patch("shutil.which", side_effect=fake_which), \
                mock.patch("subprocess.run", side_effect=fake_subrun), \
                mock.patch("builtins.open", m):
            return cli._detect_capability_tags()

    def test_arch_known_aliases(self):
        self.assertIn("arch:amd64", self._detect(machine="x86_64"))
        self.assertIn("arch:aarch64", self._detect(machine="aarch64"))
        self.assertIn("arch:aarch64", self._detect(machine="arm64"))

    def test_arch_unknown_falls_through_to_raw(self):
        # Unsupported-by-omnetpp arch still advertises *something* (raw lower).
        self.assertIn("arch:riscv64", self._detect(machine="riscv64"))

    def test_distro_versioned_tag(self):
        rel = 'ID=ubuntu\nVERSION_ID="24.04"\n'
        tags = self._detect(os_release=rel)
        self.assertIn("os:linux", tags)
        self.assertIn("distro:ubuntu-24.04", tags)

    def test_flavor_from_variant_id(self):
        rel = 'ID=ubuntu\nVERSION_ID="24.04"\nVARIANT_ID=kubuntu\n'
        self.assertIn("flavor:kubuntu-24.04", self._detect(os_release=rel))

    def test_flavor_heuristic_xfce_is_xubuntu(self):
        # No VARIANT_ID; xfce4-session on PATH ⇒ Xubuntu.
        rel = 'ID=ubuntu\nVERSION_ID="24.04"\n'
        tags = self._detect(os_release=rel, which={"xfce4-session"})
        self.assertIn("flavor:xubuntu-24.04", tags)

    def test_flavor_heuristic_lxqt_is_lubuntu(self):
        rel = 'ID=ubuntu\nVERSION_ID="24.04"\n'
        tags = self._detect(os_release=rel, which={"lxqt-session"})
        self.assertIn("flavor:lubuntu-24.04", tags)

    def test_flavor_heuristic_plain_ubuntu_has_no_flavor(self):
        rel = 'ID=ubuntu\nVERSION_ID="24.04"\n'
        tags = self._detect(os_release=rel, which=set())
        self.assertFalse(any(t.startswith("flavor:") for t in tags))

    def test_compiler_major_tag(self):
        rel = 'ID=ubuntu\nVERSION_ID="24.04"\n'
        tags = self._detect(
            os_release=rel, which={"gcc"},
            compiler_out={"gcc": "gcc (Ubuntu 13.2.0-23) 13.2.0\n"})
        self.assertIn("compiler:gcc-13", tags)


class ResolveTagsRefreshTests(unittest.TestCase):
    def test_refresh_replaces_auto_keeps_custom(self):
        current = ["os:linux", "distro:ubuntu-22.04", "compiler:gcc-11",
                   "podman", "gpu", "fast"]
        detected = ["os:linux", "distro:ubuntu-24.04", "arch:amd64",
                    "compiler:gcc-13", "nix"]
        out = cli._resolve_tags(current, None, None, None, detected)
        # Stale auto tags gone, fresh ones in, custom preserved.
        self.assertNotIn("distro:ubuntu-22.04", out)
        self.assertNotIn("compiler:gcc-11", out)
        self.assertNotIn("podman", out)
        self.assertIn("distro:ubuntu-24.04", out)
        self.assertIn("nix", out)
        self.assertIn("gpu", out)
        self.assertIn("fast", out)

    def test_refresh_then_add_remove_layers_last(self):
        current = ["os:linux", "fast"]
        detected = ["os:linux", "arch:amd64"]
        out = cli._resolve_tags(current, None, "extra", "fast", detected)
        self.assertIn("extra", out)
        self.assertNotIn("fast", out)
        self.assertIn("arch:amd64", out)

    def test_no_change_returns_none(self):
        self.assertIsNone(cli._resolve_tags(["a"], None, None, None, None))

    def test_is_auto_managed(self):
        for t in ("os:linux", "distro:ubuntu-24.04", "flavor:kubuntu-24.04",
                  "arch:amd64", "compiler:gcc-13", "podman", "nix"):
            self.assertTrue(cli._is_auto_managed_tag(t), t)
        for t in ("gpu", "fast", "label:custom"):
            self.assertFalse(cli._is_auto_managed_tag(t), t)


if __name__ == "__main__":
    unittest.main()
