-- Working tables (the crawl) and equipment tables (the facts). Additive only.

CREATE TABLE IF NOT EXISTS brands (
  id INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  name VARCHAR(190) NOT NULL,
  name_key VARCHAR(190) NOT NULL,
  status ENUM('active','stopped') NOT NULL DEFAULT 'active',
  discovered_at DATETIME NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY brands_name_key (name_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS brand_domains (
  id INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  brand_id INT UNSIGNED NOT NULL,
  host VARCHAR(253) NOT NULL,
  status ENUM('waiting','approved','stopped') NOT NULL DEFAULT 'waiting',
  found_by ENUM('ai','user') NOT NULL,
  evidence_url VARCHAR(2048) NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY brand_domains_brand_host (brand_id, host),
  KEY brand_domains_host (host),
  CONSTRAINT brand_domains_brand FOREIGN KEY (brand_id) REFERENCES brands (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS start_urls (
  id INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  brand_id INT UNSIGNED NOT NULL,
  url VARCHAR(2048) NOT NULL,
  url_sha CHAR(64) NOT NULL,
  found_by ENUM('discover','sitemap','user') NOT NULL,
  status ENUM('active','stopped') NOT NULL DEFAULT 'active',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY start_urls_brand_url (brand_id, url_sha),
  CONSTRAINT start_urls_brand FOREIGN KEY (brand_id) REFERENCES brands (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS hosts (
  id INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  host VARCHAR(253) NOT NULL,
  last_request_at DATETIME NULL,
  next_allowed_at DATETIME NULL,
  failure_count INT UNSIGNED NOT NULL DEFAULT 0,
  rest_until DATETIME NULL,
  robots_body MEDIUMTEXT NULL,
  robots_status INT NULL,
  robots_fetched_at DATETIME NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY hosts_host (host)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS pages (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  brand_id INT UNSIGNED NOT NULL,
  host_id INT UNSIGNED NOT NULL,
  start_url_id INT UNSIGNED NULL,
  url VARCHAR(2048) NOT NULL,
  url_sha CHAR(64) NOT NULL,
  depth TINYINT UNSIGNED NOT NULL DEFAULT 0,
  is_pdf TINYINT(1) NOT NULL DEFAULT 0,
  http_status INT NULL,
  content_type VARCHAR(190) NULL,
  etag VARCHAR(255) NULL,
  last_modified VARCHAR(100) NULL,
  content_sha CHAR(64) NULL,
  fetched_at DATETIME NULL,
  next_check_at DATETIME NULL,
  fail_count INT UNSIGNED NOT NULL DEFAULT 0,
  extraction_status ENUM('waiting','skipped','extracted','failed') NOT NULL DEFAULT 'waiting',
  ai_extracted_sha CHAR(64) NULL,
  ai_extractor_version VARCHAR(60) NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY pages_brand_url (brand_id, url_sha),
  KEY pages_host (host_id),
  KEY pages_next_check (next_check_at),
  KEY pages_extraction (extraction_status),
  CONSTRAINT pages_brand FOREIGN KEY (brand_id) REFERENCES brands (id),
  CONSTRAINT pages_host FOREIGN KEY (host_id) REFERENCES hosts (id),
  CONSTRAINT pages_start_url FOREIGN KEY (start_url_id) REFERENCES start_urls (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS page_text (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  page_id BIGINT UNSIGNED NOT NULL,
  page_number INT UNSIGNED NOT NULL DEFAULT 1,
  title VARCHAR(500) NULL,
  text_sha CHAR(64) NOT NULL,
  body LONGTEXT NOT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY page_text_page_number (page_id, page_number),
  CONSTRAINT page_text_page FOREIGN KEY (page_id) REFERENCES pages (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS found_models (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  page_id BIGINT UNSIGNED NOT NULL,
  brand_id INT UNSIGNED NOT NULL,
  model_number VARCHAR(190) NOT NULL,
  model_key VARCHAR(190) NOT NULL,
  is_base_model TINYINT(1) NOT NULL DEFAULT 0,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY found_models_page_key (page_id, model_key),
  KEY found_models_model_key (model_key),
  CONSTRAINT found_models_page FOREIGN KEY (page_id) REFERENCES pages (id),
  CONSTRAINT found_models_brand FOREIGN KEY (brand_id) REFERENCES brands (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS found_specs (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  found_model_id BIGINT UNSIGNED NOT NULL,
  spec_key VARCHAR(100) NOT NULL,
  value_text VARCHAR(500) NOT NULL,
  value_num DECIMAL(18,4) NULL,
  unit VARCHAR(30) NULL,
  value_key VARCHAR(64) NOT NULL,
  check_status ENUM('passed','failed') NOT NULL,
  fail_reason VARCHAR(255) NULL,
  extracted_by VARCHAR(100) NOT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY found_specs_natural (found_model_id, spec_key, value_key),
  KEY found_specs_status (check_status),
  CONSTRAINT found_specs_model FOREIGN KEY (found_model_id) REFERENCES found_models (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS energy_star_rows (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  dataset_id VARCHAR(20) NOT NULL,
  row_id VARCHAR(100) NOT NULL,
  ahri_ref VARCHAR(60) NULL,
  brand_name VARCHAR(190) NULL,
  model_number VARCHAR(190) NULL,
  model_key VARCHAR(190) NULL,
  row_json LONGTEXT NOT NULL,
  row_hash CHAR(64) NOT NULL,
  first_seen_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY energy_star_rows_row (dataset_id, row_id),
  KEY energy_star_rows_ahri (ahri_ref),
  KEY energy_star_rows_model_key (model_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS ai_calls (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  purpose VARCHAR(60) NOT NULL,
  brand_id INT UNSIGNED NULL,
  page_id BIGINT UNSIGNED NULL,
  model VARCHAR(60) NOT NULL,
  input_tokens INT UNSIGNED NOT NULL DEFAULT 0,
  output_tokens INT UNSIGNED NOT NULL DEFAULT 0,
  cache_read_tokens INT UNSIGNED NOT NULL DEFAULT 0,
  cache_write_tokens INT UNSIGNED NOT NULL DEFAULT 0,
  searches INT UNSIGNED NOT NULL DEFAULT 0,
  is_batch TINYINT(1) NOT NULL DEFAULT 0,
  rejected TINYINT(1) NOT NULL DEFAULT 0,
  cost_usd DECIMAL(12,6) NOT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY ai_calls_created (created_at),
  KEY ai_calls_brand (brand_id),
  KEY ai_calls_page (page_id),
  CONSTRAINT ai_calls_brand FOREIGN KEY (brand_id) REFERENCES brands (id),
  CONSTRAINT ai_calls_page FOREIGN KEY (page_id) REFERENCES pages (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS manufacturers (
  id INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  name VARCHAR(190) NOT NULL,
  name_key VARCHAR(190) NOT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY manufacturers_name_key (name_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS manufacturer_aliases (
  id INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  manufacturer_id INT UNSIGNED NOT NULL,
  alias VARCHAR(190) NOT NULL,
  alias_key VARCHAR(190) NOT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY manufacturer_aliases_alias_key (alias_key),
  KEY manufacturer_aliases_manufacturer (manufacturer_id),
  CONSTRAINT manufacturer_aliases_manufacturer FOREIGN KEY (manufacturer_id) REFERENCES manufacturers (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS equipment_models (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  manufacturer_id INT UNSIGNED NOT NULL,
  model_number VARCHAR(190) NOT NULL,
  model_key VARCHAR(190) NOT NULL,
  is_base_model TINYINT(1) NOT NULL DEFAULT 0,
  energy_star_confirmed TINYINT(1) NOT NULL DEFAULT 0,
  hidden_at DATETIME NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY equipment_models_manufacturer_key (manufacturer_id, model_key),
  KEY equipment_models_model_key (model_key),
  KEY equipment_models_is_base_model (is_base_model),
  CONSTRAINT equipment_models_manufacturer FOREIGN KEY (manufacturer_id) REFERENCES manufacturers (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS equipment_sources (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  url VARCHAR(2048) NOT NULL,
  url_sha CHAR(64) NOT NULL,
  title VARCHAR(500) NULL,
  content_sha CHAR(64) NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY equipment_sources_url_sha (url_sha)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS equipment_model_sources (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  model_id BIGINT UNSIGNED NOT NULL,
  source_id BIGINT UNSIGNED NOT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY equipment_model_sources_pair (model_id, source_id),
  KEY equipment_model_sources_source (source_id),
  CONSTRAINT equipment_model_sources_model FOREIGN KEY (model_id) REFERENCES equipment_models (id),
  CONSTRAINT equipment_model_sources_source FOREIGN KEY (source_id) REFERENCES equipment_sources (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS equipment_systems (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  ahri_ref VARCHAR(60) NOT NULL,
  ratings_key CHAR(64) NOT NULL,
  seer2 DECIMAL(6,2) NULL,
  eer2 DECIMAL(6,2) NULL,
  hspf2 DECIMAL(6,2) NULL,
  cooling_capacity_btuh INT UNSIGNED NULL,
  heating_capacity_btuh INT UNSIGNED NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY equipment_systems_ahri_ratings (ahri_ref, ratings_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS equipment_system_members (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  system_id BIGINT UNSIGNED NOT NULL,
  model_id BIGINT UNSIGNED NOT NULL,
  role VARCHAR(30) NOT NULL,
  UNIQUE KEY equipment_system_members_triple (system_id, model_id, role),
  KEY equipment_system_members_model (model_id),
  CONSTRAINT equipment_system_members_system FOREIGN KEY (system_id) REFERENCES equipment_systems (id),
  CONSTRAINT equipment_system_members_model FOREIGN KEY (model_id) REFERENCES equipment_models (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS equipment_specs (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  model_id BIGINT UNSIGNED NOT NULL,
  source_id BIGINT UNSIGNED NOT NULL,
  spec_key VARCHAR(100) NOT NULL,
  value_text VARCHAR(500) NOT NULL,
  value_num DECIMAL(18,4) NULL,
  unit VARCHAR(30) NULL,
  value_key VARCHAR(64) NOT NULL,
  extracted_by VARCHAR(100) NOT NULL,
  superseded TINYINT(1) NOT NULL DEFAULT 0,
  hidden_at DATETIME NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY equipment_specs_natural (model_id, spec_key, source_id, value_key),
  KEY equipment_specs_source (source_id),
  KEY equipment_specs_current (model_id, superseded),
  CONSTRAINT equipment_specs_model FOREIGN KEY (model_id) REFERENCES equipment_models (id),
  CONSTRAINT equipment_specs_source FOREIGN KEY (source_id) REFERENCES equipment_sources (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
