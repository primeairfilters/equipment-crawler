-- P5: which system each dataset row produced, so a changed row replaces only its own system.
ALTER TABLE energy_star_rows
  ADD COLUMN IF NOT EXISTS system_id BIGINT UNSIGNED NULL;

CREATE INDEX IF NOT EXISTS energy_star_rows_system ON energy_star_rows (system_id);
