# Investigator baseline

Run on 2026-11-03 22:28, RTX 5070 Ti 16 GB, llama.cpp server, 32k context, fully local.
Each model investigates every recorded incident with the same tools and prompt. No fine-tuning yet.

Scores: **category** = right kind of cause; **commit/file** = right guilty commit and file (for non-code faults: correctly blames no commit); **top-1** = all three right.

## Summary

| Model | Top-1 | Category | Commit | File | Avg time / incident | Gen speed | VRAM loaded / peak (whole card) | RAM peak (server) |
|---|---|---|---|---|---|---|---|---|
| gpt-oss-20b | **6/11** | 7/11 | 7/11 | 7/11 | 25 s | 199 tok/s | 13.4 / 13.7 GB (idle 1.6) | 16.9 GB |

## Per incident

| Fault | gpt-oss-20b |
|---|---|
| bad_config_property | ✅ cat ✅ commit ✅ file ✅ (12 s, 13 tools) |
| db_down | ✅ cat ✅ commit ✅ file ✅ (61 s, 25 tools) |
| memory_leak | ❌ cat ❌ commit ❌ file ❌ (29 s, 26 tools) |
| n_plus_one | ❌ cat ❌ commit ❌ file ❌ (38 s, 26 tools) |
| payment_down | ✅ cat ✅ commit ✅ file ✅ (17 s, 15 tools) |
| payment_timeout | ✅ cat ✅ commit ✅ file ✅ (24 s, 25 tools) |
| pool_exhausted | ❌ cat ✅ commit ❌ file ❌ (8 s, 6 tools) |
| rare_npe | ✅ cat ✅ commit ✅ file ✅ (11 s, 16 tools) |
| silent_save_failure | ✅ cat ✅ commit ✅ file ✅ (11 s, 11 tools) |
| slow_query | ❌ cat ❌ commit ❌ file ❌ (40 s, 26 tools) |
| traffic_spike | ❌ cat ❌ commit ✅ file ✅ (28 s, 26 tools) |
