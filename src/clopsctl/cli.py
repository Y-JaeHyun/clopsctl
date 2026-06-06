"""clopsctl CLI — Typer 기반."""
from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import __version__
from .config import (
    VALID_AUTH,
    VALID_ROLE,
    Server,
    load_inventory,
    load_settings,
    validate_server_input,
    write_env,
    write_inventory,
)
from .history import record, search
from .safety import is_dangerous
from .ssh import fan_out

console = Console()
err_console = Console(stderr=True, style="red")
app = typer.Typer(help="Master-side SSH fleet controller for natural-language ops via Claude.")
server_app = typer.Typer(help="서버 인벤토리 관리")
app.add_typer(server_app, name="server")


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"clopsctl {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        help="버전 출력",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    pass


def _resolve_servers(names: str) -> list[Server]:
    settings = load_settings()
    inventory = load_inventory(settings.inventory_path)
    if not inventory:
        err_console.print(
            f"인벤토리가 비어있습니다: {settings.inventory_path}\n"
            "샘플을 복사하세요: cp inventory/servers.example.toml inventory/servers.toml"
        )
        raise typer.Exit(code=2)
    selected: list[Server] = []
    missing: list[str] = []
    for name in (n.strip() for n in names.split(",") if n.strip()):
        if name in inventory:
            selected.append(inventory[name])
        else:
            missing.append(name)
    if missing:
        err_console.print(f"인벤토리에 없는 서버: {', '.join(missing)}")
        raise typer.Exit(code=2)
    return selected


@server_app.command("list")
def server_list() -> None:
    """등록된 서버 목록 표시."""
    settings = load_settings()
    inventory = load_inventory(settings.inventory_path)
    if not inventory:
        console.print("(인벤토리가 비어있습니다)")
        return
    table = Table(title=f"servers — {settings.inventory_path}")
    table.add_column("name", style="cyan", no_wrap=True)
    table.add_column("host")
    table.add_column("user")
    table.add_column("auth")
    table.add_column("role")
    table.add_column("tags")
    for s in inventory.values():
        table.add_row(s.name, s.host, s.user, s.auth, s.role, ", ".join(s.tags))
    console.print(table)


@server_app.command("check")
def server_check(name: str) -> None:
    """단일 서버에 echo 한 번 실행해 SSH 연결성 확인."""
    servers = _resolve_servers(name)
    results = fan_out(servers, "echo clopsctl-ok")
    for r in results:
        if r.exit_code == 0 and "clopsctl-ok" in r.stdout:
            console.print(f"[green]✓[/green] {r.server} ({r.host}) — OK")
        else:
            err_console.print(f"✗ {r.server} ({r.host}) — {r.error or r.stderr or 'exit ' + str(r.exit_code)}")


