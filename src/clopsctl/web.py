"""웹 UI — 인벤토리/히스토리 조회 + ask 실행 폼 (Phase 2-c).

bind 는 기본 127.0.0.1 (localhost only). 외부 노출 금지.
ask 폼은 SSH 명령을 실행하므로 GET 금지, POST 만 허용.
"""
from __future__ import annotations

import html
import json
from typing import Annotated

from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse
from rich.console import Console

from . import __version__
from .config import Server, load_inventory, load_settings
from .history import search
from .llm import list_backends, select_backend

app = FastAPI(title="clopsctl", version=__version__)


# --- helpers ----------------------------------------------------------------

def _e(s: object) -> str:
    """HTML escape — 모든 동적 값에 적용."""
    return html.escape(str(s if s is not None else ""))


_PAGE_CSS = """
body { font-family: -apple-system, system-ui, sans-serif; margin: 2rem; max-width: 980px; line-height: 1.4; }
h1 { margin: 0 0 .25rem 0; }
h2 { margin-top: 2rem; border-bottom: 1px solid #ddd; padding-bottom: .25rem; }
table { width: 100%; border-collapse: collapse; font-size: .9rem; }
th, td { text-align: left; padding: .35rem .6rem; border-bottom: 1px solid #eee; vertical-align: top; }
th { background: #fafafa; }
code, pre { background: #f4f4f4; padding: .1rem .3rem; border-radius: 3px; font-family: ui-monospace, "SF Mono", Menlo, monospace; }
pre { padding: .75rem 1rem; overflow-x: auto; white-space: pre-wrap; word-break: break-word; }
.muted { color: #777; }
form fieldset { border: 1px solid #ddd; padding: 1rem 1.25rem; border-radius: 4px; }
form legend { font-weight: 600; padding: 0 .5rem; }
form label { display: inline-block; margin-right: 1rem; }
form textarea { width: 100%; min-height: 4rem; font-family: inherit; padding: .5rem; box-sizing: border-box; }
form .row { margin: .75rem 0; }
form button { padding: .5rem 1rem; background: #0066cc; color: white; border: 0; border-radius: 4px; cursor: pointer; font-size: 1rem; }
form button:hover { background: #0052a3; }
.panel { border: 1px solid #ddd; border-radius: 4px; padding: 1rem 1.25rem; margin: .75rem 0; }
.panel.ok { border-left: 4px solid #22aa55; }
.panel.warn { border-left: 4px solid #cc8800; }
.panel.err { border-left: 4px solid #cc4444; }
.kv { font-size: .85rem; color: #555; }
.banner { background: #fff8e0; border: 1px solid #f0d878; padding: .5rem .75rem; border-radius: 4px; font-size: .85rem; }
"""


