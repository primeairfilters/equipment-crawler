# Session history

What past sessions changed, found and learned, so the next one can keep to the theme: **more crawl, faster,
efficient, accurate — lower AI cost** (CLAUDE.md). Newest first. Each entry says what was done, the evidence behind
it, and what is still open. Prod numbers come from read-only `claude_ro` queries on the prod crawler database.

## Why the theme matters right now

The crawler feeds Breeze and **Live Camera**, and Live Camera is only as good as the documents behind it. The user wants
to launch Live Camera to the public, and it is not ready because the library is missing data for the brands it
supports. The question behind every crawler session is: **how does Live Camera get the data it needs, sooner, for
less?** The brands a tech points the camera at most (Carrier, Trane, Lennox, Goodman, Rheem, York, Daikin and the
like) matter far more than brand number 350.

## Decisions the user made (2026-11-21)

- The theme above drives every crawler decision (now at the top of CLAUDE.md).
- Section embedding gets its own $5 a month **on top of** the $50 AI cap, not carved out of it.
- Models from Tracforce's own units are **not** a source for the missing-manual search yet: one customer and very few
  units. Revisit when there are more shops.
- Website search redo every 90 days with 2 web searches (cheaper redo), not just a shorter wait.
- Pending: whether to follow a robots.txt redirect to an unapproved site (see "Still open").

## What the crawler looked like on 2026-11-21 (prod)

- **Money, November 2026** (cap $50, reached on Nov 11; after that only free steps ran): link check $30.30 (of which
  $29.46 was thrown away), tagging $8.37 (16,368 batch calls, about $0.0005 each), website search $5.79, embedding
  $3.30, missing-manual search $2.29 (200 searches, about 1.1 cents each), host checks $0.01 (75 calls).
- **It runs around the clock** (Settings "Run all the time", cron hourly calls `./crawl auto --nightly`): every day
  from Nov 12 covered about 24 hours of runs. Pages downloaded per day fell from 13,693 (Nov 12) to 2,991 (Nov 21).
  Guess, not proven: it has mostly downloaded the 403 brands it knows and is now mostly rechecking.
- **The real reading backlog** is 17,238 downloaded documents waiting for tagging (15,531 PDFs, about 124,000 PDF
  pages), all on active brands. The bigger "140,471 waiting" count includes 123,000 pages never downloaded (past the
  depth limit, blocked, skipped).
- **How often things are looked at again:** a page every 30 days (`fetch.RECHECK_AFTER`), and any new link on it is
  followed right away; a brand's websites every 90 days (was 180); a missing-manual family that found nothing, after
  180 days (`gapfill.MISS_BACKOFF`).
- **New sites are mostly found for free:** when a brand's pages link to PDFs on an unknown site, the host check judges
  it with a tiny AI call. The website search only catches sites nothing links to.
- **Not leaking** (checked, all fine): the same content is not tagged twice (4,498 pages share 1,761 contents, only 2
  extra tagged, $0.01); no tagging answer hit its 16,000-token limit (0 of 17,679); no pages stuck mid-submit; no
  failed batch requests resent in a loop; 0 rejected website-search, host-check or missing-manual answers.

## Working on the crawler (practical notes)

- **Tests:** `docker compose run --rm -e EQ_DB_NAME=tracforce_equipment_testing crawler python -m pytest
  tests/<file>.py -k "<name>"`. Write the test first and watch it fail for the right reason.
