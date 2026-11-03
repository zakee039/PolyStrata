"""A conversation has one server-side sampling profile, independent of browser."""
import sqlite3
import tempfile
import unittest

from serve.session_v1 import SessionV1


def sample(thinking="low", budget=1024):
    return dict(thinking=thinking, budget=budget, temperature=1.0, top_p=0.95,
                top_k=20, max="", seed="", show=True, esp=True, mcp=True)


class SessionSampling(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.store = SessionV1(self.folder.name)

    def test_isolation_and_new_session_defaults(self):
        self.store.set_sampling("chat_a", sample("high", 3072))
        self.store.set_sampling("chat_b", sample("medium", 4096))
        self.assertEqual(self.store.get("chat_a")["sampling"]["budget"], 3072)
        self.assertEqual(self.store.get("chat_b")["sampling"]["budget"], 4096)
        self.store.store_web("chat_c", [{"role": "user", "text": "fresh"}])
        self.assertEqual(self.store.get("chat_c")["sampling"], {})
        self.assertEqual(SessionV1(self.folder.name).get("chat_a")["sampling"]["budget"], 3072)

    def test_does_not_invalidate_kv(self):
        self.store.store_web("chat_kv", [{"role": "user", "text": "Hello"}])
        path = self.store.snapshot_path("chat_kv", "model")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"snapshot")
        self.store.registered_snapshot("chat_kv", "model", 30)
        before = self.store.get("chat_kv")["content_rev"]
        self.store.set_sampling("chat_kv", sample("high", 16388))
        self.assertEqual(self.store.get("chat_kv")["content_rev"], before)
        self.assertEqual(self.store.restore_path("chat_kv", "model"), str(path))

    def test_bad_settings_rejected(self):
        for altered in ({"budget": -1}, {"thinking": "none", "budget": 12},
                        {"temperature": 4}, {"max": -9}, {"seed": 3.14},
                        {"mcp": 1}, {"not_a_setting": 1}):
            data = sample()
            data.update(altered)
            with self.subTest(altered=altered), self.assertRaises(ValueError):
                self.store.set_sampling("bad", data)
        self.assertIsNone(self.store.get("bad"))

    def test_existing_conversation_schema_is_upgraded_without_deletion(self):
        with self.store._db() as db:
            db.execute("DROP TABLE conversations")
            db.execute("""CREATE TABLE conversations
                (id TEXT PRIMARY KEY, title TEXT NOT NULL, created REAL NOT NULL,
                 updated REAL NOT NULL, api_messages TEXT NOT NULL DEFAULT '[]',
                 web_messages TEXT NOT NULL DEFAULT '[]', pinned INTEGER NOT NULL DEFAULT 0,
                 content_rev INTEGER NOT NULL DEFAULT 0)""")
            db.execute("INSERT INTO conversations(id,title,created,updated) VALUES('old','old',1,1)")
        upgraded = SessionV1(self.folder.name)
        self.assertEqual(upgraded.get("old")["sampling"], {})
        upgraded.set_sampling("old", sample("low", 999))
        self.assertEqual(upgraded.get("old")["sampling"]["budget"], 999)


if __name__ == "__main__":
    unittest.main()
