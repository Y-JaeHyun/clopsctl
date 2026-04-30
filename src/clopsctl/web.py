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

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from rich.console import Console

from . import __version__
from .config import Server, load_inventory, load_settings, write_inventory
from .history import search
from .llm import list_backends, select_backend
from .ssh import _resolve_jump_chain

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


@dataclass
class Conversation:
    """ask 한 흐름을 follow-up 으로 이어나가기 위한 turn 기록."""
    id: str
    targets: list[str]                    # 첫 turn 의 서버 — follow-up 도 동일 대상
    backend_name: str
    turns: list[dict[str, Any]] = field(default_factory=list)
    started_at: float = field(default_factory=time.monotonic)


CONVERSATIONS: dict[str, Conversation] = {}
CONVERSATION_TTL_SECS = 1800  # 30분 idle 후 자동 청소


def _cleanup_old_jobs() -> None:
    now = time.monotonic()
    stale = [jid for jid, j in JOBS.items() if j.done and now - j.started_at > JOB_TTL_SECS]
    for jid in stale:
        JOBS.pop(jid, None)
    stale_conv = [cid for cid, c in CONVERSATIONS.items() if now - c.started_at > CONVERSATION_TTL_SECS]
    for cid in stale_conv:
        CONVERSATIONS.pop(cid, None)


# --- helpers ----------------------------------------------------------------

def _e(s: object) -> str:
    """HTML escape — 모든 동적 값에 적용."""
    return html.escape(str(s if s is not None else ""))


