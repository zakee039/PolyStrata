# PolyStrata customization release 5

Source tag: `v0.1.45-poly.6-zakee.5`
Manager version: `0.1.6`
Previous tag: `v0.1.45-poly.6-zakee.4`

## Persistent API and model lifecycle

- The Windows tray Manager always starts the localhost Python API when it launches, regardless of whether model auto-load is enabled.
- Startup setting `auto_load_model` now affects GPU/model preloading only. With the option off, the API stays online in lazy mode without loading model weights.
- The existing large Overview button retains the text **启动服务 / 停止服务**, but now performs the model-only **load / unload** API action. It never stops the resident API.
- Manual unload uses `POST /unload?wait=30`: the backend first saves active V1 KV, releases GPU/RAM model resources, and remains online. A new inference request can automatically load the model again.
- Removed the redundant **立即加载模型** link. The tray right-click menu now offers **加载模型 / 卸载模型** instead of start/stop API actions.
- The status message at the bottom left is black.
- The API is gracefully stopped when exiting Manager, rather than by pressing the main Overview button.
- Config, API port and LAN access changes are applied with a safe restart of the API service. An active query cannot be forcibly terminated merely to switch settings.

## Checks

- Tauri 2 Rust and TypeScript builds succeeded; Windows portable executable and NSIS installer were generated.
- On the Windows 10 desktop, launching the upgraded Manager automatically started the API (localhost port 18081) in the `unloaded` state, without reserving model GPU memory.
- A real `POST /load` followed by `POST /unload?wait=30` succeeded. The backend returned `idle` after loading and `unloaded` after unloading, while the API listener process PID remained unchanged.
- The old API was shut down only after a safe KV-saving model unload before upgrading; no model weights, secrets, local settings or compiled artifacts are committed.

## Scope

The existing API watchdog remains enabled by default and the 15-minute idle model unload setting is retained. Watchdog recovery by intentional crash injection was not performed.
