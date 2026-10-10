-- DDL duy nhất của HRC Safety Log (SQLite WAL — journal_mode được bật ở store.py).
-- Mọi bảng dùng CREATE IF NOT EXISTS; sessions/session_samples thêm ở M3.

CREATE TABLE IF NOT EXISTS raw_samples (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  device_id TEXT NOT NULL,
  boot_id TEXT NOT NULL,
  sequence INTEGER NOT NULL,
  received_at TEXT NOT NULL,
  is_late INTEGER NOT NULL DEFAULT 0,
  payload_json TEXT NOT NULL,
  measured_at TEXT,
  monotonic_us INTEGER,
  time_synced INTEGER,
  distance_mm INTEGER,
  echo_us INTEGER,
  valid INTEGER,
  error_code TEXT,
  UNIQUE (device_id, boot_id, sequence)
);
CREATE INDEX IF NOT EXISTS idx_raw_device_boot_seq ON raw_samples(device_id, boot_id, sequence);
-- Live endpoints need newest samples without sorting the complete raw table.
CREATE INDEX IF NOT EXISTS idx_raw_device_received
  ON raw_samples(device_id, received_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS rejected_messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  received_at TEXT NOT NULL,
  topic TEXT,
  reason TEXT NOT NULL,
  payload_text TEXT
);

CREATE TABLE IF NOT EXISTS processed_samples (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  device_id TEXT NOT NULL,
  boot_id TEXT NOT NULL,
  sequence INTEGER NOT NULL,
  processor_version TEXT NOT NULL,
  computed_at TEXT NOT NULL,
  mode TEXT NOT NULL CHECK (mode IN ('live','offline')),
  result_json TEXT NOT NULL,
  UNIQUE (device_id, boot_id, sequence, processor_version)
);
CREATE INDEX IF NOT EXISTS idx_processed_key ON processed_samples(device_id, boot_id, sequence, processor_version);

-- M3: lượt thu (session) — ranh giới từ ack thiết bị hoặc fallback DB.
CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL,
  checksum TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
  session_id TEXT PRIMARY KEY,
  device_id TEXT NOT NULL,
  scenario TEXT, object TEXT, zone_note TEXT,
  status TEXT NOT NULL CHECK (status IN ('running','closed')),
  started_at TEXT NOT NULL, ended_at TEXT,
  config_json TEXT,
  start_boot_id TEXT, start_sequence INTEGER,
  stop_sequence INTEGER,
  boundary_source TEXT CHECK (boundary_source IN ('device_ack','db_fallback')),
  boot_change INTEGER NOT NULL DEFAULT 0
);

-- M3: gán mẫu vào session theo khoảng sequence cùng boot (idempotent).
CREATE TABLE IF NOT EXISTS session_samples (
  session_id TEXT NOT NULL, device_id TEXT NOT NULL,
  boot_id TEXT NOT NULL, sequence INTEGER NOT NULL,
  PRIMARY KEY (session_id, device_id, boot_id, sequence)
);

-- M14: durable detector state and idempotent event handoff.
CREATE TABLE IF NOT EXISTS event_detector_state (
  state_key TEXT PRIMARY KEY,
  state_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS event_packages (
  event_id TEXT PRIMARY KEY,
  device_id TEXT NOT NULL,
  boot_id TEXT NOT NULL,
  session_id TEXT,
  processor_version TEXT NOT NULL,
  trigger_sequence INTEGER NOT NULL,
  event_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_event_session_version
  ON event_packages(session_id, processor_version);

-- M21: durable IoT → blockchain reward settlement outbox.
CREATE TABLE IF NOT EXISTS reward_settlements (
  session_id TEXT PRIMARY KEY,
  beneficiary TEXT NOT NULL,
  outcome TEXT NOT NULL,
  evidence_hash TEXT,
  status TEXT NOT NULL CHECK (status IN ('PENDING','SETTLED','ERROR')),
  response_json TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- M27: durable safety-event bridge. Payload/hash are immutable after PREPARED;
-- transaction metadata is updated as the signer API advances the row.
CREATE TABLE IF NOT EXISTS chain_event_outbox (
  event_id TEXT PRIMARY KEY,
  payload_json TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('LOCAL_ONLY','OUTBOX_PENDING','SUBMITTED','CONFIRMED','RETRYABLE_ERROR','DEAD_LETTER')),
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TEXT,
  transaction_hash TEXT,
  block_number INTEGER,
  chain_data_hash TEXT,
  last_http_status INTEGER,
  last_error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  confirmed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_chain_outbox_due ON chain_event_outbox(status, next_attempt_at);

-- M25/M26: active artifact metadata and runtime counters are local audit data.
CREATE TABLE IF NOT EXISTS model_registry (
  model_sha256 TEXT PRIMARY KEY,
  model_version TEXT NOT NULL,
  scaler_sha256 TEXT NOT NULL,
  preprocessing_version TEXT NOT NULL,
  artifact_path TEXT NOT NULL,
  promotion_status TEXT NOT NULL,
  metrics_json TEXT NOT NULL,
  activated_at TEXT
);

CREATE TABLE IF NOT EXISTS edge_predictions (
  device_id TEXT NOT NULL,
  boot_id TEXT NOT NULL,
  sequence INTEGER NOT NULL,
  received_at TEXT NOT NULL,
  model_version TEXT NOT NULL,
  model_sha256 TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  PRIMARY KEY (device_id, boot_id, sequence, model_sha256)
);
CREATE INDEX IF NOT EXISTS idx_edge_prediction_sample ON edge_predictions(device_id, boot_id, sequence);
