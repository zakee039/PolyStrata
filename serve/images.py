"""serve/images.py - image generation (Qwen-Image-2.1-Turbo) through stable-diffusion.cpp's sd-cli.

POST /v1/images/generations takes OpenAI's request shape (prompt, size, n, response_format b64_json|url) plus PolyStrata's
own fields seed and steps.  Each request runs one `sd-cli` process with the arguments the model's card gives for
stable-diffusion.cpp (8 steps, CFG 1, euler, Turbo's sigmas, --diffusion-fa) and returns the PNG files that process
wrote, unchanged: an image from the API is byte for byte the image plain sd-cli writes with the same arguments.

The GPU is shared with the text engine one job at a time: an image job takes the service's turn (the FIFO every chat
request takes), so no chat turn runs on the GPU meanwhile, and the config's "gpu_policy" says what happens to the
text engine during the job (gpu_turn):
  keep    - it stays loaded and idle (an APU's shared memory, a card with room for both)
  shrink  - an engine started with --vram-elastic gives the image job VRAM from its expert cache and takes it back
            afterwards (POST /v1/vram's mechanism; the conversation stays in the engine)
  unload  - its conversation is saved to a session file, the engine is stopped, and the next start restores it
  auto    - (default) keep when there is room for the image job next to it, else shrink when the engine can,
            else unload
The process per request keeps nothing on the GPU between images: when the job ends, all of its memory is free again.

The config's "images" section (written by setup --images, tools/image/install.py):
  {"exe": "engine-image/sd-cli", "backend": "vulkan", "diffusion_model": "...Q8_0.gguf", "vae": "...",
   "llm": "...Qwen3-VL-8B-Instruct-UD-Q4_K_XL.gguf", "quant": "Q8_0", "args": ["--diffusion-fa"],
   "gpu_policy": "auto", "reload": "lazy"|"eager", "max_n": 4, "max_side": 2048, "timeout_s": 3600,
   "lib_dirs": [...], "uma": false, "need_mib": null, "out_dir": null, "keep_files": 64,
   "native": false}   (native: PolyStrata's own DiT runner, tools/image/sdcpp_native; CUDA / HIP builds)
"""
from __future__ import annotations

import base64
import contextlib
import os
import random
import re
import secrets
import subprocess
import tempfile
import threading
import time
from pathlib import Path

# Qwen-Image-2.1-Turbo's own schedule (model_index.json "sample_sigmas") plus the terminal 0, as Unsloth's card passes
# it to sd-cli; it is an 8-step schedule, so another step count leaves the schedule to sd.cpp
TURBO_SIGMAS = "1.0,0.978453,0.95418,0.926626,0.89508,0.845148,0.704534,0.414568,0.0"
TURBO_STEPS = 8
MODEL_NAME = "qwen-image-2.1-turbo"
POLICIES = ("auto", "keep", "shrink", "unload")
SIZE_STEP = 32                     # sd.cpp's Qwen-Image-2.1 docs: dimensions divisible by 32
MIN_SIDE = 256
FILE_NAME = re.compile(r"^[0-9a-f]{32}\.png$")
LORA_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
LORA_STEM = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
STEP_LINE = re.compile(r"\|\s*(\d+)/(\d+)\s*-")      # sd.cpp's progress bar: "|====    | 3/8 - 14.1s/it"


class ImageRequestError(ValueError):
    """A request the image endpoint refuses (400)."""


class ImageJobFailed(RuntimeError):
    """sd-cli ended without the images (500); the message carries the end of its log."""


