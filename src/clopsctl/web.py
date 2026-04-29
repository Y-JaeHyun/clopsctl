"""웹 UI — Phase 2 1차 stub.

지금은 인벤토리/히스토리 조회만 노출. ask 실행 폼은 Phase 2 후속에서 추가.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from . import __version__
from .config import load_inventory, load_settings
from .history import search

app = FastAPI(title="clopsctl", version=__version__)


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    settings = load_settings()
    inventory = load_inventory(settings.inventory_path)
    rows = search(settings.history_db, limit=20)

    server_rows = "".join(
        f"<tr><td>{s.name}</td><td>{s.host}</td><td>{s.user}</td>"
        f"<td>{s.auth}</td><td>{s.role}</td><td>{', '.join(s.tags)}</td></tr>"
        for s in inventory.values()
    ) or "<tr><td colspan='6'><i>(empty)</i></td></tr>"

    history_rows = "".join(
        f"<tr><td>{r['id']}</td><td>{r['ts'][:19]}</td><td>{r['server']}</td>"
        f"<td>{r['mode']}</td><td>{r['exit_code'] if r['exit_code'] is not None else '-'}</td>"
        f"<td><code>{(r['command'] or r['prompt'] or '')[:80]}</code></td></tr>"
        for r in rows
    ) or "<tr><td colspan='6'><i>(empty)</i></td></tr>"

    return f"""
    <!doctype html>
    <html lang='ko'><head>
      <meta charset='utf-8'>
      <title>clopsctl {__version__}</title>
      <style>
        body {{ font-family: -apple-system, system-ui, sans-serif; margin: 2rem; max-width: 980px; }}
        h1 {{ margin: 0 0 .25rem 0; }}
        h2 {{ margin-top: 2rem; border-bottom: 1px solid #ddd; padding-bottom: .25rem; }}
        table {{ width: 100%; border-collapse: collapse; font-size: .9rem; }}
        th, td {{ text-align: left; padding: .35rem .6rem; border-bottom: 1px solid #eee; }}
        th {{ background: #fafafa; }}
        code {{ background: #f4f4f4; padding: .05rem .25rem; border-radius: 3px; }}
        .muted {{ color: #777; }}
      </style>
    </head><body>
      <h1>clopsctl <span class='muted'>{__version__}</span></h1>
      <p class='muted'>Master-side SSH fleet controller — read-only stub. ask 실행 폼은 Phase 2 후속.</p>

      <h2>Servers</h2>
      <table><thead><tr>
        <th>name</th><th>host</th><th>user</th><th>auth</th><th>role</th><th>tags</th>
      </tr></thead><tbody>{server_rows}</tbody></table>

      <h2>Recent history (last 20)</h2>
      <table><thead><tr>
        <th>id</th><th>ts (UTC)</th><th>server</th><th>mode</th><th>exit</th><th>cmd / prompt</th>
      </tr></thead><tbody>{history_rows}</tbody></table>
    </body></html>
    """
