# Equipment crawler

Lives outside the Tracforce repo. Database passwords are in `~/.config/tracforce-crawler/db.env`.

Build the image once:

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

Read fetched documents with AI (needs `ANTHROPIC_API_KEY_CRAWLER` in `~/.config/tracforce-crawler/anthropic.env`). Run it once to send, and again later to collect the answers:

```
./crawl extract
```
