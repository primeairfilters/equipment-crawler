## 2026-10-28 13:29
Eval baseline is now 17/25 top 5 (MRR 0.485) with exact-equivalent vector search, on 359 live documents. The earlier 19/25 was partly the approximate index's luck before a prune; none of today's code changes moved it (checked by rebuilding with each change reverted, and with all of them reverted). Misses: RMHPHDA068 dimensions, Sky-35 relay amps, EcoNet superheat screen, 120 V conversion kit, A2L vacuum pump/leak detector, error code 33, parts contact for commercial water heaters, nitrogen pressure test. Tuning waits for the real tech questions (user decision).

---

## 2026-10-28 21:08
No total time limit on a download: crawler/fetch.py uses httpx.Timeout(20.0, connect=10.0), which only limits each read, so a slow server that keeps trickling bytes holds the whole run. Seen in Ruud batch 4: big PDFs (52-100 MB, MAX_PDF_BYTES) arriving at 19-120 KB/s, i.e. 10-50 min per file, with the crawl at 3 pages after 7 min. Decide: a total deadline per download (e.g. scaled to size), or accept slow big files.

---

## 2026-10-28 21:50
Three Ruud PDF links are empty files on Ruud's own server (HTTP 200, 0 bytes; Azure answers "InvalidRange" to a ranged GET): files.myrheem.com/webpartners/ProductDocuments/30466981-8E5D-41E6-8966-F034DABB835E.pdf, 10209168-7BAC-4D77-9913-786860609017.pdf, 43BBB570-93BC-4797-B729-CEA195C6773B.pdf. They fail as not_pdf and are retried every day (RETRY_FAILED_AFTER). Also: the failure reason is not stored on pages (only in the run summary), so finding out why needed outside requests. Decide: store the reason; back off longer after repeated failures.

---

## 2026-10-28 22:00
Two Floating Air R-454B service manuals are cut at PDF_MAX_PAGES = 200 (crawler/textextract.py): document 799 (Multi-Zone outdoor, 251 pages) and 800 (Single Zone 9-36k, 255 pages); the last 51-55 pages (often troubleshooting/error codes/wiring) are not read. Document 800 was also tagged spec_sheet by the AI, though it is a 255-page manual (the earlier Floating Air pair stays install_manual by user decision 2026-10-27).

---

## 2026-10-28 22:30
`./crawl check` lists 15 likely duplicate pairs (same maker, title and models) after Ruud batch 4, up from 7 after batch 2: (32, 690), (38, 589), (50, 840), (78, 80), (317, 318), (317, 325), (318, 325), (346, 371), (356, 368), (370, 418), (421, 428), (601, 617), (606, 618), (606, 630), (618, 630). Only (50, 840) involves a batch-4 document. Both of each pair can be live, so search may show the same manual twice. Decide: look at them (hide one, or a rule) or leave.

---
