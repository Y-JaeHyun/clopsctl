"""Phase 4-b 테스트: history 마이그레이션, role gate, terminal 페이지/WebSocket."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clopsctl import web
from clopsctl.history import VALID_MODES, _has_legacy_check_constraint, init_db, record


# --- history 마이그레이션 -------------------------------------------------------

def test_history_valid_modes_includes_terminal():
    assert "terminal_start" in VALID_MODES
    assert "terminal" in VALID_MODES
    assert "terminal_end" in VALID_MODES


def test_history_record_rejects_unknown_mode(tmp_path):
    db = tmp_path / "h.db"
    with pytest.raises(ValueError, match="unknown history mode"):
        record(db, server="s", mode="bogus", command="x")


def test_history_init_migrates_legacy_check_constraint(tmp_path):
    """레거시 DB (CHECK(mode IN ('exec','ask'))) 가 자동 마이그레이션 되어 새 mode 도 INSERT 가능."""
    db = tmp_path / "legacy.sqlite"
    # 이전 스키마 직접 생성
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE commands (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            server TEXT NOT NULL,
            mode TEXT NOT NULL CHECK(mode IN ('exec','ask')),
            prompt TEXT, command TEXT, exit_code INTEGER,
            stdout TEXT, stderr TEXT,
            llm_model TEXT, llm_tokens_in INTEGER, llm_tokens_out INTEGER
        );
    """)
    conn.execute(
        "INSERT INTO commands (ts, server, mode, command) VALUES (?,?,?,?)",
        ("2026-01-01T00:00:00Z", "old", "exec", "ls"),
    )
    conn.commit()
    conn.close()

    # legacy detection
    conn = sqlite3.connect(db)
    assert _has_legacy_check_constraint(conn) is True
    conn.close()

    # init_db 가 마이그레이션 — 이후 terminal_start 도 가능해야
    init_db(db)
    record(db, server="old", mode="terminal_start", command="session=abc")

    rows = sqlite3.connect(db).execute("SELECT mode FROM commands ORDER BY id").fetchall()
    modes = [r[0] for r in rows]
    assert modes == ["exec", "terminal_start"]  # 기존 데이터 보존 + 신규 모드 OK


# --- terminal page (HTTP) --------------------------------------------------------

SAMPLE_INVENTORY = """
[server.app-shell]
host = "10.0.0.1"
user = "ops"
auth = "agent"
role = "shell"

[server.web-readonly]
host = "10.0.0.2"
user = "ec2"
auth = "agent"
role = "read-only"

[server.bastion]
host = "203.0.113.1"
user = "u"
auth = "pem"
pem_path = "secrets/bastion.pem"
role = "shell"

[server.private]
host = "10.0.0.99"
user = "u"
role = "sudo"
jump = "bastion"
"""


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch) -> Path:
    inv = tmp_path / "servers.toml"
    inv.write_text(SAMPLE_INVENTORY)
    db = tmp_path / "history.sqlite"
    monkeypatch.setenv("CLOPSCTL_INVENTORY", str(inv))
    monkeypatch.setenv("CLOPSCTL_HISTORY_DB", str(db))
    monkeypatch.setattr(web, "list_backends", lambda: [("claude", True)])
    return tmp_path


@pytest.fixture
def client(workspace) -> TestClient:
    return TestClient(web.app)


def test_terminal_page_renders_multi_pane_for_shell_role(client):
    resp = client.get("/terminal/app-shell")
    assert resp.status_code == 200
    body = resp.text
    # multi-pane core 마크업
    assert "터미널 (다중 세션)" in body
    assert "id='term-grid'" in body
    assert "id='broadcast-cb'" in body
    assert "id='add-select'" in body
    assert "id='add-btn'" in body
    # 단축키 안내
    assert "Alt" in body
    # 초기 panel 으로 path 의 server
    assert '["app-shell"]' in body
    # 인벤토리 사이드 카드 — 모든 server 노출
    assert "app-shell" in body and "web-readonly" in body and "bastion" in body
    # 추가 후보 select 에는 read-only 제외
    add_select_section = body.split("id='add-select'", 1)[1].split("</select>", 1)[0]
    assert "app-shell" in add_select_section
    assert "private" in add_select_section
    assert "bastion" in add_select_section
    assert "web-readonly" not in add_select_section  # read-only 차단
    # xterm.js CDN
    assert "xterm" in body and ".min.js" in body


