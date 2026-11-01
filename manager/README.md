# PolyStrata Manager (Windows)

Tauri 2 / Rust + TypeScript tray control panel. Windows 10 / 11. This is a native desktop manager; the existing Python server remains the inference service. **No Python GUI runtime is used.**

## Behavior

- Double-click the tray icon to show the small control panel. Close the window to hide it; the manager stays in the tray.
- Right-click tray icon for **Open panel / Load model / Unload model / Chat / Exit**.
- Launching Manager **always starts and watches** the local Python API service, using the active deployment JSON and port 18081. Its process remains online while Manager runs. The startup auto-load option controls *model weights*, not whether the API starts.
- The main button keeps its original labels **启动服务 / 停止服务**, but now means **load/unload the model**. Unload saves active KV and releases GPU/RAM without stopping the API; the redundant 'load now' link is removed. Tray menu labels explicitly say Load / Unload model. The API process is stopped only when Manager exits.
- **Manual stop** sends `/unload?wait=30&manual=1`: KV is saved and GPU/RAM released, while inference calls return HTTP 503 until an explicit start. Manager persists this hold across restarts. **15-minute idle unload** is distinct: it releases the model but the next inference request automatically reloads it. The API stays online in both cases.
- Settings: start with Windows (HKCU Run, per-user), auto-load model on manager startup, and watchdog with a capped three-attempt retry. API settings live under the separate API tab; the selected deployment config is managed from the Overview dialog.
- **Automatic model load OFF (default)**: Manager starts the API in `--lazy` mode and leaves GPU model weights unloaded until the load button or a client inference request (unless manually held). **ON**: Manager starts the API with weights preloaded. In both cases the API stays online and its process is supervised. This setting takes effect on the next API launch.
- Chat opens the original web interface; the manager does not duplicate it.
- Workflow Prefix Cache page is a labelled placeholder until the separate native registry/prefill project is implemented.
- An existing old `.bat` server on the same port is shown as an external service, not silently adopted or killed.

## v0.1.4 UI

- The Overview card has a **选择配置文件 (Select configuration)** button. It opens a popup showing only explicitly registered configuration JSON files and the active configuration. Other JSONs on disk do not reappear automatically. **注册配置文件** opens a native file chooser. Switching the active config does not discard other registrations.
- The second tab is **API**. Port, read-only visible API Key with only **Copy / Rotate** actions, and LAN access each fit on one compact row. Tunnel shows only domain, encrypted token entry, and **Install / Test / Restart** buttons. A model-config, port or access change gracefully saves/unloads KV and automatically restarts the API with the new settings.
- Each JSON is listed **by filename** with the associated model name, maximum context tokens, and quantization. The **查看 (View)** action opens a read-only details dialog showing the model GGUF path, engine, tokenizer, KV precision, vision projector, optional draft model, VRAM reserve, memory cache budget, and idle unload timer (when set).
- The Overview card shows the **active config filename**, model name, and configured context window even when the server is stopped.
- The Overview bottom strip has equal-width **cache hit rate** and **Cloudflare Tunnel** panels. Cache hit rate counts completed requests reusing at least one prompt token divided by the backend's most recent completed requests (up to 500); without requests it displays a dash. Tunnel displays a status lamp, last measured HTTPS round-trip time, Test and Restart buttons. Connection status uses localhost-only cloudflared Prometheus metrics at port 23188 when running.
- The General Settings tab contains only manager startup behavior and watchdog; no deployment paths or explanatory card. The model/config details remain viewable from the selection dialog.

## v0.1.9 Windows port-release fix

- After the owned API is safely unloaded and its process tree is stopped, Manager waits up to 12 seconds for the old TCP listener to disappear before binding the new model on the same port. This prevents a transient Windows port-in-use error while the old Uvicorn child is still releasing the listener.
- If a switch operation still reports an error, the picker re-reads persisted settings to avoid showing the old model as active after the new config was already saved. It distinguishes a saved config with a not-yet-ready API from an unchanged selection.
- Verified on the running Windows manager using real WebView2 UIAutomation: Swift Flash → Gemma 4 → Swift Flash, each returned a healthy API at 18081 with the requested model and weights unloaded. No port errors or driver changes.

## v0.1.8 config switching fix

