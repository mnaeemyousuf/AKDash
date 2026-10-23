"""SQLite helpers for projects, uploads, chat messages and dashboard spec versions."""
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime

DB_PATH = os.environ.get("AKDASH_DB", "app.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    email TEXT DEFAULT '',
    full_name TEXT DEFAULT '',
    password_hash TEXT NOT NULL,               -- pbkdf2_sha256$iterations$salt$hash
    role TEXT NOT NULL DEFAULT 'user',         -- 'admin' or 'user'
    is_active INTEGER NOT NULL DEFAULT 1,
    persona_json TEXT DEFAULT '{}',            -- job title, department, goals, language, detail level...
    created_at TEXT NOT NULL,
    last_login TEXT
);
CREATE TABLE IF NOT EXISTS sessions (           -- "keep me logged in" tokens (only the hash is stored)
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    mode TEXT NOT NULL DEFAULT 'replace',      -- 'replace' or 'append'
    instructions TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS uploads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    upload_date TEXT NOT NULL,                 -- YYYY-MM-DD
    uploaded_at TEXT NOT NULL,
    filenames TEXT NOT NULL,                   -- JSON list of original file names
    row_count INTEGER NOT NULL,
    snapshot_dir TEXT NOT NULL,                -- folder with one parquet file per table
    tables_json TEXT NOT NULL                  -- JSON {table: {columns, rows, sources, report}}
);
CREATE TABLE IF NOT EXISTS spec_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    spec_json TEXT NOT NULL,
    source TEXT NOT NULL,                      -- generate / chat / revert / refresh
    note TEXT DEFAULT '',                      -- short description of the change
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    role TEXT NOT NULL,                        -- user / assistant
    content TEXT NOT NULL,
    spec_version_id INTEGER,                   -- set when this message changed the dashboard
    extra_json TEXT DEFAULT '{}',              -- e.g. {"sql": ..., "columns": [...], "rows": [...]}
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS column_mappings (
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    old_column TEXT NOT NULL,
    new_column TEXT NOT NULL,
    PRIMARY KEY (project_id, old_column)
);
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@contextmanager
def connect():
    # timeout: when several users write at once, wait for the lock instead of failing with "database is locked"
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        # WAL: readers don't block writers (many users at once); the setting is stored in the database file
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.executescript(SCHEMA)
        # add columns introduced after Step 1 to existing databases
        for table, column, ddl in [("spec_versions", "note", "TEXT DEFAULT ''"),
                                   ("chat_messages", "extra_json", "TEXT DEFAULT '{}'"),
                                   ("projects", "user_id", "INTEGER REFERENCES users(id)")]:
            existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


# ---------- projects ----------

def create_project(name: str, description: str = "", mode: str = "replace", user_id: int | None = None) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO projects (name, description, mode, created_at, user_id) VALUES (?, ?, ?, ?, ?)",
            (name.strip(), description.strip(), mode, _now(), user_id),
        )
        return cur.lastrowid


def list_projects(user_id: int | None = None) -> list[dict]:
    """A user's projects (all projects when user_id is None)."""
    with connect() as conn:
        if user_id is None:
            rows = conn.execute("SELECT * FROM projects ORDER BY created_at DESC, id DESC").fetchall()
        else:
            rows = conn.execute("SELECT * FROM projects WHERE user_id = ? ORDER BY created_at DESC, id DESC",
                                (user_id,)).fetchall()
        return [dict(r) for r in rows]


# ---------- users ----------

def create_user(username: str, password_hash: str, email: str = "", full_name: str = "",
                role: str = "user") -> int:
    """Creates a user. The first user becomes admin and takes over projects created before login existed."""
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")   # count + insert as one step: two sign-ups at once can't both be "first"
        first = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
        cur = conn.execute(
            "INSERT INTO users (username, email, full_name, password_hash, role, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (username.strip(), email.strip(), full_name.strip(), password_hash, "admin" if first else role, _now()),
        )
        if first:
            conn.execute("UPDATE projects SET user_id = ? WHERE user_id IS NULL", (cur.lastrowid,))
        return cur.lastrowid


def _user_row(row) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    d["persona"] = json.loads(d.pop("persona_json") or "{}")
    return d


def get_user(user_id: int) -> dict | None:
    with connect() as conn:
        return _user_row(conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone())


def get_user_by_username(username: str) -> dict | None:
    with connect() as conn:
        return _user_row(conn.execute("SELECT * FROM users WHERE username = ?", (username.strip(),)).fetchone())


def list_users() -> list[dict]:
    with connect() as conn:
        return [_user_row(r) for r in conn.execute("SELECT * FROM users ORDER BY id").fetchall()]


def update_user(user_id: int, **fields) -> None:
    allowed = {"email", "full_name", "password_hash", "role", "is_active", "last_login"}
    values = {k: v for k, v in fields.items() if k in allowed}
    if "persona" in fields:
        values["persona_json"] = json.dumps(fields["persona"], ensure_ascii=False)
    if not values:
        return
    sets = ", ".join(f"{k} = ?" for k in values)
    with connect() as conn:
        conn.execute(f"UPDATE users SET {sets} WHERE id = ?", (*values.values(), user_id))


