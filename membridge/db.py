"""SQLite storage for MemoryBridge.

Deliberately dependency-free: the MVP should run with a stock Python 3.11+
interpreter.  All timestamps are UTC ISO-8601 strings so that the same database
can be copied between machines without timezone surprises.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import sqlite3
import uuid
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1


def utcnow() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def json_loads(value: str | None, default: Any = None) -> Any:
    if value is None or value == "":
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


SCHEMA = r"""
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS identity (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    name            TEXT NOT NULL,
    persona         TEXT NOT NULL,
    style_json      TEXT NOT NULL DEFAULT '{}',
    boundaries_json TEXT NOT NULL DEFAULT '[]',
    version         INTEGER NOT NULL DEFAULT 1,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id               TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    path             TEXT NOT NULL,
    conventions_json TEXT NOT NULL DEFAULT '[]',
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_projects_path ON projects(path);

CREATE TABLE IF NOT EXISTS directives (
    id          TEXT PRIMARY KEY,
    scope       TEXT NOT NULL CHECK (scope IN ('global', 'project')),
    project_id  TEXT REFERENCES projects(id) ON DELETE CASCADE,
    content     TEXT NOT NULL,
    priority    INTEGER NOT NULL DEFAULT 50,
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_directives_scope ON directives(scope, project_id, active);

CREATE TABLE IF NOT EXISTS memories (
    id          TEXT PRIMARY KEY,
    project_id  TEXT REFERENCES projects(id) ON DELETE CASCADE,
    type        TEXT NOT NULL DEFAULT 'fact',
    content     TEXT NOT NULL,
    confidence  REAL NOT NULL DEFAULT 0.8,
    source      TEXT NOT NULL DEFAULT 'manual',
    status      TEXT NOT NULL DEFAULT 'active',
    uses        INTEGER NOT NULL DEFAULT 0,
    helpful     INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memories_project ON memories(project_id, status);
CREATE INDEX IF NOT EXISTS idx_memories_type ON memories(type, status);

CREATE TABLE IF NOT EXISTS documents (
    id          TEXT PRIMARY KEY,
    project_id  TEXT REFERENCES projects(id) ON DELETE CASCADE,
    path        TEXT NOT NULL,
    title       TEXT NOT NULL,
    content     TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_documents_project ON documents(project_id, path);

CREATE TABLE IF NOT EXISTS skills (
    id            TEXT PRIMARY KEY,
    project_id    TEXT REFERENCES projects(id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    trigger       TEXT NOT NULL,
    steps_json    TEXT NOT NULL DEFAULT '[]',
    status        TEXT NOT NULL DEFAULT 'candidate',
    success_count INTEGER NOT NULL DEFAULT 0,
    failure_count INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_skills_project ON skills(project_id, status);

CREATE TABLE IF NOT EXISTS sessions (
    context_version  TEXT PRIMARY KEY,
    agent_id         TEXT NOT NULL,
    project_id       TEXT,
    identity_version INTEGER NOT NULL,
    created_at       TEXT NOT NULL,
    expires_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    at         TEXT NOT NULL,
    actor      TEXT NOT NULL DEFAULT 'system',
    action     TEXT NOT NULL,
    entity     TEXT,
    detail_json TEXT NOT NULL DEFAULT '{}'
);
"""


def connect(db_path: str | os.PathLike[str]) -> sqlite3.Connection:
    path = str(db_path)
    if path != ":memory:":
        Path(path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    now = utcnow()
    conn.execute(
        """
        INSERT OR IGNORE INTO identity
            (id, name, persona, style_json, boundaries_json, version, updated_at)
        VALUES (1, ?, ?, ?, ?, 1, ?)
        """,
        (
            "默认",
            "可靠、直接、先给结论；不确定时明确说不确定。",
            json_dumps({"tone": "direct", "language": "zh"}),
            json_dumps(["不编造未经验证的事实", "不泄露密钥与隐私"]),
            now,
        ),
    )
    conn.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES (?, ?)",
        ("schema_version", str(SCHEMA_VERSION)),
    )
    conn.commit()
