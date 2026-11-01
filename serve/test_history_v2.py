"""Unified history + independent model KV namespace contract; CPU-only."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from serve.session_v1 import SessionV1

class TestHistoryV2(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store=SessionV1(self.tmp.name, max_snapshots=5)

    def save(self, ident, model):
        self.store.store_web(ident,[{"role":"user","text":ident}])
        path=self.store.snapshot_path(ident,model)
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(b"test-kv")
        self.store.registered_snapshot(ident,model,50)

    def test_independent_five_and_locked(self):
        for i in range(7):
            self.save("a_"+str(i),"gemma-fingerprint")
            self.save("b_"+str(i),"flash-fingerprint")
        self.assertEqual(self.store.snapshot_count("gemma-fingerprint"),5)
        self.assertEqual(self.store.snapshot_count("flash-fingerprint"),5)
        self.assertIsNone(self.store.restore_path("a_0","gemma-fingerprint"))
        self.assertIsNotNone(self.store.restore_path("b_6","flash-fingerprint"))
        self.store.lock_snapshot("a_4","gemma-fingerprint",True)
        self.save("a_7","gemma-fingerprint")
        self.assertEqual(self.store.snapshot_count("gemma-fingerprint"),5)  # locked KV is excluded
        self.assertTrue(self.store.get("a_4","gemma-fingerprint")["kv_locked_current"])
        self.assertFalse(self.store.get("a_4","flash-fingerprint")["kv_current"])
        self.assertIsNotNone(self.store.restore_path("a_4","gemma-fingerprint"))

    def test_same_conversation_separate_models_and_invalidation(self):
        self.save("shared","gemma")
        self.assertIsNone(self.store.restore_path("shared","flash"))
        p=self.store.snapshot_path("shared","flash")
        p.parent.mkdir(parents=True,exist_ok=True)
        p.write_bytes(b"flash-kv")
        self.store.registered_snapshot("shared","flash",60)
        self.assertIsNotNone(self.store.restore_path("shared","gemma"))
        self.assertIsNotNone(self.store.restore_path("shared","flash"))
        # Appending a turn keeps the prior KV prefix reusable by both models.
        self.store.store_web("shared",[{"role":"user","text":"shared"},
                                       {"role":"assistant","text":"reply"}])
        self.assertIsNotNone(self.store.restore_path("shared","gemma"))
        self.assertIsNotNone(self.store.restore_path("shared","flash"))
        self.store.store_web("shared",[{"role":"user","text":"edited later"}])
        self.assertIsNone(self.store.restore_path("shared","gemma"))
        self.assertIsNone(self.store.restore_path("shared","flash"))
        self.assertEqual(len(self.store.list()),1)

    def test_rename_pin_pending_lock_delete(self):
        self.store.store_web("chat1",[{"role":"user","text":"hello"}])
        self.store.store_web("chat2",[{"role":"user","text":"world"}])
        self.store.rename("chat1","Important")
        self.store.pin("chat1",True)
        self.assertEqual(self.store.list()[0]["id"],"chat1")
        record=self.store.lock_snapshot("chat1","model-A",True)
        self.assertTrue(record["kv_pending"])
        self.save("chat1","model-A")
        record=self.store.get("chat1","model-A")
        self.assertTrue(record["kv_locked"])
        self.assertFalse(record["kv_pending"])
        self.assertTrue(record["kv_current"])
        self.store.delete("chat1")
        self.assertIsNone(self.store.get("chat1"))
        self.assertFalse(self.store.snapshot_path("chat1","model-A").exists())

    def test_upgrade_legacy_0_byte_and_old_cache(self):
        # Current user's deleted 0-byte db should transparently initialize.
        with tempfile.TemporaryDirectory() as d:
            db=Path(d)/"conversations.sqlite3"
            db.touch()
            new=SessionV1(d)
            self.assertEqual(new.list(),[])
            new.store_web("fresh",[{"role":"user","text":"ok"}])
            self.assertEqual(new.get("fresh")["title"],"ok")

if __name__=="__main__":
    unittest.main()