@app.command("exec")
def exec_cmd(
    targets: str = typer.Argument(..., help="콤마로 구분된 서버 이름들"),
    command: str = typer.Argument(..., help="원격에서 실행할 명령"),
    yes: bool = typer.Option(False, "--yes", "-y", help="위험 명령 confirm 건너뛰기"),
    dry_run: bool = typer.Option(False, "--dry-run", help="실행 없이 게이트 검사만"),
    per_server: bool = typer.Option(
        False, "--per-server",
        help="권한 게이트를 서버별 개별 검사 (통과한 서버에만 실행). 기본은 strict (가장 엄격한 role 기준)",
    ),
) -> None:
    """LLM 거치지 않고 N대에 동일 명령 fan-out 실행."""
    from .permissions import is_allowed_for_role, strictest_role

    settings = load_settings()

    # 1) safety 게이트 (위험 명령은 confirm 또는 차단)
    flagged = is_dangerous(command)
    if flagged and settings.safety_confirm and not yes:
        err_console.print(f"[bold]위험 명령 패턴 매칭[/bold]: {flagged}")
        confirm = typer.confirm("그래도 실행하시겠습니까?", default=False)
        if not confirm:
            raise typer.Exit(code=1)

    servers = _resolve_servers(targets)

    # 2) 권한 게이트 — strict 또는 per-server
    mode = "per_server" if per_server else (settings.permission_mode or "strict").lower()
    if mode == "per_server":
        passing: list = []
        for srv in servers:
            reason = is_allowed_for_role(command, srv.role)
            if reason is None:
                passing.append(srv)
            else:
                err_console.print(f"[yellow]✗ {srv.name}[/yellow] 권한 거부: {reason}")
                record(
                    settings.history_db, server=srv.name, mode="exec", command=command,
                    exit_code=None, stderr=f"permission denied: {reason}",
                )
        if not passing:
            err_console.print("[red]모든 서버가 권한 거부되어 실행 대상이 없습니다.[/red]")
            raise typer.Exit(code=1)
        servers = passing
        role = strictest_role(servers)
    else:
        role = strictest_role(servers)
        perm_reason = is_allowed_for_role(command, role)
        if perm_reason:
            err_console.print(f"[red]권한 거부:[/red] {perm_reason}")
            err_console.print(f"대상 서버 중 가장 엄격한 role: [bold]{role}[/bold]  (--per-server 로 서버별 검사 가능)")
            for r in servers:
                record(
                    settings.history_db, server=r.name, mode="exec", command=command,
                    exit_code=None, stderr=f"permission denied: {perm_reason}",
                )
            raise typer.Exit(code=1)

    # 3) dry-run
    if dry_run:
        console.print(f"[cyan]∘ dry-run[/cyan] {[s.name for s in servers]} :: {command}")
        console.print(f"[dim]mode={mode}  role={role}  safety={'flagged' if flagged else 'ok'}  permission=ok[/dim]")
        return

    # 4) 실제 실행 (jump 해석용 inventory 전달)
    inventory = load_inventory(settings.inventory_path)
    results = fan_out(servers, command, inventory=inventory)
    for r in results:
        record(
            settings.history_db,
            server=r.server, mode="exec", command=command,
            exit_code=r.exit_code, stdout=r.stdout, stderr=r.stderr,
        )
        title = f"{r.server} ({r.host}) — exit {r.exit_code}"
        body = r.stdout if r.exit_code == 0 else (r.stderr or r.error or "")
        style = "green" if r.exit_code == 0 else "red"
        console.print(Panel(body.rstrip() or "(empty)", title=title, border_style=style))


@app.command("ask")
def ask_cmd(
    targets: str = typer.Argument(..., help="콤마로 구분된 서버 이름들"),
    prompt: str = typer.Argument(..., help="자연어 프롬프트"),
    backend_name: str | None = typer.Option(
        None, "--backend", "-b",
        help="LLM 백엔드 (claude|gemini|codex). 미지정 시 환경변수 또는 PATH 자동 감지",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="plan 만 표시하고 실제 SSH 실행과 summarize 건너뜀"),
    per_server: bool = typer.Option(
        False, "--per-server",
        help="권한 게이트를 서버별 개별 검사 (통과한 서버에만 실행)",
    ),
) -> None:
    """LLM 경유 자연어 명령 — 로컬 claude/gemini/codex CLI 활용."""
    import dataclasses

    from . import agent
    from . import llm

    settings = load_settings()
    if per_server:
        settings = dataclasses.replace(settings, permission_mode="per_server")
    try:
        backend = llm.select_backend(backend_name or settings.llm_backend)
    except RuntimeError as exc:
        err_console.print(f"[red]LLM 백엔드 사용 불가:[/red] {exc}")
        raise typer.Exit(code=2) from exc

    selected = _resolve_servers(targets)
    console.print(f"[dim]ask: {len(selected)} server(s) via {backend.name} CLI{' (dry-run)' if dry_run else ''}[/dim]")
    try:
        outcome = agent.ask(
            prompt, selected, settings=settings, console=console,
            backend=backend, dry_run=dry_run,
        )
    except Exception as exc:  # noqa: BLE001
        err_console.print(f"ask 실패: {exc}")
        raise typer.Exit(code=1) from exc

    console.print()
    console.print(Panel(outcome.final_text, title=f"answer ({outcome.backend_name})", border_style="cyan"))
    console.print(
        f"[dim]steps={outcome.n_steps}  blocked={outcome.n_blocked}  failed={outcome.n_failed}[/dim]"
    )