def parse_size(value, max_side: int) -> tuple[int, int]:
    """OpenAI's size, "1024x1024" (or "auto": 1024x1024) -> (width, height); each side a multiple of 32 from 256 to
    max_side."""
    if value is None or value == "auto":
        return 1024, 1024
    if not isinstance(value, str):
        raise ImageRequestError("size: a string WIDTHxHEIGHT, e.g. \"1024x1024\"")
    m = re.fullmatch(r"\s*(\d{2,5})\s*[xX×]\s*(\d{2,5})\s*", value)
    if not m:
        raise ImageRequestError(f"size: WIDTHxHEIGHT, e.g. \"1024x1024\", not {value!r}")
    w, h = int(m.group(1)), int(m.group(2))
    for side in (w, h):
        if side < MIN_SIDE or side > max_side or side % SIZE_STEP:
            raise ImageRequestError(f"size {w}x{h}: each side a multiple of {SIZE_STEP} from {MIN_SIDE} to {max_side}")
    return w, h


def _int_field(req: dict, key: str, lo: int, hi: int, default):
    v = req.get(key)
    if v is None:
        return default
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        raise ImageRequestError(f"{key}: a whole number from {lo} to {hi}")
    return v


def available_loras(cfg: dict) -> list[dict]:
    """Safe, server-owned LoRA catalog; never accept a path from the browser."""
    result = []
    entries = cfg.get("loras") or {}
    if not isinstance(entries, dict):
        return result
    for ident, entry in entries.items():
        if not isinstance(ident, str) or not LORA_ID.fullmatch(ident) or not isinstance(entry, dict):
            continue
        file = Path(str(entry.get("path") or ""))
        usable = file.is_file() and file.suffix.lower() == ".safetensors" and bool(LORA_STEM.fullmatch(file.stem))
        result.append({"id": ident, "name": str(entry.get("name") or ident),
                       "available": bool(usable), "family": "qwen_turbo"})
    return result


def resolve_lora(req: dict, cfg: dict) -> dict | None:
    ident = req.get("strata_lora_id")
    if ident in (None, "", "none"):
        return None
    if not isinstance(ident, str) or not LORA_ID.fullmatch(ident):
        raise ImageRequestError("strata_lora_id: invalid LoRA id")
    if (cfg.get("family") or "qwen_turbo") != "qwen_turbo":
        raise ImageRequestError("LoRA is available only for Qwen-Image 2.1 models")
    entry = (cfg.get("loras") or {}).get(ident)
    if not isinstance(entry, dict):
        raise ImageRequestError("strata_lora_id: LoRA not registered")
    path = Path(str(entry.get("path") or ""))
    if not path.is_file() or path.suffix.lower() != ".safetensors" or not LORA_STEM.fullmatch(path.stem):
        raise ImageRequestError("selected LoRA file is unavailable or invalid")
    if not path.resolve().is_relative_to(Path(cfg.get("lora_dir") or "").resolve()):
        raise ImageRequestError("LoRA file is outside the configured model directory")
    return {"id": ident, "path": path, "name": str(entry.get("name") or ident), "strength": 1.0}


