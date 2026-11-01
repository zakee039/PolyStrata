"""Markdown source retrieval: recursion, indexing refresh, caps, and isolation."""
from __future__ import annotations
import os
from pathlib import Path
import tempfile
import unittest

from serve.character_cards import CharacterStore, valid_card
from serve.lore_retriever import search_sources


class RetrievalTests(unittest.TestCase):
    def test_markdown_directory_and_live_update(self):
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp)
            p=folder/"教室"/"春季.md"
            p.parent.mkdir()
            p.write_text("# 樱花季\n春天会在教室举行樱花主题教学。", encoding="utf-8")
            (folder/"教室"/"其他.txt").write_text("樱花秘密，不能读这个文件", encoding="utf-8")
            src=[{"kind":"directory","path":str(folder),"recursive":True}]
            response=search_sources(src,"樱花季有哪些安排？")
            self.assertEqual(response["scanned_files"],1)
            self.assertTrue(response["snippets"],response)
            self.assertIn("春天",response["snippets"][0]["content"])
            self.assertIn("春季.md",response["snippets"][0]["file"])
            self.assertNotIn("其他.txt",str(response))
            # Simulate OneDrive/local modification: new content is indexed again.
            p.write_text("# 樱花季\n春天会在教室举行樱花主题教学，追加穿搭攻略。",encoding="utf-8")
            response=search_sources(src,"樱花季有哪些安排？")
            self.assertIn("穿搭攻略",response["snippets"][0]["content"])

    def test_file_mode_and_non_recursion(self):
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp)
            (folder/"top.md").write_text("# 魔法\n魔法学校有星云图书馆。",encoding="utf8")
            inner=folder/"sub";inner.mkdir()
            (inner/"child.md").write_text("# 魔法\n魔法钥匙",encoding="utf8")
            shallow=search_sources([{"kind":"directory","path":str(folder),"recursive":False}],"魔法学校")
            self.assertEqual(shallow["scanned_files"],1)
            deep=search_sources([{"kind":"directory","path":str(folder),"recursive":True}],"魔法学校")
            self.assertEqual(deep["scanned_files"],2)
            single=search_sources([{"kind":"file","path":str(inner/"child.md"),"recursive":False}],"魔法钥匙")
            self.assertEqual(single["scanned_files"],1)
            self.assertIn("钥匙", single["snippets"][0]["content"])

    def test_missing_paths_safe(self):
        missing=[{"kind":"file","path":str(Path(tempfile.gettempdir())/"not_a_doc.md"),"recursive":False}]
        response=search_sources(missing, "樱花")
        self.assertFalse(response["snippets"])
        self.assertTrue(response["errors"])

    def test_size_budget_is_bounded(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/"dataset.md"
            path.write_text("# 地理\n地理位置相关 " * 1000,encoding="utf8")
            response=search_sources([{"kind":"file","path":str(path),"recursive":False}],"地理位置",budget=400)
            self.assertLessEqual(sum(len(x["content"]) for x in response["snippets"]),400)
            self.assertLessEqual(len(response["snippets"]),4)

    def test_saved_card_sources_and_binding(self):
        with tempfile.TemporaryDirectory() as temp:
            doc=Path(temp)/"notes.md";doc.write_text("# 小镇\n小镇里有面包店",encoding="utf8")
            store=CharacterStore(temp)
            card=store.save({"name":"Miku", "sources":[{"kind":"file","path":str(doc),"recursive":False}]})
            store.assign("test_session_1",card["id"])
            assigned=CharacterStore(temp).assigned_card("test_session_1")
            self.assertEqual(assigned["sources"][0]["path"],str(doc))
            self.assertIsNone(store.assigned_card("other"))
            with self.assertRaises(ValueError):
                valid_card({"name":"bad","sources":[{"kind":"http","path":"http://somewhere"}]})

if __name__=="__main__":
    unittest.main()