@app.command("backend")
def backend_status() -> None:
    """가용한 LLM CLI 백엔드 목록 표시."""
    from . import llm
    table = Table(title="LLM backends")
    table.add_column("name", style="cyan")
    table.add_column("binary")
    table.add_column("available")
    for name, available in llm.list_backends():
        marker = "[green]✓[/green]" if available else "[red]✗[/red]"
        binary = {"claude": "claude", "gemini": "gemini", "codex": "codex"}[name]
        table.add_row(name, binary, marker)
    console.print(table)


@app.command("history")
def history_cmd(
    server: str | None = typer.Option(None, "--server", "-s"),
    grep: str | None = typer.Option(None, "--grep", "-g"),
    limit: int = typer.Option(20, "--limit", "-n"),
) -> None:
    """명령 히스토리 조회."""
    settings = load_settings()
    rows = search(settings.history_db, server=server, grep=grep, limit=limit)
    if not rows:
        console.print("(히스토리가 비어있습니다)")
        return
    table = Table(title=f"history — {settings.history_db}")
    table.add_column("id", justify="right")
    table.add_column("ts", style="dim")
    table.add_column("server", style="cyan")
    table.add_column("mode")
    table.add_column("exit", justify="right")
    table.add_column("command")
    for row in rows:
        table.add_row(
            str(row["id"]),
            row["ts"][:19],
            row["server"],
            row["mode"],
            str(row["exit_code"]) if row["exit_code"] is not None else "-",
            (row["command"] or row["prompt"] or "")[:60],
        )
    console.print(table)


@app.command("web")
def web(
    host: str | None = typer.Option(None, "--host", help="bind 주소 (기본 127.0.0.1). 외부 접근은 0.0.0.0 — 보안 주의"),
    port: int | None = typer.Option(None, "--port", help="bind 포트 (기본 CLOPSCTL_WEB_PORT 또는 8765)"),
) -> None:
    """웹 UI 실행 — 인벤토리/히스토리 + ask 폼 + SSE 스트리밍."""
    import uvicorn
    settings = load_settings()
    bind_host = host or settings.web_host
    bind_port = port or settings.web_port

    if bind_host not in ("127.0.0.1", "localhost", "::1"):
        err_console.print(
            f"[bold yellow]⚠ 외부 접근 가능한 호스트({bind_host})에 바인드합니다.[/bold yellow]\n"
            f"[yellow]이 UI 는 인증이 없으며 SSH 명령 실행을 트리거할 수 있습니다.\n"
            f"신뢰된 네트워크에서만 사용하거나 ssh -L 포트포워딩, VPN, 방화벽 규칙으로 접근을 제한하세요.[/yellow]"
        )

    console.print(f"[dim]uvicorn → http://{bind_host}:{bind_port}[/dim]")
    uvicorn.run(
        "clopsctl.web:app",
        host=bind_host,
        port=bind_port,
        reload=False,
    )


# --- init 마법사 ---------------------------------------------------------------

