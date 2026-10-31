# PolyStrata customization release 2

Source tag: `v0.1.45-poly.6-zakee.2`
Manager version: `0.1.3`
Previous source release: `v0.1.45-poly.6-zakee.1`

## Manager UI

- Overview now has a **Select Configuration** dropdown/dialog, rather than a permanent configuration page. The dialog lists all recognized and previously registered deployment JSONs; selection changes the active config but preserves the other registrations.
- A native **Register Configuration** file picker validates the model deployment JSON and registers the path without copying any GGUF files.
- The second navigation tab is now **API**. It contains the local port, API Key management, LAN access, and Cloudflare Tunnel settings.
- General Settings contains only auto-start, automatically load model, and service watchdog. No redundant explanation card or manual deployment path inputs.

## API and network

- API Key is generated on demand via Windows cryptographic RNG and stored using Windows DPAPI for the current user. The Tauri Manager passes it to the Python backend using `STRATA_API_KEY`. The UI supports reveal, copy, manually replacing, and generating a new key. Key rotation requires stopped service.
- Localhost-only listener is the default. Optional LAN mode binds `0.0.0.0`; firewall configuration is intentionally not automatically changed.
- Optional named Cloudflare Tunnel is managed by `cloudflared` with an encrypted Tunnel Token; the external hostname and Cloudflare route must be configured on the provider side. No public hostname is automatically created or claimed online.
- Secrets are not stored in `settings.json`, logged or committed. Cloudflare Tunnel remains disabled by default.

## Verification

- Manager TypeScript type-check and Rust release build passed on Windows.
- A Windows NSIS installer and portable EXE were generated for Manager 0.1.3.
- Real API-key-protected localhost lazy-start and safe-stop integration test passed. The API port was closed after stop.
- Session V1 Python tests remain passing.
- Public Cloudflare Tunnel end-to-end and LAN firewall access have **not** been verified without a configured account/token; those features are off by default.

Base `main` and the `original` tag remain unchanged.
