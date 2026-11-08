# CLAUDE.md

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