_INIT_LLM_PROMPT = """\
You are configuring one SSH server entry for the `clopsctl` inventory.
Read the user's free-form description and respond with ONLY a single JSON object.
No prose, no explanation, no markdown code fences — just the JSON.

Include only the keys you can confidently infer; omit the rest.
Keys and rules:
  name          short id, allowed chars: letters digits _ - .   (e.g. "web-1")
  host          hostname or IP address
  user          ssh login user
  port          integer, default 22
  auth          one of "agent" | "pem" | "password"
                (private key file -> "pem"; ssh-agent / default keys -> "agent"; password -> "password")
  pem_path      path to the private key file (only when auth == "pem")
  password_env  the NAME of an environment variable holding the password (only when auth == "password")
  role          one of "read-only" | "shell" | "sudo"   (default "read-only")
  tags          array of short strings
  jump          name of an existing bastion server to hop through (omit for a direct connection)

Valid jump target names already in the inventory: {known}

User description:
{desc}
"""


def _extract_json(text: str) -> dict:
    """LLM 텍스트 출력에서 첫 JSON object 를 추출해 파싱한다."""
    import json
    import re as _re

    s = text.strip()
    fence = _re.search(r"```(?:json)?\s*(\{.*?\})\s*```", s, _re.DOTALL)
    if fence:
        s = fence.group(1)
    start = s.find("{")
    if start == -1:
        raise ValueError("LLM 응답에서 JSON object 를 찾지 못했습니다")
    depth = 0
    for i in range(start, len(s)):
        if s[i] == "{":
            depth += 1
        elif s[i] == "}":
            depth -= 1
            if depth == 0:
                obj = json.loads(s[start : i + 1])
                if not isinstance(obj, dict):
                    raise ValueError("JSON 최상위가 object 가 아닙니다")
                return obj
    raise ValueError("JSON object 가 닫히지 않았습니다")


def _json_to_form(d: dict) -> dict[str, str]:
    """LLM 이 준 JSON dict 를 validate_server_input 용 문자열 form 으로 정규화."""
    form: dict[str, str] = {}
    for key in ("name", "host", "user", "auth", "pem_path", "password_env", "role", "jump"):
        val = d.get(key)
        if val is not None and str(val).strip():
            form[key] = str(val).strip()
    if d.get("port") is not None:
        form["port"] = str(d["port"]).strip()
    tags = d.get("tags")
    if isinstance(tags, list):
        form["tags"] = ",".join(str(t).strip() for t in tags if str(t).strip())
    elif isinstance(tags, str):
        form["tags"] = tags.strip()
    return form


def _prompt_choice(label: str, choices: tuple[str, ...], default: str) -> str:
    while True:
        val = typer.prompt(f"{label} ({'|'.join(choices)})", default=default).strip()
        if val in choices:
            return val
        err_console.print(f"  → {list(choices)} 중 하나를 입력하세요.")


def _collect_server_manual(known_names: list[str]) -> dict[str, str]:
    """순수 프롬프트 입력으로 한 서버의 form dict 를 수집 (AI CLI 미설치 폴백)."""
    form: dict[str, str] = {
        "name": typer.prompt("서버 이름 (name)").strip(),
        "host": typer.prompt("host (IP 또는 도메인)").strip(),
        "user": typer.prompt("ssh user").strip(),
        "port": typer.prompt("port", default="22").strip(),
    }
    auth = _prompt_choice("auth", VALID_AUTH, default="agent")
    form["auth"] = auth
    if auth == "pem":
        form["pem_path"] = typer.prompt("pem_path (개인키 파일 경로)").strip()
    elif auth == "password":
        form["password_env"] = typer.prompt("password_env (.env 의 환경변수 이름)").strip()
    form["role"] = _prompt_choice("role", VALID_ROLE, default="read-only")
    form["tags"] = typer.prompt("tags (콤마 구분, 없으면 빈칸)", default="").strip()
    if known_names:
        form["jump"] = typer.prompt(
            f"jump 서버 (bastion, 없으면 빈칸) — 후보: {', '.join(known_names)}",
            default="",
        ).strip()
    return form


