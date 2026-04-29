"""LLM 백엔드 — 로컬에 설치된 claude / gemini / codex CLI 활용.

ANTHROPIC_API_KEY 같은 직접 SDK 호출 대신, 사용자 환경에 이미 인증된
CLI를 subprocess 로 호출. 모든 백엔드는 동일한 텍스트 in/out 인터페이스.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Protocol


class LLMBackend(Protocol):
    name: str

    def is_available(self) -> bool: ...
    def invoke(self, prompt: str, *, timeout: int = 120) -> str: ...


@dataclass(slots=True)
class CLIBackend:
    """CLI 호출 공통 구현."""

    name: str
    binary: str
    args_factory: object  # Callable[[], list[str]] but Python forward-ref noise

    def is_available(self) -> bool:
        return shutil.which(self.binary) is not None

    def invoke(self, prompt: str, *, timeout: int = 120) -> str:
        if not self.is_available():
            raise RuntimeError(f"{self.name} CLI ({self.binary}) is not installed or not in PATH")
        cmd = [self.binary, *self.args_factory()]  # type: ignore[operator]
        try:
            proc = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:  # noqa: PERF203
            raise RuntimeError(f"{self.name} CLI timed out after {timeout}s") from exc

        if proc.returncode != 0:
            # 일부 CLI 는 인증 실패 등을 stdout 에 출력 → 둘 다 노출해야 디버깅 가능
            stderr = (proc.stderr or "").strip()
            stdout = (proc.stdout or "").strip()
            detail = stderr or stdout or "(empty output)"
            raise RuntimeError(
                f"{self.name} CLI exited {proc.returncode}: {detail[:500]}"
            )
        return proc.stdout


def claude_backend() -> CLIBackend:
    # `claude --print` 는 stdin 으로 prompt 받음. OAuth/keychain 인증 호환을 위해
    # --bare 는 사용하지 않음 (--bare 는 ANTHROPIC_API_KEY 만 허용해서 OAuth 로그인
    # 한 환경에서는 'Not logged in' 으로 즉시 종료됨).
    return CLIBackend(
        name="claude",
        binary="claude",
        args_factory=lambda: ["--print"],
    )


def gemini_backend() -> CLIBackend:
    # gemini -p '' 로 stdin 모드 강제. -p 빈 문자열 + stdin 입력 형태.
    return CLIBackend(
        name="gemini",
        binary="gemini",
        args_factory=lambda: ["-p", ""],
    )


def codex_backend() -> CLIBackend:
    # `codex exec` 는 prompt 를 인자로 받는 형태가 일반적이지만, 큰 prompt 안전하게
    # 보내기 위해 `--` 또는 stdin 사용. 여기서는 stdin 우선.
    return CLIBackend(
        name="codex",
        binary="codex",
        args_factory=lambda: ["exec", "-"],
    )


_BACKENDS: dict[str, "callable"] = {
    "claude": claude_backend,
    "gemini": gemini_backend,
    "codex": codex_backend,
}


def select_backend(name: str | None = None) -> CLIBackend:
    """이름이 주어지면 그 백엔드. 아니면 환경변수 `CLOPSCTL_LLM_BACKEND`,
    그것도 없으면 PATH 에서 사용 가능한 첫 백엔드 (claude → gemini → codex 순)."""
    chosen = name or os.getenv("CLOPSCTL_LLM_BACKEND")
    if chosen:
        if chosen not in _BACKENDS:
            raise RuntimeError(f"unknown LLM backend '{chosen}' (allowed: {', '.join(_BACKENDS)})")
        backend = _BACKENDS[chosen]()
        if not backend.is_available():
            raise RuntimeError(f"{chosen} CLI is not installed or not in PATH")
        return backend

    for n in ("claude", "gemini", "codex"):
        backend = _BACKENDS[n]()
        if backend.is_available():
            return backend
    raise RuntimeError(
        "no LLM CLI found in PATH (install one of: claude, gemini, codex)"
    )


def list_backends() -> list[tuple[str, bool]]:
    """진단/디버깅용 — 각 백엔드의 가용성 상태."""
    return [(n, factory().is_available()) for n, factory in _BACKENDS.items()]
