"""Tests for opp_ci.service (uvx-based service management).

Covers plan/pending/uvx-service-management.md §11:
  * the embedded uvx command — right extras per role, @<ref>, opp_repl from
    its opp_ci branch, and --refresh-package opp_ci opp_repl;
  * option → env-var mapping in the rendered env files;
  * coordinator unit / worker template / target / plist / wrapper / newsyslog /
    env files render with the expected key lines;
  * the unprivileged manual recipe lists files + contents + commands;
  * OPP_CI_OPP_ENV_CMD ("uvx --from opp-env opp_env") in the worker env;
  * the NixOS module/flake render correctly (right ExecStart, EnvironmentFile=
    references, pkgs.uv on path, no secrets in the module) and the NixOS
    detector picks the render-only branch even as root.

Run with: python -m unittest tests.test_service   (no pytest needed)
"""

import os
import unittest
from unittest import mock

from opp_ci import service


def coordinator_spec(**kw):
    kw.setdefault("os_kind", "linux")
    return service.InstallSpec(role="coordinator", **kw)


def worker_spec(**kw):
    kw.setdefault("os_kind", "linux")
    return service.InstallSpec(role="worker", **kw)


UVX = "/var/lib/opp_ci/.local/bin/uvx"


class UvxCommandTest(unittest.TestCase):
    def test_coordinator_extras_ref_and_refresh(self):
        cmd = service.uvx_command(coordinator_spec(ref="main"), uvx=UVX)
        self.assertIn("opp_ci[web,postgres,client,podman] @ "
                      "git+https://github.com/omnetpp/opp_ci.git@main", cmd)
        self.assertIn("opp_repl[all] @ "
                      "git+https://github.com/omnetpp/opp_repl.git@opp_ci", cmd)
        self.assertIn("--refresh-package opp_ci --refresh-package opp_repl", cmd)
        self.assertTrue(cmd.endswith("opp_ci coordinator start"))
        self.assertTrue(cmd.startswith(UVX))

    def test_worker_extras_and_subcommand(self):
        cmd = service.uvx_command(worker_spec(ref="v1.2"), uvx=UVX)
        self.assertIn("opp_ci[client,podman] @ "
                      "git+https://github.com/omnetpp/opp_ci.git@v1.2", cmd)
        self.assertNotIn("web,postgres", cmd)
        self.assertTrue(cmd.endswith("opp_ci worker start"))

    def test_uvx_override_from_config(self):
        with mock.patch.object(service.cfg, "UVX", "/nix/store/uv/bin/uvx"):
            self.assertEqual(coordinator_spec().uvx_path(), "/nix/store/uv/bin/uvx")
            cmd = service.uvx_command(coordinator_spec())
            self.assertTrue(cmd.startswith("/nix/store/uv/bin/uvx"))


class EnvFileTest(unittest.TestCase):
    def test_coordinator_option_to_env_mapping(self):
        body = service.render_coordinator_env(coordinator_spec(
            host="0.0.0.0", port=8080, cert="/c.pem", key="/k.pem"))
        self.assertIn("OPP_CI_COORDINATOR_HOST=0.0.0.0", body)
        self.assertIn("OPP_CI_COORDINATOR_PORT=8080", body)
        self.assertIn("OPP_CI_COORDINATOR_TLS_CERT_FILE=/c.pem", body)
        self.assertIn("OPP_CI_COORDINATOR_TLS_KEY_FILE=/k.pem", body)

    def test_worker_option_to_env_mapping(self):
        body = service.render_worker_env(worker_spec(
            name="w1", coordinator="https://ci.example.org", token="TKN",
            poll_interval=15, heartbeat_interval=45, niceness=5))
        self.assertIn("OPP_CI_COORDINATOR_URL=https://ci.example.org", body)
        self.assertIn("OPP_CI_WORKER_TOKEN=TKN", body)
        self.assertIn("OPP_CI_WORKER_POLL_INTERVAL=15", body)
        self.assertIn("OPP_CI_WORKER_HEARTBEAT_INTERVAL=45", body)
        self.assertIn("OPP_CI_WORKER_NICENESS=5", body)

    def test_worker_env_has_opp_env_cmd(self):
        body = service.render_worker_env(worker_spec(name="w1"))
        self.assertIn('OPP_CI_OPP_ENV_CMD="uvx --from opp-env opp_env"', body)

    def test_unset_options_omitted(self):
        body = service.render_coordinator_env(coordinator_spec())
        self.assertNotIn("OPP_CI_COORDINATOR_HOST", body)


