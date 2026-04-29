"""웹 UI — 인벤토리/히스토리 조회 + ask 실행 폼 + SSE 스트리밍 (Phase 2-c).

bind 는 기본 127.0.0.1 (localhost only). 외부 노출 금지.
ask 폼은 SSH 명령을 실행하므로 GET 금지, POST 만 허용.
실행 진행 상황은 SSE 로 단계별 푸시.
"""
from __future__ import annotations

import html
import json
import time
import uuid
from dataclasses import dataclass, field
from queue import Empty, Queue
from threading import Thread
from typing import Annotated, Any

from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from rich.console import Console

from . import __version__
from .config import Server, load_inventory, load_settings
from .history import search
from .llm import list_backends, select_backend

app = FastAPI(title="clopsctl", version=__version__)


# --- in-memory job 관리 (단일 프로세스 가정) ----------------------------------

@dataclass
class Job:
    id: str
    queue: Queue = field(default_factory=Queue)
    started_at: float = field(default_factory=time.monotonic)
    done: bool = False


JOBS: dict[str, Job] = {}
JOB_TTL_SECS = 600  # 10분 후 자동 청소


def _cleanup_old_jobs() -> None:
    now = time.monotonic()
    stale = [jid for jid, j in JOBS.items() if j.done and now - j.started_at > JOB_TTL_SECS]
    for jid in stale:
        JOBS.pop(jid, None)


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


def _run_ask_job(
    job: Job, prompt: str, targets: list[Server], settings, backend, dry_run: bool
) -> None:
    """별도 thread 에서 agent.ask 실행 — 모든 진행 이벤트를 job.queue 에 push."""
    from . import agent

    def emit(evt: dict[str, Any]) -> None:
        job.queue.put(evt)

    quiet_console = Console(quiet=True)
    try:
        agent.ask(
            prompt, targets, settings=settings, console=quiet_console,
            backend=backend, dry_run=dry_run, on_event=emit,
        )
    except Exception as exc:  # noqa: BLE001
        emit({"type": "error", "message": str(exc)})
    finally:
        emit({"type": "_eof"})  # 스트림 종료 신호
        job.done = True


@app.post("/ask", response_class=HTMLResponse)
def ask_post(
    prompt: Annotated[str, Form()],
    targets: Annotated[list[str], Form()] = [],
    backend: Annotated[str, Form()] = "",
    dry_run: Annotated[str, Form()] = "",
) -> str:
    """ask 폼 POST — job 시작 후 SSE 스트리밍 페이지 렌더."""
    settings = load_settings()
    inventory = load_inventory(settings.inventory_path)

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
        body += "<p><a href='/'>← 폼으로 돌아가기</a></p>"
        return _layout("clopsctl — error", body)

    selected_servers = [inventory[n] for n in targets]

    try:
        sel_backend = select_backend(backend.strip() or settings.llm_backend)
    except RuntimeError as exc:
        body = f"<h2>LLM 백엔드 오류</h2><p>{_e(str(exc))}</p><p><a href='/'>← 돌아가기</a></p>"
        return _layout("clopsctl — error", body)

    is_dry = bool(dry_run)
    _cleanup_old_jobs()
    job = Job(id=uuid.uuid4().hex)
    JOBS[job.id] = job

    Thread(
        target=_run_ask_job,
        args=(job, prompt, selected_servers, settings, sel_backend, is_dry),
        daemon=True,
    ).start()

    # SSE 스트리밍 페이지 렌더 (브라우저는 즉시 페이지 받고 EventSource 로 구독)
    body = f"""
    <h2>Ask 진행 중</h2>
    <div class='kv'>
      backend: <b>{_e(sel_backend.name)}</b>
      &nbsp;|&nbsp; servers: <code>{_e(', '.join(targets))}</code>
      &nbsp;|&nbsp; dry-run: <b>{'yes' if is_dry else 'no'}</b>
      &nbsp;|&nbsp; job: <code>{_e(job.id)}</code>
    </div>
    <h3>프롬프트</h3>
    <pre>{_e(prompt)}</pre>
    <h3>진행</h3>
    <div id='log' class='panel'></div>
    <h3>답변</h3>
    <div id='answer' class='panel'><i class='muted'>(생성 중…)</i></div>
    <p><a href='/'>← 새 ask 작성</a></p>
    <script>
    (function() {{
      var src = new EventSource('/ask/stream/{job.id}');
      var log = document.getElementById('log');
      var answer = document.getElementById('answer');
      function append(html_) {{ var p = document.createElement('div'); p.innerHTML = html_; log.appendChild(p); log.scrollTop = log.scrollHeight; }}
      function escapeHtml(s) {{ var d = document.createElement('div'); d.appendChild(document.createTextNode(s)); return d.innerHTML; }}
      src.onmessage = function(ev) {{
        try {{
          var e = JSON.parse(ev.data);
          if (e.type === 'started') append('<i class=muted>started — backend ' + escapeHtml(e.backend) + '</i>');
          else if (e.type === 'plan_start') append('· planning…');
          else if (e.type === 'plan_done') append('· plan: ' + e.n_steps + ' step' + (e.n_steps === 1 ? '' : 's'));
          else if (e.type === 'step_start') append('→ <code>' + escapeHtml(JSON.stringify(e.servers)) + '</code> :: <code>' + escapeHtml(e.command) + '</code>');
          else if (e.type === 'step_result') append('  · ' + escapeHtml(e.server) + ' exit=' + e.exit_code);
          else if (e.type === 'step_blocked') append('✗ blocked (' + escapeHtml(e.reason) + '): <code>' + escapeHtml(e.command) + '</code>');
          else if (e.type === 'step_failed') append('✗ failed: ' + escapeHtml(e.reason));
          else if (e.type === 'step_dry_run') append('∘ dry-run: <code>' + escapeHtml(e.command) + '</code>');
          else if (e.type === 'summarize_start') append('· summarizing…');
          else if (e.type === 'done') {{
            answer.innerHTML = '<pre>' + escapeHtml(e.final_text) + '</pre>';
            answer.classList.add(e.n_failed === 0 && e.n_blocked === 0 ? 'ok' : 'warn');
            append('<b>done</b> — steps=' + e.n_steps + ' blocked=' + e.n_blocked + ' failed=' + e.n_failed);
          }}
          else if (e.type === 'error') {{
            answer.innerHTML = '<pre>error: ' + escapeHtml(e.message) + '</pre>';
            answer.classList.add('err');
          }}
        }} catch (err) {{ /* ignore parse errors */ }}
      }};
      src.addEventListener('eof', function() {{ src.close(); }});
      src.onerror = function() {{ src.close(); }};
    }})();
    </script>
    """
    return _layout("clopsctl — running", body)


@app.get("/ask/stream/{job_id}")
def ask_stream(job_id: str):
    """SSE 스트림 — job 의 큐를 읽어 EventSource 로 푸시."""
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="unknown job_id")

    def gen():
        idle_loops = 0
        while True:
            try:
                evt = job.queue.get(timeout=2.0)
            except Empty:
                idle_loops += 1
                # 게시물 없을 때 keep-alive (15초마다 comment line)
                if idle_loops % 8 == 0:
                    yield ": keep-alive\n\n"
                if job.done and job.queue.empty():
                    break
                continue
            idle_loops = 0
            if evt.get("type") == "_eof":
                yield "event: eof\ndata: end\n\n"
                break
            yield f"data: {json.dumps(evt, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/healthz")
def healthz() -> dict[str, object]:
    return {"status": "ok", "version": __version__, "backends": dict(list_backends())}
