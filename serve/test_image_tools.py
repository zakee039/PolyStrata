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

from serve.image_tools import (ChatImageHub, NAME, needs_recent_image,
                              add_relevant_image, clearly_editing_recent_image)


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

    def test_image_tool_result_completes_without_a_second_model_round(self):
        from serve.frontend import Event, ToolCall
        from serve.server import run_with_mcp
        from types import SimpleNamespace
        call = ToolCall(name=NAME, arguments={"prompt": "Miku at breakfast"})

        class FakeService:
            turns = 0
            def run(self, *args):
                self.turns += 1
                yield "event", Event("tool_call", call=call)
                yield "done", {"completion_tokens": 42, "finish": "stop"}
            def prepare(self, *args, **kwargs):
                raise AssertionError("No second inference after image result")

        class FakeHub:
            settings = {"max_rounds": 8}
            def routes(self):
                return {NAME: (SimpleNamespace(name="PolyStrata"), "generate_image")}
            def call(self, name, arguments, cancel):
                return {"ok": True, "text": "Generated image", "chars": 15,
                        "truncated": False, "ms": 20, "image_file": "a" * 32 + ".png"}

        svc = FakeService()
        out = list(run_with_mcp(svc, FakeHub(), [], [], {}, [1], False, 100, 100,
                                {}, threading.Event(), {NAME}))
        self.assertEqual(svc.turns, 1)
        self.assertTrue(any(kind == "mcp" and msg.get("event") == "result"
                            and msg.get("image_file") for kind, msg in out))
        self.assertEqual(out[-1][0], "done")
        self.assertEqual(out[-1][1]["completion_tokens"], 42)

    def test_image_tool_gets_one_vision_reply_without_thinking_or_tools(self):
        from serve.frontend import Event, ToolCall
        from serve.server import run_with_mcp, IMAGE_FOLLOWUP_MAX_TOKENS
        call = ToolCall(name=NAME, arguments={"prompt": "Miku with a blue coat"})
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "generated-images").mkdir()
            image_name = "d" * 32 + ".png"
            pic = io.BytesIO()
            Image.new("RGB", (32, 32), (2, 32, 88)).save(pic, "PNG")
            (root / "generated-images" / image_name).write_bytes(pic.getvalue())

            class VisionService:
                def __init__(self):
                    self.vision = object()
                    self.session_v1 = types.SimpleNamespace(root=root)
                    self.turns = 0
                    self.seen = []

                def run(self, ids, thinking, tools, max_new, sampling, cancel):
                    self.turns += 1
                    self.seen.append((thinking, tools, max_new, sampling))
                    if self.turns == 1:
                        yield "event", Event("tool_call", call=call)
                        yield "done", {"completion_tokens": 31, "finish": "stop"}
                    else:
                        yield "event", Event("content", "这件蓝外套很适合今天的天气。")
                        yield "done", {"completion_tokens": 14, "finish": "stop"}

                def prepare(self, messages, tools, kwargs, max_new, cancel=None):
                    self.assert_called = True
                    assert tools is None
                    assert kwargs["enable_thinking"] is False
                    assert max_new == 256
                    last = messages[-1]
                    assert last["role"] == "user"
                    assert last["content"][-1] == {"type": "image", "source": str(root / "generated-images" / image_name)}
                    return [11, 12, 13], False, 256

            class Hub:
                settings = {"max_rounds": 8}
                def routes(self):
                    return {NAME: (types.SimpleNamespace(name="PolyStrata"), "generate_image")}
                def call(self, name, arguments, cancel):
                    return {"ok": True, "image_file": image_name, "text": "Generated",
                            "chars": 9, "ms": 9, "truncated": False}

            svc = VisionService()
            out = list(run_with_mcp(svc, Hub(),
                                    [{"role": "user", "content": "Draw me wearing a blue coat"}],
                                    [], {}, [1], True, 1600, 1600,
                                    {"reasoning_budget_tokens": 1024, "reasoning_effort": "low",
                                     "strata_session_id": "chat_1"},
                                    threading.Event(), {NAME}))
            self.assertEqual(svc.turns, 2)
            self.assertEqual(out[-1][1]["completion_tokens"], 45)
            self.assertEqual(out[-1][1]["prompt_tokens"], 4)
            self.assertEqual(svc.seen[-1][:3], (False, None, IMAGE_FOLLOWUP_MAX_TOKENS))
            self.assertEqual(svc.seen[-1][3]["reasoning_budget_tokens"], 0)
            self.assertEqual(svc.seen[-1][3]["answer_budget_tokens"], 256)
            self.assertNotIn("strata_session_id", svc.seen[-1][3])
            self.assertTrue(any(kind == "event" and x.kind == "content" and "蓝外套" in x.text
                                for kind, x in out))

    def test_image_visual_reply_failure_falls_back_without_retrying_generation(self):
        from serve.frontend import Event, ToolCall
        from serve.server import run_with_mcp
        call = ToolCall(name=NAME, arguments={"prompt": "image"})
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "generated-images").mkdir()
            img = "a" * 32 + ".png"
            (root / "generated-images" / img).write_bytes(b"png")

            class Service:
                vision = object()
                session_v1 = types.SimpleNamespace(root=root)
                turns = 0
                def run(self, *args, **kwargs):
                    self.turns += 1
                    yield "event", Event("tool_call", call=call)
                    yield "done", {"completion_tokens": 5, "finish": "stop"}
                def prepare(self, *args, **kwargs):
                    raise ValueError("vision unavailable")

            class Hub:
                settings = {"max_rounds": 8}
                def routes(self):
                    return {NAME: (types.SimpleNamespace(name="PolyStrata"), "generate_image")}
                def call(self, *args):
                    return {"ok": True, "image_file": img, "text": "Generated",
                            "chars": 9, "ms": 9, "truncated": False}

            svc = Service()
            out = list(run_with_mcp(svc, Hub(), [], [], {}, [1], False, 256, 256,
                                    {}, threading.Event(), {NAME}))
            self.assertEqual(svc.turns, 1)
            self.assertTrue(any(kind == "event" and x.kind == "content" and "图片已生成" in x.text
                                for kind, x in out))

    def test_recent_picture_is_only_attached_for_visual_references(self):
        class Sessions:
            def recent_generated_image(self, ident):
                self.last_ident = ident
                return {"path": Path("mock-saved-picture.png"), "user_turns_since": 1}

        sessions = Sessions()
        svc = types.SimpleNamespace(session_v1=sessions, vision=object())
        original = [{"role": "user", "content": "晚安"}]
        self.assertIs(add_relevant_image(svc, "my_chat", original), original)
        self.assertFalse(needs_recent_image("今天天气怎么样？", 1))
        for text in ("眼睛再大一点", "你今天穿的外套好漂亮", "请修改刚才的图片",
                     "The blue coat in that picture is cute"):
            modified = add_relevant_image(svc, "my_chat", [{"role": "user", "content": text}])
            self.assertEqual(modified[-1]["content"][-1]["type"], "image")
            self.assertEqual(sessions.last_ident, "my_chat")
        self.assertFalse(needs_recent_image("眼睛再大一点", 12))
        self.assertTrue(needs_recent_image("刚才那张图片", 12))
        self.assertTrue(clearly_editing_recent_image("眼睛再大一点", 1))
        self.assertFalse(clearly_editing_recent_image("你今天穿的外套好漂亮", 1))
        self.assertFalse(clearly_editing_recent_image("再画一张新的，眼睛更大", 1))
        self.assertEqual(add_relevant_image(svc, "my_chat",
            [{"role": "user", "content": [{"type": "text", "text": "改图"},
                                            {"type": "image", "source": "user_image"}]}])[-1]["content"][-1]["source"], "user_image")

    def test_edit_last_generated_picture_uses_its_real_image_reference(self):
        from serve.session_v1 import SessionV1
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "chat"
            store = SessionV1(str(root))
            prev = "f" * 32 + ".png"
            buf = io.BytesIO()
            Image.new("RGB", (96, 96), (220, 10, 10)).save(buf, "PNG")
            (store.generated_dir / prev).write_bytes(buf.getvalue())
            store.store_web("first", [
                {"role": "assistant", "tools": [
                    {"name": NAME, "ok": True, "image_file": prev}]}])
            store.register_generated_image("first", prev)
            svc = self.make_service()
            svc.session_v1 = store
            svc.images.cfg["llm_vision"] = "mmproj.gguf"
            def store_output(data):
                name = "a" * 32 + ".png"
                (store.generated_dir / name).write_bytes(buf.getvalue())  # valid fixture PNG
                return name
            svc.images.store = store_output

            @contextlib.contextmanager
            def mocked_turn(*args):
                yield "unload"

            with patch("serve.image_tools.images_api.gpu_turn", mocked_turn):
                hub = ChatImageHub(svc, session_id="first")
                reply = hub.call(NAME, {"prompt": "make eyes bigger", "edit_previous": True,
                                        "character_related": True}, threading.Event())
            self.assertTrue(reply["ok"], reply)
            self.assertTrue(reply["edited_previous"])
            self.assertTrue(svc.images.calls[-1]["reference_png"].startswith(b"\x89PNG"))
            self.assertIn("Preserve", svc.images.calls[-1]["prompt"])
            self.assertFalse(reply["character_reference_used"])
            with patch("serve.image_tools.images_api.gpu_turn", mocked_turn):
                auto = ChatImageHub(svc, session_id="first", latest_user_text="眼睛再大一点").call(
                    NAME, {"prompt": "make the eyes bigger"}, threading.Event())
            self.assertTrue(auto["ok"], auto)
            self.assertTrue(auto["edited_previous"])
            # A different conversation cannot import the picture by guessing its filename.
            with patch("serve.image_tools.images_api.gpu_turn", mocked_turn):
                no_image = ChatImageHub(svc, session_id="other").call(
                    NAME, {"prompt": "make eyes bigger", "edit_previous": True}, threading.Event())
            self.assertFalse(no_image["ok"])
            self.assertIn("no previous generated picture", no_image["text"])

    def test_failed_image_tool_also_stops_without_infinite_retry(self):
        from serve.frontend import Event, ToolCall
        from serve.server import run_with_mcp
        from types import SimpleNamespace
        call = ToolCall(name=NAME, arguments={"prompt": "Miku"})

        class FailedService:
            turns = 0
            def run(self, *args):
                self.turns += 1
                yield "event", Event("tool_call", call=call)
                yield "done", {"completion_tokens": 20, "finish": "stop"}
            def prepare(self, *args, **kwargs):
                raise AssertionError("Failed images must not be re-generated")

        class FailedHub:
            settings = {"max_rounds": 8}
            def routes(self):
                return {NAME: (SimpleNamespace(name="PolyStrata"), "generate_image")}
            def call(self, name, arguments, cancel):
                return {"ok": False, "text": "error: model missing",
                        "chars": 20, "truncated": False, "ms": 10}

        svc = FailedService()
        result = list(run_with_mcp(svc, FailedHub(), [], [], {}, [1], False, 100, 100,
                                   {}, threading.Event(), {NAME}))
        self.assertEqual(svc.turns, 1)
        self.assertTrue(any(kind == "event" and "图像生成失败" in obj.text
                            for kind, obj in result))
        self.assertEqual(result[-1][0], "done")

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
            store = CharacterStore(Path(tmp) / "character")
            card = store.save({"name": "Miku", "description": "teal twin tails"})
            buf = io.BytesIO();Image.new("RGB",(64,64),(35,180,190)).save(buf,"PNG")
            store.upload_reference(card["id"],base64.b64encode(buf.getvalue()).decode())
            store.assign("chat_sess1",card["id"])
            svc=self.make_service()
            svc.session_v1=types.SimpleNamespace(root=Path(tmp) / "chat")
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

    def test_chat_model_switch_without_changing_page_or_character_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CharacterStore(Path(tmp) / "character")
            card = store.save({"name":"Miku", "description":"teal twin tails"})
            buf = io.BytesIO();Image.new("RGB",(64,64),(35,180,190)).save(buf,"PNG")
            store.upload_reference(card["id"],base64.b64encode(buf.getvalue()).decode())
            store.assign("session_1",card["id"])
            svc = self.make_service()
            svc.images.cfg.update({"profile_id":"qwen-image-2.1-turbo-official",
                                   "llm_vision":"mmproj.gguf", "model_name":"official"})
            svc.session_v1=types.SimpleNamespace(root=Path(tmp) / "chat")
            selected = FakeImage()
            # SDXL may inherit the default Qwen mmproj key in resolved configs;
            # it must still NEVER auto-apply a character picture via img2img.
            selected.cfg.update({"family":"sdxl", "steps":28, "model_name":"Animagine",
                                 "llm_vision":"inherited-qwen-mmproj.gguf"})
            svc.image_profiles={"animagine-xl-4-opt": selected}
            store.set_global({"chat_image_profile":"animagine-xl-4-opt"},
                  allowed_image_profiles={"default","qwen-image-2.1-turbo-official","animagine-xl-4-opt"})
            with patch("serve.image_tools.images_api.gpu_turn") as turn:
                @contextlib.contextmanager
                def no_gpu(svc_arg, generator, job, cancel):
                    self.assertIs(generator, selected)
                    yield "unload"
                turn.side_effect=no_gpu
                hub = ChatImageHub(svc,session_id="session_1")
                self.assertIs(hub.gen,selected)
                self.assertIn("SDXL", hub.template_tools()[0]["description"])
                self.assertNotIn("weak appearance reference", hub.template_tools()[0]["description"])
                answer=hub.call(NAME,{"prompt":"Miku at winter festival", "character_related":True})
                self.assertTrue(answer["ok"],answer)
                self.assertEqual(answer["image_profile"],"animagine-xl-4-opt")
                self.assertEqual(selected.calls[-1]["steps"],28)
                self.assertFalse(answer["character_reference_used"])
            self.assertEqual(len(svc.images.calls),0)
            store.set_global({"chat_image_profile":"default"},
                  allowed_image_profiles={"default","animagine-xl-4-opt"})
            self.assertIs(ChatImageHub(svc,session_id="session_1").gen,svc.images)
            self.assertIsNone(ChatImageHub(svc,session_id="session_1",stateless=True).character_reference)

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
