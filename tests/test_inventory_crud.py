"""인벤토리 CRUD web UI + write_inventory 테스트."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clopsctl import web
from clopsctl.config import Server, load_inventory, write_inventory


# --- write_inventory --------------------------------------------------------

def test_write_inventory_round_trip(tmp_path):
    p = tmp_path / "servers.toml"
    servers = {
        "web-1": Server(
            name="web-1", host="10.0.1.5", user="ec2-user", port=22,
            auth="pem", pem_path="secrets/web-1.pem", role="read-only",
            tags=("prod", "web"),
        ),
        "private-app": Server(
            name="private-app", host="10.0.0.42", user="app",
            auth="agent", role="read-only", jump="bastion",
        ),
    }
    write_inventory(p, servers)
    loaded = load_inventory(p)

    assert set(loaded) == {"web-1", "private-app"}
    assert loaded["web-1"].pem_path == "secrets/web-1.pem"
    assert loaded["web-1"].tags == ("prod", "web")
    assert loaded["private-app"].jump == "bastion"
    assert loaded["private-app"].pem_path is None


def test_write_inventory_omits_empty_optional_fields(tmp_path):
    p = tmp_path / "servers.toml"
    s = Server(name="a", host="h", user="u")
    write_inventory(p, {"a": s})
    text = p.read_text()
    assert "pem_path" not in text
    assert "password_env" not in text
    assert "tags" not in text
    assert "jump" not in text


def test_write_inventory_sets_600_permissions(tmp_path):
    import os
    p = tmp_path / "servers.toml"
    write_inventory(p, {"a": Server(name="a", host="h", user="u")})
    mode = os.stat(p).st_mode & 0o777
    assert mode == 0o600


# --- web CRUD ---------------------------------------------------------------

SAMPLE_INVENTORY = """
[server.web-1]
host = "10.0.1.5"
user = "ec2-user"
auth = "agent"
role = "read-only"

[server.bastion]
host = "203.0.113.1"
user = "ops"
auth = "pem"
pem_path = "secrets/bastion.pem"
role = "shell"

