"""Optional anime 4x image post-processing.

This is a *serial* stage inside ImageGen's existing shared GPU turn. It runs
only after sd-cli exits and before the turn's eager LLM restore. No parallel
reload, no extra model process left resident, and failure keeps original PNGs.
"""
from __future__ import annotations

import contextlib
import os
from pathlib import Path
import re
import struct
import subprocess
import tempfile
import time
import threading

ANIME_WORDS = re.compile(
    r"二次元|动漫|日本动画|日式动画|日系动画|赛璐璐|动画风格|动漫风|立绘|初音|"
    r"\banime\b|\bmanga\b|\bmiku\b|\bhatsune\b|\bcel[- ]shad(?:ed|ing)\b",
    re.IGNORECASE,
)
NOT_ANIME = re.compile(
    r"非动漫|不是动漫|不要动漫|非二次元|不是二次元|不要二次元|"
    r"写实照片|写实摄影|写实风格|像素风|像素画|摄影照片|"
    r"\bnon[- ]anime\b|\bnot anime\b|\bphotorealistic\b|"
    r"\bpixel art\b",
    re.IGNORECASE,
)
PNG_HEADER = b"\x89PNG\r\n\x1a\n"


def options(req: dict) -> tuple[str, str]:
    """Explicit PolyStrata fields; OpenAI Images API defaults stay intact."""
    upscale = req.get("strata_upscale", "off")
    style = req.get("strata_style_hint", "auto")
    if upscale not in ("off", "auto", "4x"):
        raise ValueError("strata_upscale must be off, auto or 4x")
    if style not in ("auto", "anime", "other"):
        raise ValueError("strata_style_hint must be auto, anime or other")
    return upscale, style


def should_upscale(job: dict, max_output_side=3072) -> tuple[bool, str]:
    mode, style = job.get("upscale", "off"), job.get("style_hint", "auto")
    if mode == "off":
        return False, "disabled"
    if int(job["width"]) * 4 > max_output_side or int(job["height"]) * 4 > max_output_side:
        return False, "output_size_limit"
    if mode == "4x":
        return True, "explicit"
    text = job.get("prompt") or ""
    if NOT_ANIME.search(text):
        return False, "explicit_non_anime"
    if ANIME_WORDS.search(text):
        return True, "anime"
    if style == "other":
        return False, "other_style"
    if style == "anime":
        return True, "anime"
    return False, "not_anime"


def png_size(data: bytes) -> tuple[int, int]:
    if not data.startswith(PNG_HEADER) or len(data) < 24 or data[12:16] != b"IHDR":
        raise ValueError("upscaler did not produce a valid PNG")
    return struct.unpack(">II", data[16:24])


def _upscale_one(original: bytes, cfg: dict, cancel: threading.Event | None = None) -> tuple[bytes, float]:
    exe = Path(cfg["exe"])
    model_dir = Path(cfg.get("model_dir") or exe.parent / "models")
    name = cfg.get("model", "realesrgan-x4plus-anime")
    timeout = float(cfg.get("timeout_s") or 120)
    if not exe.is_file():
        raise FileNotFoundError(f"upscaler executable missing: {exe}")
    if not (model_dir / (name + ".bin")).is_file() or not (model_dir / (name + ".param")).is_file():
        raise FileNotFoundError(f"upscaler model missing: {name}")
    t = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="polystrata-upscale-") as tmp:
        src, dst = Path(tmp) / "original.png", Path(tmp) / "upscaled.png"
        src.write_bytes(original)
        cmd = [
            str(exe), "-i", str(src), "-o", str(dst), "-n", name, "-s", "4",
            "-m", str(model_dir), "-t", str(int(cfg.get("tile") or 128)),
            "-g", str(int(cfg.get("gpu") if cfg.get("gpu") is not None else 0)),
            "-j", "1:2:2", "-f", "png",
        ]
        proc = subprocess.Popen(
            cmd, cwd=str(exe.parent), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0),
        )
        if os.name == "nt":
            try:
                from serve.winjob import contain
                contain(proc)
            except Exception:
                pass
        try:
            while proc.poll() is None:
                if cancel and cancel.is_set():
                    raise RuntimeError("upscale cancelled")
                if time.monotonic() - t > timeout:
                    raise TimeoutError(f"upscale timed out after {timeout:g} s")
                time.sleep(.1)
            if proc.returncode != 0:
                raise RuntimeError(f"upscaler exited with code {proc.returncode}")
            if not dst.is_file():
                raise RuntimeError("upscaler produced no output")
            final = dst.read_bytes()
            w,h = png_size(original)
            if png_size(final) != (w * 4, h * 4):
                raise RuntimeError(f"unexpected upscaled PNG dimensions: {png_size(final)}")
            return final, round(time.monotonic() - t, 3)
        finally:
            if proc.poll() is None:
                proc.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=10)


def apply(gen, images: list[bytes], job: dict, cancel: threading.Event | None = None):
    """Return processed images and metrics. Failures never lose the original."""
    upcfg = gen.cfg.get("upscaler") or {}
    result = {"upscale_applied": False, "upscale_mode": job.get("upscale", "off")}
    if not upcfg.get("enabled", False):
        result["upscale_reason"] = "not_configured"
        return images, result
    enabled, reason = should_upscale(job, int(upcfg.get("max_output_side") or 3072))
    result["upscale_reason"] = reason
    if not enabled:
        return images, result
    result["upscale_attempted"] = True
    out = []
    original_files = []
    elapsed = []
    errors = []
    for original in images:
        started = time.monotonic()
        try:
            final, secs = _upscale_one(original, upcfg, cancel)
            original_files.append(gen.store(original))
            out.append(final)
            elapsed.append(secs)
        except Exception as exc:
            out.append(original)
            original_files.append(None)
            elapsed.append(round(time.monotonic() - started, 3))
            errors.append(f"{type(exc).__name__}: {exc}"[:240])
            if hasattr(gen, "_say"):
                gen._say(f"anime upscale failed; returning original PNG ({type(exc).__name__}: {str(exc)[:160]})")
    result["upscale_applied"] = any(o is not None for o in original_files)
    result["upscale_seconds"] = round(sum(elapsed), 3)
    result["upscale_original_files"] = original_files
    result["upscale_failures"] = len(errors)
    if errors:
        result["upscale_fallback"] = True
        result["upscale_error"] = "; ".join(errors)[:350]
    if result["upscale_applied"]:
        result["upscale_output_size"] = f"{job['width']*4}x{job['height']*4}"
    return out, result
