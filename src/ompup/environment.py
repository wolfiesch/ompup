from __future__ import annotations

from dataclasses import dataclass
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import tarfile
import tempfile
import time
from typing import Iterable

from .hosts import CONFIG_PATH, HostConfig, HostSelectionError, find_host

SCHEMA_VERSION = 3
AGENT_HOME = Path(os.environ.get("OMPUP_AGENT_HOME", Path.home() / ".omp/agent")).expanduser()
CONFIG_ROOT = Path(os.environ.get("PI_CONFIG_DIR", Path.home() / ".omp")).expanduser()
STATE_ROOT = Path(".local/state/ompup/environment")
LOCAL_CACHE_ROOT = Path.home() / ".local/state/ompup/environment/local-cache"
SHARED_FILES = ("AGENTS.md", "RULES.md", "WATCHDOG.md", "config-spark.yml")
SHARED_ENTRY_DIRS = ("skills", "agents", "commands", "rules", "hooks")
SHARED_WHOLE_DIRS = ("docs",)
PLUGIN_ROOT = Path(__file__).resolve().parents[2]
ENVIRONMENT_KEYS = {
    "mode",
    "auth_host",
    "auth_broker_url",
    "include_extensions",
    "include_profiles",
    "exclude_skills",
    "plugins",
}
HOST_LOCAL = (
    "agent.db and all SQLite sidecars",
    "sessions, histories, memories, caches, blobs, logs, and terminal state",
    ".env and credential files",
    "mcp.json, models.yml, and ssh.json",
    "skills disabled or excluded by local configuration",
    "extensions and plugins not explicitly included",
)
COPY_IGNORE_NAMES = (
    ".git",
    ".env",
    ".env.*",
    ".envrc",
    ".npmrc",
    ".pypirc",
    "credentials.json",
    "token.json",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    ".DS_Store",
    "__pycache__",
    "*.pyc",
    "node_modules",
    ".pytest_cache",
    ".mypy_cache",
    "dist",
    "build",
    "browser_profile*",
    "Singleton*",
    "RunningChromeVersion",
)
COPY_IGNORE = shutil.ignore_patterns(*COPY_IGNORE_NAMES)

REMOTE_INSTALLER = r'''#!/usr/bin/env python3
from __future__ import annotations
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

launch = shlex.split(os.path.expandvars(os.path.expanduser(sys.argv[2])))

release = Path(sys.argv[1]).expanduser().resolve()
home = Path.home()
state_root = home / ".local/state/ompup/environment"
manifest = json.loads((release / "manifest.json").read_text())
backup = state_root / "backups" / time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
backup_used = False

def move_to_backup(target: Path) -> None:
    global backup_used
    relative = target.relative_to(home)
    destination = backup / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(target), str(destination))
    backup_used = True

def managed_link(target: Path) -> bool:
    if not target.is_symlink():
        return False
    try:
        target.resolve().relative_to(state_root / "releases")
        return True
    except (OSError, ValueError):
        return False

active_path = state_root / "active.json"
old_targets = []
if active_path.is_file():
    try:
        old_targets = json.loads(active_path.read_text()).get("targets", [])
    except (OSError, ValueError, TypeError):
        old_targets = []
new_targets = {entry["target"] for entry in manifest["targets"]}
for relative in old_targets:
    target = home / relative
    if relative not in new_targets and managed_link(target):
        target.unlink()

for entry in manifest["targets"]:
    source = release / "payload" / entry["source"]
    target = home / entry["target"]
    target.parent.mkdir(parents=True, exist_ok=True)
    if entry["mode"] == "copy":
        if target.exists() or target.is_symlink():
            if target.is_file() and target.read_bytes() == source.read_bytes():
                continue
            move_to_backup(target)
        shutil.copy2(source, target)
        continue
    if target.exists() or target.is_symlink():
        if managed_link(target):
            target.unlink()
        else:
            move_to_backup(target)
    target.symlink_to(source, target_is_directory=source.is_dir())

for package in manifest.get("externalPlugins", {}):
    plugin = release / "payload/plugins" / package
    subprocess.run([*launch, "plugin", "link", str(plugin)], check=True)
plugin = release / "payload/plugins/ompup"
subprocess.run([*launch, "plugin", "link", str(plugin)], check=True)

active = {
    "fingerprint": manifest["fingerprint"],
    "installedAt": int(time.time()),
    "release": str(release),
    "targets": sorted(new_targets),
    "backup": str(backup) if backup_used else None,
}
state_root.mkdir(parents=True, exist_ok=True)
fd, temporary = tempfile.mkstemp(prefix=".active.", dir=state_root)
try:
    with os.fdopen(fd, "w") as handle:
        json.dump(active, handle, sort_keys=True)
        handle.write("\n")
    os.replace(temporary, active_path)
finally:
    try:
        os.unlink(temporary)
    except FileNotFoundError:
        pass
print(json.dumps(active))
'''

