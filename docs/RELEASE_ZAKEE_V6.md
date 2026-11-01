# PolyStrata customization release 6

Source tag: `v0.1.45-poly.6-zakee.6`
Manager version: `0.1.7`
Previous tag: `v0.1.45-poly.6-zakee.5`

## Manual model stop vs idle auto-unload

- Clicking the Manager's main **停止服务** button sends `POST /unload?wait=30&manual=1`. The model unloads and any active KV is saved, but the Python HTTP API remains running.
- The server records `manual_unloaded=true` and reports the model as `paused`. New chat/completions, Responses or Anthropic generation requests cannot automatically start the engine; they receive HTTP 503 with a clear manual-stop message.
- Only explicit `POST /load`, `POST /v1/load`, or another explicit model-load action resumes. The Manager's **启动服务** button clears the persistent hold after successful loading.
- The ordinary idle timer still uses `unload(idle_for=900)` without the manual flag. Requests after idle unloading continue to automatically reload the model.
- Manager stores the manual-hold state in local settings and sets `STRATA_MANUAL_MODEL_HOLD` for new API processes. This prevents API or Manager restarts from unexpectedly undoing a user stop, regardless of the automatic-start-model preference.
- Manager status polling synchronizes the saved hold with explicit load/unload commands performed through its local API/Web UI.

## Verification

- Three isolated Service lifecycle unit tests passed (manual stop, ordinary idle unloading, stop when already unloaded).
- Seven session/KV persistence tests passed.
- Real API test on Windows 10: manual stop returned state `paused`; a chat request was refused with HTTP 503 and no engine reload.
- Explicit `/load` restored the model, and normal `/unload` preserved automatic loading; a subsequent completion succeeded.
- API PID stayed identical throughout both cycles, and a safe unload left the GPU model offline afterward.
- Windows Tauri Manager 0.1.7 release executable/NSIS installer compiled successfully.

No user models, data, API keys or token files are included.
