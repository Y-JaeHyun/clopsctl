"""clopsctl CLI — Typer 기반."""
from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import __version__
from .config import Server, load_inventory, load_settings
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
) -> None:
    """LLM 거치지 않고 N대에 동일 명령 fan-out 실행."""
    settings = load_settings()
    flagged = is_dangerous(command)
    if flagged and settings.safety_confirm and not yes:
        err_console.print(f"[bold]위험 명령 패턴 매칭[/bold]: {flagged}")
        confirm = typer.confirm("그래도 실행하시겠습니까?", default=False)
        if not confirm:
            raise typer.Exit(code=1)

    servers = _resolve_servers(targets)
    results = fan_out(servers, command)
    for r in results:
        record(
            settings.history_db,
            server=r.server,
            mode="exec",
            command=command,
            exit_code=r.exit_code,
            stdout=r.stdout,
            stderr=r.stderr,
        )
        title = f"{r.server} ({r.host}) — exit {r.exit_code}"
        body = r.stdout if r.exit_code == 0 else (r.stderr or r.error or "")
        style = "green" if r.exit_code == 0 else "red"
        console.print(Panel(body.rstrip() or "(empty)", title=title, border_style=style))


@app.command("ask")
def ask(
    targets: str = typer.Argument(..., help="콤마로 구분된 서버 이름들"),
    prompt: str = typer.Argument(..., help="자연어 프롬프트"),
) -> None:
    """LLM 경유 자연어 명령 (Phase 2 구현 예정)."""
    err_console.print(
        "[yellow]ask 모드는 Phase 2 에서 구현됩니다[/yellow]\n"
        f"대상={targets!r} prompt={prompt!r}\n"
        "현재는 `clopsctl exec` 또는 `claude` (mcp-ssh-manager 연동) 를 직접 사용하세요."
    )
    raise typer.Exit(code=2)


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
    host: str | None = typer.Option(None, "--host"),
    port: int | None = typer.Option(None, "--port"),
) -> None:
    """로컬 웹 UI 실행 (Phase 2 본격 구현)."""
    import uvicorn
    settings = load_settings()
    uvicorn.run(
        "clopsctl.web:app",
        host=host or settings.web_host,
        port=port or settings.web_port,
        reload=False,
    )


def main() -> None:
    app()


if __name__ == "__main__":
    main()
