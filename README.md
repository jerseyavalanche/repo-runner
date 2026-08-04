# repo-runner

Continuously discovers, analyzes, scores, sandboxes, and implements
open-source repositories into a controlled lab environment.

Runs on the Surface laptop (Docker isn't available on the Wi-Moto
phone's Termux/proot environment, which is why this moved here).
RuthChat (a separate project, `pocketos`) can query and drive this via
`/reporunner status|scan|submit` chat commands over a dedicated,
argument-validating SSH relay -- see that repo's `daemon.py` for the
wiring; nothing needs configuring here for it to work.

## The job pipeline

Every candidate repository is a row in one SQLite-backed job queue,
moving through a strict state machine:

```
discovered -> analyzed -> scored -> selected -> claimed -> running -> verified -> completed
                                                                    \-> failed (from any non-terminal state)
```

- **discovered**: inserted by `scan` (GitHub search) or `submit` (a
  specific repo named directly), identified immutably by
  `(full_name, commit_sha)`.
- **analyzed / scored**: `scan` walks discovered jobs through a
  heuristic 0-100 score (stars, push recency, description quality) and
  either selects or fails them against a threshold. `submit` skips
  scoring entirely and goes straight to selected -- naming a repo
  directly already implies it's vetted.
- **selected -> claimed -> running -> verified/failed -> completed**:
  the continuous `run` worker atomically claims one selected job at a
  time and hands it to `DockerSandboxWorker`, which shallow-fetches the
  exact commit and, if the repo defines its own Dockerfile, builds and
  runs it inside a disposable, network-isolated container (`--network
  none`, memory/CPU limits, read-only root filesystem with `/tmp` and
  `/run` as writable-but-isolated tmpfs). No Dockerfile is a real,
  non-error finding, not a failure. Both the image and cloned working
  tree are removed after every job.

## Install and initialize

```sh
python3.11 -m venv .venv   # StrEnum (lifecycle.py) needs 3.11+
. .venv/bin/activate
python -m pip install -e .
```

## Commands

```sh
# Run the continuous worker (claims + sandboxes selected jobs)
repo-runner run --database repo-runner.sqlite3

# Search GitHub and score the results in one pass
repo-runner scan --database repo-runner.sqlite3 --query "topic:ai-agents language:python" --limit 10

# Manually queue one specific repo, bypassing scoring
repo-runner submit --database repo-runner.sqlite3 owner/repository

# List recent jobs, optionally filtered by state
repo-runner status --database repo-runner.sqlite3 --state selected --limit 20
```

`run` stops with Ctrl-C or SIGTERM: it stops claiming immediately, waits
for active work up to its shutdown timeout, then safely releases
unfinished work under the normal retry limit.

### `run` configuration

| Option | Environment variable | Default |
| --- | --- | --- |
| `--database` | `REPO_RUNNER_DATABASE` | `repo-runner.sqlite3` |
| `--worker-id` | `REPO_RUNNER_WORKER_ID` | `<hostname>:<pid>` |
| `--poll-interval` | `REPO_RUNNER_POLL_INTERVAL` | 1 second |
| `--heartbeat-interval` | `REPO_RUNNER_HEARTBEAT_INTERVAL` | 10 seconds |
| `--stale-claim-timeout` | `REPO_RUNNER_STALE_CLAIM_TIMEOUT` | 60 seconds |
| `--stale-recovery-interval` | `REPO_RUNNER_STALE_RECOVERY_INTERVAL` | 30 seconds |
| `--graceful-shutdown-timeout` | `REPO_RUNNER_GRACEFUL_SHUTDOWN_TIMEOUT` | 30 seconds |

The stale-claim timeout must exceed the heartbeat interval. `scan` uses
`gh` (the GitHub CLI, already authenticated on this machine) rather than
a raw HTTP client -- no extra dependency.

## Private Telegram bridge to local Codex/Claude

`codex_telegram_bridge.py` is a separate, standalone component (not part
of the job pipeline) that bridges Telegram to a local `codex` or `claude`
CLI session using the existing logins. No API key, SDK, or REST
endpoint -- Telegram's Bot API is its only network transport. New turns
use `codex exec --json`; later turns use `codex exec resume` with an
atomically persisted session ID. Plain messages go to Claude by default
(full tool access); `/codex <message>` opts into Codex instead.

```sh
mkdir -p ~/.config/codex-telegram-bridge
cp .env.example ~/.config/codex-telegram-bridge/.env
chmod 600 ~/.config/codex-telegram-bridge/.env
.venv/bin/codex-telegram-bridge --env-file ~/.config/codex-telegram-bridge/.env
```

Never paste or commit the BotFather token. Only the configured private
user and chat are accepted. Commands are `/status`, `/new`, `/codex`,
and `/help`. Failed turns are reported without stopping the bridge.

## Verification

```sh
python -m unittest discover -s tests -v
python -m compileall -q src tests
python -m build
git diff --check
```