_PAGE_CSS = """
:root {
  --bg: #f6f7f9;
  --surface: #ffffff;
  --border: #e3e6ec;
  --text: #1f2430;
  --muted: #6b7280;
  --accent: #2563eb;
  --accent-hover: #1d4ed8;
  --ok: #16a34a;
  --warn: #d97706;
  --err: #dc2626;
  --code-bg: #f3f4f6;
}
* { box-sizing: border-box; }
html, body { background: var(--bg); color: var(--text); }
body {
  font-family: -apple-system, "Segoe UI", system-ui, "Helvetica Neue", "Apple SD Gothic Neo", "Noto Sans KR", sans-serif;
  margin: 0; line-height: 1.5; font-size: 14px;
}
.topbar {
  background: linear-gradient(90deg, #0f172a 0%, #1e293b 100%);
  color: #e2e8f0; padding: .85rem 1.5rem; display: flex; align-items: center; gap: .75rem;
  border-bottom: 1px solid #0b1220;
}
.topbar .brand { font-weight: 600; font-size: 1.05rem; letter-spacing: -.01em; }
.topbar .brand .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: var(--accent); margin-right: .5rem; vertical-align: middle; }
.topbar .ver { color: #94a3b8; font-size: .8rem; }
.topbar .nav { margin-left: auto; }
.topbar .nav a { color: #cbd5e1; text-decoration: none; padding: .25rem .6rem; border-radius: 4px; font-size: .85rem; }
.topbar .nav a:hover { background: rgba(255,255,255,.06); color: white; }
.container { max-width: 1080px; margin: 1.25rem auto; padding: 0 1.25rem 3rem; }
.banner {
  background: #fffbeb; border: 1px solid #fde68a; color: #92400e;
  padding: .65rem .9rem; border-radius: 6px; font-size: .85rem; margin-bottom: 1.25rem;
}
h1 { margin: 0; }
h2 { margin: 0 0 .8rem 0; font-size: 1.1rem; font-weight: 600; letter-spacing: -.01em; }
h3 { margin: 0 0 .4rem 0; font-size: .9rem; color: var(--muted); font-weight: 500; text-transform: uppercase; letter-spacing: .04em; }
.card {
  background: var(--surface); border: 1px solid var(--border); border-radius: 8px;
  padding: 1.1rem 1.25rem; margin-bottom: 1.25rem; box-shadow: 0 1px 2px rgba(15,23,42,.04);
}
.card.tight { padding: .75rem .9rem; }
table { width: 100%; border-collapse: collapse; }
table th, table td { text-align: left; padding: .5rem .65rem; border-bottom: 1px solid var(--border); font-size: .85rem; }
table th { background: #f9fafb; font-weight: 600; color: #374151; }
table tr:last-child td { border-bottom: 0; }
table.dense th, table.dense td { padding: .35rem .55rem; }
code, pre { font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: .82em; }
code { background: var(--code-bg); padding: .1rem .35rem; border-radius: 3px; }
pre { background: var(--code-bg); padding: .75rem 1rem; border-radius: 6px; overflow-x: auto; white-space: pre-wrap; word-break: break-word; margin: 0; }
.muted { color: var(--muted); }
.kv { font-size: .82rem; color: var(--muted); margin-bottom: .75rem; }
.kv b { color: var(--text); }
.badge {
  display: inline-block; padding: .12rem .5rem; border-radius: 999px; font-size: .72rem;
  font-weight: 600; line-height: 1.5; vertical-align: middle;
}
.badge.role-read-only { background: #ecfeff; color: #0e7490; }
.badge.role-shell { background: #fef3c7; color: #92400e; }
.badge.role-sudo { background: #fee2e2; color: #b91c1c; }
.badge.tag { background: #eef2ff; color: #4338ca; margin-left: .25rem; }
.badge.jump { background: #f5f3ff; color: #6d28d9; }
form fieldset { border: 0; padding: 0; margin: 0; }
form legend { display: none; }
form .row { margin: .9rem 0; }
form .label-block { font-weight: 600; font-size: .85rem; margin-bottom: .35rem; display: block; color: #374151; }
form textarea, form input[type='text'], form input[type='number'], form select {
  width: 100%; padding: .55rem .7rem; font-family: inherit; font-size: .9rem;
  border: 1px solid var(--border); border-radius: 6px; background: white; color: var(--text);
}
form textarea { min-height: 5.5rem; resize: vertical; }
form textarea:focus, form input:focus, form select:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px rgba(37,99,235,.12); }
form .checkbox-group { display: flex; flex-wrap: wrap; gap: .4rem .5rem; }
form .checkbox-group label {
  display: inline-flex; align-items: center; gap: .35rem; padding: .3rem .65rem;
  border: 1px solid var(--border); border-radius: 6px; cursor: pointer; background: #f9fafb; font-size: .85rem;
}
form .checkbox-group label:hover { background: #f3f4f6; }
form .checkbox-group input { accent-color: var(--accent); }
form .row-inline { display: flex; gap: 1rem; flex-wrap: wrap; align-items: center; }
form .row-inline > div { flex: 1; min-width: 180px; }
form button {
  padding: .6rem 1.25rem; background: var(--accent); color: white; border: 0; border-radius: 6px;
  cursor: pointer; font-size: .95rem; font-weight: 600; letter-spacing: -.005em;
}
form button:hover { background: var(--accent-hover); }
form button.secondary { background: #6b7280; }
.panel { border: 1px solid var(--border); border-radius: 6px; padding: .85rem 1rem; margin: .5rem 0; background: #fafbfc; }
.panel.ok { border-left: 3px solid var(--ok); background: #f0fdf4; }
.panel.warn { border-left: 3px solid var(--warn); background: #fffbeb; }
.panel.err { border-left: 3px solid var(--err); background: #fef2f2; }
.event-row { padding: .35rem .5rem; border-bottom: 1px dashed #e5e7eb; font-size: .82rem; font-family: ui-monospace, monospace; }
.event-row:last-child { border-bottom: 0; }
.event-row.evt-blocked { color: var(--warn); }
.event-row.evt-failed { color: var(--err); }
.event-row.evt-ok { color: var(--ok); }
.event-row .icon { display: inline-block; width: 1.2em; }
a { color: var(--accent); }
a:hover { color: var(--accent-hover); }
.btn-link {
  display: inline-block; padding: .15rem .55rem; font-size: .78rem; border: 1px solid var(--border);
  border-radius: 4px; background: white; color: var(--text); text-decoration: none;
}
.btn-link:hover { background: #f3f4f6; color: var(--text); }
.btn-link.btn-danger { color: var(--err); border-color: #fecaca; }
.btn-link.btn-danger:hover { background: #fef2f2; color: var(--err); }
.btn-link.btn-primary { background: var(--accent); color: white; border-color: var(--accent); }
.btn-link.btn-primary:hover { background: var(--accent-hover); color: white; }
.actions { white-space: nowrap; text-align: right; }
.section-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: .8rem; }
.section-head h2 { margin: 0; }
.error-list { background: #fef2f2; border: 1px solid #fecaca; color: #991b1b; padding: .65rem .9rem; border-radius: 6px; margin-bottom: 1rem; }
.error-list ul { margin: .25rem 0 0 1rem; padding: 0; }
.turn-prior { background: #fafbfc; }
.turn-prior h2 { color: #6b7280; }
"""


