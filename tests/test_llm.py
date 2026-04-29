"""llm 백엔드 선택 + CLI 호출 동작 검증 (CLI 자체는 호출 안 함)."""
from __future__ import annotations

import pytest

from clopsctl import llm


def test_select_backend_unknown_name_raises(monkeypatch):
    monkeypatch.delenv("CLOPSCTL_LLM_BACKEND", raising=False)
    with pytest.raises(RuntimeError, match="unknown LLM backend"):
        llm.select_backend("totally-not-a-backend")


def test_select_backend_explicit_name_unavailable(monkeypatch):
    monkeypatch.delenv("CLOPSCTL_LLM_BACKEND", raising=False)
    monkeypatch.setattr(llm.shutil, "which", lambda binary: None)
    with pytest.raises(RuntimeError, match="not installed"):
        llm.select_backend("claude")


def test_select_backend_auto_detect_first_available(monkeypatch):
    monkeypatch.delenv("CLOPSCTL_LLM_BACKEND", raising=False)
    monkeypatch.setattr(llm.shutil, "which", lambda binary: f"/usr/bin/{binary}" if binary == "gemini" else None)
    backend = llm.select_backend()
    assert backend.name == "gemini"


def test_select_backend_no_cli_available(monkeypatch):
    monkeypatch.delenv("CLOPSCTL_LLM_BACKEND", raising=False)
    monkeypatch.setattr(llm.shutil, "which", lambda binary: None)
    with pytest.raises(RuntimeError, match="no LLM CLI found"):
        llm.select_backend()


def test_invoke_propagates_cli_failure(monkeypatch):
    monkeypatch.setattr(llm.shutil, "which", lambda b: f"/usr/bin/{b}")

    class FakeProc:
        returncode = 2
        stdout = ""
        stderr = "Error: invalid token"

    def fake_run(cmd, **kwargs):
        return FakeProc()
    monkeypatch.setattr(llm.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="exited 2"):
        llm.claude_backend().invoke("hi")


def test_invoke_returns_stdout_on_success(monkeypatch):
    monkeypatch.setattr(llm.shutil, "which", lambda b: f"/usr/bin/{b}")

    class FakeProc:
        returncode = 0
        stdout = "ok response\n"
        stderr = ""

    monkeypatch.setattr(llm.subprocess, "run", lambda cmd, **kw: FakeProc())
    out = llm.gemini_backend().invoke("ask")
    assert out == "ok response\n"


def test_list_backends_returns_three(monkeypatch):
    monkeypatch.setattr(llm.shutil, "which", lambda b: f"/usr/bin/{b}" if b in ("claude", "codex") else None)
    pairs = llm.list_backends()
    assert sorted(pairs) == [("claude", True), ("codex", True), ("gemini", False)]
