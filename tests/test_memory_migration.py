import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from migrate_wechat_memory import migrate
from social_bot.store import ConversationStore


class MigrationTests(unittest.TestCase):
    def test_legacy_history_and_group_states_are_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, target, config = root / "old.sqlite3", root / "new.sqlite3", root / "config.json"
            db = sqlite3.connect(source)
            db.executescript("""CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT,
                role TEXT, content TEXT, message_id TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE group_states (platform TEXT, group_id TEXT, active INTEGER,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(platform,group_id));
                INSERT INTO messages(session_id,role,content,message_id) VALUES ('wechat:group:g','user','旧消息','old');
                INSERT INTO group_states(platform,group_id,active) VALUES ('wechat','g',1);""")
            db.commit()
            db.close()
            original = json.dumps({"runtime": {"database_path": "old.sqlite3"}}).encode()
            config.write_bytes(original)
            report = migrate(config, target)
            self.assertEqual(report["group_messages"], 1)
            self.assertEqual(Path(report["config_backup"]).read_bytes(), original)
            self.assertTrue(json.loads(config.read_text(encoding="utf-8"))["runtime"]["wechat_group_memory_enabled"])
            store = ConversationStore(target)
            self.assertTrue(store.group_active("wechat", "g"))
            self.assertEqual(store.history("wechat:group:g", 20), [{"role": "user", "content": "旧消息"}])
            store.close()
            with self.assertRaises(ValueError):
                migrate(config, target)
            self.assertTrue(source.exists())

    def test_existing_destination_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "source").touch()
            target = root / "target"
            target.write_text("keep")
            config = root / "config.json"
            config.write_text(json.dumps({"runtime": {"database_path": "source"}}))
            with self.assertRaises(FileExistsError):
                migrate(config, target)
            self.assertEqual(target.read_text(), "keep")
