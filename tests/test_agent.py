"""agent.ask 의 Plan→Execute→Summarize 흐름을 fake LLM 백엔드로 검증."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from rich.console import Console

from clopsctl import agent
from clopsctl.config import Server, Settings
from clopsctl.ssh import ExecResult


@dataclass
class FakeBackend:
    """LLM CLI 백엔드 mock — 큐에 넣어둔 응답을 차례로 반환."""
    name: str = "fake"
    responses: list[str] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)

    def is_available(self) -> bool:
        return True

    def invoke(self, prompt: str, *, timeout: int = 120) -> str:
        self.calls.append(prompt)
        if not self.responses:
            raise AssertionError("FakeBackend response queue exhausted")
        return self.responses.pop(0)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        inventory_path=tmp_path / "servers.toml",
        history_db=tmp_path / "history.sqlite",
        model="claude-opus-4-7",
        safety_confirm=True,
        web_host="127.0.0.1",
        web_port=8765,
        llm_backend="fake",
    )


@pytest.fixture
def server() -> Server:
    return Server(name="web-1", host="10.0.0.1", user="ec2", auth="agent", role="read-only")


@pytest.fixture
def server2() -> Server:
    return Server(name="web-2", host="10.0.0.2", user="ec2", auth="agent", role="read-only")


@pytest.fixture
def console():
    return Console(quiet=True)


def test_plan_execute_summarize_happy_path(monkeypatch, settings, server, console):
    monkeypatch.setattr(
        agent, "run",
        lambda srv, cmd: ExecResult(srv.name, srv.host, 0, "Filesystem 90%\n", "")
    )
    backend = FakeBackend(responses=[
        json.dumps({"steps": [{"server": "web-1", "command": "df -h"}]}),
        "web-1 의 / 가 90% 사용 중입니다.",
    ])
    outcome = agent.ask("디스크 상태", [server], settings=settings, console=console, backend=backend)

    assert outcome.final_text == "web-1 의 / 가 90% 사용 중입니다."
    assert outcome.n_steps == 1
    assert outcome.n_blocked == 0
    assert outcome.backend_name == "fake"
    assert len(backend.calls) == 2
    rows = sqlite3.connect(settings.history_db).execute(
        "SELECT command, exit_code, stdout FROM commands"
    ).fetchall()
    assert rows == [("df -h", 0, "Filesystem 90%\n")]


def test_safety_blocks_dangerous_command(monkeypatch, settings, server, console):
    monkeypatch.setattr(
        agent, "run",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not run"))
    )
    backend = FakeBackend(responses=[
        json.dumps({"steps": [{"server": "web-1", "command": "rm -rf /"}]}),
        "위험 명령이라 차단되었습니다.",
    ])
    outcome = agent.ask("청소", [server], settings=settings, console=console, backend=backend)

    assert outcome.n_blocked == 1
    assert outcome.n_steps == 1
    rows = sqlite3.connect(settings.history_db).execute(
        "SELECT stderr FROM commands WHERE server='web-1'"
    ).fetchall()
    assert any("safety gate blocked" in (r[0] or "") for r in rows)


def test_fan_out_step_records_per_server(monkeypatch, settings, server, server2, console):
    monkeypatch.setattr(
        agent, "fan_out",
        lambda srvs, cmd: [ExecResult(s.name, s.host, 0, f"up on {s.name}\n", "") for s in srvs],
    )
    backend = FakeBackend(responses=[
        json.dumps({"steps": [{"servers": ["web-1", "web-2"], "command": "uptime"}]}),
        "두 노드 모두 정상.",
    ])
    outcome = agent.ask("현황", [server, server2], settings=settings, console=console, backend=backend)

    assert outcome.n_steps == 1
    rows = sqlite3.connect(settings.history_db).execute(
        "SELECT server, command FROM commands ORDER BY server"
    ).fetchall()
    assert rows == [("web-1", "uptime"), ("web-2", "uptime")]


def test_unknown_server_in_plan_records_error(monkeypatch, settings, server, console):
    monkeypatch.setattr(agent, "run", lambda srv, cmd: ExecResult(srv.name, srv.host, 0, "", ""))
    backend = FakeBackend(responses=[
        json.dumps({"steps": [{"server": "ghost", "command": "ls"}]}),
        "ghost 서버는 인벤토리에 없습니다.",
    ])
    outcome = agent.ask("테스트", [server], settings=settings, console=console, backend=backend)

    assert outcome.n_failed == 1
    assert "ghost" in backend.calls[1]


def test_plan_parser_handles_code_fence(settings, server, console):
    backend = FakeBackend(responses=[
        "여기 plan입니다:\n```json\n" + json.dumps({"steps": []}) + "\n```\n",
        "추가 정보가 필요합니다.",
    ])
    outcome = agent.ask("그냥 인사", [server], settings=settings, console=console, backend=backend)
    assert outcome.n_steps == 0
    assert outcome.final_text == "추가 정보가 필요합니다."


def test_empty_steps_skips_execution(settings, server, console):
    backend = FakeBackend(responses=[
        json.dumps({"steps": []}),
        "실행 없이 답변 가능: 안녕하세요.",
    ])
    outcome = agent.ask("hello", [server], settings=settings, console=console, backend=backend)
    assert outcome.n_steps == 0
    assert "안녕" in outcome.final_text


def test_invalid_json_raises(settings, server, console):
    backend = FakeBackend(responses=["this is not json at all"])
    with pytest.raises(RuntimeError, match="parseable JSON"):
        agent.ask("뭐든", [server], settings=settings, console=console, backend=backend)
