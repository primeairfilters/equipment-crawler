-- Pairs of manuals the user compared and found to be different products (`./crawl not-duplicates A B`): `check` no
-- longer lists them as likely duplicates. The lower id first. Additive: a new table.
CREATE TABLE IF NOT EXISTS kb.not_duplicates (
  first_id bigint NOT NULL REFERENCES kb.documents (id),
  second_id bigint NOT NULL REFERENCES kb.documents (id),
  reason text NOT NULL,
  decided_by text NOT NULL,
  decided_at timestamp without time zone NOT NULL DEFAULT (now() AT TIME ZONE 'UTC'),
  PRIMARY KEY (first_id, second_id),
  CHECK (first_id < second_id)
);
