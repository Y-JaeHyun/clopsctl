#!/usr/bin/env bash
# clopsctl 웹 UI 백그라운드 기동 — nohup uvicorn + PID 파일.
#
# - PID 파일:  .run/web.pid
# - 로그:      .run/web.log
# - 포트 충돌 감지 후 기동 (이미 우리 PID 가 살아있으면 idempotent: 그대로 둠)
# - host/port 는 .env(CLOPSCTL_WEB_HOST/PORT) → 인자/환경변수 순으로 override
#
# 사용:
#   ./bin/clopsctl-start.sh                 # 기본 (127.0.0.1:8765)
#   ./bin/clopsctl-start.sh --port 9000
#   ./bin/clopsctl-start.sh --host 0.0.0.0  # 외부 노출 (보안 주의)
#   ./bin/clopsctl-start.sh --restart       # 강제 재기동
#
# macOS / Linux 공용 (bash 3.2+, lsof/python 으로 포트 점검).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR"

RUN_DIR="$ROOT_DIR/.run"
PID_FILE="$RUN_DIR/web.pid"
LOG_FILE="$RUN_DIR/web.log"
mkdir -p "$RUN_DIR"

# ── venv 확인 ──
VENV_PY="$ROOT_DIR/.venv/bin/python"
[ -x "$VENV_PY" ] || VENV_PY="$ROOT_DIR/.venv/Scripts/python.exe"
if [ ! -x "$VENV_PY" ]; then
  echo "✗ .venv 가 없습니다. 먼저 ./scripts/install.sh 를 실행하세요." >&2
  exit 1
fi

# ── .env 에서 host/port 기본값 읽기 (단순 KEY=VALUE 파싱) ──
read_env() {
  key="$1"; default="$2"
  if [ -f "$ROOT_DIR/.env" ]; then
    val="$(grep -E "^[[:space:]]*${key}=" "$ROOT_DIR/.env" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"' | tr -d "'" | xargs 2>/dev/null || true)"
    if [ -n "${val:-}" ]; then echo "$val"; return; fi
  fi
  echo "$default"
}
HOST="$(read_env CLOPSCTL_WEB_HOST 127.0.0.1)"
PORT="$(read_env CLOPSCTL_WEB_PORT 8765)"
RESTART=0

# ── 인자 파싱 ──
while [ $# -gt 0 ]; do
  case "$1" in
    --host) HOST="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --restart) RESTART=1; shift ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "알 수 없는 옵션: $1" >&2; exit 2 ;;
  esac
done

# ── 살아있는 PID 인지 검사 ──
is_running() {
  [ -f "$PID_FILE" ] || return 1
  pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  [ -n "${pid:-}" ] || return 1
  kill -0 "$pid" 2>/dev/null
}

if is_running; then
  pid="$(cat "$PID_FILE")"
  if [ "$RESTART" -eq 1 ]; then
    echo "→ --restart: 기존 PID $pid 종료 중..."
    "$SCRIPT_DIR/clopsctl-stop.sh" || true
  else
    echo "✓ 이미 실행 중 (PID $pid) — idempotent, 그대로 둡니다."
    echo "  http://$HOST:$PORT  | 로그: $LOG_FILE  | 재기동: --restart"
    exit 0
  fi
fi
# stale PID 파일 정리
[ -f "$PID_FILE" ] && ! is_running && rm -f "$PID_FILE"

# ── 포트 충돌 감지 (lsof 우선, 없으면 python 소켓) ──
port_in_use() {
  if command -v lsof >/dev/null 2>&1; then
    lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1
  else
    "$VENV_PY" - "$HOST" "$PORT" <<'PY'
import socket, sys
host, port = sys.argv[1], int(sys.argv[2])
test = "127.0.0.1" if host in ("0.0.0.0", "::") else host
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(0.5)
sys.exit(0 if s.connect_ex((test, port)) == 0 else 1)
PY
  fi
}
if port_in_use; then
  echo "✗ 포트 $PORT 가 이미 사용 중입니다 (다른 프로세스). --port 로 변경하거나 점유 프로세스를 종료하세요." >&2
  command -v lsof >/dev/null 2>&1 && lsof -nP -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | sed 's/^/  /' >&2 || true
  exit 1
fi

# ── 외부 바인드 경고 ──
case "$HOST" in
  127.0.0.1|localhost|::1) : ;;
  *) echo "⚠ 외부 접근 가능한 호스트($HOST)에 바인드합니다. 인증이 없는 UI 이니 신뢰된 네트워크에서만 사용하세요." >&2 ;;
esac

# ── 기동: nohup uvicorn (단일 워커 — web SSE 가 in-memory job dict 가정) ──
echo "→ clopsctl web 기동: http://$HOST:$PORT"
nohup "$VENV_PY" -m uvicorn clopsctl.web:app \
  --host "$HOST" --port "$PORT" --workers 1 \
  >>"$LOG_FILE" 2>&1 &
NEW_PID=$!
echo "$NEW_PID" > "$PID_FILE"

# ── 기동 확인 (최대 ~5초) ──
ok=0
i=0
while [ "$i" -lt 25 ]; do
  if ! kill -0 "$NEW_PID" 2>/dev/null; then break; fi
  if port_in_use; then ok=1; break; fi
  sleep 0.2
  i=$((i + 1))
done

if [ "$ok" -eq 1 ]; then
  echo "✓ 기동 완료 (PID $NEW_PID)"
  echo "  URL : http://$HOST:$PORT"
  echo "  로그: $LOG_FILE"
  echo "  종료: ./bin/clopsctl-stop.sh"
else
  echo "✗ 기동 실패 — 로그 마지막 줄:" >&2
  tail -20 "$LOG_FILE" 2>/dev/null | sed 's/^/  /' >&2 || true
  rm -f "$PID_FILE"
  exit 1
fi