class SystemdRenderTest(unittest.TestCase):
    def test_coordinator_unit(self):
        unit = service.render_coordinator_unit(coordinator_spec(), uvx=UVX)
        self.assertIn("User=opp_ci", unit)
        self.assertIn("EnvironmentFile=/etc/opp_ci/opp_ci.env", unit)
        self.assertIn("EnvironmentFile=-/etc/opp_ci/coordinator.env", unit)
        self.assertIn("SupplementaryGroups=systemd-journal", unit)
        self.assertIn(f"ExecStart={UVX}", unit)
        self.assertIn("/var/lib/opp_ci/.local/bin", unit)  # PATH

    def test_worker_template(self):
        unit = service.render_worker_unit(worker_spec(), uvx=UVX)
        self.assertIn("Description=opp_ci worker (%i)", unit)
        self.assertIn("EnvironmentFile=/etc/opp_ci/workers/%i.env", unit)
        self.assertIn("KillSignal=SIGTERM", unit)

    def test_target_unit(self):
        self.assertIn("WantedBy=multi-user.target", service.render_target_unit())

    def test_run_as_user_threads_through(self):
        unit = service.render_coordinator_unit(coordinator_spec(user="ci"), uvx=UVX)
        self.assertIn("User=ci", unit)
        self.assertIn("Group=ci", unit)


class LaunchdRenderTest(unittest.TestCase):
    def test_plist_has_no_token(self):
        spec = worker_spec(os_kind="macos", name="m1", token="SECRET")
        plist = service.render_worker_plist(spec)
        self.assertIn("org.omnetpp.opp_ci.worker.m1", plist)
        self.assertIn("opp_ci-worker-run", plist)
        self.assertNotIn("SECRET", plist)

    def test_wrapper_sources_env_and_execs_uvx(self):
        spec = worker_spec(os_kind="macos", name="m1")
        wrapper = service.render_worker_wrapper(spec, uvx=UVX)
        self.assertIn(". /etc/opp_ci/opp_ci.env", wrapper)
        self.assertIn('. "/etc/opp_ci/workers/$name.env"', wrapper)
        self.assertIn(f"exec {UVX}", wrapper)

    def test_newsyslog(self):
        body = service.render_newsyslog()
        self.assertIn("/usr/local/var/log/opp_ci/worker-*.log", body)


class ManualTranscriptTest(unittest.TestCase):
    def test_coordinator_transcript_lists_files_and_commands(self):
        spec = coordinator_spec(host="0.0.0.0", port=8080)
        plan = service.build_install_plan(spec, uvx=UVX)
        t = service.render_manual_transcript(plan)
        self.assertIn("/etc/systemd/system/opp_ci-coordinator.service", t)
        self.assertIn("cat >", t)        # file contents
        self.assertIn("useradd", t)      # user creation
        self.assertIn("systemctl", t)    # enable/start
        self.assertIn("ExecStart=", t)   # the unit body is inlined

    def test_worker_transcript_carries_token_and_opp_env_cmd(self):
        spec = worker_spec(name="b1", coordinator="https://c", token="TK")
        plan = service.build_install_plan(spec, uvx=UVX)
        t = service.render_manual_transcript(plan)
        self.assertIn("workers/b1.env", t)
        self.assertIn("OPP_CI_WORKER_TOKEN=TK", t)
        self.assertIn("uvx --from opp-env opp_env", t)

    def test_uv_copy_in_transcript_for_non_self_install(self):
        spec = worker_spec(name="b1", user="opp_ci")
        plan = service.build_install_plan(spec, uvx=UVX)
        # Force a non-self-install + uv present.
        with mock.patch.object(service, "_invoking_user", return_value="alice"), \
             mock.patch("shutil.which", return_value="/usr/bin/uvx"), \
             mock.patch.object(service.cfg, "UVX", ""):
            plan = service.build_install_plan(spec, uvx=UVX)
        self.assertTrue(plan.uv_copy)


