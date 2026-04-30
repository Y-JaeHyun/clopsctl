"""LLM 에이전트 — 로컬 CLI 백엔드(claude/gemini/codex) 기반 Plan→Execute→Summarize.

Anthropic SDK 같은 양방향 tool_use 루프 대신, CLI 백엔드가 모두 동등하게
지원하는 단순 텍스트 in/out 모델을 사용:

  1. Plan:    LLM 에 인벤토리 + 사용자 질문을 주고 실행할 SSH 명령(JSON) 생성
  2. Execute: 우리가 safety 게이트 + paramiko fan-out 으로 실제 실행
  3. Summarize: 실행 결과를 LLM 에 다시 보내 사용자 질문에 한국어 답변

이 분리 덕에 모든 CLI 백엔드(claude/gemini/codex)가 동일하게 동작.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from typing import Callable

from rich.console import Console

from .config import Server, Settings
from .history import record
from .llm import LLMBackend
from .permissions import is_allowed_for_role, strictest_role
from .safety import is_dangerous
from .ssh import ExecResult, fan_out, run

MAX_STEPS = 12  # 한 번의 ask 에서 실행할 최대 SSH step 수

PLAN_INSTRUCTION = """당신은 clopsctl, SSH 운영 보조 에이전트입니다.

인벤토리 ({n_servers}대):
{inventory}
{history}
사용자 요청: {prompt}

요청에 답하기 위해 어떤 SSH 명령을 어느 서버에서 실행해야 하는지 결정하세요.

규칙:
1. 인벤토리에 없는 서버 이름은 사용하지 않습니다.
2. 읽기 전용 명령을 우선합니다 (df, free, ps, journalctl, ls, cat, grep, uptime 등).
3. 위험 명령(rm -rf /, shutdown, reboot, mkfs, dd ... of=/dev/...) 금지.
4. 같은 명령을 여러 서버에 보내야 하면 servers 배열로 한 번에(fan-out) 표현.
5. 명령 실행 없이 답변 가능하면 빈 배열로 답하세요.

