"""per-server 권한 모드 통합 테스트.

- read-only 와 sudo 가 섞인 fan-out 에서 strict 모드는 모두 차단,
  per_server 모드는 sudo 만 통과시키는지 검증.
"""
from __future__ import annotations

import dataclasses
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
    name: str = "fake"
    responses: list[str] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)

    def is_available(self):
        return True

    def invoke(self, prompt, *, timeout=120):
        self.calls.append(prompt)
        if not self.responses:
            raise AssertionError("FakeBackend response queue exhausted")
        return self.responses.pop(0)


@pytest.fixture
def base_settings(tmp_path: Path) -> Settings:
    return Settings(
        inventory_path=tmp_path / "servers.toml",
        history_db=tmp_path / "history.sqlite",
        model="claude-opus-4-7",
        safety_confirm=True,
        web_host="127.0.0.1",
        web_port=8765,
        llm_backend="fake",
        permission_mode="strict",
    )


@pytest.fixture
def mixed_servers() -> list[Server]:
    return [
        Server(name="ro-1", host="10.0.0.1", user="ec2", role="read-only"),
        Server(name="ro-2", host="10.0.0.2", user="ec2", role="read-only"),
        Server(name="su-1", host="10.0.0.3", user="root", role="sudo"),
    ]


@pytest.fixture
def console():
    return Console(quiet=True)


def test_strict_mode_blocks_all_when_any_read_only(monkeypatch, base_settings, mixed_servers, console):
    """기본(strict): read-only 가 하나라도 있으면 mutating 명령은 전부 차단."""
    monkeypatch.setattr(
        agent, "fan_out",
        lambda srvs, cmd: (_ for _ in ()).throw(AssertionError("must not run in strict")),
    )
    monkeypatch.setattr(
        agent, "run",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not run in strict")),
    )
    backend = FakeBackend(responses=[
        json.dumps({"steps": [{"servers": ["ro-1", "ro-2", "su-1"], "command": "systemctl restart nginx"}]}),
        "권한 거부로 실행되지 않음.",
    ])
    outcome = agent.ask("재시작", mixed_servers, settings=base_settings, console=console, backend=backend)

    # strict 모드: step 단위로 1번 차단 카운트, 단 history 에는 서버별 3건 기록
    assert outcome.n_blocked == 1
    rows = sqlite3.connect(base_settings.history_db).execute(
        "SELECT server, stderr FROM commands ORDER BY server"
    ).fetchall()
    servers_blocked = {r[0] for r in rows}
    assert servers_blocked == {"ro-1", "ro-2", "su-1"}
    assert all("permission denied" in (r[1] or "") for r in rows)


def test_per_server_mode_partial_pass_through(monkeypatch, base_settings, mixed_servers, console):
    """per_server 모드: read-only 두 대 차단, sudo 한 대만 fan_out 으로 실행."""
    settings = dataclasses.replace(base_settings, permission_mode="per_server")
    captured_targets: list[str] = []

    def fake_fan_out(srvs, cmd):
        captured_targets.extend(s.name for s in srvs)
        return [ExecResult(s.name, s.host, 0, "restarted\n", "") for s in srvs]

    def fake_run(srv, cmd):
        captured_targets.append(srv.name)
        return ExecResult(srv.name, srv.host, 0, "restarted\n", "")

    monkeypatch.setattr(agent, "fan_out", fake_fan_out)
    monkeypatch.setattr(agent, "run", fake_run)

    backend = FakeBackend(responses=[
        json.dumps({"steps": [{"servers": ["ro-1", "ro-2", "su-1"], "command": "systemctl restart nginx"}]}),
        "su-1 만 재시작됨, ro-* 는 권한 거부.",
    ])
    outcome = agent.ask("재시작", mixed_servers, settings=settings, console=console, backend=backend)

    assert captured_targets == ["su-1"], "su-1 만 실행됐어야"
    assert outcome.n_blocked == 2  # ro-1, ro-2

    rows = sqlite3.connect(settings.history_db).execute(
        "SELECT server, exit_code, stderr FROM commands ORDER BY server"
    ).fetchall()
    rows_by_server = {r[0]: (r[1], r[2]) for r in rows}
    assert rows_by_server["ro-1"][0] is None and "permission denied" in (rows_by_server["ro-1"][1] or "")
    assert rows_by_server["ro-2"][0] is None and "permission denied" in (rows_by_server["ro-2"][1] or "")
    assert rows_by_server["su-1"][0] == 0 and rows_by_server["su-1"][1] == ""


def test_per_server_all_blocked_skips_step(monkeypatch, base_settings, console):
    """per_server: 모두 read-only인데 mutating 명령이면 실행 0건, 모두 차단 기록."""
    settings = dataclasses.replace(base_settings, permission_mode="per_server")
    ro_servers = [
        Server(name="r-1", host="10.0.0.1", user="ec2", role="read-only"),
        Server(name="r-2", host="10.0.0.2", user="ec2", role="read-only"),
    ]
    monkeypatch.setattr(
        agent, "fan_out",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not run when all blocked")),
    )
    monkeypatch.setattr(
        agent, "run",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not run when all blocked")),
    )
    backend = FakeBackend(responses=[
        json.dumps({"steps": [{"servers": ["r-1", "r-2"], "command": "rm /tmp/foo"}]}),
        "둘 다 차단.",
    ])
    outcome = agent.ask("정리", ro_servers, settings=settings, console=console, backend=backend)
    assert outcome.n_blocked == 2


def test_per_server_read_only_command_passes_all(monkeypatch, base_settings, mixed_servers, console):
    """per_server: read-only 명령은 모든 role 에서 통과 — strict 와 동일 결과."""
    settings = dataclasses.replace(base_settings, permission_mode="per_server")
    monkeypatch.setattr(
        agent, "fan_out",
        lambda srvs, cmd: [ExecResult(s.name, s.host, 0, "ok\n", "") for s in srvs],
    )
    backend = FakeBackend(responses=[
        json.dumps({"steps": [{"servers": ["ro-1", "ro-2", "su-1"], "command": "df -h"}]}),
        "정상.",
    ])
    outcome = agent.ask("디스크", mixed_servers, settings=settings, console=console, backend=backend)
    assert outcome.n_blocked == 0
    rows = sqlite3.connect(settings.history_db).execute(
        "SELECT COUNT(*) FROM commands WHERE exit_code = 0"
    ).fetchone()
    assert rows[0] == 3