REMOTE_STATUS = r'''from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

launch = sys.argv[1]
home = Path.home()
state = home / ".local/state/ompup/environment/active.json"
if not state.is_file():
    print(json.dumps({"ok": False, "reason": "not_installed"}))
    raise SystemExit(0)
try:
    active = json.loads(state.read_text())
    release = Path(active["release"])
    manifest = json.loads((release / "manifest.json").read_text())
except (OSError, ValueError, KeyError, TypeError) as error:
    print(json.dumps({"ok": False, "reason": f"invalid_state: {error}"}))
    raise SystemExit(0)

def digest(path: Path) -> str:
    value = hashlib.sha256()
    if path.is_dir():
        for child in sorted(item for item in path.rglob("*") if item.is_file()):
            value.update(str(child.relative_to(path)).encode())
            value.update(b"\0")
            value.update(child.read_bytes())
            value.update(b"\0")
    else:
        value.update(path.read_bytes())
    return value.hexdigest()

mismatches = []
for entry in manifest["targets"]:
    target = home / entry["target"]
    if not target.exists() or digest(target) != entry["sha256"]:
        mismatches.append(entry["target"])
token_target = manifest.get("authTokenTarget")
if token_target and not (home / token_target).is_file():
    mismatches.append(token_target)
shell = os.environ.get("SHELL", "/bin/sh")
version = subprocess.run([shell, "-lc", f"{launch} --version"], text=True, capture_output=True)
print(json.dumps({
    "ok": not mismatches and version.returncode == 0,
    "fingerprint": active.get("fingerprint"),
    "expectedOmpVersion": manifest.get("ompVersion"),
    "ompVersion": version.stdout.strip(),
    "mismatches": mismatches,
}))
'''


@dataclass(frozen=True)
class EnvironmentConfig:
    mode: str = "preserve"
    auth_host: str = ""
    broker_url: str = ""
    include_extensions: tuple[str, ...] = ()
    include_profiles: tuple[str, ...] = ()
    exclude_skills: tuple[str, ...] = ()
    plugins: tuple[str, ...] = ()


@dataclass(frozen=True)
class EnvironmentRelease:
    root: Path
    manifest: dict
    ephemeral: bool = True

    @property
    def fingerprint(self) -> str:
        return str(self.manifest["fingerprint"])


@dataclass(frozen=True)
class EnvironmentStatus:
    host: HostConfig
    reachable: bool
    ok: bool
    fingerprint: str = ""
    omp_version: str = ""
    expected_omp_version: str = ""
    mismatches: tuple[str, ...] = ()
    error: str = ""
    synced: bool = False


def _string_list(raw: object, field: str) -> tuple[str, ...]:
    if not isinstance(raw, list) or any(not isinstance(item, str) or not item.strip() for item in raw):
        raise HostSelectionError(f"environment.{field} must be an array of nonempty strings")
    return tuple(dict.fromkeys(item.strip() for item in raw))


