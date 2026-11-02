"""Built-in image tool for PolyStrata's opt-in web chat.

Shares the ImageGen backend and GPU turn with POST /v1/images/generations. The
external MCP hub remains independent; the tool is exposed only when the user's
own chat page opts in and the image backend has been configured.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

from serve import images as images_api

NAME = "polystrata__generate_image"
SCHEMA = {
    "name": NAME,
    "description": ("Generate a new picture from text when the user asks you to draw, "
                    "create or visualize something. This tool cannot edit an uploaded "
                    "picture. Do not use it for ordinary questions. The "
                    "result is inserted automatically into the chat by PolyStrata. "
                    "Write a detailed visual prompt, not a conversation reply. "
                    "Use 768x768 by default. Respect the user's request to disable "
                    "upscaling, and identify anime styles with style_hint=anime."),
    "parameters": {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "The scene or picture to generate."},
            "size": {"type": "string", "enum": ["512x512", "768x768", "1024x1024"],
                     "description": "Default 768x768. Larger sizes use more resources."},
            "upscale": {"type": "string", "enum": ["auto", "off", "4x"],
                        "description": "Default auto. Anime pictures get 4x upscaling; off disables it."},
            "style_hint": {"type": "string", "enum": ["auto", "anime", "other"],
                           "description": "Anime only when clearly an anime/2D Japanese animation style."},
            "character_related": {"type": "boolean", "description": (
                "Set true only when the image subject is the chat's assigned character. "
                "Never apply the character reference image to unrelated image subjects. "
                "Follow the user's latest and earlier instructions above the reference image.")},
        },
        "required": ["prompt"],
    },
}


class ChatImageHub:
    """MCP-compatible adapter so image tools join, not replace, existing MCP."""

    def __init__(self, svc, mcp=None, *, session_id=None, stateless=False):
        self.svc, self.mcp = svc, mcp
        self.character_name = None
        self.character_reference = None
        # Reference images are server-owned, never inferred from client paths.
        # No character identity or image in the stateless/ordinary assistant.
        if not stateless and session_id and getattr(svc, "session_v1", None) is not None:
            from serve.character_cards import CharacterStore
            cards = CharacterStore(svc.session_v1.root)
            card = cards.assigned_card(session_id)
            if card and card.get("reference_image"):
                file = cards.image_file(card["id"])
                if file and file.stat().st_size <= 5 * 1024 * 1024:
                    self.character_name = card["name"]
                    self.character_reference = file
        self.settings = {**(mcp.settings if mcp else {}), "max_rounds":
                         int((mcp.settings if mcp else {}).get("max_rounds", 8))}
        self.builtin = SimpleNamespace(name="PolyStrata", status="ready")

    def wait(self, timeout):
        if self.mcp:
            self.mcp.wait(timeout)

    def routes(self):
        routes = dict(self.mcp.routes()) if self.mcp else {}
        if self.svc.images is not None:
            routes[NAME] = (self.builtin, "generate_image")
        return routes

    def template_tools(self, exclude=()):
        tools = list(self.mcp.template_tools(exclude=exclude)) if self.mcp else []
        if self.svc.images is not None and NAME not in exclude:
            tools = [tool for tool in tools if tool.get("name") != NAME]
            if self.character_reference is not None and self.svc.images.cfg.get("llm_vision"):
                tool = {**SCHEMA, "description": SCHEMA["description"] +
                        f" The current chat character is {self.character_name!r}, and a weak appearance reference is available."}
            else:
                tool = SCHEMA
            tools.append(tool)
        return tools

    def call(self, name, arguments, cancel: threading.Event | None = None):
        if name != NAME:
            return self.mcp.call(name, arguments, cancel)
        start = time.monotonic()
        output = {"server": "PolyStrata", "tool": "generate_image", "truncated": False}
        try:
            if self.svc.images is None:
                raise ValueError("image generation is not enabled")
            if not isinstance(arguments, dict):
                raise ValueError("expected image arguments as an object")
            request = {
                "prompt": arguments.get("prompt"), "size": arguments.get("size") or "768x768",
                "n": 1, "response_format": "url",
                "strata_upscale": arguments.get("upscale") or "auto",
                "strata_style_hint": arguments.get("style_hint") or "auto",
            }
            character_related = arguments.get("character_related") is True
            if character_related and self.character_reference is not None and self.svc.images.cfg.get("llm_vision"):
                import base64
                # Weak reference: never overwrite explicit scene, costume, pose,
                # calendar/holiday or subject instructions from the conversation.
                request["prompt"] = (str(arguments.get("prompt") or "") +
                    "\nReference photo is for this character's visual identity only. "
                    "Treat the text request as authoritative for the subject, "
                    "scene, holiday, outfit, colors, actions, pose, composition and "
                    "any conflicting details. Never copy unrelated scene or attire from the reference.")
                request["strata_reference_image"] = ("data:image/png;base64," +
                    base64.b64encode(self.character_reference.read_bytes()).decode("ascii"))
            job = images_api.parse_request(request, self.svc.images.cfg)
            if "strata_reference_image" in request:
                job["character_weak_reference"] = True
            if cancel and cancel.is_set():
                raise RuntimeError("image generation cancelled")
            gen = self.svc.images
            if self.svc.session_v1:
                gen.dir = self.svc.session_v1.root / "generated-images"
                gen.dir.mkdir(parents=True, exist_ok=True)
                gen.keep_files = 2**31 - 1
            with images_api.gpu_turn(self.svc, gen, job, cancel) as policy:
                images, figures = gen.run(job, cancel)
                images, up = images_api.postprocess(gen, images, job, cancel)
                figures.update(up)
            filename = gen.store(images[0])
            output["image_file"] = filename
            output["image_prompt"] = str(arguments.get("prompt") or "")
            output["character_reference_used"] = bool(job.get("character_weak_reference"))
            if figures.get("upscale_original_files"):
                output["image_original_file"] = figures["upscale_original_files"][0]
            output["upscale_applied"] = figures.get("upscale_applied", False)
            output["upscale_reason"] = figures.get("upscale_reason", "")
            if figures.get("upscale_fallback"):
                output["upscale_fallback"] = True
            output["text"] = (f"Generated the requested image successfully "
                              f"(size {figures.get('upscale_output_size') or str(job['width'])+'x'+str(job['height'])}, "
                              f"seed {job['seed']}, "
                              f"GPU policy {policy}). The image is already displayed "
                              "in this conversation; do not generate it again."
                              + (" Anime 4x upscale failed; showing the original picture."
                                 if figures.get("upscale_fallback") else ""))
            output["ok"] = True
        except Exception as exc:
            output["ok"] = False
            output["text"] = f"error: image generation failed: {str(exc)[:500]}"
        output["chars"] = len(output["text"])
        output["ms"] = int((time.monotonic() - start) * 1000)
        return output