def _layout(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang='ko'><head>
  <meta charset='utf-8'>
  <title>{_e(title)}</title>
  <style>{_PAGE_CSS}</style>
</head><body>
  <header class='topbar'>
    <span class='brand'><span class='dot'></span>clopsctl <span class='ver'>v{_e(__version__)}</span></span>
    <nav class='nav'>
      <a href='/'>Home</a>
      <a href='/healthz'>Health</a>
    </nav>
  </header>
  <main class='container'>
    <p class='banner'>⚠ 이 UI 는 인증 없이 SSH 명령을 실행합니다. 반드시 신뢰된 네트워크 (기본 127.0.0.1) 에서만 사용하세요.</p>
    {body}
  </main>
</body></html>"""


def _server_rows_html(inventory: dict[str, Server]) -> str:
    if not inventory:
        return "<tr><td colspan='8' class='muted'><i>(empty — inventory/servers.toml 미설정)</i></td></tr>"
    rows = []
    for s in inventory.values():
        role_class = f"role-{s.role}"
        tags_html = "".join(f"<span class='badge tag'>{_e(t)}</span>" for t in s.tags) or "<span class='muted'>-</span>"
        jump_html = (
            f"<span class='badge jump'>via {_e(s.jump)}</span>"
            if s.jump else "<span class='muted'>-</span>"
        )
        actions = (
            f"<a href='/servers/{_e(s.name)}/edit' class='btn-link'>편집</a> "
            f"<a href='/servers/{_e(s.name)}/delete' class='btn-link btn-danger'>삭제</a>"
        )
        rows.append(
            f"<tr><td><b>{_e(s.name)}</b></td>"
            f"<td><code>{_e(s.host)}</code></td>"
            f"<td>{_e(s.user)}</td>"
            f"<td>{_e(s.auth)}</td>"
            f"<td><span class='badge {role_class}'>{_e(s.role)}</span></td>"
            f"<td>{jump_html}</td>"
            f"<td>{tags_html}</td>"
            f"<td class='actions'>{actions}</td></tr>"
        )
    return "".join(rows)


def _history_rows_html(rows: list) -> str:
    if not rows:
        return "<tr><td colspan='6' class='muted'><i>(empty)</i></td></tr>"
    out = []
    for r in rows:
        exit_code = r["exit_code"]
        exit_html = (
            f"<span class='badge role-sudo'>{_e(exit_code)}</span>"
            if isinstance(exit_code, int) and exit_code != 0
            else (f"<span class='muted'>{_e(exit_code)}</span>" if exit_code is not None else "<span class='muted'>—</span>")
        )
        out.append(
            f"<tr><td class='muted'>{_e(r['id'])}</td>"
            f"<td class='muted'>{_e(r['ts'][:19])}</td>"
            f"<td><b>{_e(r['server'])}</b></td>"
            f"<td>{_e(r['mode'])}</td>"
            f"<td>{exit_html}</td>"
            f"<td><code>{_e((r['command'] or r['prompt'] or '')[:80])}</code></td></tr>"
        )
    return "".join(out)


def _ask_form_html(inventory: dict[str, Server], backends: list[tuple[str, bool]]) -> str:
    if not inventory:
        return (
            "<p class='muted'>인벤토리가 비어있어 ask 폼을 표시하지 않습니다. "
            "<code>inventory/servers.toml</code> 을 채운 뒤 새로고침하세요.</p>"
        )
    server_options = "".join(
        f"<label><input type='checkbox' name='targets' value='{_e(s.name)}'> "
        f"<b>{_e(s.name)}</b> <span class='muted'>({_e(s.host)} · {_e(s.role)})</span></label>"
        for s in inventory.values()
    )
    backend_options = "<option value=''>(자동 감지)</option>" + "".join(
        f"<option value='{_e(name)}'{'' if available else ' disabled'}>{_e(name)}{'' if available else ' (미설치)'}</option>"
        for name, available in backends
    )
    return f"""
    <form method='POST' action='/ask'>
      <fieldset>
        <div class='row'>
          <label class='label-block'>대상 서버</label>
          <div class='checkbox-group'>{server_options}</div>
        </div>
        <div class='row'>
          <label class='label-block' for='prompt'>프롬프트</label>
          <textarea id='prompt' name='prompt' required placeholder='예) 디스크 80% 넘는 마운트 알려줘'></textarea>
        </div>
        <div class='row row-inline'>
          <div>
            <label class='label-block' for='backend'>LLM 백엔드</label>
            <select id='backend' name='backend'>{backend_options}</select>
          </div>
          <div class='checkbox-group' style='margin-top:1.4rem'>
            <label><input type='checkbox' name='dry_run' value='1'> dry-run (실행 없이 plan 만)</label>
          </div>
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
    <section class='card'>
      <h2>Ask</h2>
      {_ask_form_html(inventory, backends)}
    </section>

    <section class='card'>
      <div class='section-head'>
        <h2>Servers</h2>
        <a href='/servers/new' class='btn-link btn-primary'>+ 서버 추가</a>
      </div>
      <table class='dense'><thead><tr>
        <th>name</th><th>host</th><th>user</th><th>auth</th><th>role</th><th>jump</th><th>tags</th><th></th>
      </tr></thead><tbody>{_server_rows_html(inventory)}</tbody></table>
    </section>

    <section class='card'>
      <h2>Recent history <span class='muted' style='font-weight:normal'>(last 20)</span></h2>
      <table class='dense'><thead><tr>
        <th>id</th><th>ts (UTC)</th><th>server</th><th>mode</th><th>exit</th><th>cmd / prompt</th>
      </tr></thead><tbody>{_history_rows_html(rows)}</tbody></table>
    </section>
    """
    return _layout(f"clopsctl {__version__}", body)


def _run_ask_job(
    job: Job, prompt: str, targets: list[Server], settings, backend, dry_run: bool,
    *, conversation_id: str | None = None, prior_turns: list[dict[str, Any]] | None = None,
) -> None:
    """별도 thread 에서 agent.ask 실행 — 진행 이벤트는 큐로, 완료된 turn 은 conversation 에 append."""
    from . import agent

    captured: dict[str, Any] = {"final_text": None, "n_steps": 0, "n_blocked": 0, "n_failed": 0}

    def emit(evt: dict[str, Any]) -> None:
        if evt.get("type") == "done":
            captured["final_text"] = evt.get("final_text")
            captured["n_steps"] = evt.get("n_steps", 0)
            captured["n_blocked"] = evt.get("n_blocked", 0)
            captured["n_failed"] = evt.get("n_failed", 0)
        job.queue.put(evt)

    quiet_console = Console(quiet=True)
    try:
        agent.ask(
            prompt, targets, settings=settings, console=quiet_console,
            backend=backend, dry_run=dry_run, on_event=emit,
            prior_turns=prior_turns,
        )
    except Exception as exc:  # noqa: BLE001
        emit({"type": "error", "message": str(exc)})
    else:
        # conversation turn 기록 (정상 완료 시)
        if conversation_id and conversation_id in CONVERSATIONS:
            CONVERSATIONS[conversation_id].turns.append(
                {
                    "prompt": prompt,
                    "final_text": captured["final_text"] or "",
                    "n_steps": captured["n_steps"],
                    "n_blocked": captured["n_blocked"],
                    "n_failed": captured["n_failed"],
                    "dry_run": dry_run,
                }
            )
    finally:
        emit({"type": "_eof"})
        job.done = True


@app.post("/ask", response_class=HTMLResponse)
def ask_post(
    prompt: Annotated[str, Form()],
    targets: Annotated[list[str], Form()] = [],
    backend: Annotated[str, Form()] = "",
    dry_run: Annotated[str, Form()] = "",
    conversation_id: Annotated[str, Form()] = "",
) -> str:
    """ask 폼 POST — job 시작 후 SSE 스트리밍 페이지 렌더.

    conversation_id 가 주어지면 follow-up 으로 처리: 기존 conversation 의 이전 turn 들을
    LLM 프롬프트에 컨텍스트로 전달하고, 페이지에 누적 turn 을 함께 렌더.
    """
    settings = load_settings()
    inventory = load_inventory(settings.inventory_path)

    _cleanup_old_jobs()

    # follow-up 인 경우 기존 conversation 의 targets 를 그대로 사용 (UI 단순화)
    conv: Conversation | None = None
    if conversation_id and conversation_id in CONVERSATIONS:
        conv = CONVERSATIONS[conversation_id]
        if not targets:
            targets = list(conv.targets)

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

    # conversation 생성 또는 활용
    if conv is None:
        conv = Conversation(
            id=uuid.uuid4().hex,
            targets=list(targets),
            backend_name=sel_backend.name,
        )
        CONVERSATIONS[conv.id] = conv
    prior_turns = list(conv.turns)  # 시작 시점 snapshot

    job = Job(id=uuid.uuid4().hex)
    JOBS[job.id] = job

    Thread(
        target=_run_ask_job,
        args=(job, prompt, selected_servers, settings, sel_backend, is_dry),
        kwargs={"conversation_id": conv.id, "prior_turns": prior_turns},
        daemon=True,
    ).start()

    # SSE 스트리밍 페이지 렌더 (브라우저는 즉시 페이지 받고 EventSource 로 구독)
    targets_html = " ".join(f"<code>{_e(t)}</code>" for t in targets)
    # 이전 turn 누적 표시
    prior_html = ""
    for i, t in enumerate(prior_turns, 1):
        style = "ok" if t.get("n_failed", 0) == 0 and t.get("n_blocked", 0) == 0 else "warn"
        prior_html += f"""
        <section class='card turn-prior'>
          <h2>Turn {i} <span class='muted' style='font-weight:normal'>· steps={t.get('n_steps', 0)} blocked={t.get('n_blocked', 0)} failed={t.get('n_failed', 0)}</span></h2>
          <h3>프롬프트</h3>
          <pre>{_e(t.get('prompt', ''))}</pre>
          <h3>답변</h3>
          <div class='panel {style}'><pre>{_e(t.get('final_text', ''))}</pre></div>
        </section>
        """

    current_turn_no = len(prior_turns) + 1
    body = f"""
    {prior_html}
    <section class='card'>
      <h2>Turn {current_turn_no} <span class='muted' style='font-weight:normal'>· {_e(sel_backend.name)}</span></h2>
      <div class='kv'>
        servers: {targets_html}
        &nbsp;·&nbsp; dry-run: <b>{'yes' if is_dry else 'no'}</b>
        &nbsp;·&nbsp; conv: <code>{_e(conv.id)}</code>
        &nbsp;·&nbsp; job: <code>{_e(job.id)}</code>
      </div>
      <h3>프롬프트</h3>
      <pre>{_e(prompt)}</pre>
      <h3>진행 <span class='muted' id='status-badge' style='font-weight:normal'>· 시작 중…</span></h3>
      <div id='log'></div>
      <h3 style='margin-top:1rem'>답변</h3>
      <div id='answer' class='panel'><i class='muted'>(생성 중…)</i></div>
    </section>

    <section class='card' id='followup-card' style='opacity:.55'>
      <h2>이어서 질문 <span class='muted' style='font-weight:normal'>· 같은 대상 서버 + 이전 답변 컨텍스트 유지</span></h2>
      <form method='POST' action='/ask' id='followup-form'>
        <input type='hidden' name='conversation_id' value='{_e(conv.id)}'>
        <div class='row'>
          <textarea name='prompt' id='followup-prompt' placeholder='이전 답변에 대해 추가 질문…' disabled></textarea>
        </div>
        <div class='row'>
          <button type='submit' id='followup-btn' disabled>이어서 질문</button>
          &nbsp;<a href='/' class='btn-link'>새 ask 작성</a>
        </div>
      </form>
    </section>

    <script>
    (function() {{
      var src = new EventSource('/ask/stream/{job.id}');
      var log = document.getElementById('log');
      var answer = document.getElementById('answer');
      var statusBadge = document.getElementById('status-badge');
      function appendRow(icon, text, klass) {{
        var p = document.createElement('div');
        p.className = 'event-row ' + (klass || '');
        p.innerHTML = '<span class="icon">' + icon + '</span>' + text;
        log.appendChild(p);
        log.scrollTop = log.scrollHeight;
      }}
      function escapeHtml(s) {{
        var d = document.createElement('div');
        d.appendChild(document.createTextNode(s == null ? '' : String(s)));
        return d.innerHTML;
      }}
      function enableFollowup() {{
        var card = document.getElementById('followup-card');
        var input = document.getElementById('followup-prompt');
        var btn = document.getElementById('followup-btn');
        if (card) card.style.opacity = '1';
        if (input) {{ input.disabled = false; input.focus(); }}
        if (btn) btn.disabled = false;
      }}
      src.onmessage = function(ev) {{
        try {{
          var e = JSON.parse(ev.data);
          switch (e.type) {{
            case 'started':
              statusBadge.textContent = '· 진행 중';
              appendRow('▸', 'started — backend <b>' + escapeHtml(e.backend) + '</b>'); break;
            case 'plan_start':
              appendRow('…', 'planning'); break;
            case 'plan_done':
              appendRow('✓', 'plan ready (' + e.n_steps + ' step' + (e.n_steps === 1 ? '' : 's') + ')', 'evt-ok'); break;
            case 'step_start':
              appendRow('→', '<b>' + escapeHtml((e.servers || []).join(', ')) + '</b> :: <code>' + escapeHtml(e.command) + '</code>');
              break;
            case 'step_result':
              var ok = e.exit_code === 0;
              appendRow(ok ? '✓' : '✗',
                escapeHtml(e.server) + ' exit=' + escapeHtml(e.exit_code) +
                (e.stdout_preview ? ' <span class="muted">— ' + escapeHtml(e.stdout_preview.split('\\n')[0].slice(0, 80)) + '</span>' : ''),
                ok ? 'evt-ok' : 'evt-failed'); break;
            case 'step_blocked':
              appendRow('⊘', 'blocked (' + escapeHtml(e.reason) + '): <code>' + escapeHtml(e.command) + '</code>', 'evt-blocked'); break;
            case 'step_failed':
              appendRow('✗', 'failed — ' + escapeHtml(e.reason), 'evt-failed'); break;
            case 'step_dry_run':
              appendRow('∘', 'dry-run: <code>' + escapeHtml(e.command) + '</code>'); break;
            case 'summarize_start':
              appendRow('…', 'summarizing'); break;
            case 'done':
              answer.innerHTML = '<pre>' + escapeHtml(e.final_text) + '</pre>';
              answer.classList.add(e.n_failed === 0 && e.n_blocked === 0 ? 'ok' : 'warn');
              appendRow('✓', '<b>done</b> — steps=' + e.n_steps + ' blocked=' + e.n_blocked + ' failed=' + e.n_failed, 'evt-ok');
              statusBadge.textContent = '· 완료';
              enableFollowup(); break;
            case 'error':
              answer.innerHTML = '<pre>error: ' + escapeHtml(e.message) + '</pre>';
              answer.classList.add('err');
              appendRow('✗', '<b>error</b> — ' + escapeHtml(e.message), 'evt-failed');
              statusBadge.textContent = '· 에러';
              enableFollowup(); break;
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


# --- 인벤토리 CRUD --------------------------------------------------------------

import re as _re

_NAME_RE = _re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]*$")
_VALID_AUTH = ("agent", "pem", "password")
_VALID_ROLE = ("read-only", "shell", "sudo")


def _server_form_html(
    *,
    inventory: dict[str, Server],
    initial: Server | None = None,
    errors: list[str] | None = None,
    is_edit: bool = False,
) -> str:
    """Server 추가/편집 폼 HTML. initial 가 있으면 prefill."""
    s = initial
    name_v = s.name if s else ""
    host_v = s.host if s else ""
    port_v = str(s.port) if s else "22"
    user_v = s.user if s else ""
    auth_v = s.auth if s else "agent"
    pem_v = s.pem_path or "" if s else ""
    pwenv_v = s.password_env or "" if s else ""
    role_v = s.role if s else "read-only"
    tags_v = ", ".join(s.tags) if s and s.tags else ""
    jump_v = s.jump or "" if s else ""

    auth_opts = "".join(
        f"<option value='{a}'{' selected' if a == auth_v else ''}>{a}</option>"
        for a in _VALID_AUTH
    )
    role_opts = "".join(
        f"<option value='{r}'{' selected' if r == role_v else ''}>{r}</option>"
        for r in _VALID_ROLE
    )
    # jump 후보: 자기 자신 제외한 모든 server
    jump_candidates = [n for n in inventory if not (s and n == s.name)]
    jump_opts = "<option value=''>(없음 — 직접 연결)</option>" + "".join(
        f"<option value='{_e(n)}'{' selected' if n == jump_v else ''}>{_e(n)}</option>"
        for n in jump_candidates
    )

    title = "서버 편집" if is_edit else "서버 추가"
    name_field = (
        f"<input type='text' value='{_e(name_v)}' disabled> "
        f"<input type='hidden' name='name' value='{_e(name_v)}'>"
        if is_edit
        else f"<input type='text' name='name' value='{_e(name_v)}' required pattern='[A-Za-z0-9_][A-Za-z0-9_.\\-]*' placeholder='예: web-1'>"
    )
    name_help = (
        "<p class='muted' style='font-size:.78rem;margin:.25rem 0 0'>name 은 변경할 수 없습니다 (jump 참조 보호).</p>"
        if is_edit
        else "<p class='muted' style='font-size:.78rem;margin:.25rem 0 0'>영숫자/_/-/. 만 사용. 예: web-1, db.stage.</p>"
    )

    errors_html = ""
    if errors:
        items = "".join(f"<li>{_e(err)}</li>" for err in errors)
        errors_html = f"<div class='error-list'><b>입력 오류</b><ul>{items}</ul></div>"

    action_url = f"/servers/{_e(name_v)}" if is_edit else "/servers"
    return f"""
    <section class='card'>
      <h2>{title}</h2>
      {errors_html}
      <form method='POST' action='{action_url}'>
        <div class='row'>
          <label class='label-block'>이름 <span class='muted'>(서버 식별자)</span></label>
          {name_field}
          {name_help}
        </div>

        <div class='row row-inline'>
          <div>
            <label class='label-block'>호스트</label>
            <input type='text' name='host' value='{_e(host_v)}' required placeholder='10.0.1.5 또는 example.internal'>
          </div>
          <div style='flex:0 0 130px'>
            <label class='label-block'>포트</label>
            <input type='number' name='port' value='{_e(port_v)}' min='1' max='65535'>
          </div>
          <div>
            <label class='label-block'>사용자</label>
            <input type='text' name='user' value='{_e(user_v)}' required placeholder='ec2-user / root / ops'>
          </div>
        </div>

        <div class='row row-inline'>
          <div>
            <label class='label-block'>인증 방식</label>
            <select name='auth'>{auth_opts}</select>
            <p class='muted' style='font-size:.78rem;margin:.25rem 0 0'>agent = ssh-agent · pem = 키 파일 · password = .env 변수</p>
          </div>
          <div>
            <label class='label-block'>pem 파일 경로 <span class='muted'>(auth=pem)</span></label>
            <input type='text' name='pem_path' value='{_e(pem_v)}' placeholder='secrets/web-1.pem'>
          </div>
          <div>
            <label class='label-block'>비밀번호 환경변수 <span class='muted'>(auth=password)</span></label>
            <input type='text' name='password_env' value='{_e(pwenv_v)}' placeholder='CLOPSCTL_LEGACY_PASSWORD'>
          </div>
        </div>

        <div class='row row-inline'>
          <div>
            <label class='label-block'>role</label>
            <select name='role'>{role_opts}</select>
            <p class='muted' style='font-size:.78rem;margin:.25rem 0 0'>read-only=조회만, shell=일반, sudo=전체</p>
          </div>
          <div>
            <label class='label-block'>jump 서버 <span class='muted'>(선택)</span></label>
            <select name='jump'>{jump_opts}</select>
            <p class='muted' style='font-size:.78rem;margin:.25rem 0 0'>bastion 경유 시 선택. 최대 1단계 chain (총 2 hop).</p>
          </div>
        </div>

        <div class='row'>
          <label class='label-block'>tags <span class='muted'>(콤마 구분)</span></label>
          <input type='text' name='tags' value='{_e(tags_v)}' placeholder='prod, web, kr'>
        </div>

        <div class='row'>
          <button type='submit'>{'저장' if is_edit else '추가'}</button>
          &nbsp;<a href='/' class='btn-link'>취소</a>
        </div>
      </form>
    </section>
    """


def _validate_server_input(
    form: dict[str, str],
    inventory: dict[str, Server],
    *,
    is_edit: bool,
) -> tuple[Server | None, list[str]]:
    """폼 dict 를 검증하고 (Server | None, errors) 반환."""
    errors: list[str] = []
    name = (form.get("name") or "").strip()
    host = (form.get("host") or "").strip()
    user = (form.get("user") or "").strip()
    port_raw = (form.get("port") or "22").strip()
    auth = (form.get("auth") or "agent").strip()
    pem_path = (form.get("pem_path") or "").strip() or None
    password_env = (form.get("password_env") or "").strip() or None
    role = (form.get("role") or "read-only").strip()
    tags_raw = (form.get("tags") or "").strip()
    jump = (form.get("jump") or "").strip() or None

    if not name:
        errors.append("name 이 비어있습니다.")
    elif not _NAME_RE.match(name):
        errors.append(f"name '{name}' 은 영숫자·_·-·. 만 허용합니다.")
    elif not is_edit and name in inventory:
        errors.append(f"name '{name}' 가 이미 존재합니다.")

    if not host:
        errors.append("host 가 비어있습니다.")
    if not user:
        errors.append("user 가 비어있습니다.")

    try:
        port = int(port_raw)
        if not (1 <= port <= 65535):
            raise ValueError
    except ValueError:
        errors.append(f"port '{port_raw}' 가 1-65535 범위를 벗어납니다.")
        port = 22

    if auth not in _VALID_AUTH:
        errors.append(f"auth '{auth}' 는 {list(_VALID_AUTH)} 중 하나여야 합니다.")
    if role not in _VALID_ROLE:
        errors.append(f"role '{role}' 는 {list(_VALID_ROLE)} 중 하나여야 합니다.")

    if auth == "pem" and not pem_path:
        errors.append("auth=pem 인 경우 pem_path 가 필요합니다.")
    if auth == "password" and not password_env:
        errors.append("auth=password 인 경우 password_env (환경변수 이름) 가 필요합니다.")

    tags: tuple[str, ...] = tuple(t.strip() for t in tags_raw.split(",") if t.strip())

    if jump:
        if jump == name:
            errors.append("jump 가 자기 자신을 가리킵니다.")
        elif jump not in inventory:
            errors.append(f"jump '{jump}' 는 인벤토리에 없는 서버입니다.")

    if errors:
        return None, errors

    server = Server(
        name=name, host=host, user=user, port=port, auth=auth,  # type: ignore[arg-type]
        pem_path=pem_path, password_env=password_env, role=role,  # type: ignore[arg-type]
        tags=tags, jump=jump,
    )

    # cycle / 깊이 검증 — 미리 인벤토리에 넣고 _resolve_jump_chain 호출
    new_inv = dict(inventory)
    new_inv[name] = server
    try:
        _resolve_jump_chain(server, new_inv)
    except ValueError as exc:
        errors.append(f"jump 검증 실패: {exc}")
        return None, errors

    return server, []


@app.get("/servers/new", response_class=HTMLResponse)
def servers_new() -> str:
    settings = load_settings()
    inventory = load_inventory(settings.inventory_path)
    body = _server_form_html(inventory=inventory, is_edit=False)
    return _layout("clopsctl — 서버 추가", body)


@app.post("/servers", response_class=HTMLResponse)
async def servers_create(request: Request) -> str:
    form_data = await request.form()
    form = {k: str(v) for k, v in form_data.items()}
    return _handle_server_create_or_update(form, name_path=None)


@app.get("/servers/{name}/edit", response_class=HTMLResponse)
def servers_edit(name: str) -> str:
    settings = load_settings()
    inventory = load_inventory(settings.inventory_path)
    if name not in inventory:
        raise HTTPException(status_code=404, detail=f"server '{name}' not found")
    body = _server_form_html(inventory=inventory, initial=inventory[name], is_edit=True)
    return _layout(f"clopsctl — {name} 편집", body)


@app.post("/servers/{name}", response_class=HTMLResponse)
async def servers_update(name: str, request: Request) -> str:
    form_data = await request.form()
    form = {k: str(v) for k, v in form_data.items()}
    form["name"] = name  # path 의 name 강제 (편집 시 변경 금지)
    return _handle_server_create_or_update(form, name_path=name)


def _handle_server_create_or_update(form: dict[str, str], name_path: str | None) -> str:
    settings = load_settings()
    inventory = load_inventory(settings.inventory_path)
    is_edit = name_path is not None

    if is_edit and name_path not in inventory:
        raise HTTPException(status_code=404, detail=f"server '{name_path}' not found")

    # 검증 시 자기 자신은 jump 후보에서 제외 — 편집인 경우 inventory 에서 빼고 새로 검증
    inv_for_check = {k: v for k, v in inventory.items() if k != name_path} if is_edit else inventory
    server, errors = _validate_server_input(form, inv_for_check, is_edit=is_edit)

    if errors or server is None:
        # 폼 다시 렌더 (값 prefill)
        try:
            initial = Server(
                name=form.get("name", "") or "",
                host=form.get("host", "") or "",
                user=form.get("user", "") or "",
                port=int(form.get("port", "22") or 22),
                auth=form.get("auth", "agent") or "agent",  # type: ignore[arg-type]
                pem_path=(form.get("pem_path") or None),
                password_env=(form.get("password_env") or None),
                role=form.get("role", "read-only") or "read-only",  # type: ignore[arg-type]
                tags=tuple(t.strip() for t in (form.get("tags", "") or "").split(",") if t.strip()),
                jump=(form.get("jump") or None),
            )
        except (ValueError, TypeError):
            initial = inventory.get(name_path) if is_edit else None
        body = _server_form_html(inventory=inventory, initial=initial, errors=errors, is_edit=is_edit)
        return _layout("clopsctl — 입력 오류", body)

    inventory[server.name] = server
    write_inventory(settings.inventory_path, inventory)

    body = (
        f"<section class='card'><h2>{'편집됨' if is_edit else '추가됨'}</h2>"
        f"<p>server <code>{_e(server.name)}</code> 가 인벤토리에 저장되었습니다.</p>"
        f"<p><a href='/' class='btn-link btn-primary'>← 인벤토리 보기</a></p></section>"
    )
    return _layout("clopsctl — saved", body)


@app.get("/servers/{name}/delete", response_class=HTMLResponse)
def servers_delete_confirm(name: str) -> str:
    settings = load_settings()
    inventory = load_inventory(settings.inventory_path)
    if name not in inventory:
        raise HTTPException(status_code=404, detail=f"server '{name}' not found")

    # 다른 서버가 jump 로 참조 중인지 확인
    referenced_by = [n for n, s in inventory.items() if s.jump == name]
    blocked = bool(referenced_by)

    body = f"""
    <section class='card'>
      <h2>서버 삭제</h2>
      <p><code>{_e(name)}</code> 를 인벤토리에서 삭제합니다. 이 작업은 되돌릴 수 없습니다.</p>
      {('<div class="error-list"><b>차단됨</b><ul><li>다음 서버들이 jump 로 이 서버를 참조 중입니다: '
        + ', '.join(f'<code>{_e(n)}</code>' for n in referenced_by)
        + '. 먼저 해당 서버들의 jump 를 변경하거나 삭제하세요.</li></ul></div>') if blocked else ''}
      <form method='POST' action='/servers/{_e(name)}/delete'>
        <p style='margin-top:1rem'>
          {('<button type="submit" disabled>삭제 (차단됨)</button>' if blocked else '<button type="submit" class="btn-primary" style="background:var(--err)">삭제 확인</button>')}
          &nbsp;<a href='/' class='btn-link'>취소</a>
        </p>
      </form>
    </section>
    """
    return _layout(f"clopsctl — {name} 삭제", body)


@app.post("/servers/{name}/delete", response_class=HTMLResponse)
def servers_delete(name: str) -> str:
    settings = load_settings()
    inventory = load_inventory(settings.inventory_path)
    if name not in inventory:
        raise HTTPException(status_code=404, detail=f"server '{name}' not found")

    referenced_by = [n for n, s in inventory.items() if s.jump == name]
    if referenced_by:
        body = (
            f"<section class='card'><h2>삭제 차단됨</h2>"
            f"<div class='error-list'>다음 서버들이 jump 로 참조 중입니다: "
            f"{', '.join(f'<code>{_e(n)}</code>' for n in referenced_by)}.</div>"
            f"<p><a href='/' class='btn-link'>← 돌아가기</a></p></section>"
        )
        return _layout("clopsctl — delete blocked", body)

    del inventory[name]
    write_inventory(settings.inventory_path, inventory)
    body = (
        f"<section class='card'><h2>삭제됨</h2>"
        f"<p>server <code>{_e(name)}</code> 가 인벤토리에서 제거되었습니다.</p>"
        f"<p><a href='/' class='btn-link btn-primary'>← 인벤토리 보기</a></p></section>"
    )
    return _layout("clopsctl — deleted", body)
