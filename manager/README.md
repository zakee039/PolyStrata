# PolyStrata Manager (Windows)

Tauri 2 / Rust + TypeScript tray control panel. Windows 10 / 11. This is a native desktop manager; the existing Python server remains the inference service. **No Python GUI runtime is used.**

## Behavior

- Double-click the tray icon to show the small control panel. Close the window to hide it; the manager stays in the tray.
- Right-click tray icon for **Open panel / Start / Stop / Chat / Exit**.
- Start uses the active deployment JSON, localhost port 18081, and `E:\LLM\deploy\polystrata\.venv\Scripts\python.exe`.
- Stop waits for the server's `POST /unload?wait=30` response before stopping the Python process tree. That endpoint saves V1 KV state before unloading. If unload reports busy or fails, manager **does not forcibly stop** the service.
- Idle unload is **not** manual shutdown: the server remains alive, accepting requests and automatically reloading the model when needed.
- Settings: start with Windows (HKCU Run, per-user), load model on manager startup, watchdog with a capped three-attempt retry and backoff after unexpected exit, project root, model JSON path, and API port.
- **Automatic model load OFF (default)**: manager starts without starting the Python service. Clicking Start launches the Python API in `--lazy` mode; first request (or immediate load button) loads the model. **ON**: manager starts the service and preloads the model when manager starts. This option only takes effect automatically on next manager launch.
- Chat opens the original web interface; the manager does not duplicate it.
- Workflow Prefix Cache page is a labelled placeholder until the separate native registry/prefill project is implemented.
- An existing old `.bat` server on the same port is shown as an external service, not silently adopted or killed.

## v0.1.2 UI

- The second tab is **配置文件 (Configuration Files)**. It owns choosing the deployment JSON, the PolyStrata project directory, and the local API port. None of these are editable from General Settings.
- Each JSON is listed **by filename** with the associated model name, maximum context tokens, and quantization. The **查看 (View)** action opens a read-only details dialog showing the model GGUF path, engine, tokenizer, KV precision, vision projector, optional draft model, VRAM reserve, memory cache budget, and idle unload timer (when set).
- The Overview card shows the **active config filename**, model name, and configured context window even when the server is stopped.
- **缓存命中率 (Cache hit rate)** now counts completed requests where at least one prompt token was reused divided by the number of completed requests in the backend's recent history (up to 500), not reused-token share. Reused-token count remains visible for context. With no requests the UI displays a dash rather than an invented zero.
- The General Settings tab holds only manager startup behavior, watchdog, and an explanation of where deployment settings moved.

## Development

```powershell
cd E:\LLM\deploy\polystrata\manager
npm ci
npm run dev
```

Requirements: Rust stable MSVC target, VS C++ Build Tools, Windows SDK, WebView2, Node.js. These are present on the current workstation.

To build without an installer: `npm run build -- --no-bundle`; the portable .exe is under `src-tauri\target\release\polystrata-manager.exe`.

To build the NSIS installer: `npm run build`. Installer output is under `src-tauri\target\release\bundle\nsis`.

The manager initially locates the project through its compile-time source root or the `POLYSTRATA_HOME` environment variable. You can change the **PolyStrata deployment directory** in Settings later, without rebuilding. The Python server and virtual environment are resolved from that directory. When moving a deployment, select the new root and model configuration before starting.

Manager settings: `%LOCALAPPDATA%\PolyStrataManager\settings.json`.
Service log: `%LOCALAPPDATA%\PolyStrataManager\service.log`.

Do not make the server publicly accessible without API key and explicit origin controls. Do not commit machine-local settings, GGUF weights, SQLite history, KV snapshots, generated installers, or secrets.

## Self-test

Run the compiled native executable with `--self-test` to verify a real start/stop cycle without loading GPU weights. Use `--self-test-full` to also validate actual model preloading and unloading (uses GPU resources). The test uses the existing V1 service with `--lazy`, checks `/metrics`, safely unloads and stops the service, and writes `%LOCALAPPDATA%\\PolyStrataManager\\self-test.json`. Both integration modes passed on 2026-10-31: lazy mode returned `model_state=unloaded`, and full model mode returned `model_state=idle`; both reported `api_started=true` and `api_stopped=true`.

## Implementation notes

- On intentional Stop, watchdog is disabled before stopping. A manual restart resets the desired state. A model idle unload does not require supervisor restart.
- On abnormal process exit, watchdog may restart no more than three times in a manager session.
- The manager only stops processes it personally started, to prevent accidentally killing another localhost service.
- Live metrics come from `GET /metrics`. When the API is offline, GPU VRAM readings use local `nvidia-smi.exe` as a fallback. Decode/Prefill speeds use the current or most recent request, and the scope of token totals is clearly labelled.
- The default production config still contains `idle_unload_s = 900`, RAM cached conversations = 4, max disk KV snapshots = 5; these remain managed by the existing backend and are not modified by launching this manager.
