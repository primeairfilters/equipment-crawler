## 2026-10-28 13:29
Eval baseline is now 17/25 top 5 (MRR 0.485) with exact-equivalent vector search, on 359 live documents. The earlier 19/25 was partly the approximate index's luck before a prune; none of today's code changes moved it (checked by rebuilding with each change reverted, and with all of them reverted). Misses: RMHPHDA068 dimensions, Sky-35 relay amps, EcoNet superheat screen, 120 V conversion kit, A2L vacuum pump/leak detector, error code 33, parts contact for commercial water heaters, nitrogen pressure test. Tuning waits for the real tech questions (user decision).

---

## 2026-10-28 21:08
No total time limit on a download: crawler/fetch.py uses httpx.Timeout(20.0, connect=10.0), which only limits each read, so a slow server that keeps trickling bytes holds the whole run. Seen in Ruud batch 4: big PDFs (52-100 MB, MAX_PDF_BYTES) arriving at 19-120 KB/s, i.e. 10-50 min per file, with the crawl at 3 pages after 7 min. Decide: a total deadline per download (e.g. scaled to size), or accept slow big files.

---

## 2026-10-28 22:00
Two Floating Air R-454B service manuals are cut at PDF_MAX_PAGES = 200 (crawler/textextract.py): document 799 (Multi-Zone outdoor, 251 pages) and 800 (Single Zone 9-36k, 255 pages); the last 51-55 pages (often troubleshooting/error codes/wiring) are not read. Document 800 was also tagged spec_sheet by the AI, though it is a 255-page manual (the earlier Floating Air pair stays install_manual by user decision 2026-10-27).

---

## 2026-10-28 22:30
`./crawl check` lists 15 likely duplicate pairs (same maker, title and models) after Ruud batch 4, up from 7 after batch 2. All are live, so search can show the same manual twice. Looked at each (read-only, 2026-10-28 23:00):
- Same form, revision suffix the form-number rule (D55, store.save_document, exact form_number match) does not see: 32 `M44-2230` / 690 `M44-2230 Rev. 1`; 50 `M44-3123 Rev. 1` / 840 `M44-3123`; 601 `AP21265 Rev 04` / 617 `AP21265`; 317 `AP11828-4 (05/14)` / 325 `AP11828-3`; 370 `AP16826-1` / 418 `AP16826-2`; 318 `AP10918-7 (03/02)` / 325 (different base number, probably another warranty). A fix would match on the base number and let the higher revision win; note 840 (found later) is the OLDER revision of 50, so "newest found replaces" would keep the wrong one. Decide: revision-aware form matching (and is a "-N" suffix a revision?).
- Same generic title, different form numbers, probably newer editions: 38 AP21681 (40 p.) / 589 AP23657 (44 p.); 346 AP22858 / 371 AP23057; 356 AP23059 / 368 AP22543; 421 AP21980 / 428 AP22673; tankless easy guides 606 (none) / 618 AP23442 / 630 AP23366. Decide: leave, or hide the older edition by hand.
- Added after batch 5 (16 pairs now): 1052 `92-26150-10-42` / 1058 `92-26150-10-41`, the same packaged/commercial HVAC warranty, last number a revision (the trailing "-N" case).
- False positive: 78 / 80 are the R-454B cassette and ducted IOMs (different products, same title "USE AND INSTALLATION INSTRUCTIONS").

---
## 2026-10-28 19:22

Ruud batch 6: 20 of the 49 failures are `bad_content_type` on image and installer links (media.ruud.com/...jpg, .png, CertiSpec3Setup.exe). The crawler downloads them, rejects them, and marks them failed, so the doubling backoff will keep retrying them for up to 30 days. Decide whether links ending in image/exe extensions should be skipped before fetching, or marked refused (never retried) instead of failed. The other failures are 29 http_404 and 19 older ones with no recorded reason (from before migration 0006).

---
## 2026-10-28 19:45

Search ranking after Ruud batch 6: eval is 16/25 (MRR 0.456), down from 17/25. The lost question is "How long does defrost run on the ductless heat pump and is it normal for the fans to stop?": a new rooftop-unit manual (doc 1265, DOAS RTU, p.88-90 "Heating, Cooling, Defrost, and Reheat") and a commercial heat pump spec sheet (doc 1258) now rank above the ductless manuals (docs 70, 72, 73). Search does not use the equipment type named in the question ("ductless", "mini split", "water heater") to filter or boost results, so the more commercial manuals get added, the more they crowd out residential answers. Decide whether search should detect the equipment type in the question and prefer sections of that type. Also: eval prints no per-question baseline we keep, so a drop like this takes digging to locate; consider saving each eval run's per-question output (for example eval/ruud.last.txt) and diffing against it.

---
