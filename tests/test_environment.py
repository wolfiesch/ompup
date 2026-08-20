from __future__ import annotations

import json
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ompup import environment  # noqa: E402
from ompup.hosts import HostConfig  # noqa: E402


class EnvironmentReleaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.agent = self.root / "agent"
        self.config_root = self.root / "config-root"
        self.plugin = self.root / "plugin"
        self.agent.mkdir()
        self.config_root.mkdir()
        self.plugin.mkdir()
        self.environment_config = environment.EnvironmentConfig(
            mode="mirror",
            broker_url="http://100.64.0.1:8765",
            include_extensions=("portable-status.ts",),
            include_profiles=("research",),
            exclude_skills=("private-deploy",),
            plugins=("omp-side",),
        )
        (self.agent / "config.yml").write_text("theme: test\n")
        (self.agent / "AGENTS.md").write_text("shared rules\n")
        (self.agent / "skills/example").mkdir(parents=True)
        (self.agent / "skills/example/SKILL.md").write_text("portable skill\n")
        (self.agent / "skills/example/.env").write_text("SHOULD_NOT_TRANSFER=1\n")
        (self.agent / "agents").mkdir()
        (self.agent / "agents/reviewer.md").write_text("review\n")
        (self.agent / "extensions").mkdir()
        (self.agent / "extensions/portable-status.ts").write_text("export default () => {}\n")
        (self.agent / "extensions/local-only.ts").write_text("local only\n")
        (self.plugin / "package.json").write_text('{"name":"ompup","version":"test"}\n')
        plugins = self.config_root / "plugins"
        plugins.mkdir()
        (plugins / "omp-plugins.lock.json").write_text(
            json.dumps({"plugins": {"omp-side": {"version": "0.1.1", "enabled": True}}})
        )
        omp_side = plugins / "node_modules/omp-side"
        omp_side.mkdir(parents=True)
        (omp_side / "package.json").write_text('{"name":"omp-side","version":"0.1.1"}\n')
        (omp_side / "index.ts").write_text("export default () => {}\n")
        profile_agent = self.config_root / "profiles/research/agent"
        profile_agent.mkdir(parents=True)
        (profile_agent / "config.yml").write_text("theme: profile\nextensions:\n  - ~/Projects/local-only.ts\n")
        (profile_agent / "AGENTS.md").write_text("profile rules\n")
        (profile_agent / "commands").mkdir()
        (profile_agent / "commands/profile.md").write_text("profile command\n")
        self.patches = [
            mock.patch.object(environment, "AGENT_HOME", self.agent),
            mock.patch.object(environment, "CONFIG_ROOT", self.config_root),
            mock.patch.object(environment, "PLUGIN_ROOT", self.plugin),
            mock.patch.object(environment, "LOCAL_CACHE_ROOT", self.root / "cache"),
            mock.patch.object(environment, "_omp_version", return_value="omp v-test"),
            mock.patch.object(environment, "_ignored_skills", return_value=()),
            mock.patch.object(
                environment,
                "_render_config",
                side_effect=lambda source, broker_url, remote: source.read_bytes() + f"auth-url: {broker_url}\n".encode(),
            ),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self) -> None:
        for patch in reversed(self.patches):
            patch.stop()
        self.temp.cleanup()

    def test_release_contains_only_declared_portable_state(self) -> None:
        release = environment.build_release(self.environment_config, HostConfig("box", "box"))
        try:
            targets = {entry["target"] for entry in release.manifest["targets"]}
            self.assertIn(".omp/agent/config.yml", targets)
            self.assertIn(".omp/agent/skills/example", targets)
            self.assertIn(".omp/agent/extensions/portable-status.ts", targets)
            self.assertNotIn(".omp/agent/extensions/local-only.ts", targets)
            self.assertIn("local-only.ts", release.manifest["excludedExtensions"])
            self.assertFalse((release.root / "payload/agent/skills/example/.env").exists())
            self.assertIn(".omp/profiles/research/agent/config.yml", targets)
            self.assertIn(".omp/profiles/research/agent/AGENTS.md", targets)
            self.assertIn(".omp/profiles/research/agent/commands", targets)
            profile_config = release.root / "payload/profiles/research/agent/config.yml"
            self.assertIn("auth-url: http://100.64.0.1:8765", profile_config.read_text())
            self.assertEqual(release.manifest["externalPlugins"], {"omp-side": "0.1.1"})
            self.assertTrue((release.root / "payload/plugins/ompup/package.json").is_file())
            manifest_text = (release.root / "manifest.json").read_text()
            self.assertNotIn("SHOULD_NOT_TRANSFER", manifest_text)
        finally:
            environment.cleanup_release(release)

    def test_disabled_and_credential_bearing_skills_are_excluded(self) -> None:
        (self.agent / "skills/ignored").mkdir()
        (self.agent / "skills/ignored/SKILL.md").write_text("ignored\n")
        (self.agent / "skills/private-deploy").mkdir()
        (self.agent / "skills/private-deploy/SKILL.md").write_text("private\n")
        with mock.patch.object(environment, "_ignored_skills", return_value=("ignored",)):
            excluded = environment._excluded_skill_names(self.environment_config)
        self.assertEqual(excluded, {"ignored", "private-deploy"})

    def test_remote_config_removes_machine_local_extension_paths(self) -> None:
        source = self.root / "portable-config.yml"
        source.write_text(
            "extensions:\n"
            "  - ~/Projects/local-extension\n"
            "speech:\n"
            "  enabled: true\n"
            "sonification:\n"
            "  enabled: true\n"
        )
        render_patch = self.patches[-1]
        render_patch.stop()
        try:
            rendered = environment._render_config(
                source,
                "http://100.64.0.1:8765",
                remote=True,
            ).decode()
        finally:
            render_patch.start()
        self.assertIn("extensions: []", rendered)
        self.assertIn("enabled: false", rendered)
        self.assertNotIn("local-extension", rendered)


    def test_fingerprint_changes_with_shared_skill_content(self) -> None:
        host = HostConfig("box", "box")
        first = environment.build_release(self.environment_config, host)
        first_fingerprint = first.fingerprint
        environment.cleanup_release(first)
        (self.agent / "skills/example/SKILL.md").write_text("changed\n")
        second = environment.build_release(self.environment_config, host)
        try:
            self.assertNotEqual(first_fingerprint, second.fingerprint)
        finally:
            environment.cleanup_release(second)

    def test_release_targets_configured_remote_roots(self) -> None:
        host = HostConfig(
            "box",
            "box",
            remote_agent_home="custom/omp-agent",
            remote_config_root="custom/omp",
        )
        release = environment.build_release(self.environment_config, host)
        try:
            targets = {entry["target"] for entry in release.manifest["targets"]}
            self.assertIn("custom/omp-agent/config.yml", targets)
            self.assertNotIn(".omp/agent/config.yml", targets)
            self.assertEqual(
                release.manifest["authTokenTargets"],
                [
                    "custom/omp/auth-broker.token",
                    "custom/omp/profiles/research/auth-broker.token",
                ],
            )
        finally:
            environment.cleanup_release(release)


