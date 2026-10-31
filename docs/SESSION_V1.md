# PolyStrata Unified Sessions V1 — Deploy-local Storage

## Layout

The deployment root is E:\LLM\deploy\polystrata. The GGUF model files and vision encoder remain untouched. The native engine was replaced with a locally built and GPU-tested Windows CUDA sm_86 engine on 2026-10-31; the previous executable is backed up.

E:\LLM\deploy\polystrata\chat-history\conversations.sqlite3 — all Web and opted-in API chats.
E:\LLM\deploy\polystrata\chat-history\images\<session-id>\<sha256>.<ext> — embedded uploaded images.
E:\LLM\deploy\polystrata\chat-history\attachments\<session-id>\<sha256>.txt — uploaded text attachments.
E:\LLM\deploy\polystrata\chat-history\generated-images\<random>.png — image-generation outputs when enabled.
E:\LLM\deploy\polystrata\kv-cache\<session-id>.bin — up to five FIFO disk KV snapshots.

All persistent data directories are gitignored. The old V1 test database under data/session_v1 was intentionally discarded. No migration was needed.

The launcher E:\LLM\deploy\polystrata\run-meromero-128k-int8.bat points to the V1 config at E:\LLM\deploy\benchmark\configs\reserve1024-128-v1.json.

## Configuration

session_v1_dir = E:\LLM\deploy\polystrata\chat-history
session_v1_kv_dir = E:\LLM\deploy\polystrata\kv-cache
session_v1_kv = true
idle_unload_s = 900

The server remains at 127.0.0.1:18081; context remains 131072 tokens. The launcher remains run-meromero-128k-int8.bat.

## Chat persistence and images

Web conversations go to the server's SQLite. Images uploaded from the Web or sent to the OpenAI chat API as embedded data:image/...;base64 URLs are decoded into per-session files. SQLite stores asset references, not large base64 strings. Text attachments from the Web are stored separately as UTF-8 files. GET /v1/strata/sessions/<session-id> returns the original message shape with rehydrated images and attachment contents. The Web retains a small metadata-only browser fallback.

Image generation results are stored as individual PNGs under chat-history/generated-images even for b64_json response format when the image-generation backend is enabled. This has not been GPU-tested with an image-generation model on the current MeroMero configuration.

External HTTP image URLs are not downloaded automatically. API clients should embed image bytes as a data URL to guarantee a local copy. API clients need to provide strata_session_id for persistent history; without an ID they remain stateless.

Deleting a KV snapshot must never delete the original chat or image attachments.

## Native Windows KV SAVE / RESTORE

C++ SAVE / RESTORE lives in src/poly/main.cpp, src/poly/conversations.hpp and src/poly/session_disk.hpp. A Windows CUDA 13.2 + MSVC 19.50 Release binary, compiled for RTX 3070 Ti (sm_86), now runs in engine-poly/strata-poly.exe. Disk snapshots include the token prefix, recurrent state and attention rows, not model weights. The format validates geometry, checksum and model identity before restoring device rows. Only text-only checkpoints are supported in V1; unsupported, mismatched or corrupt checkpoints fall back to fresh Prefill.

The five-file FIFO metadata is in SessionV1 and remains independent of chat persistence. Windows file caching handles repeat SSD reads; there is no RAM pinning policy. Snapshots save on a session switch or idle/manual unload; a compatible session snapshot restores automatically. Chat history and media survive KV eviction.

### Local deployment and rollback

Production engine: E:\LLM\deploy\polystrata\engine-poly\strata-poly.exe
Build/GPU regression scripts: E:\LLM\deploy\polystrata\cmake-build-kv\ (gitignored)
Production config: E:\LLM\deploy\benchmark\configs\reserve1024-128-v1.json

The following backups were made before KV activation:
- E:\LLM\deploy\polystrata\engine-poly\strata-poly.pre-kv-20261031.exe
- E:\LLM\deploy\polystrata\engine-poly\BUILD.pre-kv-20261031.json
- E:\LLM\deploy\benchmark\configs\reserve1024-128-v1.pre-kv-20261031.json

For rollback, stop the server, restore all three backup files to their corresponding unsuffixed paths, and relaunch run-meromero-128k-int8.bat. Restoring the original config disables native KV. Do not delete chat-history or its SQLite database to disable KV.

## Installed development tools

Visual Studio 2026 Build Tools (MSVC 19.50.35726), CMake 4.4.5, NVIDIA CUDA Toolkit 13.2 (nvcc 13.2.86), and Windows SDK 10.0.26100 are installed. Build from an x64 Developer Command Prompt via vcvars64.bat. Ensure CUDA bin/x64 is on PATH at runtime for cuBLAS.

## Tested (2026-10-31)

Seven Python SessionV1 unit tests passed, including Web/API history, real image assets, text files, FIFO, and wrong/corrupt image rejection.
Python syntax and JS syntax checks passed.
The real MeroMero server starts with the new deployment paths and its history API works.
A real local Web save of an embedded PNG and text attachment stored actual files in chat-history, and GET reconstructed them unchanged.
A real OpenAI chat request with session ID saved conversation content in the same SQLite DB.
Windows MSVC C++ disk-session regression passed after simplifying a filesystem::path expression that triggered a compiler-internal error (C1001); the optimized test also passed. The CUDA sm_86 Release build exited 0. Linking emitted LNK4098 but the resulting binary loaded and served the model.

Real GPU checks with G4-MeroMero-26B-A4B-it-uncensored-heretic-Q4_K_M.gguf, int8 KV and process restart:
- 19-token normal Gemma4 chat template: 18 tokens reused; all 12 generated token IDs matched exactly.
- 691-token normal chat template: 690 reused; all 12 output IDs matched. Cold Prefill ~1593 ms, disk restore ~324 ms, remaining Prefill ~175 ms, snapshot ~119 MB. These are small spot checks, not 128K benchmarks.
- A synthetic out-of-template 8-token prompt produced a different second generated token after restart. Exact match is not established for arbitrary invalid prompts.

HTTP API integration at isolated port 18082 passed session A→B save, B→A restore, unload/reload restore, all HTTP 200. The production port 18081 using the full 128K configuration passed the same tests with KV enabled; its isolated test sessions and snapshots were cleaned afterward. Seven Python SessionV1 tests passed again. Production engine SHA256: 4594d2f0e38147b08e98c30b0d1a785b730e19397aaa3fda11b11c0f4b26f868.
