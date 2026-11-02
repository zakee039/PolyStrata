import tempfile
import unittest
from pathlib import Path

from serve.character_cards import CharacterStore
from serve.workflow_profile import WorkflowRuntime, parse_workflow, read_workflow


MARKDOWN = """# NLP Teacher
## 初始化（仅执行一次）
- 阅读课程目录
- 根据第一句话确定主题

## 全程行为约束
- 先讲概念，再举例。
- 不擅自修改资料。

## 每轮工作流程
1. 解释问题
2. 检索相关资料

## 工具权限
- 如需修改文件，先征求同意。
"""


class WorkflowProfileTests(unittest.TestCase):
    def make(self, folder):
        root = Path(folder)
        path = root / "teacher.md"
        path.write_text(MARKDOWN, encoding="utf-8")
        store = CharacterStore(root)
        card = store.save({"name": "Miku", "workflow_path": str(path)})
        store.assign("chat1", card["id"])
        return path, store, card

    def test_parse_categories_and_unstructured(self):
        sections = parse_workflow(MARKDOWN)
        self.assertIn("阅读课程目录", sections["initial"])
        self.assertIn("先讲概念", sections["persistent"])
        self.assertIn("检索相关资料", sections["per_turn"])
        self.assertIn("征求同意", sections["tool_policy"])
        self.assertIn("自由描述", parse_workflow("# 个人流程\n自由描述")["persistent"])

    def test_first_user_turn_retry_commit_and_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path, store, card = self.make(folder)
            runtime = WorkflowRuntime(store)
            first = runtime.prepare("chat1", True)
            self.assertTrue(first["enabled"])
            self.assertTrue(first["initial_pending"])
            self.assertIn("初始化", MARKDOWN)
            # A failed first response must be able to repeat initialization.
            self.assertTrue(WorkflowRuntime(CharacterStore(folder)).prepare("chat1", True)["initial_pending"])
            self.assertEqual(runtime.commit("chat1"), {"committed": True})
            after = WorkflowRuntime(CharacterStore(folder)).prepare("chat1", False)
            self.assertFalse(after["initial_pending"])
            self.assertEqual(first["sha256"], after["sha256"])
            self.assertEqual(first["sections"], after["sections"])
            # Subsequent edits must not silently mutate a running conversation.
            path.write_text(MARKDOWN.replace("先讲概念", "先讲结论"), encoding="utf-8")
            same_session = runtime.prepare("chat1", False)
            self.assertEqual(first["sha256"], same_session["sha256"])
            store.assign("chat2", card["id"])
            later = runtime.prepare("chat2", True)
            self.assertNotEqual(first["sha256"], later["sha256"])
            self.assertIn("先讲结论", later["sections"]["persistent"])

    def test_existing_chat_skips_first_only_but_keeps_rules(self):
        with tempfile.TemporaryDirectory() as folder:
            _, store, card = self.make(folder)
            store.assign("older_chat", card["id"])
            runtime = WorkflowRuntime(store)
            ready = runtime.prepare("older_chat", False)
            self.assertTrue(ready["enabled"])
            self.assertFalse(ready["initial_pending"])
            self.assertIn("先讲概念", ready["sections"]["persistent"])

    def test_no_role_or_no_workflow_does_not_mutate_chat(self):
        with tempfile.TemporaryDirectory() as folder:
            store = CharacterStore(folder)
            rt = WorkflowRuntime(store)
            self.assertEqual(rt.prepare("empty", True), {"enabled": False})
            card = store.save({"name": "Plain"})
            store.assign("empty", card["id"])
            self.assertEqual(rt.prepare("empty", True), {"enabled": False})
            self.assertEqual(rt.commit("empty"), {"committed": False})

    def test_invalid_source_fails_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            path, store, card = self.make(folder)
            with self.assertRaises(ValueError):
                read_workflow(str(Path(folder) / "missing.md"))
            with self.assertRaises(ValueError):
                read_workflow("relative.md")
            path.write_bytes(b"x" * 65537)
            with self.assertRaises(ValueError):
                WorkflowRuntime(store).prepare("chat1", True)

    def test_role_reassignment_isolated_by_card(self):
        with tempfile.TemporaryDirectory() as folder:
            _, store, original = self.make(folder)
            rt = WorkflowRuntime(store)
            self.assertTrue(rt.prepare("chat1", True)["initial_pending"])
            rt.commit("chat1")
            alt = store.save({"name": "Other", "workflow_path": original["workflow_path"]})
            store.assign("chat1", alt["id"])
            self.assertTrue(rt.prepare("chat1", True)["initial_pending"])
            store.assign("chat1", original["id"])
            self.assertFalse(rt.prepare("chat1", True)["initial_pending"])


if __name__ == "__main__":
    unittest.main()
