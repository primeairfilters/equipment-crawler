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
