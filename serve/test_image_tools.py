"""Fast opt-in image chat tool tests; no models or GPU required."""
import contextlib
import threading
import types
import unittest
import tempfile
import base64
import io
from pathlib import Path
from PIL import Image
from serve.character_cards import CharacterStore
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

    def test_role_weak_reference_only_when_character_related(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CharacterStore(tmp)
            card = store.save({"name": "Miku", "description": "teal twin tails"})
            buf = io.BytesIO();Image.new("RGB",(64,64),(35,180,190)).save(buf,"PNG")
            store.upload_reference(card["id"],base64.b64encode(buf.getvalue()).decode())
            store.assign("chat_sess1",card["id"])
            svc=self.make_service()
            svc.session_v1=types.SimpleNamespace(root=Path(tmp))
            svc.images.cfg["llm_vision"]="test-mmproj.gguf"
            @contextlib.contextmanager
            def mocked_turn(svc_arg, gen, job, cancel):
                yield "unload"
            with patch("serve.image_tools.images_api.gpu_turn",mocked_turn):
                hub=ChatImageHub(svc,session_id="chat_sess1")
                self.assertIsNotNone(hub.character_reference)
                output=hub.call(NAME,{"prompt":"Miku at a winter festival wearing a coat", "size":"512x512", "character_related":True})
                self.assertTrue(output["ok"],output)
                self.assertTrue(output["character_reference_used"])
                self.assertEqual(output["image_prompt"],"Miku at a winter festival wearing a coat")
                job=svc.images.calls[-1]
                self.assertTrue(job["reference_png"].startswith(b"\x89PNG"))
                self.assertIn("winter festival",job["prompt"])
                self.assertIn("outfit",job["prompt"])
                self.assertIn("authoritative",job["prompt"])
                output=hub.call(NAME,{"prompt":"a landscape painting","character_related":False})
                self.assertTrue(output["ok"],output)
                self.assertFalse(output["character_reference_used"])
                self.assertIsNone(svc.images.calls[-1]["reference_png"])
                hub_stateless=ChatImageHub(svc,session_id="chat_sess1",stateless=True)
                self.assertIsNone(hub_stateless.character_reference)
                result=hub_stateless.call(NAME,{"prompt":"Miku at festival","character_related":True})
                self.assertTrue(result["ok"])
                self.assertFalse(result["character_reference_used"])
                store.assign("chat_sess1","ordinary")
                hub_ordinary=ChatImageHub(svc,session_id="chat_sess1")
                self.assertIsNone(hub_ordinary.character_reference)

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
