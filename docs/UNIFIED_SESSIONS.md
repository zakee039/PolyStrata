# PolyStrata unified conversations

## Architecture

PolyStrata follows **LLM as a service**. The service owns the durable history and the disk KV lifecycle. Swapping the selected model never clears the shared history. The selected model supplies its inference and optional session-file SAVE/RESTORE capabilities.

- Chat history: chat-history/conversations.sqlite3, shared by registered models.
- KV: kv-cache/<model-fingerprint>/<conversation-id>.bin, isolated by fingerprint.
- Each model has five FIFO-managed dynamic KV positions. A disk-locked KV does not count against these five and is not evicted by FIFO.
- Pinning the chat title is not the same as locking its KV. Both are explicit operations.
- When a current-model snapshot does not exist, does not match the conversation prefix, or cannot be restored, the request is fulfilled using normal Prefill.
- A lock request with no snapshot is stored as a pending intent. The engine performs disk SAVE only on a safe serial session switch/unload, not from the HTTP handler thread.
- Models without native disk session support still use the exact same history database; they cannot claim a saved KV until one actually exists. Upstream `strata-poly` does not support disk sessions; the PolyStrata fork advertises its native SAVE/RESTORE implementation through `INFO session_files=1` before `READY`, and the server checks that capability rather than guessing from the executable name.
- The legacy one-model schema is migrated in place; a zero-byte SQLite database initializes automatically.
- Delete removes all model-specific KV and attachments associated with the conversation.

## UI and endpoints

The Chat view has a left history sidebar with New chat and the saved conversation list. Each row offers Rename, Pin, Export Markdown, Delete and Lock/Release current model KV. The old duplicate New chat / Export toolbar buttons are hidden.

Authenticated same-origin API routes (the web sidebar prompts for the API key if the request is unauthorized; saving the key in Settings immediately retries session discovery):
GET /v1/strata/sessions; GET /v1/strata/sessions/{id};
POST /v1/strata/sessions to persist web messages;
POST /v1/strata/sessions/{id} to apply rename, pin or lock_kv actions;
DELETE /v1/strata/sessions/{id} to remove history and KV.

## Verification

Run the existing serve.test_session_v1 tests and new serve.test_history_v2 tests, as well as the broad serve.test_server suite. Check JavaScript parsing with node --check. The native GPU SAVE/RESTORE path must be tested with a real loaded model; SQLite unit tests alone cannot validate GPU serialization.
