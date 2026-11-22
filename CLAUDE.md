# CLAUDE.md

## The goal above the theme: the circle that ends at Live Camera

**The crawler is step 1 of a circle** (the user, 2026-11-22): the crawler collects documents → RAG reads, sections,
tags, embeds and searches them → Live Camera and Breeze use them to help the person at the unit → what Live Camera
learns (misses, wrong answers, pages a live web search found, brands it didn't know) feeds back to the crawler. **The
Live Camera/Breeze user's experience is the goal; every crawler and RAG change is judged by what it does for that
person's answer**, not by pages, coverage or cost alone. Two users: company techs (fast, exact answers) and Tech Support
users (inexperienced, need plain step-by-step help). Every decision, test result and parked item from that discussion
is in the HVACApp project memory, `~/.claude/projects/-Users-cesarbielich-Documents-repos-HVACApp/memory/project_live_camera_pipeline_goal.md`.
Read it before crawler, RAG or Live Camera work.

## The theme: more crawl, faster, efficient, accurate — lower AI cost

**This is the main theme of the crawler, and it drives every decision made while working on it** (the user,
2026-11-21). Live Camera and Breeze are only as good as the documents the crawler gets them, so every change is judged
by whether it gets more of the right documents, sooner, for less:

- **More crawl:** find more documents: new sites, deeper pages, models still missing a manual.
- **Faster:** less waiting between finding a document and Breeze being able to use it.
- **Efficient:** no wasted requests, downloads or reruns, like fetching files that can never be used, or asking about
  the same thing twice.
- **Accurate:** the right documents for the right models, and nothing junk in the library.
- **Lower AI cost:** every AI dollar buys real work. Check the prod `ai_calls` numbers for answers that are thrown
  away (`rejected`), cut off (`output_tokens` at the limit) or repeated, and prefer a free rule over a paid call.

When two of these pull against each other, say so and name the trade-off instead of picking silently.

## SESSION_HISTORY.md — read it, keep it

**Read `SESSION_HISTORY.md` at the start of any crawler work.** It holds what past sessions changed, found and
learned against the theme: how waste was found, what was fixed and why, prod numbers, and what is still open.

**Add to it as you go, in the same change, without being asked:** every change, discovery, measurement or insight that
would help a future session keep to the theme. Newest first, dated, with the evidence (the query or test) and what is
still open. Move an item out of "still open" when it is done or decided.

## The crawler is always a work in progress — make it faster

**The crawler is never finished, and speed is the most important thing about it.** Every time anything in the crawler
is modified, the goal is to leave it faster than it was.

- **Every step and every code change is also a speed check.** Whatever you are working on, look for a way to make that
  work run faster, and do it as part of the change.
- **Look across the whole crawler, not only the code in front of you.** The faster route may be in another part:
  fetching, reading PDFs, tagging, sections and embedding, the database, the nightly run.
- **A speed-up outside the task you were given is not made.** Write it down instead, in the HVACApp repo's mental
  notes (`~/Documents/repos/HVACApp/mental_notes.md`): what is slow, where (file and line), the idea to speed it up,
  and what you did not check. Then say in one line that you added it.
