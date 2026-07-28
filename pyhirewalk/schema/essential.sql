-- Essential index for company-scale timing: files + module→file only.
-- ports / instances are NOT filled by build_essential_db (lazy later).
-- Canonical schema also lives in pyhirewalk.index.schema.SCHEMA_SQL.

CREATE TABLE meta (
  context_id      TEXT PRIMARY KEY,
  top             TEXT NOT NULL DEFAULT '',
  top_filelist    TEXT NOT NULL,
  index_cwd       TEXT NOT NULL,
  defines_json    TEXT NOT NULL,
  created_at      TEXT NOT NULL,
  pyslang_version TEXT NOT NULL DEFAULT '',
  schema_version  TEXT NOT NULL DEFAULT '1',
  notes_json      TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE files (
  file_id    INTEGER PRIMARY KEY,
  context_id TEXT NOT NULL,
  path       TEXT NOT NULL,
  role       TEXT NOT NULL,    -- listed | library | definition
  mtime_ns   INTEGER,
  size       INTEGER,
  UNIQUE (context_id, path)
);

CREATE TABLE modules (
  context_id TEXT NOT NULL,
  name       TEXT NOT NULL,
  kind       TEXT NOT NULL DEFAULT 'module',
  file_id    INTEGER NOT NULL,
  PRIMARY KEY (context_id, name, file_id),
  FOREIGN KEY (file_id) REFERENCES files(file_id)
);

-- Present but empty after essential build
CREATE TABLE ports (
  context_id  TEXT NOT NULL,
  module_name TEXT NOT NULL,
  name        TEXT NOT NULL,
  dir         TEXT NOT NULL,
  PRIMARY KEY (context_id, module_name, name)
);

CREATE TABLE build_timing (
  context_id TEXT NOT NULL,
  phase      TEXT NOT NULL,
  seconds    REAL NOT NULL,
  PRIMARY KEY (context_id, phase)
);

CREATE INDEX idx_mod_name ON modules(context_id, name);

