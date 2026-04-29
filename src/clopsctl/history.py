"""SQLite 기반 명령/결과 히스토리 (append-only)."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS commands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    server TEXT NOT NULL,
    mode TEXT NOT NULL CHECK(mode IN ('exec','ask')),
    prompt TEXT,
    command TEXT,
    exit_code INTEGER,
    stdout TEXT,
    stderr TEXT,
    llm_model TEXT,
    llm_tokens_in INTEGER,
    llm_tokens_out INTEGER
);
CREATE INDEX IF NOT EXISTS idx_commands_server_ts ON commands(server, ts);
CREATE INDEX IF NOT EXISTS idx_commands_ts ON commands(ts);
"""


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Path) -> None:
    with _connect(db_path) as conn:
        conn.executescript(SCHEMA)


@contextmanager
def history(db_path: Path) -> Iterator[sqlite3.Connection]:
    init_db(db_path)
    conn = _connect(db_path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def record(
    db_path: Path,
    *,
    server: str,
    mode: str,
    command: str,
    prompt: str | None = None,
    exit_code: int | None = None,
    stdout: str | None = None,
    stderr: str | None = None,
    llm_model: str | None = None,
    llm_tokens_in: int | None = None,
    llm_tokens_out: int | None = None,
) -> int:
    ts = datetime.now(timezone.utc).isoformat()
    with history(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO commands
                (ts, server, mode, prompt, command, exit_code, stdout, stderr,
                 llm_model, llm_tokens_in, llm_tokens_out)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (ts, server, mode, prompt, command, exit_code, stdout, stderr,
             llm_model, llm_tokens_in, llm_tokens_out),
        )
        return int(cur.lastrowid or 0)


def search(
    db_path: Path,
    *,
    server: str | None = None,
    grep: str | None = None,
    limit: int = 50,
) -> list[sqlite3.Row]:
    init_db(db_path)
    sql = "SELECT * FROM commands"
    where: list[str] = []
    params: list[object] = []
    if server:
        where.append("server = ?")
        params.append(server)
    if grep:
        where.append("(command LIKE ? OR prompt LIKE ? OR stdout LIKE ?)")
        like = f"%{grep}%"
        params.extend([like, like, like])
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with _connect(db_path) as conn:
        return list(conn.execute(sql, params).fetchall())
