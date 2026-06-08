"""`clopsctl init` 마법사 + 공유 검증/env 작성 헬퍼 검증."""
from __future__ import annotations

import os

import pytest
from typer.testing import CliRunner

from clopsctl import cli
from clopsctl.cli import _extract_json, _json_to_form
from clopsctl.config import Server, load_inventory, validate_server_input, write_env

runner = CliRunner()


# --- _extract_json ---------------------------------------------------------

def test_extract_json_plain():
    assert _extract_json('{"name": "web-1", "host": "10.0.0.1"}') == {
        "name": "web-1",
        "host": "10.0.0.1",
    }


def test_extract_json_with_fence_and_prose():
    text = 'Sure! Here is the config:\n```json\n{"name": "db", "port": 2222}\n```\nDone.'
    assert _extract_json(text) == {"name": "db", "port": 2222}


def test_extract_json_embedded_in_prose():
    text = 'The result is {"user": "ops", "role": "shell"} for your server.'
    assert _extract_json(text) == {"user": "ops", "role": "shell"}


def test_extract_json_no_object_raises():
    with pytest.raises(ValueError, match="JSON object"):
        _extract_json("there is no json here")


# --- _json_to_form ---------------------------------------------------------

def test_json_to_form_normalizes_types():
    form = _json_to_form(
        {"name": "web-1", "port": 2200, "tags": ["prod", "web"], "role": "shell"}
    )
    assert form["name"] == "web-1"
    assert form["port"] == "2200"
    assert form["tags"] == "prod,web"
    assert form["role"] == "shell"


def test_json_to_form_omits_empty_and_none():
    form = _json_to_form({"name": "x", "pem_path": None, "jump": "  "})
    assert "pem_path" not in form
    assert "jump" not in form


# --- validate_server_input (config 로 이동했지만 web 과 동일 동작) -------------

def test_validate_server_input_happy_path():
    server, errors = validate_server_input(
        {"name": "web-1", "host": "10.0.0.1", "user": "ec2-user", "auth": "agent"},
        {},
        is_edit=False,
    )
    assert errors == []
    assert server is not None and server.name == "web-1"


def test_validate_server_input_jump_cycle_rejected():
    inv = {"a": Server(name="a", host="h", user="u", jump="b")}
    server, errors = validate_server_input(
        {"name": "b", "host": "h2", "user": "u", "jump": "a"}, inv, is_edit=False
    )
    assert server is None
    assert any("jump" in e for e in errors)


# --- write_env -------------------------------------------------------------

def test_write_env_creates_file_with_600(tmp_path):
    p = tmp_path / ".env"
    added = write_env(p, {"CLOPSCTL_LLM_BACKEND": "claude"})
    assert added == ["CLOPSCTL_LLM_BACKEND"]
    assert "CLOPSCTL_LLM_BACKEND=claude" in p.read_text()
    assert (os.stat(p).st_mode & 0o777) == 0o600


def test_write_env_append_only_preserves_existing(tmp_path):
    p = tmp_path / ".env"
    p.write_text("CLOPSCTL_LLM_BACKEND=gemini\n")
    added = write_env(p, {"CLOPSCTL_LLM_BACKEND": "claude", "MY_SECRET": "s3cr3t"})
    assert added == ["MY_SECRET"]  # 기존 키는 건드리지 않음
    body = p.read_text()
    assert "CLOPSCTL_LLM_BACKEND=gemini" in body  # 보존
    assert "CLOPSCTL_LLM_BACKEND=claude" not in body
    assert "MY_SECRET=s3cr3t" in body


# --- init CLI: manual 폴백 (AI CLI 미설치 시나리오) --------------------------

def test_init_manual_flow_writes_inventory(tmp_path):
    inv = tmp_path / "servers.toml"
    env = tmp_path / ".env"
    # name, host, user, port(엔터=22), auth(엔터=agent), role(엔터=read-only),
    # tags(엔터=빈칸), [jump 없음: 첫 서버라 known 없음],
    # confirm 추가(y), 더 추가(n), 최종 저장(y)
    answers = "\n".join(["web-1", "10.0.1.11", "ec2-user", "", "", "", "", "y", "n", "y"]) + "\n"
    result = runner.invoke(
        cli.app,
        ["init", "--backend", "manual", "--inventory", str(inv), "--env-file", str(env)],
        input=answers,
    )
    assert result.exit_code == 0, result.output
    saved = load_inventory(inv)
    assert "web-1" in saved
    assert saved["web-1"].host == "10.0.1.11"
    assert saved["web-1"].auth == "agent"


def test_init_no_servers_added_exits_clean(tmp_path):
    inv = tmp_path / "servers.toml"
    # 첫 서버 입력 후 추가 거부(n), 이후 더 추가 거부(n)
    answers = "\n".join(["web-1", "10.0.1.11", "ec2-user", "", "", "", "", "n", "n"]) + "\n"
    result = runner.invoke(
        cli.app,
        ["init", "--backend", "manual", "--inventory", str(inv)],
        input=answers,
    )
    assert result.exit_code == 0, result.output
    assert not inv.exists()  # 아무 것도 쓰지 않음


# --- init CLI: LLM 백엔드 경로 (가짜 백엔드 주입) ----------------------------

class _FakeBackend:
    name = "claude"

    def invoke(self, prompt: str, *, timeout: int = 120) -> str:
        return '{"name": "api-1", "host": "10.0.2.5", "user": "deploy", "auth": "agent", "role": "shell"}'


def test_init_llm_flow_parses_and_writes(tmp_path, monkeypatch):
    from clopsctl import llm

    monkeypatch.setattr(llm, "list_backends", lambda: [("claude", True), ("gemini", False)])
    monkeypatch.setattr(llm, "select_backend", lambda name=None: _FakeBackend())

    inv = tmp_path / "servers.toml"
    env = tmp_path / ".env"
    # desc(한 줄), confirm 추가(y), 더 추가(n), 최종 저장(y)
    answers = "\n".join(["api-1 은 10.0.2.5, deploy 사용자, shell", "y", "n", "y"]) + "\n"
    result = runner.invoke(
        cli.app,
        ["init", "--backend", "claude", "--inventory", str(inv), "--env-file", str(env)],
        input=answers,
    )
    assert result.exit_code == 0, result.output
    saved = load_inventory(inv)
    assert saved["api-1"].user == "deploy"
    assert saved["api-1"].role == "shell"
    # 선택된 백엔드가 .env 에 기록됨
    assert "CLOPSCTL_LLM_BACKEND=claude" in env.read_text()


def test_init_dry_run_writes_nothing(tmp_path, monkeypatch):
    from clopsctl import llm

    monkeypatch.setattr(llm, "list_backends", lambda: [("claude", True)])
    monkeypatch.setattr(llm, "select_backend", lambda name=None: _FakeBackend())

    inv = tmp_path / "servers.toml"
    answers = "\n".join(["api-1 은 10.0.2.5, deploy 사용자", "y", "n"]) + "\n"
    result = runner.invoke(
        cli.app,
        ["init", "--backend", "claude", "--inventory", str(inv), "--dry-run"],
        input=answers,
    )
    assert result.exit_code == 0, result.output
    assert "dry-run" in result.output
    assert not inv.exists()