def load_environment_config() -> EnvironmentConfig:
    try:
        payload = json.loads(CONFIG_PATH.read_text())
    except (OSError, ValueError, TypeError) as error:
        raise HostSelectionError(f"invalid host configuration {CONFIG_PATH}: {error}") from error
    if not isinstance(payload, dict):
        raise HostSelectionError(f"host configuration {CONFIG_PATH} must be an object")
    raw = payload.get("environment", {})
    if not isinstance(raw, dict):
        raise HostSelectionError(f"environment in {CONFIG_PATH} must be an object")
    unknown = sorted(set(raw) - ENVIRONMENT_KEYS)
    if unknown:
        raise HostSelectionError(f"environment has unknown field(s): {', '.join(unknown)}")
    mode = raw.get("mode", "preserve")
    if mode not in {"preserve", "mirror"}:
        raise HostSelectionError("environment.mode must be preserve or mirror")
    auth_host = raw.get("auth_host", "")
    broker_url = raw.get("auth_broker_url", "")
    if not isinstance(auth_host, str) or not isinstance(broker_url, str):
        raise HostSelectionError("environment auth_host and auth_broker_url must be strings")
    auth_host = auth_host.strip()
    broker_url = broker_url.strip().rstrip("/")
    if bool(auth_host) != bool(broker_url):
        raise HostSelectionError("environment auth_host and auth_broker_url must be configured together")
    if broker_url and not broker_url.startswith(("http://", "https://")):
        raise HostSelectionError("auth_broker_url must be an http(s) URL")
    include_extensions = _string_list(raw.get("include_extensions", []), "include_extensions")
    if any(Path(name).name != name or name in {".", ".."} for name in include_extensions):
        raise HostSelectionError("environment.include_extensions entries must be file names")
    include_profiles = _string_list(raw.get("include_profiles", []), "include_profiles")
    if any(Path(name).name != name or name in {".", ".."} for name in include_profiles):
        raise HostSelectionError("environment.include_profiles entries must be profile names")
    return EnvironmentConfig(
        mode=mode,
        auth_host=auth_host,
        broker_url=broker_url,
        include_extensions=include_extensions,
        include_profiles=include_profiles,
        exclude_skills=_string_list(raw.get("exclude_skills", []), "exclude_skills"),
        plugins=_string_list(raw.get("plugins", []), "plugins"),
    )


def _run(command: list[str], *, input_bytes: bytes | None = None, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        input=input_bytes,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _omp_version() -> str:
    result = _run(["omp", "--version"], timeout=15)
    if result.returncode != 0:
        raise RuntimeError("local omp --version failed")
    return result.stdout.decode().strip()


def _render_config(source: Path, broker_url: str, *, remote: bool) -> bytes:
    yq = shutil.which("yq")
    if yq is None:
        raise RuntimeError("Mike Farah yq v4 is required to build the shared OMP configuration")
    expression = "."
    if broker_url:
        expression += " | .auth.broker.url = strenv(OMPUP_BROKER_URL)"
    if remote:
        expression += " | .extensions = [] | .speech.enabled = false | .sonification.enabled = false | .computer.enabled = false | .browser.headless = true"
    env = os.environ.copy()
    env["OMPUP_BROKER_URL"] = broker_url
    result = subprocess.run(
        [yq, "eval", expression, str(source)],
        env=env,
        capture_output=True,
        timeout=20,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"could not render shared config: {result.stderr.decode().strip()}")
    return result.stdout


def configure_local_broker(broker_url: str) -> bool:
    source = AGENT_HOME / "config.yml"
    rendered = _render_config(source, broker_url, remote=False)
    if source.read_bytes() == rendered:
        return False
    backup = source.with_name(f"config.yml.ompup-backup-{int(time.time())}")
    shutil.copy2(source, backup)
    temporary = source.with_name(f".config.yml.ompup-{os.getpid()}")
    temporary.write_bytes(rendered)
    os.chmod(temporary, stat.S_IMODE(source.stat().st_mode))
    os.replace(temporary, source)
    return True


def _copy_entry(source: Path, destination: Path) -> None:
    if source.is_dir():
        shutil.copytree(
            source,
            destination,
            symlinks=False,
            ignore=COPY_IGNORE,
            ignore_dangling_symlinks=True,
        )
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination, follow_symlinks=True)


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    if path.is_dir():
        for child in sorted(item for item in path.rglob("*") if item.is_file()):
            value.update(str(child.relative_to(path)).encode())
            value.update(b"\0")
            value.update(child.read_bytes())
            value.update(b"\0")
    else:
        value.update(path.read_bytes())
    return value.hexdigest()


def _plugin_versions(config: EnvironmentConfig) -> dict[str, str]:
    if not config.plugins:
        return {}
    lock = CONFIG_ROOT / "plugins/omp-plugins.lock.json"
    if not lock.is_file():
        raise RuntimeError(f"plugin lock is unavailable: {lock}")
    try:
        payload = json.loads(lock.read_text())
        plugins = payload.get("plugins", {})
    except (OSError, ValueError, TypeError) as error:
        raise RuntimeError(f"invalid plugin lock {lock}: {error}") from error
    if not isinstance(plugins, dict):
        raise RuntimeError(f"invalid plugin lock {lock}: plugins must be an object")
    selected: dict[str, str] = {}
    for name in config.plugins:
        plugin = plugins.get(name)
        if not isinstance(plugin, dict) or not plugin.get("enabled", True) or not plugin.get("version"):
            raise RuntimeError(f"configured portable plugin is unavailable or disabled: {name}")
        selected[name] = str(plugin["version"])
    return selected


