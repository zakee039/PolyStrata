"""V1 durable conversation and bounded snapshot metadata tests. No GPU required."""
import tempfile
import unittest

from serve.session_v1 import SessionV1, valid_id


class TestSessionV1(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = SessionV1(self.tmp.name, max_snapshots=5)

    def _snapshot(self, ident, model):
        path = self.store.snapshot_path(ident, model)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def test_web_and_api_same_record(self):
        self.store.store_web("shared_1", [{"role": "user", "text": "web content"}])
        self.store.store_api("shared_1", [{"role": "user", "content": "api content"}])
        self.store.store_reply("shared_1", [{"role": "user", "content": "api content"}], "answer")
        item = self.store.get("shared_1")
        self.assertEqual(item["web_messages"][0]["text"], "web content")
        self.assertEqual(item["api_messages"][-1]["content"], "answer")
        self.assertEqual(len(self.store.list()), 1)

    def test_fifo_does_not_delete_chat(self):
        for n in range(6):
            ident = f"chat_{n}"
            self.store.store_web(ident, [{"role": "user", "text": ident}])
            self._snapshot(ident, "same-model").write_bytes(b"dummy snapshot")
            self.store.registered_snapshot(ident, "same-model", 1024)
        self.assertEqual(self.store.snapshot_count(), 5)
        self.assertFalse(self._snapshot("chat_0", "same-model").exists())
        self.assertTrue(self.store.get("chat_0")["web_messages"])
        self.assertFalse(self.store.get("chat_0")["kv_cached"])
        self._snapshot("chat_0", "same-model").write_bytes(b"fresh")
        self.store.registered_snapshot("chat_0", "same-model", 2048)
        self.assertEqual(self.store.snapshot_count(), 5)
        self.assertFalse(self._snapshot("chat_1", "same-model").exists())
        self.assertTrue(self.store.get("chat_1")["web_messages"])

    def test_restart_and_incompatible_snapshot(self):
        self.store.store_web("same_1", [{"role": "user", "text": "saved"}])
        self._snapshot("same_1", "v1").write_bytes(b"placeholder")
        self.store.registered_snapshot("same_1", "v1", 500)
        reopened = SessionV1(self.tmp.name)
        self.assertEqual(reopened.get("same_1")["web_messages"][0]["text"], "saved")
        self.assertIsNone(reopened.restore_path("same_1", "v2"))
        self.assertIsNotNone(reopened.restore_path("same_1", "v1"))
        reopened.abandon_snapshot("same_1")
        self.assertIsNone(reopened.restore_path("same_1", "v1"))

    def test_service_switch_and_restart(self):
        from pathlib import Path
        from serve.server import Service

        class Native:
            gen = 1
            spawn = ("missing-engine.exe", ["--model", "missing-model.gguf"])
            def __init__(self):
                self.actions = []
            def session_file(self, action, path):
                self.actions.append((action, Path(path).name))
                if action == "save":
                    Path(path).write_bytes(b"VALID")
                else:
                    assert Path(path).read_bytes() == b"VALID"
                return {"tokens": 128, "ms": 0.1, "bytes": 5}

        svc = Service.__new__(Service)
        svc.engine = Native()
        svc.model = "test-model"
        svc.session_v1 = self.store
        svc.session_v1_kv_enabled = True
        svc.loaded = lambda: True
        for ident in ("first", "second"):
            self.store.store_web(ident, [{"role": "user", "text": ident}])
            svc._v1_before_generate({"strata_session_id": ident})
            self.store.active_dirty = True
        self.assertEqual(svc.engine.actions, [("save", "first.bin")])
        svc.engine.gen += 1
        svc._v1_before_generate({"strata_session_id": "first"})
        self.assertEqual(svc.engine.actions[-1], ("restore", "first.bin"))
        self.assertEqual(self.store.get("second")["web_messages"][0]["text"], "second")

    def test_image_and_attachment_files_are_durable(self):
        import base64
        from pathlib import Path
        raw = b"\x89PNG\r\n\x1a\n" + b"minimal-image-test-data"
        url = "data:image/png;base64," + base64.b64encode(raw).decode()
        ident = "with_images"
        message = {"role": "user", "text": "Photo with a note",
                   "images": [{"kind": "image", "name": "example.png", "url": url}],
                   "files": [{"kind": "file", "name": "note.txt", "text": "Hello attached text!"}]}
        self.store.store_web(ident, [message])
        self.store.store_api(ident, [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": url}},
            {"type": "text", "text": "describe it"}]}])
        root = Path(self.tmp.name)
        images = list((root / "images" / ident).glob("*.png"))
        texts = list((root / "attachments" / ident).glob("*.txt"))
        self.assertEqual(len(images), 1)
        self.assertEqual(len(texts), 1)
        self.assertEqual(images[0].read_bytes(), raw)
        self.assertEqual(texts[0].read_text(encoding="utf-8"), "Hello attached text!")
        db = self.store.db_path.read_bytes()
        self.assertNotIn(base64.b64encode(raw), db)
        reopened = SessionV1(self.tmp.name)
        self.assertEqual(reopened.get(ident)["web_messages"][0]["images"][0]["url"], url)
        self.assertEqual(reopened.get(ident)["web_messages"][0]["files"][0]["text"], "Hello attached text!")
        self.assertEqual(reopened.get(ident)["api_messages"][0]["content"][0]["image_url"]["url"], url)

    def test_malformed_images_refused(self):
        ident = "bad_image"
        with self.assertRaises(ValueError):
            self.store.store_web(ident, [{"role": "user", "images": [
                {"name": "fake.png", "url": "data:image/png;base64,YWJj"}]}])

    def test_deleting_chat_removes_its_generated_pictures_but_not_images_tab(self):
        generated = self.store.generated_dir
        image = "a" * 32 + ".png"
        original = "b" * 32 + ".png"
        old_swipe = "c" * 32 + ".png"
        gallery = "d" * 32 + ".png"
        for name in (image, original, old_swipe, gallery):
            (generated / name).write_bytes(b"test PNG")
        self.store.store_web("chat_own", [
            {"role": "user", "text": "Draw an illustration"},
            {"role": "assistant", "text": "Done", "tools": [
                {"name": "polystrata__generate_image", "image_file": image,
                 "image_original_file": original}],
             "swipes": [{"tools": [
                 {"name": "polystrata__generate_image", "image_file": old_swipe}]}]}])
        self.store.delete("chat_own")
        for name in (image, original, old_swipe):
            self.assertFalse((generated / name).exists(), name)
        self.assertTrue((generated / gallery).exists())

    def test_deleting_chat_preserves_generated_images_shared_with_other_chat(self):
        name = "e" * 32 + ".png"
        png = self.store.generated_dir / name
        png.write_bytes(b"shared PNG")
        self.store.store_web("owner", [
            {"role": "assistant", "text": "Generated", "tools": [
                {"name": "polystrata__generate_image", "image_file": name}]}])
        self.store.store_web("other", [
            {"role": "user", "text": "See v1/images/files/" + name}])
        self.store.delete("owner")
        self.assertTrue(png.exists())
        self.assertIsNotNone(self.store.get("other"))

    def test_session_delete_does_not_trust_user_image_fields_or_paths(self):
        gallery = "f" * 32 + ".png"
        png = self.store.generated_dir / gallery
        png.write_bytes(b"gallery PNG")
        self.store.store_web("untrusted", [
            {"role": "user", "image_file": gallery},
            {"role": "assistant", "tools": [
                {"name": "unrelated_tool", "image_file": gallery},
                {"name": "polystrata__generate_image", "image_file": "../" + gallery}]}])
        self.store.delete("untrusted")
        self.assertTrue(png.exists())

    def test_recent_picture_is_conversation_scoped_and_ignores_swipes(self):
        a, b, stale = ("a" * 32 + ".png", "b" * 32 + ".png", "c" * 32 + ".png")
        for name in (a, b, stale):
            (self.store.generated_dir / name).write_bytes(b"png bytes")
        self.store.store_web("chat_a", [
            {"role": "user", "text": "draw"},
            {"role": "assistant", "text": "ready", "tools": [
                {"name": "polystrata__generate_image", "ok": True, "image_file": a}],
             "swipes": [{"tools": [
                 {"name": "polystrata__generate_image", "ok": True, "image_file": stale}]}]},
            {"role": "user", "text": "make the eyes bigger"}])
        self.store.store_web("chat_b", [
            {"role": "user", "text": "draw a different picture"},
            {"role": "assistant", "tools": [
                {"name": "polystrata__generate_image", "ok": True, "image_file": b}]}])
        self.store.register_generated_image("chat_a", a)
        self.store.register_generated_image("chat_b", b)
        x = self.store.recent_generated_image("chat_a")
        self.assertEqual(x["image_file"], a)
        self.assertEqual(x["user_turns_since"], 1)
        self.assertEqual(self.store.recent_generated_image("chat_b")["image_file"], b)
        self.assertIsNone(self.store.recent_generated_image("unknown_chat"))
        (self.store.generated_dir / a).unlink()
        self.assertIsNone(self.store.recent_generated_image("chat_a"))

    def test_recent_picture_rejects_user_paths_or_untrusted_tool_outputs(self):
        target = "e" * 32 + ".png"
        (self.store.generated_dir / target).write_bytes(b"private picture")
        self.store.store_web("malicious", [
            {"role": "user", "text": "image", "image_file": target},
            {"role": "assistant", "tools": [
                {"name": "other_tool", "ok": True, "image_file": target},
                {"name": "polystrata__generate_image", "ok": True, "image_file": "../" + target},
                {"name": "polystrata__generate_image", "ok": False, "image_file": target}]}])
        self.assertIsNone(self.store.recent_generated_image("malicious"))
        with self.assertRaises(ValueError):
            self.store.register_generated_image("malicious", "../" + target)
        with self.assertRaises(FileNotFoundError):
            self.store.register_generated_image("malicious", "9" * 32 + ".png")

    def test_recent_picture_registration_cleans_on_delete_but_keeps_other_owner(self):
        name = "7" * 32 + ".png"
        pic = self.store.generated_dir / name
        pic.write_bytes(b"png")
        self.store.register_generated_image("first", name)
        self.store.register_generated_image("second", name)
        self.store.delete("first")
        self.assertTrue(pic.exists())
        self.assertEqual(self.store.recent_generated_image("second")["image_file"], name)
        self.store.delete("second")
        self.assertFalse(pic.exists())

    def test_invalid_identifiers(self):
        for ident in ("../escape", "/tmp/p", "a.b", "", "a" * 65):
            with self.assertRaises(ValueError):
                valid_id(ident)

    def test_restart_reclaims_image_turn_snapshots_without_touching_pictures(self):
        from pathlib import Path
        root = Path(self.tmp.name)
        current_dir = root / "image-session-cache"
        current_dir.mkdir(parents=True, exist_ok=True)
        recent = current_dir / ("llm-session-" + "b" * 12 + ".bin")
        picture = root / "generated-images" / ("c" * 32 + ".png")
        for file in (recent, picture):
            file.write_bytes(b"fixture")
        SessionV1(self.tmp.name)
        self.assertFalse(recent.exists())
        self.assertTrue(picture.exists())


if __name__ == "__main__":
    unittest.main()
