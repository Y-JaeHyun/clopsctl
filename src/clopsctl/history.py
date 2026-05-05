"""SQLite 기반 명령/결과 히스토리 (append-only)."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

# mode 는 코드 단에서 검증 (다양한 모드 추가에 유연 — exec/ask/terminal_start/terminal/terminal_end 등).
SCHEMA = """
CREATE TABLE IF NOT EXISTS commands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    server TEXT NOT NULL,
    mode TEXT NOT NULL,
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

VALID_MODES = frozenset({"exec", "ask", "terminal_start", "terminal", "terminal_end"})


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _has_legacy_check_constraint(conn: sqlite3.Connection) -> bool:
    """기존 commands 테이블에 mode CHECK(...exec, ask...) 제약이 남아있는지 검사."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='commands'"
    ).fetchone()
    if not row or not row[0]:
        return False
    sql = row[0].lower()
    return "check" in sql and "mode" in sql


def _migrate_drop_mode_check(conn: sqlite3.Connection) -> None:
    """레거시 CHECK 제약을 제거 — 데이터 보존하며 테이블 재생성."""
    conn.execute("BEGIN")
    try:
        conn.execute("ALTER TABLE commands RENAME TO commands_old")
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT INTO commands "
            "(id, ts, server, mode, prompt, command, exit_code, stdout, stderr, "
            " llm_model, llm_tokens_in, llm_tokens_out) "
            "SELECT id, ts, server, mode, prompt, command, exit_code, stdout, stderr, "
            "       llm_model, llm_tokens_in, llm_tokens_out FROM commands_old"
        )
        conn.execute("DROP TABLE commands_old")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def init_db(db_path: Path) -> None:
    with _connect(db_path) as conn:
        conn.executescript(SCHEMA)
        if _has_legacy_check_constraint(conn):
            _migrate_drop_mode_check(conn)


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
    if mode not in VALID_MODES:
        raise ValueError(f"unknown history mode '{mode}' (allowed: {sorted(VALID_MODES)})")
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
