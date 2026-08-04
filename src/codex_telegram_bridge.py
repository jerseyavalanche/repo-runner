"""Private Telegram transport for the locally installed Codex and Claude CLIs."""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


class ConfigurationError(ValueError):
    pass


class CodexError(RuntimeError):
    pass


class ClaudeError(RuntimeError):
    pass


class TelegramError(RuntimeError):
    pass


def load_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ConfigurationError(f"{path}:{number}: expected NAME=value")
        name, value = line.split("=", 1)
        value = value.strip()
        if value[:1] in {"'", '"'} and value[-1:] == value[:1]:
            value = value[1:-1]
        values[name.strip()] = value
    return values


def positive_int(name: str, value: str) -> int:
    try:
        result = int(value)
    except ValueError as error:
        raise ConfigurationError(f"{name} must be an integer") from error
    if result <= 0:
        raise ConfigurationError(f"{name} must be positive")
    return result


@dataclass(frozen=True)
class Config:
    telegram_bot_token: str
    telegram_allowed_user_id: int
    telegram_allowed_chat_id: int
    codex_workspace: Path
    codex_binary: str
    codex_sandbox: str
    codex_timeout_seconds: int
    telegram_poll_timeout_seconds: int
    state_file: Path
    log_file: Path
    claude_workspace: Path
    claude_binary: str
    claude_timeout_seconds: int
    claude_state_file: Path

    @classmethod
    def load(cls, env_file: Path, environ: Mapping[str, str] | None = None) -> "Config":
        values = load_dotenv(env_file)
        values.update(os.environ if environ is None else environ)
        required = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_USER_ID",
                    "TELEGRAM_ALLOWED_CHAT_ID", "CODEX_WORKSPACE")
        missing = [name for name in required if not values.get(name)]
        if missing:
            raise ConfigurationError("missing configuration: " + ", ".join(missing))
        workspace = Path(values["CODEX_WORKSPACE"]).expanduser().resolve()
        if not workspace.is_dir():
            raise ConfigurationError(f"CODEX_WORKSPACE is not a directory: {workspace}")
        binary = values.get("CODEX_BINARY", "codex")
        if shutil.which(binary) is None:
            raise ConfigurationError(f"Codex executable not found: {binary}")
        sandbox = values.get("CODEX_SANDBOX", "workspace-write")
        if sandbox not in {"read-only", "workspace-write"}:
            raise ConfigurationError("CODEX_SANDBOX must be read-only or workspace-write")
        claude_binary = values.get("CLAUDE_BINARY", "claude")
        if shutil.which(claude_binary) is None:
            raise ConfigurationError(f"Claude executable not found: {claude_binary}")
        claude_workspace_raw = values.get("CLAUDE_WORKSPACE", values["CODEX_WORKSPACE"])
        claude_workspace = Path(claude_workspace_raw).expanduser().resolve()
        if not claude_workspace.is_dir():
            raise ConfigurationError(f"CLAUDE_WORKSPACE is not a directory: {claude_workspace}")
        return cls(
            values["TELEGRAM_BOT_TOKEN"],
            positive_int("TELEGRAM_ALLOWED_USER_ID", values["TELEGRAM_ALLOWED_USER_ID"]),
            positive_int("TELEGRAM_ALLOWED_CHAT_ID", values["TELEGRAM_ALLOWED_CHAT_ID"]),
            workspace,
            binary,
            sandbox,
            positive_int("CODEX_TIMEOUT_SECONDS", values.get("CODEX_TIMEOUT_SECONDS", "1800")),
            positive_int("TELEGRAM_POLL_TIMEOUT_SECONDS", values.get("TELEGRAM_POLL_TIMEOUT_SECONDS", "30")),
            Path(values.get("BRIDGE_STATE_FILE", "~/.local/state/codex-telegram/session.json")).expanduser(),
            Path(values.get("BRIDGE_LOG_FILE", "~/.local/state/codex-telegram/bridge.log")).expanduser(),
            claude_workspace,
            claude_binary,
            positive_int("CLAUDE_TIMEOUT_SECONDS", values.get("CLAUDE_TIMEOUT_SECONDS", "1800")),
            Path(values.get("CLAUDE_STATE_FILE", "~/.local/state/claude-telegram/session.json")).expanduser(),
        )


class SessionStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> str | None:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8")).get("thread_id")
        except (FileNotFoundError, OSError, json.JSONDecodeError, AttributeError):
            return None
        return value if isinstance(value, str) and value else None

    def save(self, thread_id: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{self.path.name}.", dir=self.path.parent, text=True
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump({"thread_id": thread_id}, stream)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


class CodexRunner:
    def __init__(self, config: Config, sessions: SessionStore, logger: logging.Logger) -> None:
        self.config, self.sessions, self.logger = config, sessions, logger
        self._lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None

    def command(self, thread_id: str | None) -> list[str]:
        command = [
            self.config.codex_binary, "--ask-for-approval", "never",
            "--sandbox", self.config.codex_sandbox, "--cd",
            str(self.config.codex_workspace), "exec",
        ]
        if thread_id:
            command.extend(["resume", "--json", thread_id, "-"])
        else:
            command.extend(["--json", "--color", "never", "-"])
        return command

    def run(self, prompt: str) -> str:
        if not prompt.strip():
            raise CodexError("empty prompt")
        with self._lock:
            thread_id = self.sessions.load()
            self.logger.info("starting Codex turn session=%s", thread_id or "new")
            try:
                process = subprocess.Popen(
                    self.command(thread_id), cwd=self.config.codex_workspace,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, bufsize=1,
                )
            except OSError as error:
                raise CodexError(f"could not start Codex CLI: {error}") from error
            self._process = process
            try:
                try:
                    stdout, stderr = process.communicate(
                        input=prompt, timeout=self.config.codex_timeout_seconds
                    )
                except subprocess.TimeoutExpired as error:
                    process.terminate()
                    try:
                        process.communicate(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.communicate()
                    raise CodexError(
                        f"Codex exceeded the {self.config.codex_timeout_seconds}-second timeout"
                    ) from error
                messages: list[str] = []
                for raw in stdout.splitlines():
                    try:
                        event = json.loads(raw)
                    except json.JSONDecodeError:
                        self.logger.warning("ignored non-JSON Codex output")
                        continue
                    if event.get("type") == "thread.started":
                        started = event.get("thread_id")
                        if isinstance(started, str):
                            self.sessions.save(started)
                    item = event.get("item")
                    if (event.get("type") == "item.completed"
                            and isinstance(item, dict)
                            and item.get("type") == "agent_message"
                            and isinstance(item.get("text"), str)):
                        messages.append(item["text"])
                code = process.returncode
                stderr = stderr.strip()
                if code:
                    raise CodexError(
                        f"Codex CLI failed: {stderr[-1500:] or f'exit status {code}'}"
                    )
                if not messages:
                    raise CodexError("Codex completed without an agent response")
                return "\n\n".join(messages)
            finally:
                self._process = None

    def stop(self) -> None:
        process = self._process
        if process is not None and process.poll() is None:
            process.terminate()


class ClaudeRunner:
    def __init__(self, config: Config, sessions: SessionStore, logger: logging.Logger) -> None:
        self.config, self.sessions, self.logger = config, sessions, logger
        self._lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None

    def command(self, session_id: str | None) -> list[str]:
        cmd = [
            self.config.claude_binary, "--print",
            "--output-format", "json",
            "--permission-mode", "bypassPermissions",
        ]
        if session_id:
            cmd.extend(["--resume", session_id])
        return cmd

    def run(self, prompt: str) -> str:
        if not prompt.strip():
            raise ClaudeError("empty prompt")
        with self._lock:
            session_id = self.sessions.load()
            self.logger.info("starting Claude turn session=%s", session_id or "new")
            try:
                process = subprocess.Popen(
                    self.command(session_id),
                    cwd=self.config.claude_workspace,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1,
                )
            except OSError as error:
                raise ClaudeError(f"could not start Claude CLI: {error}") from error
            self._process = process
            try:
                try:
                    stdout, stderr = process.communicate(
                        input=prompt, timeout=self.config.claude_timeout_seconds
                    )
                except subprocess.TimeoutExpired as error:
                    process.terminate()
                    try:
                        process.communicate(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.communicate()
                    raise ClaudeError(
                        f"Claude exceeded the {self.config.claude_timeout_seconds}-second timeout"
                    ) from error
                for raw in stdout.splitlines():
                    try:
                        event = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(event, dict):
                        continue
                    new_session = event.get("session_id")
                    if isinstance(new_session, str) and new_session:
                        self.sessions.save(new_session)
                    if event.get("type") == "result":
                        if event.get("is_error"):
                            raise ClaudeError(event.get("result") or "Claude returned an error")
                        result = event.get("result")
                        if isinstance(result, str):
                            return result
                code = process.returncode
                if code:
                    raise ClaudeError(stderr.strip()[-1500:] or f"Claude CLI exit status {code}")
                raise ClaudeError("Claude completed without a result")
            finally:
                self._process = None

    def stop(self) -> None:
        process = self._process
        if process is not None and process.poll() is None:
            process.terminate()


def split_message(text: str, limit: int = 4000) -> list[str]:
    parts: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit + 1)
        if cut < limit // 2:
            cut = limit
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text or not parts:
        parts.append(text)
    return parts


class TelegramClient:
    def __init__(self, token: str, api_root: str = "https://api.telegram.org") -> None:
        self.base_url = f"{api_root.rstrip('/')}/bot{token}"

    def call(self, method: str, payload: dict[str, Any]) -> Any:
        request = urllib.request.Request(
            f"{self.base_url}/{method}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request, timeout=int(payload.get("timeout", 30)) + 10
            ) as response:
                result = json.load(response)
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as error:
            raise TelegramError(f"Telegram request failed: {error}") from error
        if not result.get("ok"):
            raise TelegramError(result.get("description", "Telegram rejected request"))
        return result.get("result")

    def updates(self, offset: int | None, timeout: int) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"timeout": timeout, "allowed_updates": ["message"]}
        if offset is not None:
            payload["offset"] = offset
        result = self.call("getUpdates", payload)
        return result if isinstance(result, list) else []

    def send(self, chat_id: int, text: str) -> None:
        for part in split_message(text):
            self.call("sendMessage", {"chat_id": chat_id, "text": part})

    def typing(self, chat_id: int) -> None:
        self.call("sendChatAction", {"chat_id": chat_id, "action": "typing"})


class Bridge:
    def __init__(self, config: Config, telegram: TelegramClient,
                 codex: CodexRunner, claude: ClaudeRunner, logger: logging.Logger) -> None:
        self.config, self.telegram, self.codex, self.claude, self.logger = (
            config, telegram, codex, claude, logger
        )
        self.stopping = threading.Event()
        self.offset: int | None = None

    def stop(self) -> None:
        self.stopping.set()
        self.codex.stop()
        self.claude.stop()

    def authorized(self, update: dict[str, Any]) -> tuple[int, str] | None:
        message = update.get("message")
        if not isinstance(message, dict):
            return None
        sender, chat, text = message.get("from"), message.get("chat"), message.get("text")
        if not isinstance(sender, dict) or not isinstance(chat, dict) or not isinstance(text, str):
            return None
        user_id, chat_id = sender.get("id"), chat.get("id")
        if (chat.get("type") != "private"
                or user_id != self.config.telegram_allowed_user_id
                or chat_id != self.config.telegram_allowed_chat_id):
            self.logger.warning("rejected message user_id=%s chat_id=%s", user_id, chat_id)
            return None
        return chat_id, text

    def handle(self, chat_id: int, text: str) -> None:
        parts = text.strip().split(maxsplit=1)
        command = parts[0].lower()
        rest = parts[1] if len(parts) > 1 else ""

        if command == "/new":
            self.codex.sessions.clear()
            self.claude.sessions.clear()
            self.telegram.send(chat_id, "Started fresh sessions for both Claude and Codex.")
            return

        if command == "/status":
            codex_session = self.codex.sessions.load()
            claude_session = self.claude.sessions.load()
            self.telegram.send(
                chat_id,
                f"Claude workspace: {self.config.claude_workspace}\n"
                f"Claude session: {claude_session or 'new'}\n\n"
                f"Codex workspace: {self.config.codex_workspace}\n"
                f"Codex session: {codex_session or 'new'}\n"
                f"Codex sandbox: {self.config.codex_sandbox}"
            )
            return

        if command in {"/start", "/help"}:
            self.telegram.send(
                chat_id,
                "Messages go to Claude by default (full tool access).\n"
                "/codex <message> — send to Codex instead\n"
                "/new — reset both sessions\n"
                "/status — show current sessions"
            )
            return

        if command == "/codex":
            if not rest:
                self.telegram.send(chat_id, "Usage: /codex <your message>")
                return
            self.telegram.typing(chat_id)
            try:
                self.telegram.send(chat_id, self.codex.run(rest))
            except CodexError as error:
                self.logger.exception("Codex turn failed")
                self.telegram.send(chat_id, f"Codex error: {error}")
            return

        # Default: route to Claude
        self.telegram.typing(chat_id)
        try:
            self.telegram.send(chat_id, self.claude.run(text))
        except ClaudeError as error:
            self.logger.exception("Claude turn failed")
            self.telegram.send(chat_id, f"Claude error: {error}")

    def run(self) -> int:
        self.logger.info("bridge started workspace=%s", self.config.codex_workspace)
        backoff = 1.0
        while not self.stopping.is_set():
            try:
                updates = self.telegram.updates(
                    self.offset, self.config.telegram_poll_timeout_seconds
                )
                backoff = 1.0
                for update in updates:
                    update_id = update.get("update_id")
                    if isinstance(update_id, int):
                        self.offset = update_id + 1
                    message = self.authorized(update)
                    if message:
                        self.handle(*message)
            except TelegramError as error:
                self.logger.error("Telegram error: %s", error)
                self.stopping.wait(backoff)
                backoff = min(backoff * 2, 30)
            except Exception:
                self.logger.exception("unexpected bridge error")
                self.stopping.wait(backoff)
                backoff = min(backoff * 2, 30)
        self.logger.info("bridge stopped")
        return 0


def configure_logging(path: Path) -> logging.Logger:
    path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[logging.FileHandler(path), logging.StreamHandler()],
        force=True,
    )
    return logging.getLogger("codex_telegram_bridge")


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="codex-telegram-bridge")
    parser.add_argument(
        "--env-file", type=Path,
        default=Path("~/.config/codex-telegram-bridge/.env").expanduser(),
    )
    options = parser.parse_args(arguments)
    try:
        config = Config.load(options.env_file)
    except ConfigurationError as error:
        parser.error(str(error))
    logger = configure_logging(config.log_file)
    sessions = SessionStore(config.state_file)
    codex = CodexRunner(config, sessions, logger)
    claude_sessions = SessionStore(config.claude_state_file)
    claude = ClaudeRunner(config, claude_sessions, logger)
    bridge = Bridge(config, TelegramClient(config.telegram_bot_token), codex, claude, logger)
    signal.signal(signal.SIGINT, lambda *_: bridge.stop())
    signal.signal(signal.SIGTERM, lambda *_: bridge.stop())
    return bridge.run()
