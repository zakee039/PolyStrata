# PolyStrata customization release 3

Source tag: `v0.1.45-poly.6-zakee.3`
Manager version: `0.1.4`
Previous source release: `v0.1.45-poly.6-zakee.2`

## Compact manager

- Removed redundant headings and descriptions from the API/Settings layout.
- API port, read-only API Key (Copy / Rotate), and LAN access each occupy one horizontal row.
- Cloudflare Tunnel no longer asks for the executable path. It shows hostname, encrypted Tunnel Token, Install / Test / Restart.
- Overview displays cache hit rate in one half-width card and Cloudflare Tunnel state, HTTPS latency, Test / Restart in the other.

## Tunnel actions

- **Install** checks for cloudflared and installs it with winget if needed, validates the requested public hostname and encrypted token, enables tunnel management, and starts the connector if the inference service is running.
- **Test** performs an external HTTPS request to `https://<configured-domain>/v1/models`, without forwarding the API Key or Tunnel Token. It only verifies successful end-to-end routing if the backend returns its expected API-authentication response. The displayed latency measures round-trip HTTP time rather than Cloudflare edge RTT.
- **Restart** restarts only the managed cloudflared process and requires the inference service to be running.
- Tunnel connection indicator uses locally bound cloudflared Prometheus metrics, reporting connected when at least one HA connection is observed. Running the process alone is not represented as proof of connection.

## Verification

- TypeScript check, Vite production build, Rust check and Windows Tauri release build passed.
- NSIS and portable Windows executables generated.
- Real API-key-protected lazy-start and safe-stop integration test passed without loading model weights.
- Actual tunnel end-to-end public connection **not tested** because no Cloudflare hostname/token was supplied. LAN and Tunnel remain disabled unless explicitly enabled.

No model weights, secrets, local settings or generated binaries were committed.
