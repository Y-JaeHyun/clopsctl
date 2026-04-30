"""Follow-up (대화 이어가기) 흐름 테스트."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from rich.console import Console

from clopsctl import agent, web
from clopsctl.agent import AskOutcome
from clopsctl.config import Server, Settings
from clopsctl.ssh import ExecResult


SAMPLE_INVENTORY = """
[server.web-1]
host = "10.0.0.1"
user = "ec2"
auth = "agent"
role = "read-only"
"""


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch) -> Path:
    inv = tmp_path / "servers.toml"
    inv.write_text(SAMPLE_INVENTORY)
    db = tmp_path / "history.sqlite"
    monkeypatch.setenv("CLOPSCTL_INVENTORY", str(inv))
    monkeypatch.setenv("CLOPSCTL_HISTORY_DB", str(db))
    monkeypatch.setattr(
        web, "list_backends", lambda: [("claude", True)]
    )
    web.JOBS.clear()
    web.CONVERSATIONS.clear()
    return tmp_path


@pytest.fixture
def client(workspace) -> TestClient:
    return TestClient(web.app)


# --- agent.ask prior_turns -----------------------------------------------------

def test_agent_ask_passes_prior_turns_into_plan_prompt(monkeypatch, tmp_path):
    captured: dict = {}

    class FakeBackend:
        name = "fake"
        def is_available(self): return True
        def invoke(self, prompt, *, timeout=120):
            captured.setdefault("prompts", []).append(prompt)
            return json.dumps({"steps": []})

    settings = Settings(
        inventory_path=tmp_path / "x.toml", history_db=tmp_path / "h.db",
        model="m", safety_confirm=True, web_host="127.0.0.1", web_port=1,
        llm_backend="fake", permission_mode="strict",
    )
    server = Server(name="s1", host="h", user="u")

    prior = [
        {"prompt": "디스크 상태 알려줘", "final_text": "/var 가 92% 입니다."},
    ]
    agent.ask(
        "그럼 무엇을 지우면 되는지 알려줘", [server],
        settings=settings, console=Console(quiet=True), backend=FakeBackend(),
        prior_turns=prior,
    )

    plan_prompt = captured["prompts"][0]
    assert "이전 대화" in plan_prompt
    assert "디스크 상태 알려줘" in plan_prompt
    assert "/var 가 92%" in plan_prompt
    assert "그럼 무엇을 지우면 되는지 알려줘" in plan_prompt


def test_agent_ask_no_history_when_no_prior_turns(monkeypatch, tmp_path):
    captured: list[str] = []

    class FakeBackend:
        name = "fake"
        def is_available(self): return True
        def invoke(self, prompt, *, timeout=120):
            captured.append(prompt)
            return json.dumps({"steps": []})

    settings = Settings(
        inventory_path=tmp_path / "x.toml", history_db=tmp_path / "h.db",
        model="m", safety_confirm=True, web_host="127.0.0.1", web_port=1,
        llm_backend="fake", permission_mode="strict",
    )
    server = Server(name="s1", host="h", user="u")
    agent.ask(
        "p", [server],
        settings=settings, console=Console(quiet=True), backend=FakeBackend(),
    )
    assert "이전 대화" not in captured[0]


# --- web follow-up flow --------------------------------------------------------

def _job_id_from(html_text: str) -> str:
    m = re.search(r"EventSource\('/ask/stream/([0-9a-f]+)'\)", html_text)
    assert m, "job_id not found"
    return m.group(1)


def _conv_id_from(html_text: str) -> str:
    m = re.search(r"name='conversation_id' value='([0-9a-f]+)'", html_text)
    assert m, "conversation_id not found"
    return m.group(1)


def _drain(client: TestClient, job_id: str, max_seconds: float = 5.0):
    deadline = time.monotonic() + max_seconds
    with client.stream("GET", f"/ask/stream/{job_id}") as resp:
        for line in resp.iter_lines():
            if time.monotonic() > deadline:
                break
            if not line:
                continue
            if isinstance(line, bytes):
                line = line.decode()
            if line.startswith("data: "):
                if line[6:] == "end":
                    break
                yield json.loads(line[6:])
            elif line.startswith("event: eof"):
                break


def test_followup_creates_conversation_then_appends_turn(client, monkeypatch):
    seen_prior: list[list] = []

    def fake_ask(prompt, targets, *, settings, console, backend, dry_run=False, on_event=None, prior_turns=None):
        seen_prior.append(list(prior_turns or []))
        if on_event:
            on_event({"type": "done", "final_text": f"answer to: {prompt}",
                      "n_steps": 1, "n_blocked": 0, "n_failed": 0})
        return AskOutcome(final_text=f"answer to: {prompt}", backend_name="claude",
                          n_steps=1, n_blocked=0, n_failed=0)

    class FakeBackend:
        name = "claude"
        def is_available(self): return True
        def invoke(self, *a, **kw): return ""

    monkeypatch.setattr(web, "select_backend", lambda *_a, **_kw: FakeBackend())
    monkeypatch.setattr(agent, "ask", fake_ask)

    # 1차 ask
    r1 = client.post("/ask", data={"prompt": "첫 질문", "targets": ["web-1"]})
    assert r1.status_code == 200
    conv_id = _conv_id_from(r1.text)
    assert "name='conversation_id'" in r1.text  # follow-up 폼이 페이지에 있음
    job1 = _job_id_from(r1.text)
    list(_drain(client, job1))

    # conversation 에 turn 1 기록됐는지
    assert conv_id in web.CONVERSATIONS
    assert len(web.CONVERSATIONS[conv_id].turns) == 1
    assert web.CONVERSATIONS[conv_id].turns[0]["prompt"] == "첫 질문"

    # 2차 follow-up — targets 비어도 conversation 에서 가져옴
    r2 = client.post(
        "/ask",
        data={"prompt": "이전 답변 기반 추가 질문", "conversation_id": conv_id},
    )
    assert r2.status_code == 200
    # 페이지에 이전 turn 카드가 함께 노출
    assert "Turn 1" in r2.text
    assert "첫 질문" in r2.text
    assert "answer to: 첫 질문" in r2.text
    # 새 turn 도 표시
    assert "Turn 2" in r2.text
    job2 = _job_id_from(r2.text)
    list(_drain(client, job2))

    # 두 번째 호출에 prior_turns 가 1개 들어감
    assert len(seen_prior) == 2
    assert seen_prior[0] == []  # 1차는 없음
    assert len(seen_prior[1]) == 1
    assert seen_prior[1][0]["prompt"] == "첫 질문"
    # turn 누적
    assert len(web.CONVERSATIONS[conv_id].turns) == 2


def test_followup_with_unknown_conversation_id_starts_fresh(client, monkeypatch):
    """존재하지 않는 conversation_id 면 새 conversation 으로 처리, prior_turns 없음."""
    seen_prior: list[list] = []

    def fake_ask(prompt, targets, *, settings, console, backend, dry_run=False, on_event=None, prior_turns=None):
        seen_prior.append(list(prior_turns or []))
        if on_event:
            on_event({"type": "done", "final_text": "x",
                      "n_steps": 0, "n_blocked": 0, "n_failed": 0})
        return AskOutcome(final_text="x", backend_name="claude", n_steps=0, n_blocked=0, n_failed=0)

    class FakeBackend:
        name = "claude"
        def is_available(self): return True
        def invoke(self, *a, **kw): return ""

    monkeypatch.setattr(web, "select_backend", lambda *_a, **_kw: FakeBackend())
    monkeypatch.setattr(agent, "ask", fake_ask)

    # 모르는 conv_id + targets 명시
    r = client.post(
        "/ask",
        data={"prompt": "첫 질문", "conversation_id": "ghost", "targets": ["web-1"]},
    )
    assert r.status_code == 200
    job = _job_id_from(r.text)
    list(_drain(client, job))
    assert seen_prior[-1] == []


def test_followup_card_present_in_streaming_page(client, monkeypatch):
    def fake_ask(prompt, targets, *, settings, console, backend, dry_run=False, on_event=None, prior_turns=None):
        if on_event:
            on_event({"type": "done", "final_text": "x", "n_steps": 0, "n_blocked": 0, "n_failed": 0})
        return AskOutcome(final_text="x", backend_name="claude", n_steps=0, n_blocked=0, n_failed=0)

    class FakeBackend:
        name = "claude"
        def is_available(self): return True
        def invoke(self, *a, **kw): return ""

    monkeypatch.setattr(web, "select_backend", lambda *_a, **_kw: FakeBackend())
    monkeypatch.setattr(agent, "ask", fake_ask)

    r = client.post("/ask", data={"prompt": "p", "targets": ["web-1"]})
    body = r.text
    assert "id='followup-card'" in body
    assert "id='followup-prompt'" in body
    assert "이어서 질문" in body
    # 초기에는 disabled
    assert "disabled>" in body