def _ignored_skills() -> tuple[str, ...]:
    yq = shutil.which("yq")
    if yq is None:
        raise RuntimeError("Mike Farah yq v4 is required to resolve the active skill set")
    result = subprocess.run(
        [yq, "-o=json", ".skills.ignoredSkills // []", str(AGENT_HOME / "config.yml")],
        capture_output=True,
        timeout=20,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("could not read ignored skills from the local OMP config")
    payload = json.loads(result.stdout)
    return tuple(str(item) for item in payload if isinstance(item, str))


def _excluded_skill_names(config: EnvironmentConfig) -> set[str]:
    source = AGENT_HOME / "skills"
    if not source.is_dir():
        return set()
    patterns = (*_ignored_skills(), *config.exclude_skills)
    return {
        entry.name
        for entry in source.iterdir()
        if any(fnmatch.fnmatch(entry.name, pattern) for pattern in patterns)
    }


def _build_release_uncached(config: EnvironmentConfig, host: HostConfig) -> EnvironmentRelease:
    temporary = Path(tempfile.mkdtemp(prefix="ompup-environment-"))
    payload = temporary / "payload"
    excluded_skills = _excluded_skill_names(config)
    targets: list[dict[str, str]] = []

    def agent_target(relative: str) -> str:
        return f"{host.remote_agent_home}/{relative}"

    config_target = payload / "agent/config.yml"
    config_target.parent.mkdir(parents=True, exist_ok=True)
    config_target.write_bytes(_render_config(AGENT_HOME / "config.yml", config.broker_url, remote=True))
    targets.append({"source": "agent/config.yml", "target": agent_target("config.yml"), "mode": "copy"})

    for name in SHARED_FILES:
        source = AGENT_HOME / name
        if source.exists():
            relative = f"agent/{name}"
            _copy_entry(source, payload / relative)
            targets.append({"source": relative, "target": agent_target(name), "mode": "link"})

    for directory in SHARED_ENTRY_DIRS:
        source_dir = AGENT_HOME / directory
        if not source_dir.is_dir():
            continue
        for source in sorted(source_dir.iterdir(), key=lambda item: item.name):
            if not source.exists():
                continue
            if directory == "skills" and source.name in excluded_skills:
                continue
            relative = f"agent/{directory}/{source.name}"
            _copy_entry(source, payload / relative)
            targets.append(
                {
                    "source": relative,
                    "target": agent_target(f"{directory}/{source.name}"),
                    "mode": "link",
                }
            )

    for directory in SHARED_WHOLE_DIRS:
        source = AGENT_HOME / directory
        if source.is_dir():
            relative = f"agent/{directory}"
            _copy_entry(source, payload / relative)
            targets.append({"source": relative, "target": agent_target(directory), "mode": "link"})

    for name in config.include_extensions:
        source = AGENT_HOME / "extensions" / name
        if not source.is_file():
            raise RuntimeError(f"configured portable extension is unavailable: {name}")
        relative = f"agent/extensions/{name}"
        _copy_entry(source, payload / relative)
        targets.append(
            {"source": relative, "target": agent_target(f"extensions/{name}"), "mode": "link"}
        )

    for profile in config.include_profiles:
        source_agent = CONFIG_ROOT / "profiles" / profile / "agent"
        if not source_agent.is_dir():
            raise RuntimeError(f"configured portable profile is unavailable: {profile}")
        profile_prefix = f"profiles/{profile}/agent"
        profile_target = f"{host.remote_config_root}/{profile_prefix}"
        profile_config = source_agent / "config.yml"
        if not profile_config.is_file():
            raise RuntimeError(f"configured portable profile has no config.yml: {profile}")
        relative = f"{profile_prefix}/config.yml"
        destination = payload / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(_render_config(profile_config, config.broker_url, remote=True))
        targets.append({"source": relative, "target": f"{profile_target}/config.yml", "mode": "copy"})
        for name in SHARED_FILES:
            source = source_agent / name
            if source.exists():
                relative = f"{profile_prefix}/{name}"
                _copy_entry(source, payload / relative)
                targets.append({"source": relative, "target": f"{profile_target}/{name}", "mode": "link"})
        for directory in (*SHARED_ENTRY_DIRS, *SHARED_WHOLE_DIRS):
            source = source_agent / directory
            if source.is_dir():
                relative = f"{profile_prefix}/{directory}"
                _copy_entry(source, payload / relative)
                targets.append(
                    {"source": relative, "target": f"{profile_target}/{directory}", "mode": "link"}
                )

    _copy_entry(PLUGIN_ROOT, payload / "plugins/ompup")
    plugin_versions = _plugin_versions(config)
    for package in plugin_versions:
        source = CONFIG_ROOT / "plugins/node_modules" / package
        if not source.is_dir():
            raise RuntimeError(f"configured portable plugin source is unavailable: {package}")
        _copy_entry(source, payload / "plugins" / package)

    for target in targets:
        target["sha256"] = _digest(payload / target["source"])
    omp_version = _omp_version()
    auth_token_targets = (
        [
            f"{host.remote_config_root}/auth-broker.token",
            *[
                f"{host.remote_config_root}/profiles/{profile}/auth-broker.token"
                for profile in config.include_profiles
            ],
        ]
        if config.broker_url
        else []
    )
    fingerprint_input = json.dumps(
        {
            "schema": SCHEMA_VERSION,
            "ompVersion": omp_version,
            "targets": targets,
            "externalPlugins": plugin_versions,
            "authTokenTargets": auth_token_targets,
            "plugins": {
                "ompup": _digest(payload / "plugins/ompup"),
                **{
                    package: _digest(payload / "plugins" / package)
                    for package in plugin_versions
                },
            },
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    manifest = {
        "schema": SCHEMA_VERSION,
        "fingerprint": hashlib.sha256(fingerprint_input).hexdigest(),
        "ompVersion": omp_version,
        "targets": targets,
        "externalPlugins": plugin_versions,
        "authTokenTargets": auth_token_targets,
        "hostLocal": list(HOST_LOCAL),
        "excludedExtensions": sorted(
            path.name
            for path in (AGENT_HOME / "extensions").glob("*")
            if path.is_file() and path.name not in config.include_extensions
        ),
        "excludedSkills": sorted(excluded_skills),
    }
    (temporary / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    installer = temporary / "install.py"
    installer.write_text(REMOTE_INSTALLER)
    installer.chmod(0o700)
    return EnvironmentRelease(temporary, manifest)


def _signature_entry(label: str, source: Path) -> list[tuple[str, int, str]]:
    if not source.exists():
        return []
    resolved = source.resolve()
    if resolved.is_file():
        return [(label, stat.S_IMODE(resolved.stat().st_mode), _digest(resolved))]
    entries: list[tuple[str, int, str]] = []
    visited: set[tuple[int, int]] = set()
    for current, directories, files in os.walk(resolved, followlinks=True):
        current_path = Path(current)
        try:
            current_stat = current_path.stat()
        except OSError:
            directories[:] = []
            continue
        inode = (current_stat.st_dev, current_stat.st_ino)
        if inode in visited:
            directories[:] = []
            continue
        visited.add(inode)
        directories[:] = sorted(
            name
            for name in directories
            if not any(fnmatch.fnmatch(name, pattern) for pattern in COPY_IGNORE_NAMES)
        )
        for name in sorted(files):
            if any(fnmatch.fnmatch(name, pattern) for pattern in COPY_IGNORE_NAMES):
                continue
            child = current_path / name
            try:
                mode = stat.S_IMODE(child.stat().st_mode)
                digest = _digest(child)
            except OSError:
                continue
            entries.append((f"{label}/{child.relative_to(resolved)}", mode, digest))
    return entries


def _source_signature(config: EnvironmentConfig, host: HostConfig) -> str:
    entries: list[tuple[str, int, str]] = []
    for name in SHARED_FILES:
        entries.extend(_signature_entry(f"agent/{name}", AGENT_HOME / name))
    excluded_skills = _excluded_skill_names(config)
    for directory in SHARED_ENTRY_DIRS:
        source = AGENT_HOME / directory
        if directory == "skills" and source.is_dir():
            for entry in sorted(source.iterdir(), key=lambda item: item.name):
                if entry.name not in excluded_skills:
                    entries.extend(_signature_entry(f"agent/skills/{entry.name}", entry))
        else:
            entries.extend(_signature_entry(f"agent/{directory}", source))
    for directory in SHARED_WHOLE_DIRS:
        entries.extend(_signature_entry(f"agent/{directory}", AGENT_HOME / directory))
    for name in config.include_extensions:
        entries.extend(_signature_entry(f"agent/extensions/{name}", AGENT_HOME / "extensions" / name))
    for profile in config.include_profiles:
        source_agent = CONFIG_ROOT / "profiles" / profile / "agent"
        entries.extend(_signature_entry(f"profiles/{profile}/agent/config.yml", source_agent / "config.yml"))
        for name in SHARED_FILES:
            entries.extend(_signature_entry(f"profiles/{profile}/agent/{name}", source_agent / name))
        for directory in (*SHARED_ENTRY_DIRS, *SHARED_WHOLE_DIRS):
            entries.extend(
                _signature_entry(f"profiles/{profile}/agent/{directory}", source_agent / directory)
            )
    entries.extend(_signature_entry("agent/config.yml", AGENT_HOME / "config.yml"))
    entries.extend(_signature_entry("plugins/ompup", PLUGIN_ROOT))
    if config.plugins:
        entries.extend(_signature_entry("plugins/lock", CONFIG_ROOT / "plugins/omp-plugins.lock.json"))
    for package in _plugin_versions(config):
        entries.extend(
            _signature_entry(
                f"plugins/{package}",
                CONFIG_ROOT / "plugins/node_modules" / package,
            )
        )
    payload = json.dumps(
        {
            "schema": SCHEMA_VERSION,
            "config": {
                "brokerUrl": config.broker_url,
                "includeExtensions": config.include_extensions,
                "includeProfiles": config.include_profiles,
                "excludeSkills": config.exclude_skills,
                "plugins": config.plugins,
                "remoteAgentHome": host.remote_agent_home,
                "remoteConfigRoot": host.remote_config_root,
            },
            "ompVersion": _omp_version(),
            "entries": sorted(entries),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def build_release(config: EnvironmentConfig, host: HostConfig) -> EnvironmentRelease:
    if config.mode != "mirror":
        raise RuntimeError("environment releases require environment.mode mirror")
    signature = _source_signature(config, host)
    index_path = LOCAL_CACHE_ROOT / "indexes" / f"{signature}.json"
    try:
        index = json.loads(index_path.read_text())
        cached = LOCAL_CACHE_ROOT / "releases" / str(index["fingerprint"])
        if (cached / "manifest.json").is_file():
            manifest = json.loads((cached / "manifest.json").read_text())
            return EnvironmentRelease(cached, manifest, ephemeral=False)
    except (OSError, ValueError, KeyError, TypeError):
        pass

    release = _build_release_uncached(config, host)
    destination = LOCAL_CACHE_ROOT / "releases" / release.fingerprint
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if destination.exists():
        shutil.rmtree(release.root)
    else:
        try:
            os.rename(release.root, destination)
        except OSError:
            if not (destination / "manifest.json").is_file():
                raise
            shutil.rmtree(release.root, ignore_errors=True)
    index_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = index_path.with_name(
        f".{index_path.name}.{os.getpid()}.{time.time_ns()}"
    )
    temporary.write_text(
        json.dumps({"fingerprint": release.fingerprint}, sort_keys=True) + "\n"
    )
    os.replace(temporary, index_path)
    return EnvironmentRelease(destination, release.manifest, ephemeral=False)


def cleanup_release(release: EnvironmentRelease) -> None:
    if release.ephemeral:
        shutil.rmtree(release.root, ignore_errors=True)


def _scan_release(release: EnvironmentRelease) -> None:
    gitleaks = shutil.which("gitleaks")
    if gitleaks is None:
        raise RuntimeError("gitleaks is required before transferring the shared environment")
    result = _run(
        [gitleaks, "dir", "--no-banner", "--redact", "--exit-code", "1", str(release.root / "payload")],
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError("shared environment failed the gitleaks scan; nothing was transferred")


def environment_status(host: HostConfig) -> EnvironmentStatus:
    command = [
        "ssh",
        "-o",
        "ConnectTimeout=8",
        host.ssh,
        f"python3 -c {shlex.quote(REMOTE_STATUS)} {shlex.quote(host.launch)}",
    ]
    try:
        result = _run(command, timeout=30)
    except subprocess.TimeoutExpired:
        return EnvironmentStatus(host, False, False, error="connection timed out")
    if result.returncode != 0:
        error = result.stderr.decode(errors="replace").strip() or "remote status failed"
        return EnvironmentStatus(host, False, False, error=error)
    try:
        payload = json.loads(result.stdout)
    except (ValueError, TypeError):
        return EnvironmentStatus(host, True, False, error="invalid remote environment status")
    return EnvironmentStatus(
        host=host,
        reachable=True,
        ok=bool(payload.get("ok")),
        fingerprint=str(payload.get("fingerprint", "")),
        omp_version=str(payload.get("ompVersion", "")),
        expected_omp_version=str(payload.get("expectedOmpVersion", "")),
        mismatches=tuple(str(item) for item in payload.get("mismatches", [])),
        error=str(payload.get("reason", "")),
    )
def preserved_environment_status(host: HostConfig) -> EnvironmentStatus:
    command = f"{host.launch} --version"
    try:
        result = _run(
            ["ssh", "-o", "ConnectTimeout=8", host.ssh, command],
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return EnvironmentStatus(host, False, False, error="connection timed out")
    if result.returncode != 0:
        error = result.stderr.decode(errors="replace").strip() or "remote omp --version failed"
        return EnvironmentStatus(host, True, False, error=error)
    version = result.stdout.decode(errors="replace").strip()
    return EnvironmentStatus(
        host=host,
        reachable=True,
        ok=bool(version),
        omp_version=version,
        expected_omp_version=version,
    )



def _update_remote_omp(host: HostConfig, expected: str) -> None:
    version_command = f"{host.launch} --version"
    update_command = f"{host.launch} update"
    current = _run(
        ["ssh", "-o", "ConnectTimeout=8", host.ssh, version_command],
        timeout=20,
    )
    if current.returncode == 0 and current.stdout.decode().strip() == expected:
        return
    updated = _run(
        ["ssh", "-o", "ConnectTimeout=8", host.ssh, update_command],
        timeout=180,
    )
    if updated.returncode != 0 and host.launch == "omp":
        updated = _run(
            ["ssh", "-o", "ConnectTimeout=8", host.ssh, "sudo -n omp update"],
            timeout=180,
        )
    if updated.returncode != 0:
        raise RuntimeError(
            f"could not update OMP on {host.name}: "
            f"{updated.stderr.decode(errors='replace').strip()}"
        )
    verified = _run(["ssh", host.ssh, version_command], timeout=20)
    if verified.returncode != 0 or verified.stdout.decode().strip() != expected:
        actual = verified.stdout.decode(errors="replace").strip() or "unavailable"
        raise RuntimeError(f"OMP version mismatch on {host.name}: local={expected}, remote={actual}")


def _provision_token(host: HostConfig, token: bytes, targets: Iterable[str]) -> None:
    for token_path in targets:
        parent = str(Path(token_path).parent)
        command = (
            f'umask 077; mkdir -p "$HOME/{parent}"; '
            f'cat > "$HOME/{token_path}"; chmod 600 "$HOME/{token_path}"'
        )
        result = _run(["ssh", "-o", "ConnectTimeout=8", host.ssh, command], input_bytes=token, timeout=20)
        if result.returncode != 0:
            raise RuntimeError(f"could not provision broker token on {host.name}: {token_path}")


def sync_environment(host: HostConfig, release: EnvironmentRelease, token: bytes | None = None) -> EnvironmentStatus:
    _scan_release(release)
    _update_remote_omp(host, str(release.manifest["ompVersion"]))
    if token is not None:
        _provision_token(host, token, release.manifest.get("authTokenTargets", ()))
    release_relative = STATE_ROOT / "releases" / release.fingerprint
    prepare = f"umask 077; mkdir -p {shlex.quote(str(release_relative.parent))}; rm -rf {shlex.quote(str(release_relative))}; mkdir -p {shlex.quote(str(release_relative))}; tar -xzf - -C {shlex.quote(str(release_relative))}"
    with tempfile.NamedTemporaryFile(prefix="ompup-environment-", suffix=".tar.gz") as archive:
        with tarfile.open(fileobj=archive, mode="w:gz", dereference=True) as tar:
            for child in release.root.iterdir():
                tar.add(child, arcname=child.name, recursive=True)
        archive.flush()
        archive.seek(0)
        transferred = _run(
            ["ssh", "-o", "ConnectTimeout=8", host.ssh, prepare],
            input_bytes=archive.read(),
            timeout=180,
        )
    if transferred.returncode != 0:
        raise RuntimeError(f"environment transfer to {host.name} failed")
    remote_release = f"$HOME/{release_relative}"
    installed = _run(
        [
            "ssh",
            host.ssh,
            f"python3 {remote_release}/install.py {remote_release} {shlex.quote(host.launch)}",
        ],
        timeout=180,
    )
    if installed.returncode != 0:
        detail = installed.stderr.decode(errors="replace").strip()
        raise RuntimeError(f"environment install on {host.name} failed: {detail}")
    status = environment_status(host)
    if not status.ok or status.fingerprint != release.fingerprint:
        raise RuntimeError(f"environment verification on {host.name} failed")
    return EnvironmentStatus(
        host=status.host,
        reachable=status.reachable,
        ok=status.ok,
        fingerprint=status.fingerprint,
        omp_version=status.omp_version,
        expected_omp_version=status.expected_omp_version,
        mismatches=status.mismatches,
        error=status.error,
        synced=True,
    )


def ensure_environment(host: HostConfig, *, auto_sync: bool = True) -> EnvironmentStatus:
    config = load_environment_config()
    if config.mode == "preserve":
        status = preserved_environment_status(host)
        if not status.ok:
            raise RuntimeError(f"remote OMP is unavailable on {host.name}: {status.error}")
        return status
    release = build_release(config, host)
    try:
        status = environment_status(host)
        expected_version = str(release.manifest["ompVersion"])
        if status.ok and status.fingerprint == release.fingerprint and status.omp_version == expected_version:
            return status
        if not auto_sync:
            return status
        token = None
        if config.broker_url:
            token_path = CONFIG_ROOT / "auth-broker.token"
            if not token_path.is_file():
                raise RuntimeError(
                    f"auth broker token is missing at {token_path}; run `ompup auth setup`"
                )
            token = token_path.read_bytes()
        return sync_environment(host, release, token)
    finally:
        cleanup_release(release)


def bootstrap_auth(hosts: list[HostConfig]) -> tuple[EnvironmentConfig, bool]:
    config = load_environment_config()
    if not config.auth_host or not config.broker_url:
        raise HostSelectionError(
            "auth commands require environment.auth_host and environment.auth_broker_url"
        )
    auth_host = find_host(hosts, config.auth_host)
    token_path = CONFIG_ROOT / "auth-broker.token"
    remote_token = f"$HOME/{auth_host.remote_config_root}/auth-broker.token"
    fetched = _run(
        ["ssh", "-o", "ConnectTimeout=8", auth_host.ssh, "cat", remote_token],
        timeout=20,
    )
    if fetched.returncode != 0 or len(fetched.stdout.strip()) < 24:
        raise RuntimeError(f"could not retrieve the broker token from {auth_host.name}")
    if token_path.is_file() and token_path.read_bytes() != fetched.stdout:
        backup = token_path.with_name(f"auth-broker.token.ompup-backup-{int(time.time())}")
        shutil.copy2(token_path, backup)
        backup.chmod(0o600)
    token_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = token_path.with_name(f".auth-broker.token.{os.getpid()}")
    temporary.write_bytes(fetched.stdout)
    temporary.chmod(0o600)
    os.replace(temporary, token_path)
    changed = configure_local_broker(config.broker_url)
    return config, changed


def select_hosts(hosts: list[HostConfig], values: Iterable[str], all_hosts: bool) -> list[HostConfig]:
    requested = list(values)
    if all_hosts or not requested:
        return hosts
    selected: list[HostConfig] = []
    for value in requested:
        host = find_host(hosts, value)
        if host not in selected:
            selected.append(host)
    return selected


def format_environment_status(status: EnvironmentStatus, expected: str = "") -> str:
    if not status.reachable:
        return f"{status.host.name}: unreachable ({status.error})"
    if not expected:
        if status.ok:
            return f"{status.host.name}: preserved ({status.omp_version})"
        return f"{status.host.name}: unavailable ({status.error or 'unknown'})"
    if status.ok and status.fingerprint == expected and status.omp_version == status.expected_omp_version:
        return f"{status.host.name}: ready {expected[:12]} ({status.omp_version})"
    details = []
    if status.error:
        details.append(status.error)
    if status.fingerprint != expected:
        details.append(f"fingerprint {status.fingerprint[:12] or 'missing'}")
    if status.omp_version != status.expected_omp_version:
        details.append(f"OMP {status.omp_version or 'missing'}")
    if status.mismatches:
        details.append(f"{len(status.mismatches)} changed target(s)")
    return f"{status.host.name}: drift ({', '.join(details) or 'unknown'})"