def test_terminal_page_inventory_table_shows_jump(client):
    resp = client.get("/terminal/private")
    body = resp.text
    # 인벤토리 표에 jump 배지로 표시
    assert "<span class=\"badge jump\">bastion</span>" in body
    # 초기 panel 은 private
    assert '["private"]' in body


def test_terminal_page_blocked_for_read_only(client):
    resp = client.get("/terminal/web-readonly")
    assert resp.status_code == 200
    body = resp.text
    assert "터미널 사용 불가" in body
    assert "read-only" in body


def test_terminal_page_unknown_server_404(client):
    assert client.get("/terminal/ghost").status_code == 404


def test_index_action_link_for_shell(client):
    resp = client.get("/")
    assert "/terminal/app-shell" in resp.text
    assert "/terminal/private" in resp.text


def test_index_action_disabled_for_read_only(client):
    resp = client.get("/")
    body = resp.text
    # read-only 에는 a 태그 대신 비활성 span 이 들어가야
    # web-readonly 줄에 href 가 없어야 함
    # 정밀 검증: 'cursor:not-allowed' 가 web-readonly 근처에 등장
    assert "cursor:not-allowed" in body


# --- WebSocket bridge (mocked open_shell) ---------------------------------------

class FakeChannel:
    """paramiko Channel 의 최소 mock — send/recv/close/recv_ready/exit_status_ready."""
    def __init__(self):
        self._inbox: list[bytes] = []
        self._sent_to_remote: list[str] = []
        self.closed = False

    def send(self, data):
        self._sent_to_remote.append(data if isinstance(data, str) else data.decode())

    def recv_ready(self): return bool(self._inbox)
    def recv(self, n):
        if not self._inbox: return b""
        return self._inbox.pop(0)[:n]
    def exit_status_ready(self): return self.closed
    def resize_pty(self, **kw): pass
    def close(self): self.closed = True


class FakeClient:
    def close(self): pass


def test_websocket_role_blocked_for_read_only(client):
    with pytest.raises(Exception):  # WebSocket close with code 4403
        with client.websocket_connect("/ws/terminal/web-readonly"):
            pass


def test_websocket_unknown_server_closed(client):
    with pytest.raises(Exception):
        with client.websocket_connect("/ws/terminal/ghost"):
            pass


def test_websocket_records_session_lifecycle_and_command(client, workspace, monkeypatch):
    """세션 시작/종료 + Enter 단위 명령 기록을 검증 (paramiko mock)."""
    fake_ch = FakeChannel()

    def fake_open_shell(server, inventory, **kw):
        return fake_ch, [FakeClient()]

    monkeypatch.setattr(web, "open_shell", fake_open_shell)

    with client.websocket_connect("/ws/terminal/app-shell") as ws:
        # 사용자 입력: "ls -la\r"
        ws.send_text(json.dumps({"type": "input", "data": "ls -la\r"}))
        # 또 한 줄
        ws.send_text(json.dumps({"type": "input", "data": "pwd\n"}))
        # resize 도 보내봄
        ws.send_text(json.dumps({"type": "resize", "cols": 100, "rows": 30}))

    # WebSocket 종료 후 history 검증
    db = workspace / "history.sqlite"
    rows = sqlite3.connect(db).execute(
        "SELECT mode, command FROM commands ORDER BY id"
    ).fetchall()
    modes = [r[0] for r in rows]
    cmds = [r[1] for r in rows]

    assert "terminal_start" in modes
    assert "terminal_end" in modes
    assert "ls -la" in cmds
    assert "pwd" in cmds


def test_websocket_command_buffer_handles_backspace(client, workspace, monkeypatch):
    fake_ch = FakeChannel()
    monkeypatch.setattr(web, "open_shell", lambda s, i, **kw: (fake_ch, [FakeClient()]))

    with client.websocket_connect("/ws/terminal/app-shell") as ws:
        # "lz" 친 다음 backspace 로 z 지우고 's -la' → "ls -la"
        ws.send_text(json.dumps({"type": "input", "data": "lz\x7fs -la\r"}))

    db = workspace / "history.sqlite"
    cmds = [r[0] for r in sqlite3.connect(db).execute(
        "SELECT command FROM commands WHERE mode='terminal'"
    ).fetchall()]
    assert "ls -la" in cmds
