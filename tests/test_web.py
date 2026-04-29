"""웹 UI 라우트 테스트 — agent.ask 와 select_backend 를 mock 처리."""
from __future__ import annotations

import json
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


class _FakeBackend:
    name = "claude"

    def is_available(self):
        return True

    def invoke(self, prompt, *, timeout=120):
        return ""


def _drain_sse(client: TestClient, job_id: str, max_seconds: float = 5.0) -> list[dict]:
    """SSE 스트림에서 모든 이벤트 dict 를 수집 (eof 또는 타임아웃까지)."""
    import time as _t
    events = []
    deadline = _t.monotonic() + max_seconds
    with client.stream("GET", f"/ask/stream/{job_id}") as resp:
        assert resp.status_code == 200
        for line in resp.iter_lines():
            if _t.monotonic() > deadline:
                break
            if not line:
                continue
            if isinstance(line, bytes):
                line = line.decode()
            if line.startswith("data: "):
                payload = line[6:]
                if payload == "end":
                    break
                events.append(json.loads(payload))
            elif line.startswith("event: eof"):
                break
    return events


def test_ask_post_happy_path_streams_events(client, monkeypatch):
    """POST 가 즉시 streaming 페이지 반환, 백그라운드 thread 가 큐에 이벤트 push."""
    captured: dict = {}

    def fake_ask(prompt, targets, *, settings, console, backend, dry_run=False, on_event=None):
        captured.update(prompt=prompt, targets=[t.name for t in targets], dry_run=dry_run)
        if on_event:
            on_event({"type": "started", "backend": backend.name, "dry_run": dry_run, "servers": [t.name for t in targets]})
            on_event({"type": "plan_done", "n_steps": 1, "steps": []})
            on_event({"type": "step_result", "step": 0, "server": "web-1", "exit_code": 0,
                      "stdout_preview": "ok", "stderr_preview": "", "error": None})
            on_event({"type": "done", "final_text": "모든 서버 정상입니다.",
                      "n_steps": 1, "n_blocked": 0, "n_failed": 0})
        return AskOutcome(final_text="모든 서버 정상입니다.", backend_name=backend.name,
                          n_steps=1, n_blocked=0, n_failed=0)

    monkeypatch.setattr(web, "select_backend", lambda *_a, **_kw: _FakeBackend())
    monkeypatch.setattr(agent, "ask", fake_ask)

    resp = client.post(
        "/ask",
        data={"prompt": "현황 보고", "targets": ["web-1", "web-2"], "backend": "claude"},
    )
    assert resp.status_code == 200
    assert "Ask 진행 중" in resp.text
    # 페이지 안에 EventSource 가 있고 job_id 가 임베드됨
    assert "EventSource('/ask/stream/" in resp.text

    # job_id 추출
    import re
    m = re.search(r"EventSource\('/ask/stream/([0-9a-f]+)'\)", resp.text)
    assert m, "job_id not found in streaming page"
    job_id = m.group(1)

    events = _drain_sse(client, job_id)
    types = [e["type"] for e in events]
    assert "started" in types
    assert "plan_done" in types
    assert "step_result" in types
    assert "done" in types
    done = next(e for e in events if e["type"] == "done")
    assert done["final_text"] == "모든 서버 정상입니다."
    assert captured["prompt"] == "현황 보고"
    assert sorted(captured["targets"]) == ["web-1", "web-2"]


def test_ask_post_dry_run_emits_done_event(client, monkeypatch):
    def fake_ask(prompt, targets, *, settings, console, backend, dry_run=False, on_event=None):
        assert dry_run is True
        if on_event:
            on_event({"type": "step_dry_run", "step": 0, "command": "df -h", "servers": ["web-1"]})
            on_event({"type": "done", "final_text": "[DRY-RUN] plan: web-1 :: df -h",
                      "n_steps": 1, "n_blocked": 0, "n_failed": 0})
        return AskOutcome(final_text="x", backend_name="claude", n_steps=1, n_blocked=0, n_failed=0)

    monkeypatch.setattr(web, "select_backend", lambda *_a, **_kw: _FakeBackend())
    monkeypatch.setattr(agent, "ask", fake_ask)

    resp = client.post(
        "/ask",
        data={"prompt": "디스크", "targets": ["web-1"], "dry_run": "1"},
    )
    assert resp.status_code == 200
    assert "dry-run: <b>yes</b>" in resp.text

    import re
    job_id = re.search(r"EventSource\('/ask/stream/([0-9a-f]+)'\)", resp.text).group(1)
    events = _drain_sse(client, job_id)
    types = [e["type"] for e in events]
    assert "step_dry_run" in types
    done = next(e for e in events if e["type"] == "done")
    assert "[DRY-RUN]" in done["final_text"]


def test_ask_stream_unknown_job_404(client):
    resp = client.get("/ask/stream/not-a-real-job-id")
    assert resp.status_code == 404


def test_ask_stream_emits_error_on_exception(client, monkeypatch):
    def fake_ask(*_a, **_kw):
        raise RuntimeError("boom: backend exploded")

    monkeypatch.setattr(web, "select_backend", lambda *_a, **_kw: _FakeBackend())
    monkeypatch.setattr(agent, "ask", fake_ask)

    resp = client.post("/ask", data={"prompt": "x", "targets": ["web-1"]})
    assert resp.status_code == 200
    import re
    job_id = re.search(r"EventSource\('/ask/stream/([0-9a-f]+)'\)", resp.text).group(1)
    events = _drain_sse(client, job_id)
    err = next(e for e in events if e["type"] == "error")
    assert "boom: backend exploded" in err["message"]


def test_ask_post_backend_unavailable(client, monkeypatch):
    def fail_select(*_a, **_kw):
        raise RuntimeError("no LLM CLI found in PATH")
    monkeypatch.setattr(web, "select_backend", fail_select)

    resp = client.post("/ask", data={"prompt": "x", "targets": ["web-1"]})
    assert resp.status_code == 200
    assert "LLM 백엔드 오류" in resp.text
    assert "no LLM CLI found" in resp.text


def test_ask_post_html_escapes_prompt(client, monkeypatch):
    """XSS 방지 — 폼 페이지에 echo 되는 prompt 가 escape 처리되어야."""
    def fake_ask(prompt, targets, *, settings, console, backend, dry_run=False, on_event=None):
        if on_event:
            on_event({"type": "done", "final_text": "ok", "n_steps": 0, "n_blocked": 0, "n_failed": 0})
        return AskOutcome(final_text="ok", backend_name="claude",
                          n_steps=0, n_blocked=0, n_failed=0)

    monkeypatch.setattr(web, "select_backend", lambda *_a, **_kw: _FakeBackend())
    monkeypatch.setattr(agent, "ask", fake_ask)

    resp = client.post(
        "/ask",
        data={"prompt": "<img src=x onerror='alert(1)'>", "targets": ["web-1"]},
    )
    assert resp.status_code == 200
    # 페이지 안에 raw <img> 태그가 들어가면 안 됨 (escape 됐어야)
    assert "<img src=x onerror=" not in resp.text
    assert "&lt;img src=x onerror=" in resp.text
