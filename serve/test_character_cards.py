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

    def test_persistence_assignment_and_deletion(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = CharacterStore(tmp)
            self.assertEqual(db.snapshot(), {"cards": [], "assignments": {}, "persona": ""})
            card = db.save(self.sample())
            db.set_persona("喜欢编程")
            db.assign("a" * 32, card["id"])
            saved = CharacterStore(tmp).snapshot()
            self.assertEqual(saved["persona"], "喜欢编程")
            self.assertEqual(saved["assignments"]["a" * 32], card["id"])
            self.assertEqual(saved["cards"][0]["name"], "Miku 老师")
            db.remove(card["id"])
            self.assertEqual(CharacterStore(tmp).snapshot()["assignments"], {})
            self.assertEqual(CharacterStore(tmp).snapshot()["cards"], [])

    def test_card_roundtrip(self):
        x = valid_card(self.sample())
        imported = card_from_tavern(card_to_tavern(x))
        for key in ("name", "description", "personality", "scenario",
                    "first_mes", "mes_example", "lorebook"):
            self.assertEqual(imported[key], x[key], key)

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
                db.set_persona("u" * 4001)


if __name__ == "__main__":
    unittest.main()