def _collect_server_llm(backend, known_names: list[str]) -> dict[str, str]:
    """사용자의 자연어 설명을 LLM 으로 구조화해 form dict 반환. 실패 시 예외."""
    desc = typer.prompt(
        "서버를 자연어로 설명해 주세요 (예: 'web-1 은 10.0.1.11, ec2-user 로 secrets/web-1.pem 키, read-only')"
    )
    prompt = _INIT_LLM_PROMPT.format(
        known=", ".join(known_names) if known_names else "(none yet)",
        desc=desc,
    )
    console.print(f"[dim]{backend.name} CLI 로 구조화 중…[/dim]")
    raw = backend.invoke(prompt, timeout=120)
    parsed = _extract_json(raw)
    return _json_to_form(parsed)


def _preview_server(server: Server) -> None:
    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_column(style="cyan", no_wrap=True)
    table.add_column()
    table.add_row("name", server.name)
    table.add_row("host", f"{server.host}:{server.port}")
    table.add_row("user", server.user)
    table.add_row("auth", server.auth + (f"  ({server.pem_path})" if server.pem_path else ""))
    if server.password_env:
        table.add_row("password_env", server.password_env)
    table.add_row("role", server.role)
    if server.tags:
        table.add_row("tags", ", ".join(server.tags))
    if server.jump:
        table.add_row("jump", f"via {server.jump}")
    console.print(Panel(table, title="추가될 서버", border_style="cyan"))