class EnvironmentConfigTests(unittest.TestCase):
    def test_hosts_only_configuration_defaults_to_preserve(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            config_path = Path(temp_name) / "hosts.json"
            config_path.write_text('{"hosts":[{"name":"box","ssh":"box"}]}\n')
            with mock.patch.object(environment, "CONFIG_PATH", config_path):
                config = environment.load_environment_config()
        self.assertEqual(config.mode, "preserve")
        self.assertFalse(config.broker_url)

    def test_mirror_configuration_does_not_require_auth_broker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            config_path = Path(temp_name) / "hosts.json"
            config_path.write_text(
                '{"hosts":[{"name":"box","ssh":"box"}],"environment":{"mode":"mirror"}}\n'
            )
            with mock.patch.object(environment, "CONFIG_PATH", config_path):
                config = environment.load_environment_config()
        self.assertEqual(config.mode, "mirror")
        self.assertFalse(config.auth_host)

    def test_mirror_configuration_accepts_named_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            config_path = Path(temp_name) / "hosts.json"
            config_path.write_text(
                '{"hosts":[{"name":"box","ssh":"box"}],'
                '"environment":{"mode":"mirror","include_profiles":["research","review","research"]}}\n'
            )
            with mock.patch.object(environment, "CONFIG_PATH", config_path):
                config = environment.load_environment_config()
        self.assertEqual(config.include_profiles, ("research", "review"))

    def test_mirror_configuration_rejects_profile_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            config_path = Path(temp_name) / "hosts.json"
            config_path.write_text(
                '{"environment":{"mode":"mirror","include_profiles":["../private"]}}\n'
            )
            with (
                mock.patch.object(environment, "CONFIG_PATH", config_path),
                self.assertRaisesRegex(environment.HostSelectionError, "profile names"),
            ):
                environment.load_environment_config()

    def test_environment_rejects_unknown_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            config_path = Path(temp_name) / "hosts.json"
            config_path.write_text('{"environment":{"mode":"preserve","pluginss":[]}}\n')
            with (
                mock.patch.object(environment, "CONFIG_PATH", config_path),
                self.assertRaisesRegex(environment.HostSelectionError, "pluginss"),
            ):
                environment.load_environment_config()

    def test_preserve_mode_checks_remote_without_building_release(self) -> None:
        host = HostConfig("box", "box")
        expected = environment.EnvironmentStatus(
            host=host,
            reachable=True,
            ok=True,
            omp_version="omp/current",
            expected_omp_version="omp/current",
        )
        with (
            mock.patch.object(
                environment,
                "load_environment_config",
                return_value=environment.EnvironmentConfig(),
            ),
            mock.patch.object(environment, "preserved_environment_status", return_value=expected),
            mock.patch.object(environment, "build_release") as build_release,
        ):
            actual = environment.ensure_environment(host)
        self.assertIs(actual, expected)
        build_release.assert_not_called()



class EnvironmentTransportTests(unittest.TestCase):
    def test_status_quotes_remote_python_as_one_command(self) -> None:
        host = HostConfig("box", "box-ssh")
        payload = {
            "ok": True,
            "fingerprint": "abc",
            "expectedOmpVersion": "omp 1",
            "ompVersion": "omp 1",
            "mismatches": [],
        }
        completed = subprocess.CompletedProcess([], 0, json.dumps(payload).encode(), b"")
        with mock.patch.object(environment, "_run", return_value=completed) as run:
            status = environment.environment_status(host)
        command = run.call_args.args[0]
        self.assertEqual(command[:5], ["ssh", "-o", "ConnectTimeout=8", "box-ssh", command[4]])
        self.assertTrue(command[4].startswith("python3 -c "))
        self.assertTrue(status.ok)

    def test_token_is_sent_over_stdin_not_command_line(self) -> None:
        host = HostConfig("box", "box-ssh")
        completed = subprocess.CompletedProcess([], 0, b"", b"")
        token = b"private-broker-token-value"
        targets = (".omp/auth-broker.token", ".omp/profiles/research/auth-broker.token")
        with mock.patch.object(environment, "_run", return_value=completed) as run:
            environment._provision_token(host, token, targets)
        self.assertEqual(run.call_count, 2)
        for call, target in zip(run.call_args_list, targets, strict=True):
            command = call.args[0]
            self.assertNotIn(token.decode(), " ".join(command))
            self.assertEqual(call.kwargs["input_bytes"], token)
            self.assertIn(f"$HOME/{target}", command[-1])

    def test_token_uses_host_specific_remote_config_root(self) -> None:
        host = HostConfig("box", "box-ssh", remote_config_root="custom/omp")
        completed = subprocess.CompletedProcess([], 0, b"", b"")
        with mock.patch.object(environment, "_run", return_value=completed) as run:
            environment._provision_token(
                host,
                b"private-broker-token-value",
                ("custom/omp/auth-broker.token",),
            )
        self.assertIn('$HOME/custom/omp/auth-broker.token', run.call_args.args[0][-1])

    def test_configure_local_broker_backs_up_changed_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            agent = Path(temp_name) / "agent"
            agent.mkdir()
            config = agent / "config.yml"
            config.write_text("old\n")
            config.chmod(0o600)
            with (
                mock.patch.object(environment, "AGENT_HOME", agent),
                mock.patch.object(environment, "_render_config", return_value=b"new\n"),
            ):
                changed = environment.configure_local_broker("http://100.64.0.1:8765")
            self.assertTrue(changed)
            self.assertEqual(config.read_text(), "new\n")
            self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o600)
            backups = list(agent.glob("config.yml.ompup-backup-*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(), "old\n")

    def test_remote_update_uses_noninteractive_sudo_for_system_install(self) -> None:
        host = HostConfig("box", "box-ssh", launch="omp")
        results = [
            subprocess.CompletedProcess([], 0, b"omp/old\n", b""),
            subprocess.CompletedProcess([], 1, b"", b"permission denied"),
            subprocess.CompletedProcess([], 0, b"", b""),
            subprocess.CompletedProcess([], 0, b"omp/current\n", b""),
        ]
        with mock.patch.object(environment, "_run", side_effect=results) as run:
            environment._update_remote_omp(host, "omp/current")
        self.assertEqual(run.call_args_list[2].args[0][-1], "sudo -n omp update")


if __name__ == "__main__":
    unittest.main()
