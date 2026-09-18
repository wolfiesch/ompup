# ompup

Jump from any local project into a persistent [Oh My Pi](https://github.com/can1357/oh-my-pi) session on the best available remote machine.

ompup probes configured hosts, honors project and workload affinity, pins the first placement, transfers Git commits and uncommitted state safely, attaches a project-named tmux session, and launches `omp`. Remote OMP configuration is preserved by default. An explicit `environment.mode: "mirror"` opt-in can align selected configuration, skills, extensions, plugins, authentication, and OMP versions. The `/ompup handoff` extension command also moves the current live conversation into remote tmux and replaces its cmux surface in place.

```bash
ompup                       # current Git project
ompup example-project           # named project from any directory
ompup --pick                # interactive project picker
ompup --cmux                # open or reuse a cmux workspace
/ompup handoff              # from inside local omp running in cmux
ompup env status --all      # inspect remote OMP without changing it
ompup env sync --all        # mirror mode only: align every reachable machine
ompup auth status           # optional: verify a configured credential broker
```

## How it works

```text
local Git commits + working-tree objects ────────> ~/Projects/<name>
local session JSONL + artifacts ──────────────> ~/.local/state/ompup/handoffs/...
                                                         │
                                                   tmux + omp --resume
                                                         │
                                                   cmux SSH surface
```

- Git negotiates commit and working-snapshot objects directly with temporary `refs/ompup/*` references, so subsequent transfers send only objects the destination lacks. Fast-forward commits transfer automatically in either direction; divergent history fails visibly.
- A temporary Git index captures tracked files plus untracked, non-ignored files as an exact tree without materializing a second filesystem copy. Dependency directories, build output, ignored files, and sensitive paths stay out of the snapshot.
- Every successful transfer records the exact Git HEAD and snapshot tree. A later transfer proceeds only when the destination still matches that baseline. Temporary transport references are removed after application. Conflicting edits and repository-name collisions fail visibly.
- The first transfer scans reachable history, each later commit transfer scans only incoming history, and every transferred working tree is represented as a synthetic root commit and scanned with `gitleaks`.
- An existing tmux session is reattached without synchronizing files beneath the running process. Use `ompup sync` explicitly when you want a later local change transferred.
- Host selection is sticky. Live capacity chooses the first placement; the project remains pinned until `ompup unpin`.
- A live handoff waits for OMP to become idle, syncs the project, copies the session and artifacts into a private remote state directory, verifies the checksum and remote export, and starts OMP in tmux. Only then does cmux replace the local surface.
- In `mirror` mode, an OMP launch or live handoff requires the selected machine to match the generated environment fingerprint and OMP version. Each managed tmux session records the fingerprint it started with. In the default `preserve` mode, ompup verifies that remote OMP launches but does not read or change its configuration.
- Commands for a selected host reuse a private, process-scoped SSH control connection for 60 seconds. Live-session files and artifacts still use resumable rsync over that connection.

## Requirements

- Local: Python 3.12 or newer, `ssh`, `git`, and `gitleaks`; live session handoff also requires `rsync`, mirror mode requires Mike Farah `yq` v4, and cmux is required for in-place handoff
- Remote: Python 3.7 or newer, Bash, `tmux`, `git`, and `omp`; live session handoff also requires `rsync`
- An SSH host or alias that reaches your box (an entry in `~/.ssh/config` works well)

## Install

### CLI

```bash
git clone https://github.com/wolfiesch/ompup.git
ln -s "$PWD/ompup/bin/ompup" ~/.local/bin/ompup   # or anywhere on PATH
mkdir -p ~/.config/ompup
```

### Oh My Pi extension

The same repo is an omp plugin. It adds `/ompup sync`, `/ompup pull`, `/ompup status`, and `/ompup handoff`. Handoff resumes the exact persisted conversation on the selected host, replaces the calling cmux surface with SSH attached to remote tmux, and shuts down the local OMP process.

```bash
omp plugin link ./ompup      # from the clone
```

Or point omp at it directly:

```bash
omp --extension ./ompup/extension/index.ts
```

## Usage

| Command | Effect |
|---|---|
| `ompup [PROJECT]` | Select a host, bootstrap or sync when needed, then attach tmux and launch omp |
| `ompup --pick` | Interactively choose a project |
| `ompup sync [PROJECT]` | Safely transfer fast-forward commits and local uncommitted state, no attach |
| `ompup pull [PROJECT]` | Safely transfer fast-forward commits and remote uncommitted state to local |
| `ompup status [PROJECT]` | Show selected host, capacity banner, Git synchronization, and tmux state |
| `ompup doctor [PROJECT]` | Probe every host and explain the selection |
| `ompup hosts` | Show live reachability, tools, platform, load, memory, disk, and latency |
| `ompup pin HOST [PROJECT]` | Pin a project to a configured host |
| `ompup unpin [PROJECT]` | Return a project to automatic placement |
| `ompup shell [PROJECT]` | Bootstrap or sync when needed, then attach a plain shell |
| `ompup [PROJECT] --cmux` | Open or reuse a named cmux workspace for the remote session |
| `/ompup handoff` | Wait for idle, transfer and verify this session, start remote OMP, then replace the current cmux surface |
| `/ompup handoff --host HOST` | Hand the session to a specific configured host |
| `ompup env status --all` | Compare the local shared-environment fingerprint with every configured host |
| `ompup env sync --all` | Update OMP, transfer the safe declarative environment, provision broker tokens over SSH, and verify each reachable host |
| `ompup auth setup` | Copy the broker bearer token over SSH into a local mode-0600 file and configure the broker URL |
| `ompup auth status` | Verify authenticated broker access without printing the token |
| `ompup auth migrate [--dry-run]` | Move local stored credentials, including OAuth accounts, into the broker |

## Host configuration

Create `~/.config/ompup/hosts.json`:

```json
{
  "hosts": [
    {
      "name": "compute",
      "ssh": "compute-box",
      "roles": ["general", "linux", "auth"],
      "reserve_gb": 40,
      "priority": 0,
      "launch": "omp"
    },
    {
      "name": "storage",
      "ssh": "storage-box",
      "roles": ["general", "linux", "storage"],
      "reserve_gb": 100,
      "priority": 10,
      "launch": "omp"
    },
    {
      "name": "mac",
      "ssh": "mac-worker",
      "roles": ["general", "macos", "arm64"],
      "reserve_gb": 25,
      "priority": 0,
      "remote_root": "Projects",
      "remote_agent_home": ".omp/agent",
      "remote_config_root": ".omp",
      "launch": "$HOME/.local/bin/omp"
    }
  ],
  "environment": {
    "mode": "preserve"
  }
}
```

Add machines by appending host objects. No source change is required.

Selection precedence:

1. Explicit `--host`
2. Project pin stored in local Git config
3. A unique live tmux session
4. A unique existing remote checkout
5. Capability and live-capacity score for new placement

The capacity score uses declared roles, minimum free-space reserves, free disk, normalized load, available memory, and optional priority. It is a placement heuristic, not a hardware benchmark. Common profiles are `general`, `linux`, `storage`, `macos`, and `services`; custom role names work without source changes. Swift and Xcode projects select `macos` automatically. Set a persistent override with `git config ompup.profile PROFILE`.

## OMP environment modes

The default mode is `preserve`. It checks that the selected host can launch OMP, then leaves that host's configuration, plugins, extensions, credentials, and installed version untouched. A hosts-only configuration therefore works without an auth broker:

```json
{
  "hosts": [{"name": "compute", "ssh": "compute-box"}]
}
```

Set `environment.mode` to `mirror` only when the local machine should manage remote OMP state:

```json
{
  "environment": {
    "mode": "mirror",
    "include_extensions": ["portable-status.ts"],
    "exclude_skills": ["private-*"],
    "plugins": ["portable-plugin"],
    "auth_host": "compute",
    "auth_broker_url": "https://broker.example.internal"
  }
}
```

Mirror mode installs a content-addressed release on each selected host. Existing target files move into timestamped backups before replacement. Only explicitly listed extensions and plugins transfer. Skills ignored by local OMP or matched by `exclude_skills` remain local. Common credential files, machine-local state, sessions, memories, caches, databases, MCP definitions, and model files remain local. Every release and project snapshot is scanned with `gitleaks`.

The broker fields are optional and must be configured together. When present, `ompup auth setup` retrieves the token over SSH and stores it under the configured OMP root with mode `0600`. The broker URL should be reachable only through a trusted private network or TLS.

Each host may override `remote_root`, `remote_agent_home`, and `remote_config_root`. All three are safe relative paths below remote `$HOME`. Environment policy is declarative; no extension, plugin, or skill name is hardcoded as portable.

Mirror rollout:

```bash
ompup auth setup             # only when broker fields are configured
ompup auth migrate --dry-run # optional broker migration
ompup auth migrate
ompup env sync --all
ompup env status --all
```

## Environment

| Variable | Default | Meaning |
|---|---|---|
| `OMPUP_CONFIG` | `~/.config/ompup/hosts.json` | Host inventory path |
| `OMPUP_HOST` | empty | Legacy single-host fallback; use `auto` with a host inventory |
| `OMPUP_PROJECTS_ROOT` | `~/Projects` | Directory searched by project names and the picker |
| `OMPUP_REMOTE_ROOT` | `Projects` | Directory under remote `$HOME` for checkouts |
| `OMPUP_CMD` | host `launch` value | One-invocation remote OMP command override |
| `OMPUP_EXCLUDES` | unsupported | Use `.gitignore` or `.git/info/exclude` |
| `OMPUP_AGENT_HOME` | `~/.omp/agent` | Local OMP agent configuration source used by mirror mode |
| `PI_CONFIG_DIR` | `~/.omp` | Local OMP configuration root and broker-token location |

## Notes

- Project names resolve from the current Git repository, an explicit path, or a case-insensitive directory under `OMPUP_PROJECTS_ROOT`.
- A directory that is not yet a Git repository is initialized automatically by `ompup`, `ompup sync`, `ompup shell`, and handoff. Home, `OMPUP_PROJECTS_ROOT`, and common personal directories are refused; create a project directory first.
- `/ompup` subcommands accept any unique prefix, such as `/ompup st` for status.
- The repository directory name also becomes the tmux session name.
- In sessions created by `ompup up`, quitting omp drops to a remote shell instead of killing tmux.
- Fast-forward commits and dirty working trees use negotiated Git object transfer. Divergent history requires normal Git reconciliation.
- `ompup pull` fetches the remote snapshot as Git objects and may delete local paths when applying its tree, but only when the local checkout exactly matches the recorded baseline.
- A successful handoff keeps the local session file as a rollback copy. A failed transfer, checksum, remote load, launch, or cmux replacement leaves local OMP running and removes only remote state created by that attempt.
- Handoff refuses while asynchronous jobs are running because their local processes cannot migrate with the session transcript.
- Handoff refuses to overwrite an existing project tmux session. Attach that session or select another host.
- If cmux cannot respawn the calling surface, ompup opens a focused fallback workspace and closes the old surface when cmux can identify it.

## License

MIT
