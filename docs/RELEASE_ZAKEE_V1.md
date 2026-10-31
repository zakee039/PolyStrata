# PolyStrata customization release 1

Tag: `v0.1.45-poly.6-zakee.1`
Branch: `polystrata`
Base: `VecSzn/PolyStrata` commit `20d839dbbaf34f28413e84d6e99301725f4f624b` (`v0.1.45-poly.6`)
Original baseline: `original` tag (same base commit).

## Changes

- Windows native CUDA GPU session SAVE/RESTORE with format/compatibility checks and native regression test.
- Unified Web/API session history in SQLite; user-uploaded images, text attachments, and generated images can be persisted in the deployment.
- Per-conversation disk KV snapshots, capped at five independent FIFO entries; graceful snapshot persistence on engine unload.
- Windows Tauri 2 / Rust native tray manager **v0.1.2**: start/stop, daemon/watchdog, startup settings, local telemetry, Chat shortcut, model configuration list/details.
- A design document for the planned Workflow Prefix Cache feature; **workflow registration and warming are not yet implemented**.
- Local data, generated binaries, test files, and model downloads excluded from Git.

## Verification

- Seven Python SessionV1 unit tests pass.
- `cmake-build-kv/poly_session_disk_test.exe` passes on the Windows development workstation.
- Manager TypeScript typecheck and Windows release/NSIS builds pass.
- Manager's real localhost API lazy-start/stop and full model start/stop self-tests passed during development.
- The compiled inference engine, GGUF weights, native runtime dependencies, local JSON deployment configuration, SQLite databases and KV snapshot files **are not bundled in this source tag**. They remain deployment-local and must be built/configured separately; see `docs/SESSION_V1.md` and `manager/README.md`.

## Repository layout

- `main`: original fork source, not overwritten.
- `polystrata`: customization development branch.
- `original`: immutable annotated tag at the unmodified upstream baseline.
- `v0.1.45-poly.6-zakee.1`: source release of this customization.

This release does not enable remote model access or install auto-start by default.
