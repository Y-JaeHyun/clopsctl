"""웹 UI 라우트 테스트 — agent.ask 와 select_backend 를 mock 처리."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clopsctl import agent, web
from clopsctl.agent import AskOutcome
from clopsctl.config import Server


SAMPLE_INVENTORY_TOML = """
[server.web-1]
host = "10.0.0.1"
user = "ec2-user"
auth = "agent"
role = "read-only"
tags = ["prod"]

[server.web-2]
host = "10.0.0.2"
user = "ec2-user"
auth = "agent"
role = "shell"
"""


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch) -> Path:
    inv = tmp_path / "servers.toml"
    inv.write_text(SAMPLE_INVENTORY_TOML)
    db = tmp_path / "history.sqlite"
    monkeypatch.setenv("CLOPSCTL_INVENTORY", str(inv))
    monkeypatch.setenv("CLOPSCTL_HISTORY_DB", str(db))
    monkeypatch.setenv("CLOPSCTL_LLM_BACKEND", "claude")
    # llm.list_backends() 가 PATH lookup 하지 않도록 mock
    monkeypatch.setattr(
        web, "list_backends", lambda: [("claude", True), ("gemini", False), ("codex", False)]
    )
    return tmp_path


@pytest.fixture
def client(workspace) -> TestClient:
    return TestClient(web.app)


def test_index_renders_inventory_and_form(client):
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.text
    assert "clopsctl" in body
    assert "web-1" in body and "web-2" in body
    assert "<form method='POST' action='/ask'>" in body
    assert "name='targets' value='web-1'" in body
    assert "dry-run" in body
    # 보안 배너 노출 확인
    assert "127.0.0.1" in body


def test_healthz(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "claude" in data["backends"]


def test_ask_post_validation_no_targets(client):
    resp = client.post("/ask", data={"prompt": "hi"})
    assert resp.status_code == 200
    assert "최소 하나의 서버" in resp.text


def test_ask_post_validation_empty_prompt(client):
    resp = client.post("/ask", data={"prompt": "  ", "targets": ["web-1"]})
    assert resp.status_code == 200
    assert "프롬프트가 비어있습니다" in resp.text


def test_ask_post_validation_unknown_server(client):
    resp = client.post("/ask", data={"prompt": "x", "targets": ["ghost"]})
    assert resp.status_code == 200
    assert "인벤토리에 없는 서버" in resp.text


def test_ask_post_happy_path(client, monkeypatch):
    """agent.ask 와 select_backend 를 mock — 실제 SSH/LLM 호출 안 함."""
    captured: dict = {}

    def fake_ask(prompt, targets, *, settings, console, backend, dry_run=False):
        captured.update(prompt=prompt, targets=[t.name for t in targets], dry_run=dry_run, backend=backend.name)
        return AskOutcome(
            final_text="모든 서버 정상입니다.",
            backend_name=backend.name,
            n_steps=2, n_blocked=0, n_failed=0,
        )

    class FakeBackend:
        name = "claude"

        def is_available(self):
            return True

        def invoke(self, prompt, *, timeout=120):
            return ""

    monkeypatch.setattr(web, "select_backend", lambda *_args, **_kw: FakeBackend())
    monkeypatch.setattr(agent, "ask", fake_ask)

    resp = client.post(
        "/ask",
        data={"prompt": "현황 보고", "targets": ["web-1", "web-2"], "backend": "claude"},
    )
    assert resp.status_code == 200
    assert "Ask 결과" in resp.text
    assert "모든 서버 정상입니다" in resp.text
    assert captured["prompt"] == "현황 보고"
    assert sorted(captured["targets"]) == ["web-1", "web-2"]
    assert captured["dry_run"] is False


def test_ask_post_dry_run(client, monkeypatch):
    def fake_ask(prompt, targets, *, settings, console, backend, dry_run=False):
        assert dry_run is True
        return AskOutcome(
            final_text="[DRY-RUN] plan: web-1 :: df -h",
            backend_name="claude",
            n_steps=1, n_blocked=0, n_failed=0,
        )

    class FakeBackend:
        name = "claude"

        def is_available(self):
            return True

        def invoke(self, prompt, *, timeout=120):
            return ""

    monkeypatch.setattr(web, "select_backend", lambda *_args, **_kw: FakeBackend())
    monkeypatch.setattr(agent, "ask", fake_ask)

    resp = client.post(
        "/ask",
        data={"prompt": "디스크", "targets": ["web-1"], "dry_run": "1"},
    )
    assert resp.status_code == 200
    assert "[DRY-RUN]" in resp.text
    assert "dry-run: <b>yes</b>" in resp.text


def test_ask_post_backend_unavailable(client, monkeypatch):
    def fail_select(*_a, **_kw):
        raise RuntimeError("no LLM CLI found in PATH")
    monkeypatch.setattr(web, "select_backend", fail_select)

    resp = client.post("/ask", data={"prompt": "x", "targets": ["web-1"]})
    assert resp.status_code == 200
    assert "LLM 백엔드 오류" in resp.text
    assert "no LLM CLI found" in resp.text


def test_ask_post_html_escapes_prompt(client, monkeypatch):
    """XSS 방지 — prompt 가 HTML 이어도 escape 되어 스크립트 실행 안 됨."""
    def fake_ask(prompt, targets, *, settings, console, backend, dry_run=False):
        return AskOutcome(
            final_text="<script>alert(1)</script>",
            backend_name="claude",
            n_steps=0, n_blocked=0, n_failed=0,
        )

    class FakeBackend:
        name = "claude"

        def is_available(self):
            return True

        def invoke(self, *_a, **_kw):
            return ""

    monkeypatch.setattr(web, "select_backend", lambda *_a, **_kw: FakeBackend())
    monkeypatch.setattr(agent, "ask", fake_ask)

    resp = client.post(
        "/ask",
        data={"prompt": "<img src=x onerror='alert(1)'>", "targets": ["web-1"]},
    )
    assert resp.status_code == 200
    # 응답에 raw <script> 가 들어가면 안 됨
    assert "<script>alert(1)</script>" not in resp.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in resp.text
    assert "<img src=x" not in resp.text