- **Test traps met on 2026-11-21:** a link whose address or text has a word the rules like ("products") is taken by
  rule for free, so a test about the link check must use neutral words; with `checker=None` unsure links just wait as
  `ask_ai`; a never-fetched page row has `next_check_at` NULL (a test copying one must clear it, or the page is "not
  due"); `site_searches` has one row per brand and family, so test families must be unique.
- **Migrations:** add one in `migrations/`, apply it locally with
  `docker compose run --rm crawler python -m crawler migrate` (and the testing database with
  `-e EQ_DB_NAME=tracforce_equipment_testing`). `deploy.sh` applies it on prod.
- **Deploy:** the user runs `./deploy.sh` on the server; it refuses while a run is going. Changes take effect on the
  next run the hourly cron starts. Committing and pushing are blocked for the assistant in sessions started from the
  HVACApp repo; the user commits.
- **Prod queries** go through the read-only `claude_ro` tunnel (HVACApp memory, reference-prod-db-readonly), with a
  10-second limit per query. Matching every model against every page's text times out; aggregate one document's text
  first, or sample 20 documents at a time.
- **Testing a site from a desktop:** only with the crawler's honest name
  (`TracforceEquipmentBot/1.0 (+mailto:support+crawler@tracforce.ai)`). Never pose as a browser to get past a block.

## How to look for waste (worked on 2026-11-21)

- **Start from prod `crawl.ai_calls`, grouped by `purpose`.** Look for `rejected` answers, `output_tokens` at the
  call's limit (cut off), and the same `page_id` asked many times. That is how the link-check leak was found.
- **Split a cost before guessing at it.** Discovery looked like "the AI reading results" but the web-search fee was
  $4.74 of $5.81. Group by `searches` and read the real cost per call.
- **Count states, then check why.** "Never crawled" start pages looked like starvation; every one had run records and
  had failed for a reason (403, robots.txt, data answers, unapproved site). Query the page's `fetch_state` and
  `fail_reason` before believing a cause.
- **A reviewer's finding is a lead, not a fact.** Of the reviewers' findings on 2026-11-21, several were not happening
  on prod (stuck submits, paused-brand backlog, repeat discovery asks: all 0). Check each against prod first.
- **A shared cap stops cheap steps too.** When the $50 ran out on Nov 11, embedding (pennies) stopped with everything
  else and 48,201 already-paid sections went unsearchable.
- **Check where a limit is applied, not just its size.** The link check's 1,500 tokens were fine; 60 links per call
  was not. The sitemap's 5,000-address cap is counted before the folder filter. The tagging set-aside used the
  answer limit, not real answer sizes.
- **Follow a fix through to the end.** Raising the missing-manual search was worth little while its finds could fail
  to load; check that the next step can use what a step produces.
- **Sample before you blame.** "840 manuals over 20 pages with no models" pointed at the 20-page limit; a 40-manual
  sample showed 25 had the number in the text the AI was sent, so the AI not naming it was the main cause.
- **Three reviewers on separate parts** (AI spending, downloading, reading) found leads fast; their guesses about
  causes were wrong about half the time, so verify each on prod before building.

## 2026-11-22 — LIVE_CAMERA_CIRCLE.md phase 6: brands from misses, websites looked up first

The goal above the theme is now the circle that ends at Live Camera (see CLAUDE.md). The spec is in HVACApp
`Obsidian Tracforce/LIVE_CAMERA_CIRCLE.md`, D13 and D14.

- **Found:** 246 of 406 prod brands were never looked up. Website finding last ran 2026-11-11, because the $50 cap ran
  out and it only happens at a brand's own turn.
- **Found:** the 90-day redo (`discover.REDO_AFTER`) never ran from the nightly run. `auto.py` only calls discovery
  for brands never looked up; the redo ran only with `./crawl discover` by hand.
- **Built — website finding goes first** (`control.find_websites`, first step of `auto.start_of_run`, beside the
  workers as decided 2026-11-07). The order is: (1) brands added from Breeze misses, (2) never looked up, (3) due their
  90-day redo. It stops at the cap or the window, skips brands over their own monthly budget, and still never asks
  about a brand with a typed start page and no lookup (P19).
  - Measured: ~25 s a lookup on Nov 11, so prod's 246 take ~1.7 h and ~$9. The workers spent ~$2.30 in run 52's first
    2 hours, so it fits well inside the $50.
  - `tests/test_find_websites.py`: the order test was shown to fail with the order broken.
- **Built — brands from misses:**
  - `misses.close_brands`: tested against the 406 prod names. 18 of 18 misheard names came out close or exact; real
    brands we lack came out not close.
  - Nothing close → `add_brand(..., added_by='breeze miss')`, kind `brand_added`, looked up first next run.
  - Close → kind `close_brand`, with every close brand, nearest spelling first, for the user on the admin Breeze
    misses tab.
  - No letters, or over 60 characters → `uncovered_brand` as before.
  - `crawl_brand` now also matches a name written another way ("QUILLMAR, Inc." = Quillmar), which only worked
    before if a manufacturer alias existed.
  - Migration `0041_miss_brand_kinds.sql` (CHECK swap).
- **Still open:** prod has 2 test brands from `e2e-superadmin@example.com` among the 246; they'll be looked up too.
  Nothing removes them.

## 2026-11-21

Built and tested locally unless marked deployed; all go out with `./deploy.sh`.

- **Link check cut off and thrown away.** 60 links per call overran the 1,500-token answer on 28,131 of 32,738
  November calls; $29.46 of the $50 cap bought nothing and the same 5,145 pages were asked about 5.5 times each.
  Now 20 links per call and a few-word reason (`linkcheck.py`). Check after deploy: `rejected` share for
  `link_check` should be near 0.
- **Help-center reader.** support.trane.com answered 403 on all 2,494 article pages, while its robots.txt allows them
  and its help-center (Zendesk) API answered 200 with full text. `/hc/<locale>/articles/<id>` pages are now read
  through `/api/v2/help_center/<locale>/articles/<id>.json` and kept under the article's own address, falling back
  to the page (`zendesk.py`, `Fetcher.fetch`). Open: whether Trane allows the prod server's address, and whether its
  `/hc/article_attachments/` PDFs are blocked too. Speed idea (not built): the list endpoint gives 100 articles per
  request.
- **Missing-manual search widened.** It only hunted ENERGY STAR models; 31,365 of 42,077 models found in our own
  documents had only spec sheets, parts lists or brochures. It now also searches those (asking for the install or
  service manual), and its limit is 2,000 searches a month instead of 200 (about a penny each). 13,876 new families,
  8,241 HVAC (`gapfill.py`, `store.HAS_NO_MANUAL_SQL`).
- **Website search redo.** Every 90 days instead of 180; a redo names the known sites, asks only for new ones and
  allows 2 web searches instead of 8 (measured: 2-search calls cost 2.28 cents vs 3.63 average) (`discover.py`).
  Open: whether 2 searches find enough — the first redos land late January 2027.
- **PDFs one step past the depth limit are fetched.** 10,343 PDF links sat as `beyond_depth` (8,014 of 9,947 start
  pages are depth 1). A PDF is a dead end and skips the link check (`crawl.py` `_children`).
- **Site-search pages that can never load.** 214 were forbidden by robots.txt (Lennox `Disallow: /*?q=`, Haier
  `Disallow: /search`) and 210 LG China ones answered with data. Such searches are no longer queued and the saved
  ones are stopped (`coverage.site_search`).
- **Embedding has its own $5 a month**, outside the $50 cap (the user's call). 48,201 sections waited on about 40
  cents of embedding (`embed.py`, `ai.monthly_spend` leaves out `embed`).
- **Tagging set-aside made realistic.** It reserved 16,000 output tokens at full price; prod answers are 109 median,
  720 at the 99th percentile, 6,156 max. Now the batch price of 2,000 tokens (`extract.RESERVE_OUTPUT_TOKENS`). A rare
  longer answer can pass the cap by a fraction of a cent.
- **Redirected files rechecked with their ETag** at the final address; 1,314 redirected PDFs were downloaded whole
  every 30 days (`fetch.py`).
- **Sitemaps** are asked for only when due (not every night) and read in full when unchanged, so an unchanged index
  still leads to its sitemaps (`sitemaps.py`). Open: the 5,000-address cap is still applied before the folder filter.
- **Manuals with no model numbers** get their own brand's known model numbers printed in their text, no AI, rechecked
  every 30 days (`known_models.py`, migration 0040). 840 of 2,638 manuals over 20 pages had none; in a sample of 40,
  25 printed a known number in the text the AI was sent. Trade-off: a manual that only mentions a model it fits gets
  linked to it.
- **Links to the brand's own start pages** skip the link check (653 such checks so far) (`crawl.py`).
- **robots.txt redirect path.** After a redirect the crawler asked for `/robots.txt` again, looped, and refused the
  whole site (Danby, 209 pages waiting; Franklin Water). It now follows the redirect's real path, up to 5 redirects
  (`Fetcher._fetch_robots`).

- **robots.txt redirects to a site not approved are followed** (the user's call), only to read the rules (RFC 9309).
  The other site is **not** approved or crawled by this; a refused host (AHRI) is never asked (`Fetcher._fetch_robots`,
  tests in `test_p2_review_fixes.py`). Fujitsu General (→ `www.airstagehvac.com`) and Nortek (→
  `literature.nordyne.com`) had their whole sites refused before.

- **Drawings, 3D models and archives are skipped by file ending** before any request or link check (`.zip`, `.gz`,
  `.dwg`, `.dxf`, `.rfa`, `.rvt`, `.sat`, `.ifc`, `.step`/`.stp`, `.igs`, `.skp`, `.edz`, `.rtf` and more), including
  links the link check had already said yes to (`rules._NOT_DOCUMENTS`, `crawl.py`). Of 3,882 `bad_content_type`
  failures, 1,356 were .zip, 487 .dwg, 341 .rtf, 198 .rfa. Spreadsheets and Word files are deliberately not on the
  list.

- **Spreadsheets and Word files on brand sites are read and tagged like PDFs.** 479 `.xlsx` and 339 `.docx` downloads
  were thrown away as `bad_content_type`, though `filereaders.py` reads both for uploads. Taken by their declared type,
  or as octet-stream at an address with the ending, and the body must be a zip; price lists are left out as for
  uploads; originals are stored as .xlsx/.docx (`fetch.OFFICE_TYPES`, `crawl._office`, `library.EXTENSIONS`, tagging
  filter in `extract.py`; tests in `test_crawled_office.py`).
- **Bug from the embedding change, fixed:** removing `embed.DEFAULT_CAP_USD` broke `crawler/search.py`'s import, so the
  nightly Breeze-miss step (`misses.py`) and `./crawl ask`/`eval` failed to load. Breeze itself was not affected (it
  searches with HVACApp's own `EquipmentKnowledgeSearch.sql`). `search.py` now has its own default. Lesson: after
  removing a name, import every module (`python -c "import pkgutil, importlib, crawler; ..."`) and run the tests of
  every file that imported it, not only the changed file's tests.

- **77 waiting sites approved by the user** on prod with `./crawl ok <host>` (no `--brand`: approves the host for
  every brand that found it), confirmed by a read-only query: 77 approved, 0 of them still waiting. Among them
  `files.hvacnavigator.com` (York, Coleman), `www.meppi.com` (Mitsubishi Electric), `literature.nortekhvac.com`.
  **27 were left waiting on purpose**: stores, distributors and dealer sites (Lowe's, Home Depot's image server,
  Wesco, `asdealernet.com`, `resource.ecmdi.com` and more), Dropbox, Scribd, `shareddocs.com`, and shared hosting that
  serves thousands of companies (`s3.amazonaws.com`, `cdn.prod.website-files.com`, HubSpot's file server, Adobe's
  scene7), in line with `hostjudge.BLOCKLIST`. Lesson for pasting long commands into the server: the terminal wraps
  long lines and indents pasted lines, which breaks a `for` loop and a heredoc; one short `./crawl ok <host>` per line
  works.
- **Fujitsu's site moved to `www.airstagehvac.com`** (product pages answer 308 to the same path there). The user added
  `https://www.airstagehvac.com/us/` for brand Fujitsu on the Hosts tab; the next run's sync makes it a start page and
  approves the site (`control.take_start_pages`). Check after the next run that Fujitsu pages and PDFs come in.

- **Sister-brand models linked across brands** (`known_models.link_sister_brand_models`, nightly). A model a sister
  brand also sells, by the exact same model key of 8+ characters, is linked to the manual under that brand too, and
  the brand is added to the manual's `document_brands`. Why it mattered for Live Camera: Breeze, asked about a Payne
  unit, looked for documents filed under Payne only (`modelDocuments` and `brandDocuments` in HVACApp's
  `EquipmentKnowledgeSearch.php`), missed the Bryant manual that prints the number, and marked it down as another
  brand's (`FOREIGN_CODE`) when the whole-library search found it. On prod: 315 ENERGY STAR models looked uncovered
  this way; with 8+ characters every brand pair was a real family (Rheem/Ruud/Friedrich, Carrier/Bryant/Payne/Midea
  and the ICP brands, Trane/American Standard, York/Johnson Controls/Coleman/Luxaire, Nortek's brands, Bradford
  White/Laars), 2,487 links over 307 manuals and 1,052 models. Data only: no Breeze change needed.

### Overnight batch (2026-11-21, the user asleep; each cause checked on prod first, each test watched fail)

- **Tagging batches are capped per pass** at 5,000 requests or about 150 MB of text (`extract.MAX_BATCH_REQUESTS`,
  `MAX_BATCH_BYTES`); the Batch API takes up to 100,000 requests or 256 MB. Urgent because of the set-aside fix: with
  realistic set-asides, $50 would admit all 17,238 waiting documents (about 650 MB) in one batch on Dec 1, and the
  send would fail every pass.
- **A failed send now saves its reset** (`extract.py`): the nightly step rolls back after a failure, which kept the
  "submitting" mark saved just before the send. Those documents were never tagged and their set-aside counted against
  the cap forever. Confirmed by a test that saw all 3 documents still "submitting" at the last save; prod had 0 at
  the time.
- **No tokens counted when the money can't cover a document:** a low price guess (5 characters a token) is checked
  first; counting is a network call per document and ran for every waiting document even with nothing left.
- **One page too big for a request is cut at line ends** into pieces (`extract._cut`) instead of the whole document
  being skipped (a big spreadsheet's sheet is one page).
- **Sitemap limit counts matches:** the 5,000 limit now counts addresses under a start section, not the sitemap's
  first 5,000 (`sitemaps.take`); an index still reads at most 5,000 sitemaps.
- **PDFs with no text are read again nightly:** up to 20 a night, least recently tried first
  (`documents.reread_unread`); `reread` had only run by hand.
- **Short sister-brand numbers (6-7 characters)** are linked only between brands proven a family by 5+ links of 8+
  characters (`known_models.FAMILY_PROOF`). On prod the short matches inside proven families were Ruud/Rheem,
  Carrier/Bryant, Trane/American Standard, Daikin/Goodman; outside were chance pairs like Bosch/Bryant.
- **The 52 never-loaded start pages on approved sites needed no code:** they sat on sites approved only tonight
  (Dunham-Bush, Krack, Hussmann, CaptiveAire) or on Fujitsu (robots redirect, fixed).
- **Nortek's literature site moved** to `literature.nordyne.com` (every page and robots.txt answer 301 there; checked
  with the honest name). Brands using the old address: Nortek and Mammoth. Needs the user's approval (below).
- **Found the run switched off:** "Run all the time" is off (`control.settings.run_nonstop = f`, changed 04:58 UTC
  when the crawler was stopped to deploy) and the window is 1:00-1:05 AM Pacific. Nothing has run since the deploy
  (last run ended 05:07 UTC). Migration 0040 is on prod, so the deploy went through.

### Still open from 2026-11-21

- **Turn "Run all the time" back on** (cog on the /admin crawler page), or nothing new runs.
- **Approve Nortek's new literature site:** `./crawl ok literature.nordyne.com --brand "Nortek"` and the same with
  `--brand "Mammoth"`.
- **Old Word and Excel files** need new packages in the image: `xlrd` (pip) for .xls, `catdoc` or `antiword` (apt) for
  .doc. Not added overnight; 181 .doc and 120 .xls on prod.
- **Help-center articles one at a time:** Trane's 2,470 articles take about 7 hours of Trane's host time at the
  10-second gap, again on every 30-day recheck, and a brand's turn is 3 hours, so they crowd out Trane's other pages
  for about three nights a month. The list endpoint gives 100 per request (about 25 requests). Not built: it needs the
  list's articles saved as pages outside the normal fetch, too much to do unattended.
- **Any edit to `textextract.py` rebuilds every document's sections** (its file hash is in `build_version`). Left
  alone: it is how reader fixes reach every document, and changing how the version is worked out would itself cause
  one full rebuild. A trade-off for the user.
- **After the next runs** (needs "Run all the time" back on): check the Fujitsu/AirStage page went from `waiting` to
  crawled (`control.start_page_status`), Trane articles arrive through the help-center API (1 fetched, 2,494 failed
  on 2026-11-22 06:54 UTC), and the 48,201 unembedded sections got vectors. (Link check and tagging ran again
  on 2026-11-22, see the after-deploy check below.)
- Model numbers shorter than 6 characters shared by two brands are never linked across brands.
- Eaton and Baldor time out even from a desktop with the crawler's honest name: bot blocking, nothing honest to fix.
- `www.www.nortekair.com` and `www.www.lennoxpros.com` are typo hosts from discovery.
- **`tests/test_p14_search.py::test_a_search_inside_some_manuals_measures_every_distance_instead_of_walking_the_vector_index`
  is flaky:** it failed twice on 2026-11-21 (the plan had no `section_vectors_te3s_1536_hnsw`) and passed in a later
  run with no change to search. It reads the testing database's query plan, which depends on what that database holds
  at the time. Do not read a failure of it as a broken search without running it again.

### After-deploy check, 2026-11-22 08:53 UTC (prod, read-only)

- **Run 52 started 06:58 UTC and was still running** at 08:53 (`crawl.runs`), so its report and `needs_you` weren't
  written yet; the results below are read from the data. In two hours it fetched 1,611 pages across 9 brands
  (C.H.I. to Carrier, alphabetical); Fujitsu and Trane hadn't had their turn yet.
- **The AI work started again before Dec 1.** Leaving embedding out of the $50 cap dropped the counted spend from
  $50+ to $46.77, so the capped steps ran until the cap: $47.94 by 08:53. This is a side effect of the embedding
  change, not a bug: the $50 still holds.
- **Link check fixed:** 180 calls this run, 0 rejected (was 28,131 of 32,738).
- **Tagging under the new limits:** one batch of 940 requests, $0.41, largest answer 991 tokens, 0 rejected.
- **Embedding:** unembedded live sections went from 48,201 to 6,044 (of 298,576). 763 embedding calls this run
  ($0.50); November embedding spend is $3.80 of its own $5, so roughly $1.20 is left for the remaining 6,044.
- **Free model links:** `link_known_models` checked 2,206 manuals and gave 454 of them their own brand's models
  (2,677 links). The sister-brand step made 3,229 links under other brands on 467 manuals. Another 3,560 own-brand
  links on manuals that already had models arrived this run, most likely from the sister-brand step linking a key
  back to the document's own brand (a guess, not checked).
- **Crawled Word files are kept:** 10 new .docx documents this run (15 total); no new .xlsx yet.
- **Reread:** 77 PDFs still have no text. Reread can't be told apart from other updates in `documents.updated_at`;
  the run report's count will show it.
- **AirStage (Fujitsu):** the start page went from `waiting` to `crawling` (start URL made 06:58), but 0 pages
  fetched so far, because Fujitsu hadn't had its turn yet.
- **Trane help center:** still 1 fetched, 2,494 failed (all `http_403`), none tried this run. Retries are due between
  2026-11-22 02:12 and 2026-11-29 02:11 UTC, when Trane's turn comes.
- **Check again after run 52 ends:** the report's `needs_you` and reread count, AirStage pages, Trane pages fetched
  through the API.

**16:01 UTC the same day:** run 52 was still running after 9 hours. It had fetched 6,287 pages across 49 brands and
made 2,916 new documents. The capped spend reached exactly $50.00, so link check and tagging stop until Dec 1. This
run: link check 1,152 calls, 0 rejected; tagging 3,600 batch requests, $1.27, 0 rejected; gap-fill 145 searches, $1.79.
AirStage is `crawled` (12:52 UTC): 137 pages fetched, 0 failed this run, 0 PDFs. 80 are past the depth limit and 13
wait for the link check, which is now capped. Trane is not reached yet: 0 pages this run, and its help center is
still 1 fetched / 2,494 failed. Unembedded sections rose to 13,439 of 305,790 because new sections were built. They
get embedded at the end of the run, from the $1.20 left of the embedding $5.

### The missing-manual search rarely ends in a manual (prod, 2026-11-22)

Of 365 gap-fill searches (2026-10-31 to 11-22), 115 returned at least one link. For 18 of those, an install or
service manual naming a model in that family (first 6 characters of the key) arrived afterwards. Of the 50 with links
from before Nov 8, which had the most time to be crawled, 7 did (14%). Gap-fill start pages: 1,048 active (909
fetched, 27 of them PDFs), 411 stopped (none fetched). For the motto, this is the weakest link between a Breeze miss and
a fix: most searches buy nothing. Worth finding out why: wrong pages, pages that only lead to PDFs that are never
reached, or manuals that arrive but aren't tied to the family because tagging was capped after Nov 11. Also, a sorted
miss is never looked at again (`misses.py`), so nothing notices when a later crawl answers it.