- The API is resident while Manager is open, so an owned/running API must not disable the **使用** action for another registered model. Only the current selection, an in-progress switch, transitional start/stop, or an external/unowned API blocks switching.
- Switching uses the existing backend safe unload/KV preservation, persists the newly selected JSON, and automatically restarts the API. Manual model load remains a separate action.
- The picker prevents duplicate switch requests and displays switch errors in the dialog.
- Verified in the real Windows WebView2 UI by accessibility inspection: Gemma **使用中** disabled and Swift Flash **使用** enabled while port 18081 is managed and unloaded.

## Development

```powershell
cd E:\LLM\deploy\polystrata\manager
npm ci
npm run dev
```

Requirements: Rust stable MSVC target, VS C++ Build Tools, Windows SDK, WebView2, Node.js. These are present on the current workstation.

To build without an installer: `npm run build -- --no-bundle`; the portable .exe is under `src-tauri\target\release\polystrata-manager.exe`.

To build the NSIS installer: `npm run build`. Installer output is under `src-tauri\target\release\bundle\nsis`.

The manager initially locates the project through its compile-time source root or the `POLYSTRATA_HOME` environment variable. Future deployments should keep the project and virtual environment in the same directory. Existing saved project paths are preserved internally but are no longer an ordinary user-facing setting. Import deployment JSON using the config picker; only its path is registered, not copied.

Manager settings: `%LOCALAPPDATA%\PolyStrataManager\settings.json`.
Service log: `%LOCALAPPDATA%\PolyStrataManager\service.log`.

The Manager automatically creates an API Key if absent; the API tab displays it in a compact read-only field with Copy and Rotate actions (no separate hide/reveal button). It passes the key to the Python service as `STRATA_API_KEY`, not as a command-line argument. The key and Cloudflare Tunnel token are encrypted under `%LOCALAPPDATA%\PolyStrataManager\*.dpapi`; they do not appear in the regular settings JSON. LAN access defaults to off; tunneling defaults to off. When enabled, Cloudflare Tunnel needs a valid Tunnel Token and the public hostname must be configured in Cloudflare's dashboard to forward to `http://127.0.0.1:<port>`. The Install action checks for the local `cloudflared` binary (and attempts to install it through `winget` if absent), stores the chosen hostname and encrypted token, and starts the connector when the inference service is running. It does **not** register a Cloudflare Tunnel or DNS hostname on your behalf: those must exist in your Cloudflare dashboard. The Test action probes `https://<domain>/v1/models` without disclosing any secret and measures HTTPS request latency; only PolyStrata's expected API 401 response is counted as end-to-end success. Restart affects the connector only. The installed `cloudflared` uses the `TUNNEL_TOKEN` environment variable; the Manager never displays that token. For public deployments, use HTTPS/Cloudflare Access and restrict access to the API; do not expose the port directly to the public Internet.

Do not make the server publicly accessible without API key and explicit origin controls. Do not commit machine-local settings, GGUF weights, SQLite history, KV snapshots, generated installers, or secrets.

## Self-test

Run the compiled native executable with `--self-test` to verify a real start/stop cycle without loading GPU weights. Use `--self-test-full` to also validate actual model preloading and unloading (uses GPU resources). The test uses the existing V1 service with `--lazy`, checks `/metrics`, safely unloads and stops the service, and writes `%LOCALAPPDATA%\\PolyStrataManager\\self-test.json`. Both integration modes passed on 2026-10-31: lazy mode returned `model_state=unloaded`, and full model mode returned `model_state=idle`; both reported `api_started=true` and `api_stopped=true`.

## Implementation notes

- Unloading the model **does not** disable the API watchdog; manual and idle unload retain the API process and V1 KV snapshots. Only exiting Manager requests a safe API shutdown. Automatic inference-triggered loading applies only after ordinary idle unload, not manual stop.
- On abnormal process exit, watchdog may restart no more than three times in a manager session.
- The manager only stops processes it personally started, to prevent accidentally killing another localhost service.
- Live metrics come from `GET /metrics`. When the API is offline, GPU VRAM readings use local `nvidia-smi.exe` as a fallback. Decode/Prefill speeds use the current or most recent request, and the scope of token totals is clearly labelled.
- The default production config still contains `idle_unload_s = 900`, RAM cached conversations = 4, max disk KV snapshots = 5; these remain managed by the existing backend and are not modified by launching this manager.
