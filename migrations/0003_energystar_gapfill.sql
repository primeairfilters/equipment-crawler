-- P5: ENERGY STAR systems that get replaced, the pull's last-seen state, and the gap-fill back-off.
ALTER TABLE equipment_systems
  ADD COLUMN IF NOT EXISTS superseded TINYINT(1) NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS equipment_systems_current ON equipment_systems (ahri_ref, superseded);

CREATE INDEX IF NOT EXISTS equipment_models_es ON equipment_models (manufacturer_id, energy_star_confirmed);

CREATE TABLE IF NOT EXISTS energy_star_state (
  dataset_id VARCHAR(20) NOT NULL PRIMARY KEY,
  rows_updated_at BIGINT NOT NULL,
  pulled_at DATETIME NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS gapfill_searches (
  id INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  brand_id INT UNSIGNED NOT NULL,
  family VARCHAR(20) NOT NULL,
  searched_at DATETIME NOT NULL,
  found TINYINT(1) NOT NULL DEFAULT 0,
  UNIQUE KEY gapfill_searches_family (brand_id, family),
  CONSTRAINT gapfill_searches_brand FOREIGN KEY (brand_id) REFERENCES brands (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