def add_session(token_hash: str, user_id: int, expires_at: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM sessions WHERE expires_at < ?", (_now(),))
        conn.execute("INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
                     (token_hash, user_id, expires_at))


def session_user_id(token_hash: str) -> int | None:
    with connect() as conn:
        row = conn.execute("SELECT user_id FROM sessions WHERE token_hash = ? AND expires_at > ?",
                           (token_hash, _now())).fetchone()
        return row["user_id"] if row else None


def delete_session(token_hash: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))


def delete_user_sessions(user_id: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))


def count_admins() -> int:
    with connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM users WHERE role = 'admin' AND is_active = 1").fetchone()[0]


def get_project(project_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        return dict(row) if row else None


def update_project(project_id: int, **fields) -> None:
    allowed = {"name", "description", "mode", "instructions"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields:
        return
    sets = ", ".join(f"{k} = ?" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE projects SET {sets} WHERE id = ?", (*fields.values(), project_id))


def delete_project(project_id: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))


# ---------- uploads ----------

def add_upload(project_id: int, upload_date: str, filenames: list[str], row_count: int,
               snapshot_dir: str, tables: dict) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO uploads (project_id, upload_date, uploaded_at, filenames, row_count, snapshot_dir, tables_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (project_id, upload_date, _now(), json.dumps(filenames), row_count, snapshot_dir,
             json.dumps(tables, default=str)),
        )
        return cur.lastrowid


def set_upload_snapshot_dir(upload_id: int, snapshot_dir: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE uploads SET snapshot_dir = ? WHERE id = ?", (snapshot_dir, upload_id))


def delete_upload(upload_id: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM uploads WHERE id = ?", (upload_id,))


def _upload_row(row) -> dict:
    d = dict(row)
    d["filenames"] = json.loads(d["filenames"])
    d["tables"] = json.loads(d.pop("tables_json"))
    return d


def list_uploads(project_id: int) -> list[dict]:
    """Newest first."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM uploads WHERE project_id = ? ORDER BY id DESC", (project_id,)
        ).fetchall()
        return [_upload_row(r) for r in rows]


def latest_upload(project_id: int) -> dict | None:
    uploads = list_uploads(project_id)
    return uploads[0] if uploads else None


# ---------- dashboard spec versions ----------

def save_spec_version(project_id: int, spec: dict, source: str, note: str = "") -> int:
    with connect() as conn:
        version = conn.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 FROM spec_versions WHERE project_id = ?", (project_id,)
        ).fetchone()[0]
        cur = conn.execute(
            "INSERT INTO spec_versions (project_id, version, spec_json, source, note, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (project_id, version, json.dumps(spec, default=str), source, note[:200], _now()),
        )
        return cur.lastrowid


def list_spec_versions(project_id: int) -> list[dict]:
    """Newest first, without the spec body."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, version, source, note, created_at FROM spec_versions WHERE project_id = ?"
            " ORDER BY version DESC", (project_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_spec_version(version_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM spec_versions WHERE id = ?", (version_id,)).fetchone()
        return _spec_row(row) if row else None


def revert_to_version(project_id: int, version_id: int) -> int | None:
    """Revert by saving a copy of an old version as the newest one (history is never deleted)."""
    old = get_spec_version(version_id)
    if old is None or old["project_id"] != project_id:
        return None
    return save_spec_version(project_id, old["spec"], "revert", f"Reverted to version {old['version']}")


# ---------- column mappings (daily uploads) ----------

def get_mappings(project_id: int) -> dict[str, str]:
    """{"old_table": "new_table", "table.old_column": "new_column"}"""
    with connect() as conn:
        rows = conn.execute("SELECT old_column, new_column FROM column_mappings WHERE project_id = ?",
                            (project_id,)).fetchall()
        return {r["old_column"]: r["new_column"] for r in rows}


def save_mappings(project_id: int, mappings: dict[str, str]) -> None:
    with connect() as conn:
        conn.executemany(
            "INSERT INTO column_mappings (project_id, old_column, new_column) VALUES (?, ?, ?)"
            " ON CONFLICT(project_id, old_column) DO UPDATE SET new_column = excluded.new_column",
            [(project_id, old, new) for old, new in mappings.items()],
        )


# ---------- chat ----------

def add_chat_message(project_id: int, role: str, content: str, spec_version_id: int | None = None,
                     extra: dict | None = None) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO chat_messages (project_id, role, content, spec_version_id, extra_json, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (project_id, role, content, spec_version_id, json.dumps(extra or {}, default=str), _now()),
        )
        return cur.lastrowid


def list_chat_messages(project_id: int) -> list[dict]:
    """Oldest first."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT m.*, v.version AS spec_version FROM chat_messages m"
            " LEFT JOIN spec_versions v ON v.id = m.spec_version_id"
            " WHERE m.project_id = ? ORDER BY m.id", (project_id,)
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["extra"] = json.loads(d.pop("extra_json") or "{}")
        out.append(d)
    return out


def _spec_row(row) -> dict:
    d = dict(row)
    d["spec"] = json.loads(d.pop("spec_json"))
    return d


def latest_spec(project_id: int) -> dict | None:
    """The newest spec version row ({id, version, spec, source, created_at}) or None."""
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM spec_versions WHERE project_id = ? ORDER BY version DESC LIMIT 1", (project_id,)
        ).fetchone()
        return _spec_row(row) if row else None
