"""SQLite storage for llm_bus."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS groups (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(project_id, name)
);
CREATE TABLE IF NOT EXISTS project_members (
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    agent TEXT NOT NULL,
    role TEXT,
    joined_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (project_id, agent)
);
CREATE TABLE IF NOT EXISTS group_members (
    group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    agent TEXT NOT NULL,
    role TEXT,
    joined_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (group_id, agent)
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    sender TEXT NOT NULL,
    role TEXT,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_messages_group ON messages(group_id, id);
"""


class BusError(Exception):
    """User-facing error."""


def default_db_path() -> Path:
    env = os.environ.get("LLM_BUS_DB")
    if env:
        return Path(env)
    return Path.home() / ".llm_bus" / "bus.db"


class Bus:
    def __init__(self, path: Path | None = None):
        self.path = path or default_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, isolation_level=None, timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # --- projects -----------------------------------------------------
    def create_project(self, name: str) -> dict:
        try:
            self.conn.execute("INSERT INTO projects(name) VALUES (?)", (name,))
        except sqlite3.IntegrityError:
            raise BusError(f"project '{name}' already exists") from None
        return self.get_project(name)

    def get_project(self, name: str) -> dict:
        row = self.conn.execute(
            "SELECT * FROM projects WHERE name=?", (name,)
        ).fetchone()
        if row is None:
            raise BusError(f"project '{name}' not found")
        return dict(row)

    def list_projects(self) -> list[dict]:
        rows = self.conn.execute(
            """SELECT p.id, p.name, p.created_at,
                      (SELECT COUNT(*) FROM groups g WHERE g.project_id=p.id) AS groups,
                      (SELECT COUNT(*) FROM project_members m WHERE m.project_id=p.id) AS members
               FROM projects p ORDER BY p.name"""
        ).fetchall()
        return [dict(r) for r in rows]

    def join_project(self, project: str, agent: str, role: str | None = None) -> dict:
        p = self.get_project(project)
        self._upsert_member("project_members", "project_id", p["id"], agent, role)
        return p

    def _upsert_member(
        self, table: str, col: str, oid: int, agent: str, role: str | None
    ) -> None:
        self.conn.execute(
            f"INSERT INTO {table}({col}, agent, role) VALUES (?,?,?)"
            f" ON CONFLICT({col}, agent) DO UPDATE SET role=COALESCE(excluded.role, role)",
            (oid, agent, role),
        )

    def project_members(self, project: str) -> list[dict]:
        p = self.get_project(project)
        rows = self.conn.execute(
            "SELECT agent, role FROM project_members WHERE project_id=? ORDER BY agent",
            (p["id"],),
        ).fetchall()
        return [dict(r) for r in rows]

    # --- groups -------------------------------------------------------
    def create_group(self, project: str, name: str) -> dict:
        p = self.get_project(project)
        try:
            self.conn.execute(
                "INSERT INTO groups(project_id, name) VALUES (?,?)", (p["id"], name)
            )
        except sqlite3.IntegrityError:
            raise BusError(
                f"group '{name}' already exists in project '{project}'"
            ) from None
        return self.get_group(project, name)

    def get_group(self, project: str, name: str) -> dict:
        p = self.get_project(project)
        row = self.conn.execute(
            "SELECT * FROM groups WHERE project_id=? AND name=?", (p["id"], name)
        ).fetchone()
        if row is None:
            raise BusError(f"group '{name}' not found in project '{project}'")
        return dict(row)

    def list_groups(self, project: str) -> list[dict]:
        p = self.get_project(project)
        rows = self.conn.execute(
            """SELECT g.id, g.name, g.created_at,
                      (SELECT COUNT(*) FROM group_members m WHERE m.group_id=g.id) AS members,
                      (SELECT COUNT(*) FROM messages x WHERE x.group_id=g.id) AS messages
               FROM groups g WHERE g.project_id=? ORDER BY g.name""",
            (p["id"],),
        ).fetchall()
        return [dict(r) for r in rows]

    def join_group(
        self, project: str, group: str, agent: str, role: str | None = None
    ) -> dict:
        g = self.get_group(project, group)
        self._upsert_member(
            "project_members", "project_id", g["project_id"], agent, role
        )
        self._upsert_member("group_members", "group_id", g["id"], agent, role)
        return g

    def group_members(self, project: str, group: str) -> list[dict]:
        g = self.get_group(project, group)
        rows = self.conn.execute(
            "SELECT agent, role FROM group_members WHERE group_id=? ORDER BY agent",
            (g["id"],),
        ).fetchall()
        return [dict(r) for r in rows]

    def memberships(self, agent: str) -> list[dict]:
        rows = self.conn.execute(
            """SELECT p.name AS project, g.name AS "group"
               FROM group_members m JOIN groups g ON g.id=m.group_id
               JOIN projects p ON p.id=g.project_id
               WHERE m.agent=? ORDER BY p.name, g.name""",
            (agent,),
        ).fetchall()
        return [dict(r) for r in rows]

    # --- messages -----------------------------------------------------
    def send(
        self, project: str, group: str, sender: str, body: str, role: str | None = None
    ) -> dict:
        g = self.get_group(project, group)
        cur = self.conn.execute(
            "INSERT INTO messages(group_id, sender, role, body) VALUES (?,?,?,?)",
            (g["id"], sender, role, body),
        )
        return self._message(cur.lastrowid)

    def _message(self, mid: int) -> dict:
        row = self.conn.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
        return dict(row)

    def latest(
        self, project: str, group: str, limit: int = 20, after: int = 0
    ) -> list[dict]:
        """Newest `limit` messages (returned oldest->newest). With `after`, only ids > after."""
        g = self.get_group(project, group)
        rows = self.conn.execute(
            """SELECT * FROM (
                   SELECT * FROM messages WHERE group_id=? AND id>? ORDER BY id DESC LIMIT ?
               ) ORDER BY id ASC""",
            (g["id"], after, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def last_id(self, project: str, group: str) -> int:
        g = self.get_group(project, group)
        row = self.conn.execute(
            "SELECT COALESCE(MAX(id),0) AS m FROM messages WHERE group_id=?", (g["id"],)
        ).fetchone()
        return int(row["m"])

    def search(
        self, project: str, group: str, query: str, limit: int = 50
    ) -> list[dict]:
        g = self.get_group(project, group)
        rows = self.conn.execute(
            """SELECT * FROM messages
               WHERE group_id=? AND (body LIKE ? OR sender LIKE ?)
               ORDER BY id DESC LIMIT ?""",
            (g["id"], f"%{query}%", f"%{query}%", limit),
        ).fetchall()
        return [dict(r) for r in rows][::-1]
