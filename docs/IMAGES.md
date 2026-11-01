# Image generation (Qwen-Image-2.1-Turbo)

PolyStrata's server can also make images: `POST /v1/images/generations`, OpenAI's images API. The model is
[Qwen-Image-2.1-Turbo](https://huggingface.co/Qwen/Qwen-Image-2.1-Turbo) (8 steps, CFG 1) in
[Unsloth's GGUF quants](https://huggingface.co/unsloth/Qwen-Image-2.1-Turbo-GGUF), run by
[stable-diffusion.cpp](https://github.com/leejet/stable-diffusion.cpp) (`sd-cli`), which setup compiles for your GPU.
It is optional and off unless you ask for it.

The model is under the **Qwen Research License**, not Apache 2.0: read
[its license](https://huggingface.co/Qwen/Qwen-Image-2.1-Turbo/blob/main/LICENSE) before you use the images
commercially.

## Set it up

    ./setup.sh --setup --images qwen-image          (Linux)
    START-HERE.bat --setup --images qwen-image      (Windows)

or, for an install that is already set up, without running the whole setup again:

    .venv/bin/python tools/image/install.py --config strata-<model>.json

Setup compiles `sd-cli` (stable-diffusion.cpp at a pinned commit) into `engine-image/`, downloads three files into
the data folder's `models/qwen-image-2.1-turbo/` and checks each against its SHA-256, and writes an `"images"` section
into the model's run config.

| piece | file | size |
|---|---|---|
| denoiser (by GPU memory) | `qwen-image-2.1-turbo-Q4_K_M.gguf` up to 8 GB, `Q6_K` up to 12 GB, `Q8_0` above | 4.2 / 6.3 / 7.6 GB |
| text encoder | `Qwen3-VL-8B-Instruct-UD-Q4_K_XL.gguf` | 5.1 GB |
| VAE | `qwen_image_2.1_vae_bf16.safetensors` | 0.7 GB |

Options: `--image-quant Q4_K_M|Q6_K|Q8_0` (a bigger one than recommended is kept, with a warning),
`--image-backend cuda|hip|vulkan|cpu`, `--image-gpu-policy auto|keep|shrink|unload`, `--images no` (takes the section
out of the config).

Backends: CUDA on NVIDIA (needs the CUDA Toolkit), HIP on AMD cards with their own memory (Linux, the same ROCm as the
engine), Vulkan on AMD APUs/iGPUs (Radeon 780M, Strix Point/Halo), Intel Arc and AMD under Windows. A Linux PC without
the Vulkan development files gets the LunarG Vulkan SDK unpacked into `third_party/` (no sudo); on Windows install the
Vulkan SDK first. On Vulkan setup adds `--vae-tiling --vae-tile-overlap 0.25`: an untiled 1024x1024 VAE decode on a Radeon 780M is one
GPU job long enough to trip the kernel driver's job timeout (no image), and a quarter's overlap decodes in half the
time of sd.cpp's default half overlap.

## Use it

> **Implemented (opt-in per deployment):**
> [768px first-party defaults, automatic Anime 4×, strict serial GPU
> scheduling and KV/LLM restoration](IMAGE_PIPELINE_DECISION.md).
> The generic OpenAI Images API's omitted-size default remains 1024px.

### Generate an image inside a chat

When image generation is enabled in the run config, PolyStrata's own web chat
also offers a built-in `polystrata__generate_image` tool. The model decides when
to call it: for example, ask it to "draw a mint-green robot cat". The resulting
PNG is displayed **in the same assistant message**, followed by the model's
reply. Nothing is sent to an external service. Calls are opt-in for the web
chat: they use `strata_image_tool: true`, are checked for the page's trusted
Origin, and are **not** offered to ordinary OpenAI API requests.

The built-in tool reuses `POST /v1/images/generations`'s image backend, FIFO
GPU turn and resource policy; it does not install an MCP server or replace
any external MCP tools. For an 8 GB GPU, `"gpu_policy": "unload"` and
`"reload": "eager"` let the service save a text conversation, unload
the LLM, create an image, and reload the LLM before resuming. If restoring
the KV session fails, subsequent turns can rebuild their context from the
chat history. The PNG is kept by the existing `/v1/images/files/` route,
using a random, unguessable file name; the name acts as its access token.

The built-in tool defaults to 768×768 and can select 512×512 or 1024×1024.
The image backend must be configured separately for each server deployment;
it is not tied to a particular text model. If image generation is not
configured, the regular chat and external MCP tools behave as before.

### Anime 4× post-processing (optional)

The first-party Chat tool and Images page use 768×768 by default and send
`strata_upscale: "auto"`. Explicit modes are `auto`, `off` and `4x`; the
optional `strata_style_hint` accepts `auto`, `anime` or `other`.
Anime-themed prompts (动漫、二次元、anime、manga、Miku/初音、立绘) are upscaled
by `realesrgan-x4plus-anime` to 3072×3072. Generic illustration, photo,
watercolor and pixel art are not automatically treated as anime.

Enable the feature per deployment with an `images.upscaler` section:

    "upscaler": {
      "enabled": true,
      "exe": "PATH_TO/realesrgan-ncnn-vulkan.exe",
      "model_dir": "PATH_TO/models",
      "model": "realesrgan-x4plus-anime",
      "tile": 128, "gpu": 1, "timeout_s": 180,
      "max_output_side": 3072
    }

The `gpu` value is the ncnn-Vulkan device index, not necessarily a CUDA
index. The source PNG is kept for viewing. If the requested 4× output
exceeds the size limit, automatic upscale is skipped. If upscale crashes,
times out or runs out of resources, the original PNG is returned. The
server runs Qwen-Image, waits for sd-cli to exit, runs Real-ESRGAN, waits
for it to exit, and only then eagerly reloads the text model and KV state.

The response's `strata` metadata reports `upscale_applied`,
`upscale_seconds`, `upscale_reason`, `upscale_original_files` and a
failure flag where appropriate. Third-party API calls remain unchanged:
when `size` is omitted they still default to 1024×1024, and no 4× is
applied unless `strata_upscale` is explicitly requested.

    curl http://127.0.0.1:8080/v1/images/generations -H "Content-Type: application/json" \
      -d '{"prompt": "a cartoon sloth mascot waving, flat vector illustration", "size": "1024x1024", "seed": 42}'

| field | |
|---|---|
| `prompt` | the description (required) |
| `size` | `WIDTHxHEIGHT`, each side a multiple of 32 from 256 to 2048 (`max_side`); default and `auto`: `1024x1024` |
| `n` | images, 1 to 4 (`max_n`); image *i* uses seed + *i* |
| `response_format` | `b64_json` (default) or `url` (`/v1/images/files/<random name>.png`; the server keeps the newest 64) |
| `seed` | PolyStrata's: a whole number for a repeatable image (default: random; the answer says which) |
| `steps` | PolyStrata's: 8 (Turbo's schedule). Another count uses stable-diffusion.cpp's own schedule, which Turbo was not made for |

The answer is OpenAI's shape (`created`, `data[].b64_json` or `data[].url`, `revised_prompt` = the prompt) plus
`data[].seed` and a `strata` object with the seconds, steps and backend. The settings are the model card's for
stable-diffusion.cpp: 8 steps, CFG 1, euler, Turbo's own sigmas, `--diffusion-fa`. An image from the API is the PNG
plain `sd-cli` writes with the same arguments, byte for byte.

## Sharing the GPU with the text model

One job at a time: an image job takes the same turn a chat request takes, so a chat request waits for the image and
the other way round. What the text model does meanwhile is the config's `images.gpu_policy`:

| policy | the text model during an image job |
|---|---|
| `auto` (default) | `keep` when the image job fits next to it (an APU always), else `shrink` when the engine was started with `--vram-elastic`, else `unload` |
| `keep` | stays loaded and idle |
| `shrink` | gives VRAM from its expert cache (as `POST /v1/vram`) and takes it back afterwards; the conversation stays |
| `unload` | its conversation is saved to a file, the engine is stopped, and the next start restores the conversation (`"reload": "eager"` starts it again right after the image; default `lazy`: with the next chat request) |

Each image runs its own `sd-cli` process, so no image memory stays on the GPU between images. Loading the three
files takes a few seconds per image from the page cache.

## Speed

Seconds per image through the API, Q8_0, 8 steps (one `sd-cli` process per image, loading included):

| GPU | backend | 1024x1024 | 512x512 |
|---|---|---|---|
| RTX A4000 16 GB | CUDA | 27 | 8.8 |
| Radeon 780M (iGPU) | HIP | 131 | 29 |
| Radeon 780M (iGPU) | Vulkan | 179 | 43 |

On the 780M HIP is faster but runs through the ROCm path whose queue evictions reset this GPU on current firmware
(a driver/firmware defect, not this feature; no reset was seen in these image runs); Vulkan is setup's default there, `--image-backend hip` picks HIP.