def parse_request(req: dict, cfg: dict) -> dict:
    """The fields of one /v1/images/generations request, checked.  Unknown OpenAI fields (quality, style, user,
    background, moderation) are accepted and ignored; an output format other than PNG is refused."""
    prompt = req.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ImageRequestError("prompt: a non-empty string")
    if len(prompt) > 16000:
        raise ImageRequestError("prompt: at most 16000 characters")
    if "\0" in prompt:
        raise ImageRequestError("prompt: no NUL characters")
    fmt = req.get("response_format") or "b64_json"
    if fmt not in ("b64_json", "url"):
        raise ImageRequestError("response_format: b64_json or url")
    out_fmt = req.get("output_format")
    if out_fmt not in (None, "png"):
        raise ImageRequestError("output_format: only png")
    w, h = parse_size(req.get("size"), int(cfg.get("max_side") or 2048))
    n = _int_field(req, "n", 1, int(cfg.get("max_n") or 4), 1)
    steps = _int_field(req, "steps", 1, 100, int(cfg.get("steps") or (28 if cfg.get("family") == "sdxl" else TURBO_STEPS)))
    seed = _int_field(req, "seed", 0, 2**62, None)
    if seed is None:
        seed = random.SystemRandom().randrange(0, 2**31)
    negative = req.get("negative_prompt", "")
    if not isinstance(negative, str) or len(negative) > 3000 or "\0" in negative:
        raise ImageRequestError("negative_prompt: text up to 3000 characters, no NUL")
    reference_png = None
    reference = req.get("strata_reference_image")
    strength = req.get("strata_denoise_strength", 0.65)
    if reference is not None:
        family = cfg.get("family") or "qwen_turbo"
        if family != "sdxl" and not (family == "qwen_turbo" and cfg.get("llm_vision")):
            raise ImageRequestError("selected image model does not support direct reference images")
        if not isinstance(reference, str) or len(reference) > 7_200_000:
            raise ImageRequestError("reference image must be a PNG, JPEG or WebP data URL up to 5 MB")
        if isinstance(strength, bool) or not isinstance(strength, (int, float)) or not 0.05 <= strength <= 1:
            raise ImageRequestError("strata_denoise_strength: between 0.05 and 1")
        import base64, binascii, io
        from PIL import Image, ImageOps
        match = re.fullmatch(r"data:image/(png|jpeg|webp);base64,([a-zA-Z0-9+/=]+)", reference)
        if not match:
            raise ImageRequestError("reference: expected a PNG, JPEG or WebP data URL")
        try:
            raw = base64.b64decode(match.group(2), validate=True)
            if not 0 < len(raw) <= 5 * 1024 * 1024:
                raise ValueError("reference image size exceeded")
            with Image.open(io.BytesIO(raw)) as probe:
                if probe.format not in ("PNG", "JPEG", "WEBP") or probe.width * probe.height > 16_000_000:
                    raise ValueError("unsupported reference image")
                probe.verify()
            with Image.open(io.BytesIO(raw)) as image:
                image = ImageOps.exif_transpose(image).convert("RGB")
                if image.size != (w, h):
                    image = ImageOps.fit(image, (w, h), method=Image.Resampling.LANCZOS)
                buffer = io.BytesIO()
                image.save(buffer, format="PNG")
                reference_png = buffer.getvalue()
        except (OSError, ValueError, binascii.Error) as exc:
            raise ImageRequestError("reference: unsupported or corrupted image") from exc
    from serve.upscale import options
    upscale, style_hint = options(req)
    lora = resolve_lora(req, cfg)
    return {"prompt": prompt, "negative_prompt": negative, "reference_png": reference_png,
            "denoise_strength": float(strength) if reference_png is not None else None,
            "width": w, "height": h, "n": n, "steps": steps, "seed": seed,
            "response_format": fmt, "upscale": upscale, "style_hint": style_hint, "lora": lora}


def postprocess(gen, images: list[bytes], job: dict, cancel: threading.Event | None = None):
    """Serial postprocess inside gpu_turn, before eager-reloading the text LLM.

    The feature is opt-in. Failures retain the original PNGs.
    """
    from serve.upscale import apply
    return apply(gen, images, job, cancel)