def _layout(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang='ko'><head>
  <meta charset='utf-8'>
  <title>{_e(title)}</title>
  <style>{_PAGE_CSS}</style>
</head><body>
  <h1>clopsctl <span class='muted'>{_e(__version__)}</span></h1>
  <p class='muted'>Master-side SSH fleet controller. <a href='/'>home</a></p>
  <p class='banner'>⚠ 이 UI 는 SSH 명령을 실행합니다. 반드시 localhost (127.0.0.1) bind 인지 확인하세요.</p>
  {body}
</body></html>"""


def _server_rows_html(inventory: dict[str, Server]) -> str:
    if not inventory:
        return "<tr><td colspan='6'><i>(empty — inventory/servers.toml 미설정)</i></td></tr>"
    return "".join(
        f"<tr><td>{_e(s.name)}</td><td>{_e(s.host)}</td><td>{_e(s.user)}</td>"
        f"<td>{_e(s.auth)}</td><td>{_e(s.role)}</td><td>{_e(', '.join(s.tags))}</td></tr>"
        for s in inventory.values()
    )


def _history_rows_html(rows: list) -> str:
    if not rows:
        return "<tr><td colspan='6'><i>(empty)</i></td></tr>"
    return "".join(
        f"<tr><td>{_e(r['id'])}</td><td>{_e(r['ts'][:19])}</td><td>{_e(r['server'])}</td>"
        f"<td>{_e(r['mode'])}</td><td>{_e(r['exit_code'] if r['exit_code'] is not None else '-')}</td>"
        f"<td><code>{_e((r['command'] or r['prompt'] or '')[:80])}</code></td></tr>"
        for r in rows
    )


def _ask_form_html(inventory: dict[str, Server], backends: list[tuple[str, bool]]) -> str:
    if not inventory:
        return "<p class='muted'>(인벤토리가 비어있어 ask 폼을 표시하지 않습니다)</p>"
    server_options = "".join(
        f"<label><input type='checkbox' name='targets' value='{_e(s.name)}'> "
        f"{_e(s.name)} <span class='muted'>({_e(s.host)}, {_e(s.role)})</span></label>"
        for s in inventory.values()
    )
    backend_options = "<option value=''>(auto)</option>" + "".join(
        f"<option value='{_e(name)}'{'' if available else ' disabled'}>{_e(name)}{'' if available else ' (미설치)'}</option>"
        for name, available in backends
    )
    return f"""
    <form method='POST' action='/ask'>
      <fieldset>
        <legend>대상 서버</legend>
        <div class='row'>{server_options}</div>
        <div class='row'>
          <label>프롬프트<br>
            <textarea name='prompt' required placeholder='예) 디스크 80% 넘는 마운트 알려줘'></textarea>
          </label>
        </div>
        <div class='row'>
          <label>백엔드 <select name='backend'>{backend_options}</select></label>
          <label><input type='checkbox' name='dry_run' value='1'> dry-run (실행 없이 plan 만)</label>
        </div>
        <div class='row'><button type='submit'>ask</button></div>
      </fieldset>
    </form>
    """


# --- routes -----------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def index() -> str:
    settings = load_settings()
    inventory = load_inventory(settings.inventory_path)
    rows = search(settings.history_db, limit=20)
    backends = list_backends()

    body = f"""
    <h2>Ask</h2>
    {_ask_form_html(inventory, backends)}

    <h2>Servers</h2>
    <table><thead><tr>
      <th>name</th><th>host</th><th>user</th><th>auth</th><th>role</th><th>tags</th>
    </tr></thead><tbody>{_server_rows_html(inventory)}</tbody></table>

    <h2>Recent history (last 20)</h2>
    <table><thead><tr>
      <th>id</th><th>ts (UTC)</th><th>server</th><th>mode</th><th>exit</th><th>cmd / prompt</th>
    </tr></thead><tbody>{_history_rows_html(rows)}</tbody></table>
    """
    return _layout(f"clopsctl {__version__}", body)


@app.post("/ask", response_class=HTMLResponse)
def ask_post(
    prompt: Annotated[str, Form()],
    targets: Annotated[list[str], Form()] = [],
    backend: Annotated[str, Form()] = "",
    dry_run: Annotated[str, Form()] = "",
) -> str:
    """ask 폼 POST — 명령 실행 → 결과 페이지 렌더."""
    # 지연 import (web 모듈 import 시 paramiko 등 무거운 의존성 회피)
    from . import agent

    settings = load_settings()
    inventory = load_inventory(settings.inventory_path)
    backends = list_backends()

    # 입력 검증
    errors: list[str] = []
    if not prompt or not prompt.strip():
        errors.append("프롬프트가 비어있습니다.")
    if not targets:
        errors.append("최소 하나의 서버를 선택해야 합니다.")
    unknown = [n for n in targets if n not in inventory]
    if unknown:
        errors.append(f"인벤토리에 없는 서버: {', '.join(unknown)}")

    if errors:
        body = "<h2>입력 오류</h2><ul>" + "".join(f"<li>{_e(e)}</li>" for e in errors) + "</ul>"
        body += f"<p><a href='/'>← 폼으로 돌아가기</a></p>"
        return _layout("clopsctl — error", body)

    selected_servers = [inventory[n] for n in targets]

    try:
        sel_backend = select_backend(backend.strip() or settings.llm_backend)
    except RuntimeError as exc:
        body = f"<h2>LLM 백엔드 오류</h2><p>{_e(str(exc))}</p><p><a href='/'>← 돌아가기</a></p>"
        return _layout("clopsctl — error", body)

    is_dry = bool(dry_run)
    quiet_console = Console(quiet=True)

    try:
        outcome = agent.ask(
            prompt, selected_servers, settings=settings, console=quiet_console,
            backend=sel_backend, dry_run=is_dry,
        )
    except Exception as exc:  # noqa: BLE001
        body = (
            f"<h2>ask 실행 실패</h2><pre>{_e(str(exc))}</pre>"
            f"<p><a href='/'>← 돌아가기</a></p>"
        )
        return _layout("clopsctl — error", body)

    style = "ok" if outcome.n_failed == 0 and outcome.n_blocked == 0 else "warn"
    body = f"""
    <h2>Ask 결과</h2>
    <div class='kv'>
      backend: <b>{_e(outcome.backend_name)}</b>
      &nbsp;|&nbsp; servers: <code>{_e(', '.join(targets))}</code>
      &nbsp;|&nbsp; dry-run: <b>{'yes' if is_dry else 'no'}</b>
      &nbsp;|&nbsp; steps: {outcome.n_steps}, blocked: {outcome.n_blocked}, failed: {outcome.n_failed}
    </div>
    <h3>프롬프트</h3>
    <pre>{_e(prompt)}</pre>
    <h3>답변</h3>
    <div class='panel {style}'><pre>{_e(outcome.final_text)}</pre></div>
    <p><a href='/'>← 새 ask 작성</a></p>
    """
    return _layout("clopsctl — answer", body)


@app.get("/healthz")
def healthz() -> dict[str, object]:
    return {"status": "ok", "version": __version__, "backends": dict(list_backends())}
