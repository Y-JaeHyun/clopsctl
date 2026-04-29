"""LLM 에이전트 — Anthropic SDK + tool_use 루프 + 우리 SSH 통합.

흐름:
  1. 사용자: clopsctl ask web-1,web-2 "디스크 80% 초과 경로 찾아줘"
  2. 시스템 프롬프트에 대상 서버 인벤토리(role/태그 포함) 주입
  3. ssh_exec / ssh_fan_out 도구 노출
  4. 도구 호출마다 safety regex 게이트 → paramiko 실행 → SQLite history append
  5. stop_reason == 'end_turn' 까지 반복, 마지막 텍스트 + 사용 토큰 표시
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

import anthropic
from rich.console import Console

from .config import Server, Settings
from .history import record
from .safety import is_dangerous
from .ssh import ExecResult, fan_out, run

MAX_ITERATIONS = 12  # 무한 루프 방지

TOOLS: list[dict[str, Any]] = [
    {
        "name": "ssh_exec",
        "description": (
            "Run a shell command on a single SSH server from the inventory. "
            "Returns stdout, stderr, and exit_code. Use this for targeted, single-host work."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "server": {
                    "type": "string",
                    "description": "Server name as it appears in the inventory (e.g. 'web-1').",
                },
                "command": {
                    "type": "string",
                    "description": "Shell command to execute on the remote host.",
                },
            },
            "required": ["server", "command"],
        },
    },
    {
        "name": "ssh_fan_out",
        "description": (
            "Run the same shell command on multiple SSH servers in parallel. "
            "Returns one result per server. Prefer this when you need to compare or aggregate "
            "across hosts (e.g. 'check disk usage on all web nodes')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "servers": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of server names from the inventory.",
                },
                "command": {
                    "type": "string",
                    "description": "Shell command to execute on every selected host.",
                },
            },
            "required": ["servers", "command"],
        },
    },
]


def _system_prompt(servers: list[Server]) -> str:
    inventory_lines = []
    for s in servers:
        tags = ", ".join(s.tags) if s.tags else "-"
        inventory_lines.append(f"- {s.name} (host={s.host}, user={s.user}, role={s.role}, tags={tags})")
    inventory = "\n".join(inventory_lines)

    return (
        "You are clopsctl, an SSH ops assistant. You operate on the user's behalf via two tools "
        "(`ssh_exec`, `ssh_fan_out`) that run shell commands over SSH on pre-registered servers.\n\n"
        "Rules:\n"
        "1. Only use server names that appear in the inventory below.\n"
        "2. Prefer read-only commands (df, free, ps, journalctl, ls, cat, grep) unless the user "
        "explicitly asks for a state change.\n"
        "3. Never run destructive commands (rm -rf /, shutdown, reboot, mkfs, dd to /dev/...). "
        "The host-side safety gate will reject them and return is_error=true.\n"
        "4. When fan-out makes sense (comparing/aggregating across hosts), use `ssh_fan_out` "
        "in a single call rather than many sequential `ssh_exec` calls.\n"
        "5. If a command fails, read the stderr and decide: try a different approach, or report the "
        "limitation. Do not loop on the same failing command.\n"
        "6. Final answer: respond in Korean. Summarize findings concisely. Reference servers by "
        "their inventory name. If numeric data is involved, present it as a small table.\n\n"
        f"Inventory ({len(servers)} server{'s' if len(servers) != 1 else ''}):\n{inventory}\n"
    )


@dataclass(slots=True)
class AskOutcome:
    final_text: str
    iterations: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int


def _format_exec_result(r: ExecResult) -> str:
    if r.error:
        return json.dumps(
            {"server": r.server, "host": r.host, "error": r.error, "exit_code": r.exit_code},
            ensure_ascii=False,
        )
    return json.dumps(
        {
            "server": r.server,
            "host": r.host,
            "exit_code": r.exit_code,
            "stdout": r.stdout[-4000:],  # 큰 출력 절단 (LLM 컨텍스트 보호)
            "stderr": r.stderr[-2000:],
        },
        ensure_ascii=False,
    )


def _execute_tool(
    tool_name: str,
    tool_input: dict[str, Any],
    *,
    inventory: dict[str, Server],
    settings: Settings,
    prompt: str,
) -> tuple[str, bool]:
    """도구 호출 1건 실행. (tool_result_content, is_error) 반환."""

    def _resolve(name: str) -> Server | None:
        return inventory.get(name)

    if tool_name == "ssh_exec":
        server_name = tool_input.get("server", "")
        command = tool_input.get("command", "")
        srv = _resolve(server_name)
        if srv is None:
            return f"unknown server '{server_name}' (not in inventory)", True

        flagged = is_dangerous(command)
        if flagged:
            record(
                settings.history_db,
                server=srv.name,
                mode="ask",
                command=command,
                prompt=prompt,
                exit_code=None,
                stderr=f"safety gate blocked: {flagged}",
            )
            return f"blocked by safety gate (pattern: {flagged}). Try a non-destructive alternative.", True

        result = run(srv, command)
        record(
            settings.history_db,
            server=srv.name,
            mode="ask",
            command=command,
            prompt=prompt,
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr or (result.error or ""),
            llm_model=settings.model,
        )
        return _format_exec_result(result), result.exit_code != 0 and bool(result.error)

    if tool_name == "ssh_fan_out":
        names: list[str] = tool_input.get("servers", []) or []
        command = tool_input.get("command", "")
        unknown = [n for n in names if n not in inventory]
        if unknown:
            return f"unknown servers: {', '.join(unknown)}", True
        if not names:
            return "servers list is empty", True

        flagged = is_dangerous(command)
        if flagged:
            for n in names:
                record(
                    settings.history_db,
                    server=n,
                    mode="ask",
                    command=command,
                    prompt=prompt,
                    exit_code=None,
                    stderr=f"safety gate blocked: {flagged}",
                )
            return f"blocked by safety gate (pattern: {flagged}). Try a non-destructive alternative.", True

        targets = [inventory[n] for n in names]
        results = fan_out(targets, command)
        for r in results:
            record(
                settings.history_db,
                server=r.server,
                mode="ask",
                command=command,
                prompt=prompt,
                exit_code=r.exit_code,
                stdout=r.stdout,
                stderr=r.stderr or (r.error or ""),
                llm_model=settings.model,
            )
        payload = json.dumps([json.loads(_format_exec_result(r)) for r in results], ensure_ascii=False)
        any_error = any(r.exit_code != 0 and bool(r.error) for r in results)
        return payload, any_error

    return f"unknown tool: {tool_name}", True


def ask(
    prompt: str,
    targets: list[Server],
    *,
    settings: Settings,
    console: Console,
    client: anthropic.Anthropic | None = None,
) -> AskOutcome:
    if not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set in .env")

    inventory = {s.name: s for s in targets}
    client = client or anthropic.Anthropic(api_key=settings.anthropic_api_key)

    system_prompt = _system_prompt(targets)
    messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]

    totals = {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}
    final_text = ""
    iteration = 0

    for iteration in range(1, MAX_ITERATIONS + 1):
        response = client.messages.create(
            model=settings.model,
            max_tokens=8192,
            thinking={"type": "adaptive"},
            system=[
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=TOOLS,
            messages=messages,
        )

        usage = response.usage
        totals["input"] += getattr(usage, "input_tokens", 0) or 0
        totals["output"] += getattr(usage, "output_tokens", 0) or 0
        totals["cache_read"] += getattr(usage, "cache_read_input_tokens", 0) or 0
        totals["cache_creation"] += getattr(usage, "cache_creation_input_tokens", 0) or 0

        # 어시스턴트 응답을 history에 그대로 append
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            for block in response.content:
                if block.type == "text":
                    final_text += block.text
            break

        if response.stop_reason == "tool_use":
            tool_results: list[dict[str, Any]] = []
            for block in response.content:
                if block.type == "tool_use":
                    console.print(f"[dim]→ {block.name}({json.dumps(block.input, ensure_ascii=False)[:120]})[/dim]")
                    content, is_error = _execute_tool(
                        block.name,
                        block.input,
                        inventory=inventory,
                        settings=settings,
                        prompt=prompt,
                    )
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": content,
                            "is_error": is_error,
                        }
                    )
            messages.append({"role": "user", "content": tool_results})
            continue

        # max_tokens / refusal / 그 외 → 루프 종료
        for block in response.content:
            if block.type == "text":
                final_text += block.text
        break

    if not final_text:
        final_text = f"(LLM did not return final text — stop_reason={response.stop_reason})"

    return AskOutcome(
        final_text=final_text.strip(),
        iterations=iteration,
        input_tokens=totals["input"],
        output_tokens=totals["output"],
        cache_read_tokens=totals["cache_read"],
        cache_creation_tokens=totals["cache_creation"],
    )