def sd_cli_args(cfg: dict, job: dict, out_pattern: str) -> list[str]:
    """The sd-cli command for one job: the model card's stable-diffusion.cpp command (8 steps, CFG 1, euler, Turbo's
    sigmas, --diffusion-fa from the config's args), the size, seed and batch count, and the config's other args."""
    family = cfg.get("family") or "qwen_turbo"
    cmd = [str(cfg["exe"])]
    if family == "sdxl":
        # A standalone SDXL checkpoint contains UNet, CLIP-L/G and VAE.
        # It must not load the Qwen text encoder or Qwen Turbo sigmas.
        cmd += ["--model", str(cfg["checkpoint"])]
    else:
        cmd += ["--diffusion-model", str(cfg["diffusion_model"]),
                "--vae", str(cfg["vae"]), "--llm", str(cfg["llm"])]
        if cfg.get("llm_vision"):
            cmd += ["--llm_vision", str(cfg["llm_vision"])]
    lora = job.get("lora")
    prompt = job["prompt"]
    if lora:
        prompt += f" <lora:{lora['path'].stem}:{lora['strength']:g}>"
        cmd += ["--lora-model-dir", str(lora["path"].parent), "--lora-apply-mode", "at_runtime"]
    cmd += ["-p", prompt, "--steps", str(job["steps"]),
            "--cfg-scale", str(cfg.get("cfg_scale", 7.0 if family == "sdxl" else 1.0)),
            "--sampling-method", str(cfg.get("sampler", "euler")),
            "-W", str(job["width"]), "-H", str(job["height"])]
    negative = job.get("negative_prompt") or (cfg.get("negative_prompt") if family == "sdxl" else "")
    if negative:
        cmd += ["--negative-prompt", str(negative)]
    if job.get("init_image_path"):
        if family == "sdxl":
            cmd += ["--init-img", job["init_image_path"],
                    "--strength", str(job.get("denoise_strength") or 0.65)]
        elif family == "qwen_turbo" and cfg.get("llm_vision"):
            cmd += ["--ref-image", job["init_image_path"]]
            if job.get("character_weak_reference"):
                # The role image contributes appearance only; explicit user
                # intent in the text prompt takes precedence.
                cmd += ["--img-cfg-scale", str(cfg.get("character_img_cfg_scale", 0.7))]
        else:
            raise ImageRequestError("direct reference images are unavailable for this image model")
    if family == "qwen_turbo" and job["steps"] == TURBO_STEPS:
        cmd += ["--sigmas", str(cfg.get("sigmas") or TURBO_SIGMAS)]
    cmd += ["-s", str(job["seed"])]
    if job["n"] > 1:
        cmd += ["-b", str(job["n"])]
    cmd += ["-o", out_pattern]
    cmd += [str(a) for a in (cfg.get("args") or [])]
    if cfg.get("native") is True:                   # PolyStrata's DiT runner inside sd-cli (CUDA / HIP builds)
        cmd += ["--model-args", "qwen_image_2_1_native=1"]
    return cmd


