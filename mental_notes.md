2026-10-28 13:29
Breeze's own search must set the same pgvector options per query, or its "meaning" results get cut short: `SELECT set_config('hnsw.ef_search', '400', true), set_config('hnsw.iterative_scan', 'strict_order', true)` in the same transaction before the vector query (crawler/search.py does this now). With pgvector's default (40) the index returned fewer candidates than the 50 asked for, before the language and maker filters, and results changed with the index's state. Add this to the Tracforce-side contract (spec, "How Breeze reads it").

---

2026-10-28 13:29
Eval baseline is now 17/25 top 5 (MRR 0.485) with exact-equivalent vector search, on 359 live documents. The earlier 19/25 was partly the approximate index's luck before a prune; none of today's code changes moved it (checked by rebuilding with each change reverted, and with all of them reverted). Misses: RMHPHDA068 dimensions, Sky-35 relay amps, EcoNet superheat screen, 120 V conversion kit, A2L vacuum pump/leak detector, error code 33, parts contact for commercial water heaters, nitrogen pressure test. Tuning waits for the real tech questions (user decision).

---