다음 JSON 한 개만 출력하세요. 코드 블록(```)도 금지. 다른 어떤 설명/머리말/꼬리말도 금지:

{{"steps": [
  {{"server": "web-1", "command": "df -h"}},
  {{"servers": ["web-1", "web-2"], "command": "uptime"}}
]}}
"""

SUMMARIZE_INSTRUCTION = """당신은 clopsctl 입니다. 다음 SSH 실행 결과를 바탕으로 사용자 질문에 한국어로 간결하게 답하세요.
{history}
사용자 질문: {prompt}

실행 결과 (JSON):
{results}

답변 규칙:
- 한국어, 간결하게.
- 서버는 인벤토리 이름으로 지칭.
- 수치 비교가 필요하면 작은 표를 사용.
- 결과가 비어있거나 에러뿐이면 어떤 한계가 있었는지 정직하게 보고.
"""


@dataclass(slots=True)
class AskOutcome:
    final_text: str
    backend_name: str
    n_steps: int
    n_blocked: int
    n_failed: int


def _format_inventory(servers: list[Server]) -> str:
    lines = []
    for s in servers:
        tags = ", ".join(s.tags) if s.tags else "-"
        lines.append(f"- {s.name} (host={s.host}, user={s.user}, role={s.role}, tags={tags})")
    return "\n".join(lines)


def _format_history(prior_turns: list[dict[str, Any]] | None) -> str:
    """이전 대화 turn 들을 LLM 프롬프트용 텍스트로 직렬화."""
    if not prior_turns:
        return ""
    lines = ["", "이전 대화 (참고용):"]
    for i, t in enumerate(prior_turns, 1):
        prompt = (t.get("prompt") or "").strip()
        answer = (t.get("final_text") or "").strip()
        lines.append(f"[{i}] 질문: {prompt}")
        if answer:
            lines.append(f"    답변: {answer[:300]}{'…' if len(answer) > 300 else ''}")
    lines.append("")
    return "\n".join(lines)


_JSON_BLOCK = re.compile(r"\{[\s\S]*\}")


def _parse_plan(text: str) -> list[dict[str, Any]]:
    """LLM 응답에서 JSON 추출. 코드 펜스/주변 설명 허용."""
    cleaned = text.strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        m = _JSON_BLOCK.search(cleaned)
        if not m:
            raise RuntimeError(f"LLM did not return parseable JSON. raw: {cleaned[:300]}")
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"LLM JSON malformed: {exc}. raw: {cleaned[:300]}") from exc

    steps = data.get("steps", [])
    if not isinstance(steps, list):
        raise RuntimeError(f"plan.steps is not a list: {type(steps).__name__}")
    return steps[:MAX_STEPS]


def _serialize_result(r: ExecResult) -> dict[str, Any]:
    return {
        "server": r.server,
        "host": r.host,
        "exit_code": r.exit_code,
        "stdout": (r.stdout or "")[-3000:],
        "stderr": (r.stderr or "")[-1000:],
        "error": r.error,
    }


def _record_block(db_path, server: str, command: str, prompt: str, reason: str) -> None:
    record(
        db_path,
        server=server, mode="ask", command=command, prompt=prompt,
        exit_code=None, stderr=reason,
    )


EventCallback = Callable[[dict[str, Any]], None]


def _emit(cb: EventCallback | None, event_type: str, **data: Any) -> None:
    if cb is None:
        return
    cb({"type": event_type, **data})


def _execute_plan(
    steps: list[dict[str, Any]],
    *,
    inventory: dict[str, Server],
    settings: Settings,
    prompt: str,
    console: Console,
    dry_run: bool = False,
    on_event: EventCallback | None = None,
) -> tuple[list[dict[str, Any]], int, int]:
    results: list[dict[str, Any]] = []
    n_blocked = 0
    n_failed = 0
    permission_mode = (settings.permission_mode or "strict").lower()

    for idx, step in enumerate(steps):
        command = (step.get("command") or "").strip()
        if not command:
            results.append({"server": "?", "error": "empty command in plan", "skipped": True})
            continue

        # 1) safety 게이트 (전체 공통)
        flagged = is_dangerous(command)
        if flagged:
            n_blocked += 1
            console.print(f"[yellow]✗ safety[/yellow] {command!r} (pattern: {flagged})")
            target_names = step.get("servers") or ([step["server"]] if step.get("server") else [])
            for n in target_names:
                _record_block(settings.history_db, n, command, prompt, f"safety gate blocked: {flagged}")
                results.append({"server": n, "blocked": "safety", "pattern": flagged, "command": command})
            _emit(on_event, "step_blocked", step=idx, reason="safety", pattern=flagged, command=command, servers=target_names)
            continue

        # 2) 대상 서버 해석
        if "servers" in step:
            names = step["servers"] or []
        elif step.get("server"):
            names = [step["server"]]
        else:
            results.append({"error": "step has neither 'server' nor 'servers'", "command": command})
            n_failed += 1
            _emit(on_event, "step_failed", step=idx, reason="malformed step", command=command)
            continue

        unknown = [n for n in names if n not in inventory]
        if unknown:
            results.append({"error": f"unknown servers: {unknown}", "command": command})
            n_failed += 1
            _emit(on_event, "step_failed", step=idx, reason=f"unknown servers: {unknown}", command=command)
            continue
        target_servers = [inventory[n] for n in names]

        # 3) 권한 게이트
        if permission_mode == "per_server":
            # 서버별 개별 검사 — 통과한 서버만 실행, 차단된 서버는 별도 결과로 기록
            passing: list[Server] = []
            for srv in target_servers:
                reason = is_allowed_for_role(command, srv.role)
                if reason is None:
                    passing.append(srv)
                else:
                    n_blocked += 1
                    console.print(f"[yellow]✗ permission[/yellow] {srv.name} ({reason})")
                    _record_block(settings.history_db, srv.name, command, prompt, f"permission denied: {reason}")
                    results.append({"server": srv.name, "blocked": "permission", "reason": reason, "command": command})
                    _emit(on_event, "step_blocked", step=idx, reason="permission",
                          detail=reason, command=command, servers=[srv.name])
            if not passing:
                continue  # 모두 차단됨 — 다음 step
            target_servers = passing
            names = [s.name for s in passing]
            role = strictest_role(target_servers)
        else:
            # strict 모드: 가장 엄격한 role 기준으로 전부 차단 또는 전부 통과
            role = strictest_role(target_servers)
            perm_reason = is_allowed_for_role(command, role)
            if perm_reason:
                n_blocked += 1
                console.print(f"[yellow]✗ permission[/yellow] {command!r} ({perm_reason})")
                for n in names:
                    _record_block(settings.history_db, n, command, prompt, f"permission denied: {perm_reason}")
                    results.append({"server": n, "blocked": "permission", "reason": perm_reason, "command": command})
                _emit(on_event, "step_blocked", step=idx, reason="permission",
                      detail=perm_reason, command=command, servers=names)
                continue

        # 4) dry-run 이면 실행 대신 plan 만 기록
        if dry_run:
            console.print(f"[cyan]∘ dry-run[/cyan] {names} :: {command}")
            for n in names:
                results.append({"server": n, "dry_run": True, "command": command})
            _emit(on_event, "step_dry_run", step=idx, command=command, servers=names)
            continue

        # 5) 실제 실행
        _emit(on_event, "step_start", step=idx, command=command, servers=names, role=role)
        if len(target_servers) > 1:
            console.print(f"[dim]→ fan_out {names} :: {command}[/dim]")
            execs = fan_out(target_servers, command, inventory=inventory)
        else:
            console.print(f"[dim]→ exec {names[0]} :: {command}[/dim]")
            from .ssh import _INVENTORY  # type: ignore[attr-defined]
            token = _INVENTORY.set(inventory)
            try:
                execs = [run(target_servers[0], command)]
            finally:
                _INVENTORY.reset(token)
        for r in execs:
            record(
                settings.history_db,
                server=r.server, mode="ask", command=command, prompt=prompt,
                exit_code=r.exit_code, stdout=r.stdout, stderr=r.stderr or (r.error or ""),
                llm_model=settings.model,
            )
            results.append(_serialize_result(r))
            if r.exit_code != 0 and r.error:
                n_failed += 1
            _emit(
                on_event, "step_result",
                step=idx, server=r.server, exit_code=r.exit_code,
                stdout_preview=(r.stdout or "")[:400],
                stderr_preview=(r.stderr or "")[:200],
                error=r.error,
            )

    return results, n_blocked, n_failed


def ask(
    prompt: str,
    targets: list[Server],
    *,
    settings: Settings,
    console: Console,
    backend: LLMBackend,
    dry_run: bool = False,
    on_event: EventCallback | None = None,
    prior_turns: list[dict[str, Any]] | None = None,
) -> AskOutcome:
    """on_event 가 주어지면 phase 별 진행 이벤트를 푸시 (web SSE 등에서 활용).

    prior_turns 가 있으면 이전 대화 (질문/답변) 를 LLM 프롬프트에 포함해 follow-up 처리.
    """
    inventory = {s.name: s for s in targets}
    history_text = _format_history(prior_turns)

    _emit(on_event, "started", backend=backend.name, dry_run=dry_run, servers=[s.name for s in targets])

    plan_prompt = PLAN_INSTRUCTION.format(
        n_servers=len(targets),
        inventory=_format_inventory(targets),
        prompt=prompt,
        history=history_text,
    )
    console.print(f"[dim]ask via {backend.name} CLI — planning…[/dim]")
    _emit(on_event, "plan_start")
    plan_text = backend.invoke(plan_prompt)
    steps = _parse_plan(plan_text)
    _emit(on_event, "plan_done", n_steps=len(steps), steps=steps)

    if not steps:
        console.print("[dim](no SSH commands proposed — summarizing directly)[/dim]")

    if dry_run:
        results, n_blocked, n_failed = _execute_plan(
            steps, inventory=inventory, settings=settings, prompt=prompt,
            console=console, dry_run=True, on_event=on_event,
        )
        plan_summary = "\n".join(
            f"- {s.get('servers', [s.get('server')])} :: {s.get('command')}" for s in steps
        ) or "(plan empty)"
        outcome = AskOutcome(
            final_text=f"[DRY-RUN] 실행하지 않았습니다. 제안된 plan:\n{plan_summary}",
            backend_name=backend.name,
            n_steps=len(steps),
            n_blocked=n_blocked,
            n_failed=n_failed,
        )
        _emit(on_event, "done", final_text=outcome.final_text, n_steps=outcome.n_steps,
              n_blocked=outcome.n_blocked, n_failed=outcome.n_failed)
        return outcome

    results, n_blocked, n_failed = _execute_plan(
        steps, inventory=inventory, settings=settings, prompt=prompt,
        console=console, on_event=on_event,
    )

    summary_prompt = SUMMARIZE_INSTRUCTION.format(
        prompt=prompt,
        results=json.dumps(results, ensure_ascii=False, indent=2),
        history=history_text,
    )
    console.print(f"[dim]summarizing via {backend.name} CLI…[/dim]")
    _emit(on_event, "summarize_start")
    final_text = backend.invoke(summary_prompt).strip()
    _emit(on_event, "summarize_done")

    outcome = AskOutcome(
        final_text=final_text,
        backend_name=backend.name,
        n_steps=len(steps),
        n_blocked=n_blocked,
        n_failed=n_failed,
    )
    _emit(on_event, "done", final_text=outcome.final_text, n_steps=outcome.n_steps,
          n_blocked=outcome.n_blocked, n_failed=outcome.n_failed)
    return outcome