def image_env(cfg: dict, base: dict | None = None) -> dict:
    """sd-cli's environment: the libraries its backend needs (ROCm's, setup's lib_dirs) first on the search path, the
    GPU to use when the config names one, and the config's own "env"."""
    env = dict(os.environ if base is None else base)
    dirs = [str(d) for d in cfg.get("lib_dirs") or [] if Path(d).is_dir()]
    if dirs:
        var = "PATH" if os.name == "nt" else "LD_LIBRARY_PATH"
        env[var] = os.pathsep.join(dirs + ([env[var]] if env.get(var) else []))
    gpu = cfg.get("gpu")
    if gpu is not None:
        var = {"cuda": "CUDA_VISIBLE_DEVICES", "hip": "HIP_VISIBLE_DEVICES",
               "vulkan": "GGML_VK_VISIBLE_DEVICES"}.get(cfg.get("backend"))
        if var:
            env[var] = str(gpu)
            if var == "CUDA_VISIBLE_DEVICES":
                env["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
    for k, v in (cfg.get("env") or {}).items():
        env[str(k)] = str(v)
    return env


def need_mib(cfg: dict, width: int, height: int) -> int:
    """The GPU memory an image job is planned with: the three model files plus the working buffers (about 2 GiB plus
    4 GiB per megapixel at Turbo's settings; the 780M baseline peaked 7.4 GB over the weights at 1024x1024 on HIP);
    the config's "need_mib" overrides it."""
    if cfg.get("need_mib"):
        return int(cfg["need_mib"])
    files = 0
    keys = ("checkpoint",) if cfg.get("family") == "sdxl" else ("diffusion_model", "vae", "llm", "llm_vision")
    for k in keys:
        try:
            files += Path(cfg[k]).stat().st_size
        except (OSError, KeyError, TypeError):
            pass
    work = (2 << 30) + int(4 * (1 << 30) * width * height / (1024 * 1024))
    return (files + work) >> 20


class ImageGen:
    """One sd-cli process per request, one request at a time (the service's turn, gpu_turn)."""

    def __init__(self, cfg: dict, log=None):
        self.cfg = dict(cfg)
        policy = self.cfg.get("gpu_policy") or "auto"
        if policy not in POLICIES:
            raise ValueError(f'images "gpu_policy" must be one of {", ".join(POLICIES)}, not {policy!r}')
        reload = self.cfg.get("reload") or "lazy"
        if reload not in ("lazy", "eager"):
            raise ValueError(f'images "reload" must be lazy or eager, not {reload!r}')
        self.family = self.cfg.get("family") or "qwen_turbo"
        if self.family not in ("qwen_turbo", "sdxl"):
            raise ValueError(f'unsupported image model family {self.family!r}')
        required = ("exe", "checkpoint") if self.family == "sdxl" else ("exe", "diffusion_model", "vae", "llm")
        for k in required:
            if not isinstance(self.cfg.get(k), str) or not self.cfg[k]:
                raise ValueError(f'the config\'s "images" section needs "{k}" (run setup with --images again)')
        self.policy, self.reload = policy, reload
        self.model = self.cfg.get("model_name") or MODEL_NAME
        self.log = log
        out = self.cfg.get("out_dir")
        self.dir = Path(out) if out else Path(tempfile.mkdtemp(prefix="strata-images-"))
        self.dir.mkdir(parents=True, exist_ok=True)
        self.keep_files = int(self.cfg.get("keep_files") or 64)
        self.lock = threading.Lock()              # one sd-cli at a time even when the service's turn is not taken
        self.totals = {"images": 0, "jobs": 0, "seconds": 0.0, "failed": 0}
        self.last = None                           # the last job's figures (/v1/status)
        self.progress = None                       # (step, steps) of the running job

    def missing(self) -> list[str]:
        required = ("exe", "checkpoint") if self.family == "sdxl" else ("exe", "diffusion_model", "vae", "llm")
        if self.family == "qwen_turbo" and self.cfg.get("llm_vision"):
            required += ("llm_vision",)
        return [self.cfg[k] for k in required if not Path(self.cfg[k]).is_file()]

    def _say(self, text: str) -> None:
        print(f"[strata] {text}", flush=True)
        if self.log is not None:
            try:
                self.log.write(f"[images] {text}\n")
                self.log.flush()
            except (OSError, ValueError):
                pass

    def _prune(self) -> None:
        """Keep the newest keep_files PNGs of response_format url (the oldest go)."""
        files = sorted((p for p in self.dir.glob("*.png") if FILE_NAME.match(p.name)), key=lambda p: p.stat().st_mtime)
        for p in files[:max(0, len(files) - self.keep_files)]:
            with contextlib.suppress(OSError):
                p.unlink()

    def file(self, name: str) -> Path | None:
        """A stored image (response_format url) by its name, or None."""
        if not FILE_NAME.match(name or ""):
            return None
        p = self.dir / name
        return p if p.is_file() else None

    def run(self, job: dict, cancel: threading.Event | None = None) -> tuple[list[bytes], dict]:
        """Run sd-cli for `job` (parse_request's dict) -> (the PNG files' bytes in order, figures).  The caller holds
        the GPU turn.  ImageJobFailed when sd-cli fails, times out or is cancelled."""
        work = Path(tempfile.mkdtemp(prefix="job-", dir=self.dir))
        pattern = str(work / "img_%d.png")
        cmd_job = job
        if job.get("reference_png") is not None:
            init = work / "reference.png"
            init.write_bytes(job["reference_png"])
            cmd_job = {**job, "init_image_path": str(init)}
        cmd = sd_cli_args(self.cfg, cmd_job, pattern)
        timeout = float(self.cfg.get("timeout_s") or 3600)
        logf = work / "sd-cli.log"
        t0 = time.monotonic()
        self.progress = (0, job["steps"])
        with self.lock:
            with open(logf, "w", encoding="utf-8", errors="replace") as lf:
                try:
                    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                            stdin=subprocess.DEVNULL, env=image_env(self.cfg),
                                            cwd=str(work), text=True, errors="replace", bufsize=1)
                except OSError as e:
                    # A reference may already be materialized when launching
                    # the subprocess fails. Do not leave it behind on disk.
                    lf.close()
                    for stale in (work / "reference.png", logf):
                        with contextlib.suppress(OSError):
                            stale.unlink()
                    with contextlib.suppress(OSError):
                        work.rmdir()
                    raise ImageJobFailed(f"sd-cli did not start ({cmd[0]}): {e}") from e
                try:
                    from serve.winjob import contain
                    contain(proc)                 # ends with the server (Windows)
                except Exception:
                    pass
                why = None

                def pump():
                    for line in proc.stdout:
                        lf.write(line)
                        m = STEP_LINE.search(line)
                        if m:
                            self.progress = (int(m.group(1)), int(m.group(2)))
                reader = threading.Thread(target=pump, daemon=True)
                reader.start()
                while proc.poll() is None:
                    if cancel is not None and cancel.is_set():
                        why = "cancelled (the client left)"
                    elif time.monotonic() - t0 > timeout:
                        why = f"it ran past timeout_s ({timeout:g} s)"
                    if why:
                        proc.kill()
                        break
                    time.sleep(0.2)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=30)
                reader.join(timeout=10)
        secs = time.monotonic() - t0
        self.progress = None
        log = logf.read_text(encoding="utf-8", errors="replace")
        outs = [work / f"img_{i}.png" for i in range(job["n"])]
        try:
            if why or proc.returncode != 0 or not all(p.is_file() for p in outs):
                self.totals["failed"] += 1
                tail = "\n".join(log.strip().splitlines()[-12:])
                what = why or (f"sd-cli exited with code {proc.returncode}" if proc.returncode
                               else "sd-cli wrote no image")
                self._say(f"image job failed: {what}\n{tail}")
                raise ImageJobFailed(f"{what}; the end of its log:\n{tail}")
            images = [p.read_bytes() for p in outs]
        finally:
            for p in work.iterdir():
                with contextlib.suppress(OSError):
                    p.unlink()
            with contextlib.suppress(OSError):
                work.rmdir()
        figures = {"seconds": round(secs, 2), "steps": job["steps"], "n": job["n"],
                   "reference_used": bool(job.get("reference_png")),
                   "size": f"{job['width']}x{job['height']}", "seed": job["seed"],
                   "backend": self.cfg.get("backend"), "quant": self.cfg.get("quant")}
        for key, pat in (("text_encode_ms", r"get_learned_condition completed, taking ([0-9.]+) ?ms"),
                         ("sampling_s", r"sampling completed, taking ([0-9.]+) ?s"),
                         ("vae_decode_s", r"decode_first_stage completed, taking ([0-9.]+) ?s")):
            m = re.findall(pat, log)
            if m:
                figures[key] = float(m[-1])
        self.totals["jobs"] += 1
        self.totals["images"] += job["n"]
        self.totals["seconds"] += secs
        self.last = figures
        return images, figures

    def store(self, data: bytes) -> str:
        """Keep one PNG for response_format url -> its file name (32 random hex digits: the name is the access)."""
        name = secrets.token_hex(16) + ".png"
        (self.dir / name).write_bytes(data)
        self._prune()
        return name

    def response(self, images: list[bytes], job: dict, figures: dict, base_url: str) -> dict:
        data = []
        for i, png in enumerate(images):
            item = {"revised_prompt": job["prompt"], "seed": job["seed"] + i}
            if job["response_format"] == "url":
                item["url"] = f"{base_url}/v1/images/files/{self.store(png)}"
            else:
                item["b64_json"] = base64.b64encode(png).decode("ascii")
            data.append(item)
        return {"created": int(time.time()), "data": data, "model": self.model,
                "output_format": "png",
                "size": figures.get("upscale_output_size") or f"{job['width']}x{job['height']}",
                "strata": figures}

    def status(self) -> dict:
        out = {"enabled": True, "model": self.model, "backend": self.cfg.get("backend"),
               "quant": self.cfg.get("quant"), "gpu_policy": self.policy, **self.totals}
        if self.progress:
            out["step"], out["steps"] = self.progress
        if self.last:
            out["last"] = self.last
        return out


