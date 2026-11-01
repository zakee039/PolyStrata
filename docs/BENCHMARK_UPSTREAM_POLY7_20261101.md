# PolyStrata upstream poly.7: Gemma Q4 old/new benchmark

Tested **2026-11-01**, Windows 10 desktop, Intel Core i7-12700, 64 GiB DDR4 RAM, NVIDIA RTX 3070 Ti 8 GiB, CUDA 13.2. This measures the integration of upstream `v0.1.45-poly.7` plus subsequent main commits into our fork; it does not measure a stock upstream binary against the fork.

## Method

Both engines used the same `G4-MeroMero-26B-A4B-it-uncensored-heretic-Q4_K_M.gguf`, 131072-token max context, `--kv int8`, `--vram-reserve-mib 1024`, `--no-draft`, automatic conversation cache and identical original config arguments. Both used the **same merged Python server**. The old binary was `engine-poly/strata-poly.exe` (from before upstream merge); the new one `cmake-build-upstream-poly7/Release/strata-poly.exe` (compiled for NVIDIA SM86). Tests ran sequentially, with separate empty history/KV folders and no other model loaded.

Two complete passes were run in opposite orders: **old → new** and **new → old**. Each pass measured the cold load and three identical requests: A (long initial prompt, 1719 tokens), B (new chat), and A again (restored KV prefix, 1712 tokens reused). Every request generated 128 tokens, temperature zero. We sampled process memory during execution and took an idle measurement after 3.5 seconds following each response. RAM figures sum the Python service and child engine processes, and are not whole-system free-memory measurements. GPU used memory is from `nvidia-smi`; the peak GPU measurement is sampled, not an instrumented hardware maximum.

## Results (two-pass means)

| Metric | Previous | Merged | Interpretation |
| --- | ---: | ---: | --- |
| Model load, seconds | 2.30 | 2.67 | New loads ~0.37 s slower |
| Peak process working set, MiB | 19,240.5 | 15,501.2 | **19.4% lower** |
| Peak private bytes, MiB | 9,123.6 | 7,920.0 | **13.2% lower** |
| Working set 3.5 s after restored chat, MiB | 19,232.8 | 13,680.0 | **28.9% lower** |
| Private bytes 3.5 s after restored chat, MiB | 9,116.2 | 7,574.1 | **16.9% lower** |
| Loaded GPU memory, MiB | 6,630 | 6,630 | No steady-state change |
| Highest sampled GPU used memory, MiB | 6,670 | 7,054 | New transients **384 MiB higher** |
| A initial decode, tokens/s | 41.8 | 47.1 | Highly variable by pass |
| B new chat decode, tokens/s | 38.4 | 46.5 | Highly variable by pass |
| A resumed decode, tokens/s | 46.4 | 52.4 | Both restored 1712-token prefix |

Run-level decoded token rates (A / B / A return): old **48.5 / 43.1 / 45.6** and **35.2 / 33.6 / 47.2**; new **44.8 / 53.9 / 56.9** and **49.3 / 39.0 / 47.8**. Speed varied enough that two passes cannot establish a stable relative throughput gain. RAM results repeated closely and are the strongest evidence of improvement.

Native session SAVE and RESTORE were tested on merged Gemma separately: A → B → A successfully restored 18 tokens in **285 ms**. The benchmark's A return reused 1712 tokens in both binaries. The service preserves shared conversation history, model-specific five-slot dynamic KV cache, locked disk snapshots outside the dynamic quota, and Prefill fallback when KV cannot be restored. Existing on-disk snapshots can become incompatible with a rebuilt engine's model fingerprint and require re-Prefill once.

## Rollout

After the two successful comparison passes, the new `strata-poly.exe` was copied to the existing production `engine-poly` location after a byte-for-byte SHA256-verified backup at:

`engine-poly/.backups/strata-poly-before-upstream-poly7-20261101.exe`.

The Swift Flash `strata.exe` remains unchanged in production; it was built separately but not replaced. The manager's registered configs and global chat database were not modified. The production-path binary was additionally tested against an isolated chat database and API port after copying. Backout is to stop the app/service, copy the backup file over `engine-poly/strata-poly.exe`, then restart the app.

The raw per-run data and logs are kept locally under `E:\LLM\deploy\benchmark\upstream-poly7-comparison\` and `...\round2\`.

**Caveat:** Two passes on a single machine are enough to confirm the working-set reduction under this workload but not enough to project stable speedups, all-model behavior, or long-duration leak freedom. Measure longer conversations and real traffic before tightening any default memory limits.
