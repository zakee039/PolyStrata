"""Answer budget is independent of the model's reasoning budget."""
import tempfile
import threading
import unittest
from pathlib import Path

from serve.frontend import ChatTemplate, openai_to_messages
from serve.server import Service, MockEngine, answer_budget_tokens
from serve.test_gemma import GemmaTokens, TEMPLATE


class AnswerBudget(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        path = Path(self.tmp.name) / "chat_template.jinja"
        path.write_text(TEMPLATE, encoding="utf8")
        self.tok = GemmaTokens()
        self.path = path

    def run_reply(self, answer_cap):
        reply = "<|channel>thought\ninternal thought" + "<channel|>" + "abcdefghijklmnopqrstuvwxyz" + "<turn|>"
        svc = Service(MockEngine(self.tok, reply), self.tok, ChatTemplate(self.path))
        sampling = {} if answer_cap is None else {"answer_budget_tokens": answer_cap}
        tools = [{"type": "function", "function": {"name": "noop", "parameters": {}}}]
        messages, tools, kw = openai_to_messages({
            "messages": [{"role": "user", "content": "hello"}], "tools": tools})
        ids = svc.encode_prompt(messages, tools, kw)
        events = list(svc.run(ids, True, tools, 250, sampling, threading.Event()))
        answer = "".join(x.text for kind, x in events if kind == "event" and x.kind == "content")
        thought = "".join(x.text for kind, x in events if kind == "event" and x.kind == "reasoning")
        done = [x for kind, x in events if kind == "done"][-1]
        return answer, thought, done

    def test_answer_only_cap_leaves_reasoning_intact(self):
        full, thought, _ = self.run_reply(None)
        partial, thought2, done = self.run_reply(5)
        self.assertEqual(full, "abcdefghijklmnopqrstuvwxyz")
        self.assertEqual(thought, thought2)
        self.assertEqual(partial, full[:5])
        self.assertEqual(done["finish"], "length")

    def test_none_has_no_limit_and_invalid_values_fail(self):
        self.assertIsNone(answer_budget_tokens(None))
        self.assertEqual(answer_budget_tokens(1024), 1024)
        for bad in (0, -1, True, 0.5, "1024", 262145):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                answer_budget_tokens(bad)


if __name__ == "__main__":
    unittest.main()
