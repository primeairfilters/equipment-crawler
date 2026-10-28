2026-10-28 10:26
Three Ruud documents stay untagged forever (kb ids 114, 317, 318). Each has the same text as a document that is already tagged (114 matches warranty 107) but is a different file, so extract marks its pages "already read" and never tags it. Decide: copy the tags from the twin, or leave them out.

---

2026-10-28 10:26
Decide whether pages with only a caption or page number (mostly diagrams, e.g. "9. WIRING DIAGRAM") should also get OCR. Today OCR only reads pages with no text at all. More OCR there means more diagram-label junk.

---

2026-10-28 10:26
Eval question "How far from the wall does the mini split condenser need to sit?" flips in and out of the top 5. The expected page and document 70's indoor-unit clearance page are within 0.006 in meaning distance. Consider whether document 70 p.6-7 should count as an answer, or reword the question.

---

2026-10-28 10:26
OCR heading confidence cutoff is 85. One real heading on the Ruud run scored lower and is now body text: "The Relief Valve" (78, water heater Use & Care manual 8f814d48). Raise or lower the cutoff only with the eval in hand.

---

2026-10-28 10:54
Short top-level sections in brochures merge into the next one and take an empty path (document 68 p.4, "PRECISE PERFORMANCE & STATE-OF-THE-ART FEATURES"). That is the sectioner's merge rule (never merge across a higher heading, so at the top level the path is empty); the heading stays as a line in the text. Decide whether a merged top-level section should keep the first heading as its path.

---

2026-10-28 11:54
Five Ruud PDFs are over the fetcher's 50 MB limit (MAX_PDF_BYTES in crawler/fetch.py) and were skipped as too_big: three Floating Air R-454B service manuals on media.ruud.com (96161401_01 SM 9-36k 54 MB, 961101405_01 MZ Service Manual -3 and -4, 52 MB each) and two files.myrheem.com documents (1609D211… 65 MB, F299E2AF… 53 MB). Service manuals are high value for Breeze. Decide whether to raise the limit (e.g. 100 MB). The reader peaked at 548 MB on a 41 MB manual with a 1 GB cap per reader, so a 65 MB one may need the cap raised too; storage is about 280 MB more.

---

2026-10-28 12:33
Eval after Ruud batch 3 (359 live documents, up from 257): top 5 19/25 (was 20), top 1 9, MRR 0.499 (was 0.528). Now found: error code 9, wall clearance. Now missed: "Error code 33 on the R-454B wall mount mini split" (expected doc 72 p.81-83 out of the top 25; capacity-chart tables in docs 74/76 win on the number "33"), "Where on the EcoNet thermostat can I see superheat…" (doc 84 p.17-18 beaten by other pages of the same guide), "What vacuum pump and leak detector do I need for A2L refrigerant?" (OCR'd safety pages of docs 78/80/79 rank first; checked: doc 78 p.5-6 "Leak detection methods" answers the leak-detector half (electronic detectors calibrated for the refrigerant, no halide torch or ignition source) but not the vacuum pump; decide whether it counts as an answer in eval/ruud.json, as doc 79 p.17 did for the nitrogen question). The expected sections themselves did not change. Search tuning, best done with the real tech questions (to avoid fitting the 25 drafted ones).

---
