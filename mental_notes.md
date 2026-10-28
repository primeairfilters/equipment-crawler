2026-10-28 10:26
Three Ruud documents stay untagged forever (kb ids 114, 317, 318). Each has the same text as a document that is already tagged (114 matches warranty 107) but is a different file, so extract marks its pages "already read" and never tags it. Decide: copy the tags from the twin, or leave them out.

---

2026-10-28 10:26
`./crawl status` says "47 manuals needing OCR", but most of those pages are not scans: 111 are Arabic (the has_text check counts Latin letters only), 73 are empty, and about 120 hold only "Notes", a page number, "Intentionally left empty", ES/FR headers or a diagram caption. The count overstates what OCR could still add.

---

2026-10-28 10:26
Decide whether pages with only a caption or page number (mostly diagrams, e.g. "9. WIRING DIAGRAM") should also get OCR. Today OCR only reads pages with no text at all. More OCR there means more diagram-label junk.

---

2026-10-28 10:26
Two odd headings come from normal text-layer pages, not OCR: "]AGL-AGN-AHM MODELS)*([ 9.0" (document 375, pages 14-30) and "! CAUTION" (document 385, pages 7-12). The sectioner's existing rules let them through. Left as is.

---

2026-10-28 10:26
pdfminer (inside the PDF reader) is not deterministic: when two text boxes are the same distance apart it breaks the tie by memory address, so some lines at one height come out in a different order between reads (8 Ruud documents changed on one run). The layout cache hides it, but any change to pdfchild.py or the pdfplumber version re-reads everything and can reshuffle those documents again.

---

2026-10-28 10:26
Eval question "How far from the wall does the mini split condenser need to sit?" flips in and out of the top 5. The expected page and document 70's indoor-unit clearance page are within 0.006 in meaning distance. Consider whether document 70 p.6-7 should count as an answer, or reword the question.

---

2026-10-28 10:26
OCR heading confidence cutoff is 85. One real heading on the Ruud run scored lower and is now body text: "The Relief Valve" (78, water heater Use & Care manual 8f814d48). Raise or lower the cutoff only with the eval in hand.

---

2026-10-28 10:26
Cold rebuild (empty layout cache) took 21.8 min with 2 workers, using 609 MB of the container's 2 GB. Trying 3 workers could cut it to about 15 min, but this Mac ran low on memory today (Claude Code stopped a background job for it). Test when the machine is quiet.

---

2026-10-28 10:26
All the OCR, heading and rebuild-speed work is uncommitted (Dockerfile, crawler/ocrchild.py, textextract.py, sections.py, kb.py, __main__.py, eval/ruud.json, three new test files and three changed ones). Full suite 785 passed. Ready for you to commit.

---
