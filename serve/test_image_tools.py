"""Fast opt-in image chat tool tests; no models or GPU required."""
import contextlib
import threading
import types
import unittest
from unittest.mock import patch

from serve.image_tools import ChatImageHub, NAME


class FakeImage:
    def __init__(self):
        self.cfg = {"max_side": 1024, "max_n": 1}
        self.calls = []

    def run(self, job, cancel=None):
        self.calls.append(job)
        return [b"\x89PNG\r\n\x1a\nmock"], {"seconds": 1}

    def store(self, data):
        assert data.startswith(b"\x89PNG")
        return "a"*32 + ".png"


class ChatImageToolTests(unittest.TestCase):
    def make_service(self, enabled=True):
        return types.SimpleNamespace(images=FakeImage() if enabled else None,
                                     session_v1=None)

    def test_disabled_has_no_builtin_tool(self):
        hub = ChatImageHub(self.make_service(False))
        self.assertEqual(hub.template_tools(), [])
        self.assertEqual(hub.routes(), {})

    def test_schema_is_opt_in_and_has_fixed_name(self):
        hub = ChatImageHub(self.make_service())
        tools = hub.template_tools()
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]["name"], NAME)
        self.assertFalse(hub.template_tools(exclude={NAME}))

    def test_image_runs_in_gpu_turn_and_returns_random_name(self):
        svc = self.make_service()
        seen = []

        @contextlib.contextmanager
        def mocked_turn(svc_arg, gen, job, cancel):
            self.assertIs(svc_arg, svc)
            seen.append(("enter", job["width"], job["height"]))
            yield "unload"
            seen.append(("leave", job["width"], job["height"]))

        with patch("serve.image_tools.images_api.gpu_turn", mocked_turn):
            result = ChatImageHub(svc).call(
                NAME, {"prompt": "Green cat", "size": "512x512"}, threading.Event())
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["image_file"], "a"*32 + ".png")
        self.assertEqual(result["image_prompt"], "Green cat")
        self.assertEqual(seen, [("enter", 512, 512), ("leave", 512, 512)])
        self.assertEqual(len(svc.images.calls), 1)
        self.assertNotIn("b64_json", result)

    def test_bad_request_fails_without_invoking_generator(self):
        svc = self.make_service()
        result = ChatImageHub(svc).call(NAME, {"prompt": "", "size": "512x512"})
        self.assertFalse(result["ok"])
        self.assertEqual(svc.images.calls, [])

    def test_external_mcp_kept_when_builtin_exists(self):
        class External:
            settings = {"max_rounds": 3}

            def routes(self):
                return {"ext__search": (types.SimpleNamespace(name="ext"), "search")}

            def template_tools(self, exclude=()):
                return [] if "ext__search" in exclude else [
                    {"name": "ext__search", "description": "Search", "parameters": {}}]

            def call(self, name, arguments, cancel=None):
                return {"text": "found", "ok": True, "ms": 1, "chars": 5, "truncated": False}

            def wait(self, timeout):
                return None

        hub = ChatImageHub(self.make_service(), External())
        self.assertEqual({tool["name"] for tool in hub.template_tools()},
                         {"ext__search", NAME})
        self.assertEqual(hub.settings["max_rounds"], 3)
        self.assertIn("ext__search", hub.routes())
        self.assertEqual(hub.call("ext__search", {})["text"], "found")


if __name__ == "__main__":
    unittest.main()