@app.command("init")
def init_cmd(
    inventory: Path | None = typer.Option(
        None, "--inventory", "-i", help="인벤토리 파일 경로 (기본: 설정값/inventory/servers.toml)",
    ),
    env_file: Path = typer.Option(
        Path(".env"), "--env-file", help="작성할 .env 경로 (백엔드 선택·비밀번호 저장용)",
    ),
    backend_name: str | None = typer.Option(
        None, "--backend", "-b",
        help="대화 백엔드 (claude|gemini|codex|manual). 미지정 시 가용 CLI 선택 프롬프트",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="파일을 쓰지 않고 작성될 내용 미리보기만",
    ),
) -> None:
    """대화형 마법사로 서버 인벤토리(servers.toml) + .env 를 생성한다.

    로컬 AI CLI(claude/gemini/codex)가 설치돼 있으면 자연어 설명을 구조화해 채우고,
    없으면 순수 프롬프트 입력 마법사로 폴백한다.
    """
    from . import llm

    settings = load_settings()
    inv_path = inventory or settings.inventory_path
    existing = load_inventory(inv_path)
    working: dict[str, Server] = dict(existing)

    console.print(Panel(
        f"인벤토리: [cyan]{inv_path}[/cyan]    .env: [cyan]{env_file}[/cyan]\n"
        f"기존 서버: {len(existing)}개" + (f"  ({', '.join(existing)})" if existing else ""),
        title="clopsctl init", border_style="cyan",
    ))

    # 1) 백엔드 결정 — 가용 CLI 자동 탐지 → 선택, 없으면 manual 폴백
    available = [name for name, ok in llm.list_backends() if ok]
    chosen_backend = None
    use_llm = False
    if backend_name == "manual":
        console.print("[dim]manual 모드 — 순수 프롬프트 입력[/dim]")
    elif backend_name:
        try:
            chosen_backend = llm.select_backend(backend_name)
            use_llm = True
        except RuntimeError as exc:
            err_console.print(f"[red]{exc}[/red] — manual 모드로 폴백합니다.")
    elif available:
        console.print(f"가용 AI CLI: [green]{', '.join(available)}[/green]")
        pick = typer.prompt(
            f"대화에 사용할 백엔드 ({'|'.join(available)}|manual)", default=available[0]
        ).strip()
        if pick != "manual":
            try:
                chosen_backend = llm.select_backend(pick)
                use_llm = True
            except RuntimeError as exc:
                err_console.print(f"[red]{exc}[/red] — manual 모드로 폴백합니다.")
    else:
        console.print(
            "[yellow]설치된 AI CLI 가 없습니다 (claude/gemini/codex).[/yellow] "
            "순수 프롬프트 입력 마법사로 진행합니다."
        )

    # 2) 서버 수집 루프
    password_secrets: dict[str, str] = {}
    added_count = 0
    while True:
        known_names = list(working.keys())
        try:
            if use_llm and chosen_backend is not None:
                form = _collect_server_llm(chosen_backend, known_names)
            else:
                form = _collect_server_manual(known_names)
        except Exception as exc:  # noqa: BLE001 — LLM 호출/파싱 실패는 manual 폴백
            err_console.print(f"[red]구조화 실패:[/red] {exc}")
            if use_llm and typer.confirm("이 서버를 manual 입력으로 진행할까요?", default=True):
                form = _collect_server_manual(known_names)
            else:
                if not typer.confirm("다른 서버를 계속 추가할까요?", default=False):
                    break
                continue

        server, errors = validate_server_input(form, working, is_edit=False)
        if errors:
            err_console.print("[red]검증 실패:[/red]")
            for e in errors:
                err_console.print(f"  • {e}")
            if typer.confirm("이 서버를 다시 입력할까요?", default=True):
                continue
            break

        assert server is not None
        _preview_server(server)
        if not typer.confirm(f"'{server.name}' 를 인벤토리에 추가할까요?", default=True):
            if typer.confirm("다른 서버를 계속 추가할까요?", default=True):
                continue
            break

        working[server.name] = server
        added_count += 1

        # auth=password 면 .env 에 저장할 비밀번호를 선택적으로 수집
        if server.auth == "password" and server.password_env:
            secret = typer.prompt(
                f"{server.password_env} 값 (.env 에 저장, 비워두면 나중에 직접 입력)",
                default="", hide_input=True,
            )
            if secret:
                password_secrets[server.password_env] = secret

        if not typer.confirm("다른 서버를 더 추가할까요?", default=False):
            break

    if added_count == 0:
        console.print("[yellow]추가된 서버가 없습니다. 변경 사항 없이 종료합니다.[/yellow]")
        raise typer.Exit(code=0)

    # 3) 최종 미리보기 (dry-run 프리뷰)
    console.print()
    table = Table(title=f"최종 인벤토리 — {inv_path}")
    table.add_column("name", style="cyan", no_wrap=True)
    table.add_column("host")
    table.add_column("user")
    table.add_column("auth")
    table.add_column("role")
    table.add_column("jump")
    for s in working.values():
        table.add_row(s.name, f"{s.host}:{s.port}", s.user, s.auth, s.role, s.jump or "-")
    console.print(table)

    env_values: dict[str, str] = {}
    if use_llm and chosen_backend is not None:
        env_values["CLOPSCTL_LLM_BACKEND"] = chosen_backend.name
    env_values.update(password_secrets)

    if dry_run:
        console.print(
            f"\n[cyan]∘ dry-run[/cyan] — 파일을 쓰지 않습니다.\n"
            f"  servers.toml ← {inv_path} ({len(working)}개 서버)\n"
            f"  .env         ← {env_file} (추가 키: "
            f"{', '.join(env_values) if env_values else '없음'})"
        )
        return

    if not typer.confirm(f"\n위 내용을 {inv_path} 와 {env_file} 에 저장할까요?", default=True):
        console.print("[yellow]취소되었습니다. 파일을 쓰지 않았습니다.[/yellow]")
        raise typer.Exit(code=0)

    write_inventory(inv_path, working)
    console.print(f"[green]✓[/green] 인벤토리 작성: {inv_path} (chmod 600)")
    if env_values:
        added = write_env(env_file, env_values)
        if added:
            console.print(f"[green]✓[/green] .env 갱신: {env_file} — 추가 키 {', '.join(added)} (chmod 600)")
        else:
            console.print(f"[dim].env 의 모든 키가 이미 존재 — 변경 없음 ({env_file})[/dim]")
    console.print("\n다음: [bold]clopsctl server list[/bold] / [bold]clopsctl server check <name>[/bold]")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
