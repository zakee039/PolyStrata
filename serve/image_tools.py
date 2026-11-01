"""Built-in image tool for PolyStrata's opt-in web chat.

Shares the ImageGen backend and GPU turn with POST /v1/images/generations. The
external MCP hub remains independent; the tool is exposed only when the user's
own chat page opts in and the image backend has been configured.
"""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from serve import images as images_api

NAME = "polystrata__generate_image"
SCHEMA = {
    "name": NAME,
    "description": ("Generate a new picture from text when the user asks you to draw, "
                    "create or visualize something. This tool cannot edit an uploaded "
                    "picture. Do not use it for ordinary questions. The "
                    "result is inserted automatically into the chat by PolyStrata. "
                    "Write a detailed visual prompt, not a conversation reply."),
    "parameters": {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "The scene or picture to generate."},
            "size": {"type": "string", "enum": ["512x512", "768x768", "1024x1024"],
                     "description": "Default 512x512. Larger sizes use more resources."},
        },
        "required": ["prompt"],
    },
}


class ChatImageHub:
    """MCP-compatible adapter so image tools join, not replace, existing MCP."""

    def __init__(self, svc, mcp=None):
        self.svc, self.mcp = svc, mcp
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
            tools.append(SCHEMA)
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
            job = images_api.parse_request({
                "prompt": arguments.get("prompt"), "size": arguments.get("size") or "512x512",
                "n": 1, "response_format": "url",
            }, self.svc.images.cfg)
            if cancel and cancel.is_set():
                raise RuntimeError("image generation cancelled")
            gen = self.svc.images
            if self.svc.session_v1:
                gen.dir = self.svc.session_v1.root / "generated-images"
                gen.dir.mkdir(parents=True, exist_ok=True)
                gen.keep_files = 2**31 - 1
            with images_api.gpu_turn(self.svc, gen, job, cancel) as policy:
                images, figures = gen.run(job, cancel)
            filename = gen.store(images[0])
            output["image_file"] = filename
            output["image_prompt"] = job["prompt"]
            output["text"] = (f"Generated the requested image successfully "
                              f"(size {job['width']}x{job['height']}, seed {job['seed']}, "
                              f"GPU policy {policy}). The image is already displayed "
                              "in this conversation; do not generate it again.")
            output["ok"] = True
        except Exception as exc:
            output["ok"] = False
            output["text"] = f"error: image generation failed: {str(exc)[:500]}"
        output["chars"] = len(output["text"])
        output["ms"] = int((time.monotonic() - start) * 1000)
        return output