def service_image_file(svc, name: str) -> Path | None:
    """Resolve an opaque PNG id across Chat and Images, including after restart.

    With SessionV1 enabled the generated-images directory is the sole source
    of truth for all Chat and Images outputs. No old temporary-directory
    fallback is used in Mikuchin deployments.
    """
    if not isinstance(name, str) or FILE_NAME.fullmatch(name) is None:
        return None
    sessions = getattr(svc, "session_v1", None)
    if sessions is not None:
        path = sessions.root / "generated-images" / name
        return path if path.is_file() else None
    # PolyStrata can also run without SessionV1; its own generator then owns
    # the outputs. This is normal standalone operation, not a migration shim.
    default = getattr(svc, "images", None)
    generators = ([default] if default is not None else [])
    generators += list(getattr(svc, "image_profiles", {}).values())
    for gen in generators:
        found = gen.file(name)
        if found is not None:
            return found
    return None


# ------------------------------------------------------------------------------------------- the GPU turn
def resolve_policy(svc, gen: ImageGen, need: int) -> str:
    """auto -> keep / shrink / unload for this job (see the module's docstring); a fixed policy is kept, except that
    an engine that is not running has nothing to give back (keep)."""
    eng = svc.engine
    running = not hasattr(eng, "alive") or eng.alive()
    if not running or not hasattr(eng, "unload"):
        return "keep"
    if gen.policy != "auto":
        return gen.policy
    if gen.cfg.get("uma"):
        return "keep"                         # an APU: the image job and the engine share the RAM either way
    free = svc.free_vram_mib()
    if free is None or free >= need:
        return "keep"
    if engine_elastic(eng):
        return "shrink"
    return "unload"


