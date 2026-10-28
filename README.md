# Equipment crawler

Lives outside the Tracforce repo. Collects manufacturer manuals into its own PostgreSQL 18 + pgvector server
(database `tracforce_equipment`, schemas `crawl` and `kb`). Passwords are in `~/.config/tracforce-crawler/pg.env`
and `pg-root.env`, made by the one-time setup below. The old MariaDB database is left as it was.

One-time setup (passwords, server, roles, databases, extensions; adds only, safe to run again):

```
./setup/pg-setup.sh
```

Build the image (includes headless Chromium for JavaScript sites):

```
docker compose build
```

Create or update the tables (safe to run again):

```
./crawl migrate
```

Run the tests (they only touch `tracforce_equipment_testing` and stop if the server says the database is not a test one):

```
docker compose run --rm -e EQ_DB_NAME=tracforce_equipment_testing crawler python -m pytest
```

## Gathering

```
./crawl add Ruud                                        # finds the brand's sites (AI, ~$0.02); nothing is fetched yet
./crawl add Ruud https://www.ruud.com/products/ --depth 4   # a start address of your own (default depth 3)
./crawl add Mitsubishi https://www.mylinkdrive.com/api/v0/categoryMenu?slugName=USA --feed   # a JSON document feed
./crawl ok files.myrheem.com --brand Ruud               # approve a host
./crawl ok www.york.com --brand York --browser          # render its pages in headless Chromium
./crawl ok www.goodmanmfg.com --ignore-robots           # your call (D67); the 10 s gap and honest name stay
./crawl skip add news* --note "press pages"             # skip keywords match whole address parts; PDFs are never skipped
./crawl skip off news*      ./crawl skip list           # prefer add/off/list work the same way
./crawl verdict https://www.ruud.com/about/ skip        # your decision beats keywords, rules and the AI
./crawl run                                             # crawl; unsure links go to the AI (needs the key), else they wait
./crawl status                                          # per brand, including what was not reached and why
```

## Tagging, sections, visibility

Read fetched documents with AI (needs `ANTHROPIC_API_KEY_CRAWLER` in `~/.config/tracforce-crawler/anthropic.env`).
Run it once to send, and again later to collect the answers:

```
./crawl extract
./crawl sections                                        # cut tagged documents into sections and put them live (free)
./crawl preview <sha>                                   # print the sections a stored PDF would get, store nothing
./crawl hide manual 42 --reason "takedown request"      # or: hide manufacturer "Ruud" --reason ...; unhide the same way
./crawl check                                           # read-only health report of what Breeze can see
```
