"""SQLite schema for essential index (no full ports / hierarchy required)."""

from __future__ import annotations

SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;

CREATE TABLE IF NOT EXISTS meta (
  context_id     TEXT PRIMARY KEY,
  top            TEXT NOT NULL DEFAULT '',
  top_filelist   TEXT NOT NULL,
  index_cwd      TEXT NOT NULL,
  defines_json   TEXT NOT NULL,
  created_at     TEXT NOT NULL,
  pyslang_version TEXT NOT NULL DEFAULT '',
  schema_version TEXT NOT NULL DEFAULT '1',
  notes_json     TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS files (
  file_id    INTEGER PRIMARY KEY,
  context_id TEXT NOT NULL,
  path       TEXT NOT NULL,
  role       TEXT NOT NULL,
  mtime_ns   INTEGER,
  size       INTEGER,
  UNIQUE (context_id, path)
);

CREATE TABLE IF NOT EXISTS modules (
  context_id TEXT NOT NULL,
  name       TEXT NOT NULL,
  kind       TEXT NOT NULL DEFAULT 'module',
  file_id    INTEGER NOT NULL,
  PRIMARY KEY (context_id, name, file_id),
  FOREIGN KEY (file_id) REFERENCES files(file_id)
);

-- Optional / lazy: empty unless build is asked to fill a subset later
CREATE TABLE IF NOT EXISTS ports (
  context_id  TEXT NOT NULL,
  module_name TEXT NOT NULL,
  name        TEXT NOT NULL,
  dir         TEXT NOT NULL,
  PRIMARY KEY (context_id, module_name, name)
);

CREATE TABLE IF NOT EXISTS build_timing (
  context_id TEXT NOT NULL,
  phase      TEXT NOT NULL,
  seconds    REAL NOT NULL,
  PRIMARY KEY (context_id, phase)
);

CREATE INDEX IF NOT EXISTS idx_files_ctx ON files(context_id);
CREATE INDEX IF NOT EXISTS idx_mod_name ON modules(context_id, name);
CREATE INDEX IF NOT EXISTS idx_mod_file ON modules(context_id, file_id);
"""

SCHEMA_VERSION = "1"
