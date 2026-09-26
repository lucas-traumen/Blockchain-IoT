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
