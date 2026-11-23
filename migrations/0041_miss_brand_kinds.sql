-- LIVE_CAMERA_CIRCLE.md D13 (the user, 2026-11-22): a brand Breeze missed is no longer only listed. brand_added: nothing
-- close, so it was added and its website is looked up first in the next run; close_brand: close to brands we have, so it
-- waits for the user with every close one listed. Swapping the CHECK removes no data (DROP CONSTRAINT IF EXISTS,
-- approved for 0002).
ALTER TABLE breeze.misses DROP CONSTRAINT IF EXISTS misses_kind_check;
ALTER TABLE breeze.misses ADD CONSTRAINT misses_kind_check
  CHECK (kind IN ('uncovered_brand', 'brand_added', 'close_brand', 'no_manual', 'search_missed', 'not_in_manuals', 'repeat'));
