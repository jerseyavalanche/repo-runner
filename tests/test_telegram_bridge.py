import logging
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_telegram_bridge import (
    Bridge,
    CodexError,
    CodexRunner,
    Config,
    ConfigurationError,
    SessionStore,
    load_dotenv,
    split_message,
)


class RecordingTelegram:
    def __init__(self):
        self.sent = []
        self.typed = []

    def send(self, chat_id, text):
        self.sent.append((chat_id, text))

    def typing(self, chat_id):
        self.typed.append(chat_id)


class RecordingCodex:
    def __init__(self, sessions):
        self.sessions = sessions
        self.prompts = []

    def run(self, prompt):
        self.prompts.append(prompt)
        return "answer"

    def stop(self):
        pass


class TelegramBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.logger = logging.getLogger(f"bridge-test-{id(self)}")
        self.logger.addHandler(logging.NullHandler())

    def tearDown(self):
        self.temp.cleanup()

    def config(self, binary="codex"):
        return Config(
            "secret", 10, 20, self.root, binary, "workspace-write",
            30, 1, self.root / "state.json", self.root / "bridge.log",
            self.root, "claude", 30, self.root / "claude-state.json",
        )

    def test_session_store_is_atomic_and_private(self):
        store = SessionStore(self.root / "state" / "session.json")
        store.save("thread-1")
        self.assertEqual(store.load(), "thread-1")
        self.assertEqual(stat.S_IMODE(store.path.stat().st_mode), 0o600)
        store.clear()
        self.assertIsNone(store.load())

    def test_codex_json_events_are_parsed_and_resume_is_selected(self):
        executable = self.root / "fake-codex"
        executable.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' "
            "'{\"type\":\"thread.started\",\"thread_id\":\"abc\"}' "
            "'{\"type\":\"item.completed\",\"item\":"
            "{\"type\":\"agent_message\",\"text\":\"hello\"}}'\n",
            encoding="utf-8",
        )
        executable.chmod(0o755)
        config = self.config(str(executable))
        store = SessionStore(config.state_file)
        runner = CodexRunner(config, store, self.logger)
        self.assertEqual(runner.run("first"), "hello")
        self.assertEqual(store.load(), "abc")
        self.assertIn("resume", runner.command("abc"))
        self.assertIn("--ask-for-approval", runner.command(None))
        self.assertNotIn("danger-full-access", runner.command(None))

    def test_codex_failure_is_reported(self):
        executable = self.root / "bad-codex"
        executable.write_text(
            "#!/bin/sh\necho broken >&2\nexit 7\n", encoding="utf-8"
        )
        executable.chmod(0o755)
        config = self.config(str(executable))
        runner = CodexRunner(
            config, SessionStore(config.state_file), self.logger
        )
        with self.assertRaisesRegex(CodexError, "broken"):
            runner.run("prompt")

    def test_private_allowlist_and_message_flow(self):
        config = self.config()
        telegram = RecordingTelegram()
        codex = RecordingCodex(SessionStore(config.state_file))
        claude = RecordingCodex(SessionStore(config.claude_state_file))
        bridge = Bridge(config, telegram, codex, claude, self.logger)
        allowed = {
            "message": {
                "from": {"id": 10},
                "chat": {"id": 20, "type": "private"},
                "text": "hello",
            }
        }
        denied = {
            "message": {
                "from": {"id": 11},
                "chat": {"id": 20, "type": "private"},
                "text": "hello",
            }
        }
        self.assertEqual(bridge.authorized(allowed), (20, "hello"))
        self.assertIsNone(bridge.authorized(denied))
        bridge.handle(20, "hello")
        # Plain messages (no prefix) go to Claude by default; /codex opts
        # into Codex instead (see Bridge.handle's /help text).
        self.assertEqual(claude.prompts, ["hello"])
        self.assertEqual(codex.prompts, [])
        self.assertEqual(telegram.sent[-1], (20, "answer"))

    def test_new_clears_context(self):
        config = self.config()
        sessions = SessionStore(config.state_file)
        sessions.save("old")
        telegram = RecordingTelegram()
        claude = RecordingCodex(SessionStore(config.claude_state_file))
        bridge = Bridge(
            config, telegram, RecordingCodex(sessions), claude, self.logger
        )
        bridge.handle(20, "/new")
        self.assertIsNone(sessions.load())

    def test_message_splitting_preserves_content(self):
        text = "x" * 9001
        parts = split_message(text)
        self.assertEqual("".join(parts), text)
        self.assertTrue(all(len(part) <= 4000 for part in parts))

    def test_dotenv_and_safe_config(self):
        env_file = self.root / ".env"
        env_file.write_text("TOKEN='quoted'\nexport VALUE=ok\n", encoding="utf-8")
        self.assertEqual(
            load_dotenv(env_file), {"TOKEN": "quoted", "VALUE": "ok"}
        )
        values = {
            "TELEGRAM_BOT_TOKEN": "token",
            "TELEGRAM_ALLOWED_USER_ID": "10",
            "TELEGRAM_ALLOWED_CHAT_ID": "20",
            "CODEX_WORKSPACE": str(self.root),
        }
        with patch("codex_telegram_bridge.shutil.which", return_value="/usr/bin/codex"):
            config = Config.load(env_file, values)
        self.assertEqual(config.codex_sandbox, "workspace-write")
        values["CODEX_SANDBOX"] = "danger-full-access"
        with patch("codex_telegram_bridge.shutil.which", return_value="/usr/bin/codex"):
            with self.assertRaises(ConfigurationError):
                Config.load(env_file, values)


if __name__ == "__main__":
    unittest.main()