def engine_elastic(eng) -> bool:
    """An engine that resizes its expert cache on request (--vram-elastic, #533)."""
    if not hasattr(eng, "vram"):
        return False
    if (getattr(eng, "info", None) or {}).get("vram_elastic"):
        return True
    spawn = getattr(eng, "spawn", None)
    args = spawn[1] if isinstance(spawn, (list, tuple)) and len(spawn) > 1 else []
    return "--vram-elastic" in (args or [])


def _client_gone(svc) -> Exception:
    """The server module's ClientGone (it may run as __main__, so not imported by name)."""
    import sys
    return getattr(sys.modules.get(type(svc).__module__), "ClientGone", RuntimeError)()


@contextlib.contextmanager
def gpu_turn(svc, gen: ImageGen, job: dict, cancel: threading.Event | None = None):
    """The image job's turn on the GPU: the service's FIFO (and, with parallel requests, the engine's control lines
    once no slot is generating), and the text engine made room for it per the policy.  Yields the policy used."""
    with svc.status_lock:
        svc.status["queued"] += 1
    queued = True
    try:
        with svc._turn(cancel):
            with svc.status_lock:
                svc.status["queued"] -= 1
            queued = False
            ctl = getattr(svc.engine, "ctl", None) if getattr(svc.engine, "batch", 0) else None
            if ctl is not None:
                while not ctl.acquire(timeout=0.25):
                    if cancel is not None and cancel.is_set():
                        raise _client_gone(svc)
            try:
                if ctl is not None:                   # running parallel requests finish first
                    while getattr(svc, "live_reqs", None):
                        if cancel is not None and cancel.is_set():
                            raise _client_gone(svc)
                        time.sleep(0.25)
                need = need_mib(gen.cfg, job["width"], job["height"])
                policy = resolve_policy(svc, gen, need)
                with svc.status_lock:
                    svc.status.update(busy=True, phase="generating an image", started=time.time(),
                                      first_token=None, prompt_tokens=None, generated=None, max_tokens=None)
                restore = _make_room(svc, gen, policy, need)
                try:
                    yield policy
                finally:
                    _give_back(svc, gen, policy, restore)
                    with svc.status_lock:
                        svc.status["busy"] = False
                    svc.last_request_at = time.time()
            finally:
                if ctl is not None:
                    ctl.release()
    finally:
        if queued:
            with svc.status_lock:
                svc.status["queued"] -= 1


