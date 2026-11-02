import base64
import io
import json
import tempfile
import unittest

from serve.character_cards import (
    CharacterStore, card_from_tavern, card_to_tavern, compose, valid_card,
)


class CharacterTests(unittest.TestCase):
    def sample(self):
        return {"name": "Miku 老师", "description": "教师", "personality": "认真、亲切",
                "scenario": "教室", "first_mes": "欢迎上课",
                "mes_example": "{{user}}: 你好\n{{char}}: 欢迎",
                "lorebook": [
                    {"keys": "樱花, cherry", "content": "今天是春天", "constant": False},
                    {"keys": "", "content": "始终保持友好", "constant": True},
                ]}

    def test_reference_image_upload_roundtrip_and_restrictions(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as root:
            store = CharacterStore(root)
            card = store.save({"name":"Test Miku"})
            image = Image.new("RGB", (320, 640), (30, 195, 190))
            image_data = io.BytesIO()
            image.save(image_data, format="PNG")
            uploaded = store.upload_reference(card["id"], base64.b64encode(image_data.getvalue()).decode())
            self.assertTrue(uploaded["reference_image"])
            file = store.image_file(card["id"])
            self.assertTrue(file and file.is_file())
            with Image.open(file) as actual:
                self.assertEqual(actual.size, (320, 640))
            edited = store.save({"id":card["id"],"name":"Updated Miku"})
            self.assertTrue(edited["reference_image"])
            self.assertTrue(store.get(card["id"])["reference_image"])
            with self.assertRaises(ValueError):
                store.upload_reference(card["id"], "NOT BASE64")
            with self.assertRaises(ValueError):
                store.upload_reference("..\\unsafe", "AA==")
            self.assertFalse(store.remove_reference(card["id"])["reference_image"])
            self.assertIsNone(store.image_file(card["id"]))
            self.assertFalse(file.exists())
            store.upload_reference(card["id"], base64.b64encode(image_data.getvalue()).decode())
            store.remove(card["id"])
            self.assertFalse(file.exists())

    def test_persistence_assignment_and_deletion(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = CharacterStore(tmp)
            self.assertEqual(db.snapshot(), {"cards":[], "assignments":{}, "global":{
                "user_name":"", "user_description":"", "default_role":"stateless", "chat_image_profile":"default"}})
            card = db.save(self.sample())
            db.set_global({"user_name":"Zakee", "user_description":"喜欢编程", "default_role":"ordinary"})
            db.assign("a" * 32, card["id"])
            saved = CharacterStore(tmp).snapshot()
            self.assertEqual(saved["global"], {"user_name":"Zakee", "user_description":"喜欢编程",
                                              "default_role":"ordinary", "chat_image_profile":"default"})
            self.assertEqual(saved["assignments"]["a" * 32], card["id"])
            self.assertEqual(saved["cards"][0]["name"], "Miku 老师")
            db.remove(card["id"])
            self.assertEqual(CharacterStore(tmp).snapshot()["assignments"], {})
            self.assertEqual(CharacterStore(tmp).snapshot()["cards"], [])

    def test_builtin_roles_and_persona_modes(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = CharacterStore(tmp)
            for role in ("stateless", "ordinary"):
                db.assign("session_"+role, role)
                self.assertEqual(db.snapshot()["assignments"]["session_"+role], role)
                self.assertIsNone(db.assigned_card("session_"+role))
            card = db.save({"name":"Miku", "persona_mode":"independent",
                            "user_name":"主人", "user_description":"住在青海"})
            self.assertEqual(db.get(card["id"])["persona_mode"], "independent")
            self.assertEqual(db.get(card["id"])["user_name"], "主人")
            db.set_global({"user_name":"Zakee", "user_description":"喜欢编程",
                           "default_role":card["id"]})
            self.assertEqual(db.snapshot()["global"]["default_role"], card["id"])
            db.remove(card["id"])
            self.assertEqual(db.snapshot()["global"]["default_role"], "stateless")
            for invalid in ("unknown", "../../file", "e" * 32):
                with self.assertRaises(ValueError):
                    db.set_global({"default_role": invalid})
            with self.assertRaises(ValueError):
                db.save({"name":"Oops", "persona_mode":"unknown"})

    def test_chat_image_profile_global_persistence_and_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CharacterStore(tmp)
            allowed = {"default", "qwen-image-2.1-turbo-official",
                       "qwen-image-2.1-turbo-uncensored", "animagine-xl-4-opt", "sdxl-base-1"}
            self.assertEqual(store.chat_image_profile(), "default")
            saved = store.set_global({"user_name":"u", "user_description":"",
                "default_role":"ordinary", "chat_image_profile":"sdxl-base-1"},
                allowed_image_profiles=allowed)
            self.assertEqual(saved["chat_image_profile"], "sdxl-base-1")
            self.assertEqual(CharacterStore(tmp).chat_image_profile(), "sdxl-base-1")
            store.set_global({"user_name":"u2", "default_role":"stateless"},
                             allowed_image_profiles=allowed)
            self.assertEqual(store.snapshot()["global"]["chat_image_profile"], "sdxl-base-1")
            for invalid in ("../../foo", "not-configured", 1, ""):
                with self.assertRaises(ValueError):
                    store.set_global({"chat_image_profile": invalid}, allowed_image_profiles=allowed)
            self.assertEqual(store.chat_image_profile(), "sdxl-base-1")

    def test_card_roundtrip(self):
        x = valid_card(self.sample())
        imported = card_from_tavern(card_to_tavern(x))
        for key in ("name", "description", "personality", "scenario",
                    "first_mes", "mes_example", "lorebook"):
            self.assertEqual(imported[key], x[key], key)

    def test_worldbook_and_workflow_are_scoped_to_each_character(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = CharacterStore(tmp)
            a = db.save({"name": "Character A",
                         "lorebook": [{"keys": "alpha", "content": "A-only", "constant": False}],
                         "sources": [{"kind": "directory", "path": tmp + "/a", "recursive": True}],
                         "workflow_path": tmp + "/a-workflow.md"})
            b = db.save({"name": "Character B",
                         "lorebook": [{"keys": "beta", "content": "B-only", "constant": False}],
                         "sources": [{"kind": "file", "path": tmp + "/b.md", "recursive": False}],
                         "workflow_path": tmp + "/b-workflow.md"})
            db.assign("chat_a", a["id"])
            db.assign("chat_b", b["id"])
            self.assertEqual(db.assigned_card("chat_a")["lorebook"][0]["content"], "A-only")
            self.assertEqual(db.assigned_card("chat_a")["workflow_path"], tmp + "/a-workflow.md")
            self.assertEqual(db.assigned_card("chat_b")["lorebook"][0]["content"], "B-only")
            self.assertEqual(db.assigned_card("chat_b")["sources"][0]["kind"], "file")
            self.assertEqual(db.assigned_card("chat_b")["workflow_path"], tmp + "/b-workflow.md")
            a["lorebook"][0]["content"] = "A-updated"
            db.save(a)
            self.assertEqual(db.get(b["id"])["lorebook"][0]["content"], "B-only")

    def test_import_v3(self):
        data = {"spec": "chara_card_v3", "data": {"name": "Tester",
            "description": "a", "character_book": {"entries": [
                {"keys": ["hello"], "content": "matched", "enabled": True},
                {"keys": ["skip"], "content": "ignored", "enabled": False},
            ]}}}
        card = card_from_tavern(data)
        self.assertEqual(card["lorebook"], [{"keys": "hello", "content": "matched", "constant": False}])

    def test_triggers_and_budget(self):
        card = valid_card(self.sample())
        txt, activated = compose(card, "学生", [{"role": "user", "content": "讲讲樱花吧"}])
        self.assertIn("今天是春天", txt)
        self.assertIn("始终保持友好", txt)
        self.assertEqual(len(activated), 2)
        txt, activated = compose(card, "学生", [{"role": "user", "content": "数学"}])
        self.assertNotIn("今天是春天", txt)
        self.assertEqual(activated, ["(always)"])
        txt, activated = compose(card, "学生", [{"role": "user", "content": "樱花"}], lore_limit=1)
        self.assertEqual(activated, [])

    def test_unconfigured_is_noop(self):
        self.assertEqual(compose(None, "", [{"role": "user", "content": "hello"}]), ("", []))

    def test_validation(self):
        bad = self.sample()
        bad["name"] = "n" * 81
        with self.assertRaises(ValueError):
            valid_card(bad)
        bad = self.sample()
        bad["lorebook"][0]["constant"] = "true"
        with self.assertRaises(ValueError):
            valid_card(bad)
        with tempfile.TemporaryDirectory() as tmp:
            db = CharacterStore(tmp)
            with self.assertRaises(ValueError):
                db.assign("../bad", None)
            with self.assertRaises(ValueError):
                db.assign("safe_id", "a" * 32)
            with self.assertRaises(ValueError):
                db.set_global({"user_name":"Zakee", "user_description":"u" * 4001})


if __name__ == "__main__":
    unittest.main()