class NixosDetectorTest(unittest.TestCase):
    def test_detector_etc_nixos(self):
        def fake_exists(p):
            return p == "/etc/NIXOS"
        with mock.patch("os.path.exists", side_effect=fake_exists):
            self.assertTrue(service.is_nixos())
            self.assertEqual(service.detect_os(), "nixos")

    def test_detector_os_release_id(self):
        with mock.patch("os.path.exists", return_value=False), \
             mock.patch("builtins.open",
                        mock.mock_open(read_data='ID=nixos\nNAME="NixOS"\n')):
            self.assertTrue(service.is_nixos())

    def test_non_nixos(self):
        with mock.patch("os.path.exists", return_value=False), \
             mock.patch("builtins.open", side_effect=OSError):
            self.assertFalse(service.is_nixos())

    def test_nixos_install_is_render_only_even_as_root(self):
        """do_install on NixOS must mutate nothing, even euid 0."""
        spec = coordinator_spec(os_kind="nixos", host="0.0.0.0", port=8080)
        out = []
        with mock.patch.object(service, "apply_plan") as apply_mock, \
             mock.patch.object(service, "_is_root", return_value=True):
            service.do_install(spec, echo=out.append)
        apply_mock.assert_not_called()
        text = "\n".join(out)
        self.assertIn("opp_ci.nix", text)
        self.assertIn("nixos-rebuild", text)


class NixosRenderTest(unittest.TestCase):
    def test_coordinator_module(self):
        spec = coordinator_spec(os_kind="nixos", host="0.0.0.0", port=8080, ref="main")
        mod = service.render_nixos_module(spec)
        # pkgs.uv on the path, store-resolved uvx in ExecStart.
        self.assertIn("path = [ cfg.uvPackage ]", mod)
        self.assertIn("${cfg.uvPackage}/bin/uvx", mod)
        self.assertIn("--refresh-package opp_ci --refresh-package opp_repl", mod)
        self.assertIn('EnvironmentFile = cfg.environmentFiles', mod)
        self.assertIn("default = pkgs.uv", mod)
        self.assertIn("services.postgresql", mod)

    def test_worker_module_template_and_no_secret(self):
        spec = worker_spec(os_kind="nixos", name="b1",
                           coordinator="https://c", token="SUPER-SECRET", ref="main")
        mod = service.render_nixos_module(spec)
        self.assertIn('systemd.services."opp_ci-worker@"', mod)
        self.assertIn("/etc/opp_ci/workers/%i.env", mod)
        self.assertIn("virtualisation.podman", mod)
        # Secret must NOT enter the world-readable Nix store.
        self.assertNotIn("SUPER-SECRET", mod)

    def test_flake_exposes_modules(self):
        flake = service.render_nixos_flake(coordinator_spec(os_kind="nixos"))
        self.assertIn("nixosModules.opp_ci-coordinator", flake)
        self.assertIn("nixosModules.opp_ci-worker", flake)
        self.assertIn("nixosModules.default", flake)

    def test_bundle_separates_env_bodies(self):
        spec = worker_spec(os_kind="nixos", name="b1", token="TK")
        names = [n for n, _ in service.render_nixos_bundle(spec)]
        self.assertIn("opp_ci.nix", names)
        self.assertIn("flake.nix", names)
        self.assertIn("b1.env", names)


class MacosCoordinatorRefusalTest(unittest.TestCase):
    def test_coordinator_install_targets_linux_only(self):
        # The CLI refuses `coordinator service` on macOS; the spec itself still
        # maps to the linux/state layout. Sanity-check the detector seam the
        # CLI relies on.
        with mock.patch.object(service, "is_nixos", return_value=False), \
             mock.patch("sys.platform", "darwin"):
            self.assertEqual(service.detect_os(), "macos")


if __name__ == "__main__":
    unittest.main()