[server.private]
host = "10.0.0.42"
user = "app"
jump = "bastion"
"""


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch) -> Path:
    inv = tmp_path / "servers.toml"
    inv.write_text(SAMPLE_INVENTORY)
    db = tmp_path / "history.sqlite"
    monkeypatch.setenv("CLOPSCTL_INVENTORY", str(inv))
    monkeypatch.setenv("CLOPSCTL_HISTORY_DB", str(db))
    monkeypatch.setattr(
        web, "list_backends", lambda: [("claude", True), ("gemini", False), ("codex", False)]
    )
    return tmp_path


@pytest.fixture
def client(workspace) -> TestClient:
    return TestClient(web.app)


def test_get_servers_new_renders_form(client):
    resp = client.get("/servers/new")
    assert resp.status_code == 200
    body = resp.text
    assert "서버 추가" in body
    assert "name='host'" in body
    assert "name='auth'" in body
    assert "name='role'" in body
    assert "name='jump'" in body
    # 기존 서버들이 jump 후보로 노출
    assert "<option value='bastion'" in body or "value='web-1'" in body


def test_post_servers_creates_new_entry(client, workspace):
    resp = client.post(
        "/servers",
        data={
            "name": "db-stage", "host": "10.0.2.10", "user": "ops", "port": "22",
            "auth": "agent", "role": "shell", "tags": "stage, db", "jump": "",
        },
    )
    assert resp.status_code == 200
    assert "추가됨" in resp.text or "saved" in resp.text.lower()

    inv = load_inventory(workspace / "servers.toml")
    assert "db-stage" in inv
    assert inv["db-stage"].tags == ("stage", "db")
    assert inv["db-stage"].jump is None


def test_post_servers_duplicate_name_shows_error(client):
    resp = client.post(
        "/servers",
        data={"name": "web-1", "host": "x", "user": "u", "port": "22", "auth": "agent", "role": "shell"},
    )
    assert resp.status_code == 200
    assert "이미 존재" in resp.text


def test_post_servers_invalid_name_pattern(client):
    resp = client.post(
        "/servers",
        data={"name": "bad name!", "host": "x", "user": "u", "port": "22", "auth": "agent", "role": "shell"},
    )
    assert resp.status_code == 200
    assert "영숫자" in resp.text or "허용" in resp.text


def test_post_servers_pem_requires_path(client):
    resp = client.post(
        "/servers",
        data={"name": "p1", "host": "x", "user": "u", "port": "22", "auth": "pem", "role": "shell"},
    )
    assert resp.status_code == 200
    assert "pem_path" in resp.text and ("필요" in resp.text or "비어" in resp.text or "auth=pem" in resp.text)


def test_post_servers_jump_self_rejected(client):
    resp = client.post(
        "/servers",
        data={
            "name": "loop", "host": "x", "user": "u", "port": "22",
            "auth": "agent", "role": "shell", "jump": "loop",
        },
    )
    assert resp.status_code == 200
    # name 검증 단계에서 잡힐 수도, jump 자기참조에서 잡힐 수도 — 어느 쪽이든 폼이 다시 노출됨
    # jump 값은 inventory 에 없어서 'unknown' 메시지가 나올 수도 있음
    assert "입력 오류" in resp.text


def test_post_servers_unknown_jump(client):
    resp = client.post(
        "/servers",
        data={
            "name": "x1", "host": "h", "user": "u", "port": "22",
            "auth": "agent", "role": "shell", "jump": "ghost",
        },
    )
    assert resp.status_code == 200
    assert "ghost" in resp.text and "인벤토리에 없는" in resp.text


def test_get_edit_returns_prefilled_form(client):
    resp = client.get("/servers/web-1/edit")
    assert resp.status_code == 200
    assert "서버 편집" in resp.text
    assert "value='10.0.1.5'" in resp.text
    assert "value='ec2-user'" in resp.text
    # name 필드는 disabled
    assert "disabled" in resp.text


def test_post_edit_updates_entry(client, workspace):
    resp = client.post(
        "/servers/web-1",
        data={
            "name": "ignored",  # path 가 우선
            "host": "10.0.1.99", "user": "ec2-user", "port": "22",
            "auth": "agent", "role": "shell", "tags": "prod, updated",
        },
    )
    assert resp.status_code == 200
    assert "편집됨" in resp.text or "saved" in resp.text.lower()

    inv = load_inventory(workspace / "servers.toml")
    assert inv["web-1"].host == "10.0.1.99"
    assert inv["web-1"].role == "shell"
    assert inv["web-1"].tags == ("prod", "updated")


def test_get_delete_confirm_blocks_referenced_server(client):
    resp = client.get("/servers/bastion/delete")
    assert resp.status_code == 200
    # private 가 bastion 을 jump 로 참조하므로 차단됨
    assert "차단됨" in resp.text or "blocked" in resp.text.lower()
    assert "private" in resp.text


def test_post_delete_blocked_when_referenced(client, workspace):
    resp = client.post("/servers/bastion/delete")
    assert resp.status_code == 200
    assert "private" in resp.text
    inv = load_inventory(workspace / "servers.toml")
    assert "bastion" in inv  # 삭제되지 않음


def test_post_delete_succeeds_for_unreferenced(client, workspace):
    resp = client.post("/servers/private/delete")
    assert resp.status_code == 200
    assert "삭제됨" in resp.text
    inv = load_inventory(workspace / "servers.toml")
    assert "private" not in inv
    assert "bastion" in inv  # 다른 서버는 영향 없음


def test_index_shows_action_links(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "+ 서버 추가" in resp.text
    assert "/servers/web-1/edit" in resp.text
    assert "/servers/web-1/delete" in resp.text


def test_edit_unknown_server_404(client):
    assert client.get("/servers/ghost/edit").status_code == 404
    assert client.post("/servers/ghost", data={"host": "x", "user": "u"}).status_code == 404
    assert client.get("/servers/ghost/delete").status_code == 404
    assert client.post("/servers/ghost/delete").status_code == 404