def _make_room(svc, gen: ImageGen, policy: str, need: int):
    """Do what `policy` says before the job; -> what _give_back needs."""
    eng = svc.engine
    if policy == "shrink":
        try:
            out = eng.vram(need)
            gen._say(f"image job: the engine's expert cache gave VRAM back ({out.get('vram_free_mib')} MiB free)")
            return "shrunk"
        except Exception as e:                       # noqa: BLE001 - the job still runs, with what is free
            gen._say(f"image job: the engine could not give VRAM back ({e}); the job runs with what is free")
            return None
    if policy == "unload":
        saved = None
        if hasattr(eng, "session_file") and not getattr(eng, "batch", 0):
            # A KV snapshot is not a generated image. Keep it in the session
            # cache namespace, never alongside user-owned PNGs.
            sessions = getattr(svc, "session_v1", None)
            transient_dir = ((sessions.root / "image-session-cache") if sessions is not None
                             else Path(tempfile.gettempdir()) / "polystrata-image-turn")
            transient_dir.mkdir(parents=True, exist_ok=True)
            path = str(transient_dir / f"llm-session-{secrets.token_hex(6)}.bin")
            try:
                r = eng.session_file("save", path)
                saved = path
                gen._say(f"image job: the conversation ({r.get('tokens')} tokens) saved; unloading the model")
            except Exception as e:                   # noqa: BLE001 - unloading still frees the GPU
                gen._say(f"image job: the conversation could not be saved ({e}); unloading the model")
                with contextlib.suppress(OSError):
                    os.unlink(path)
        eng.unload()
        if svc.vision is not None and hasattr(svc.vision, "unload"):
            svc.vision.unload()
        return saved or "unloaded"
    return None


def _give_back(svc, gen: ImageGen, policy: str, restore) -> None:
    eng = svc.engine
    if restore == "shrunk":
        try:
            eng.vram(svc.vram_reserve)               # the reserve asked for last (None: the start's)
        except Exception as e:                       # noqa: BLE001
            gen._say(f"image job: the expert cache did not grow back ({e}); POST /v1/vram can retry")
    elif policy == "unload" and restore:
        if restore != "unloaded":
            previous = getattr(svc, "restore_after_load", None)
            if previous and previous != restore:
                with contextlib.suppress(OSError):
                    os.unlink(previous)
            svc.restore_after_load = restore         # Service.ensure_loaded restores it after the next start
        if gen.reload == "eager":
            try:
                svc.ensure_loaded()                  # the caller still holds the FIFO
            except Exception as e:                   # noqa: BLE001 - the next request tries again
                gen._say(f"image job: the model did not load again yet ({e}); the next request loads it")


def restore_session(svc) -> None:
    """After an engine start: restore the conversation an image job saved before it unloaded the engine."""
    path = getattr(svc, "restore_after_load", None)
    if not path:
        return
    svc.restore_after_load = None
    try:
        r = svc.engine.session_file("restore", path)
        print(f"[strata] the conversation from before the image job is back ({r.get('tokens')} tokens)", flush=True)
    except Exception as e:                           # noqa: BLE001 - the engine runs, without the old cache
        print(f"[strata] the conversation from before the image job was not restored ({e}); the next request "
              "reads its prompt again", flush=True)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(path)
