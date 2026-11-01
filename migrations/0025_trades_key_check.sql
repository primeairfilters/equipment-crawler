-- 0024's shape check let a blank trade and one with a comma in it through (array_to_string joins the elements with
-- commas, so "hvac,plumbing" as one element looked like two). This one reads the array's own text, where such an
-- element is quoted ({"hvac,plumbing"}, {""}), so only plain lowercase keys pass. Additive: a CHECK swapped (DROP
-- CONSTRAINT IF EXISTS removes no data; approved for 0002).
ALTER TABLE control.brands DROP CONSTRAINT IF EXISTS brands_trades_check;
ALTER TABLE control.brands ADD CONSTRAINT brands_trades_check
  CHECK (trades::text ~ '^\{([a-z_]+(,[a-z_]+)*)?\}$');
