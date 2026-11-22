# CLAUDE.md

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
