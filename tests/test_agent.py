"""agent.ask 의 도구 디스패치 / safety 게이트 / history 기록을 mocked client 로 검증."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from clopsctl import agent
from clopsctl.config import Server, Settings


# --- 가짜 Anthropic 응답 객체들 ---------------------------------------------------

@dataclass
class FakeBlock:
    type: str
    text: str | None = None
    id: str | None = None
    name: str | None = None
    input: dict[str, Any] | None = None


@dataclass
class FakeUsage:
    input_tokens: int = 100
    output_tokens: int = 50
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


@dataclass
class FakeResponse:
    content: list[FakeBlock]
    stop_reason: str
    usage: FakeUsage = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.usage is None:
            self.usage = FakeUsage()


class FakeMessages:
    def __init__(self, scripted: list[FakeResponse]):
        self._scripted = list(scripted)
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> FakeResponse:
        # snapshot messages at call time (list reference would mutate later)
        snapshot = dict(kwargs)
        snapshot["messages"] = [
            {"role": m["role"], "content": list(m["content"]) if isinstance(m["content"], list) else m["content"]}
            for m in kwargs["messages"]
        ]
        self.calls.append(snapshot)
        return self._scripted.pop(0)


class FakeClient:
    def __init__(self, scripted: list[FakeResponse]):
        self.messages = FakeMessages(scripted)


# --- 픽스처 ---------------------------------------------------------------------

@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        inventory_path=tmp_path / "servers.toml",
        history_db=tmp_path / "history.sqlite",
        model="claude-opus-4-7",
        safety_confirm=True,
        web_host="127.0.0.1",
        web_port=8765,
        anthropic_api_key="test-key",
    )


@pytest.fixture
def server() -> Server:
    return Server(name="web-1", host="10.0.0.1", user="ec2", auth="agent", role="read-only")


@pytest.fixture
def console():
    from rich.console import Console
    return Console(quiet=True)


# --- 테스트 -----------------------------------------------------------------------

def test_safety_gate_blocks_destructive_command(monkeypatch, settings, server, console) -> None:
    # 가짜 LLM: 위험한 명령을 호출 → tool_result 받은 후 다른 응답으로 마무리
    scripted = [
        FakeResponse(
            content=[
                FakeBlock(
                    type="tool_use",
                    id="t1",
                    name="ssh_exec",
                    input={"server": "web-1", "command": "rm -rf /"},
                )
            ],
            stop_reason="tool_use",
        ),
        FakeResponse(
            content=[FakeBlock(type="text", text="위험 명령이라 차단됨. 다른 방식 제안.")],
            stop_reason="end_turn",
        ),
    ]
    client = FakeClient(scripted)

    # 실제 paramiko 호출되면 안 됨 — 호출되면 테스트 실패
    def _no_run(*a, **kw):
        raise AssertionError("ssh.run must not be called when safety gate blocks")
    monkeypatch.setattr(agent, "run", _no_run)

    outcome = agent.ask("청소해줘", [server], settings=settings, console=console, client=client)  # type: ignore[arg-type]

    assert "차단" in outcome.final_text or "위험" in outcome.final_text
    assert outcome.iterations == 2
    # safety 차단도 history 에 기록되었는지
    rows = sqlite3.connect(settings.history_db).execute(
        "SELECT stderr FROM commands WHERE server='web-1'"
    ).fetchall()
    assert any("safety gate blocked" in (r[0] or "") for r in rows)


def test_ssh_exec_dispatch_records_history(monkeypatch, settings, server, console) -> None:
    from clopsctl.ssh import ExecResult

    monkeypatch.setattr(
        agent,
        "run",
        lambda srv, cmd: ExecResult(srv.name, srv.host, 0, "/var/log/syslog\n", ""),
    )

    scripted = [
        FakeResponse(
            content=[
                FakeBlock(
                    type="tool_use",
                    id="t1",
                    name="ssh_exec",
                    input={"server": "web-1", "command": "ls /var/log"},
                )
            ],
            stop_reason="tool_use",
        ),
        FakeResponse(
            content=[FakeBlock(type="text", text="syslog 만 있습니다.")],
            stop_reason="end_turn",
        ),
    ]
    client = FakeClient(scripted)

    outcome = agent.ask("로그 디렉토리 확인", [server], settings=settings, console=console, client=client)  # type: ignore[arg-type]

    assert outcome.final_text == "syslog 만 있습니다."
    rows = sqlite3.connect(settings.history_db).execute(
        "SELECT mode, command, exit_code, stdout, llm_model FROM commands"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0] == ("ask", "ls /var/log", 0, "/var/log/syslog\n", "claude-opus-4-7")


def test_unknown_server_returns_error_to_llm(monkeypatch, settings, server, console) -> None:
    captured: dict[str, Any] = {}

    scripted = [
        FakeResponse(
            content=[
                FakeBlock(
                    type="tool_use",
                    id="t1",
                    name="ssh_exec",
                    input={"server": "ghost", "command": "ls"},
                )
            ],
            stop_reason="tool_use",
        ),
        FakeResponse(
            content=[FakeBlock(type="text", text="해당 서버가 인벤토리에 없습니다.")],
            stop_reason="end_turn",
        ),
    ]
    client = FakeClient(scripted)
    outcome = agent.ask("뭐든", [server], settings=settings, console=console, client=client)  # type: ignore[arg-type]

    # 두 번째 호출 시 messages 에 tool_result(is_error=True) 가 들어갔는지
    second_call = client.messages.calls[1]
    last_user = second_call["messages"][-1]
    assert last_user["role"] == "user"
    tool_result = last_user["content"][0]
    assert tool_result["is_error"] is True
    assert "unknown server" in tool_result["content"]
    assert outcome.iterations == 2


def test_max_iterations_breaks_safely(monkeypatch, settings, server, console) -> None:
    # 무한히 tool_use 만 반환하는 LLM 시뮬레이션
    from clopsctl.ssh import ExecResult
    monkeypatch.setattr(agent, "run", lambda srv, cmd: ExecResult(srv.name, srv.host, 0, "ok", ""))

    scripted = [
        FakeResponse(
            content=[
                FakeBlock(type="tool_use", id=f"t{i}", name="ssh_exec",
                          input={"server": "web-1", "command": f"echo {i}"})
            ],
            stop_reason="tool_use",
        )
        for i in range(agent.MAX_ITERATIONS + 5)
    ]
    client = FakeClient(scripted)

    outcome = agent.ask("loop", [server], settings=settings, console=console, client=client)  # type: ignore[arg-type]
    assert outcome.iterations == agent.MAX_ITERATIONS
