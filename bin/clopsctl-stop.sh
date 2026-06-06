#!/usr/bin/env bash
# clopsctl 웹 UI 종료 — PID 파일 기반 graceful shutdown.
#
# - .run/web.pid 의 PID 에 SIGTERM → 최대 10초 대기 → 안 죽으면 SIGKILL
# - PID 파일이 없거나 이미 죽었으면 정상 종료(0)로 처리 (idempotent)
#
# 사용:
#   ./bin/clopsctl-stop.sh
#   ./bin/clopsctl-stop.sh --force   # 대기 없이 즉시 SIGKILL
#
# macOS / Linux 공용.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PID_FILE="$ROOT_DIR/.run/web.pid"

FORCE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --force) FORCE=1; shift ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "알 수 없는 옵션: $1" >&2; exit 2 ;;
  esac
done

if [ ! -f "$PID_FILE" ]; then
  echo "· PID 파일 없음 ($PID_FILE) — 이미 종료된 것으로 간주."
  exit 0
fi

PID="$(cat "$PID_FILE" 2>/dev/null || true)"
if [ -z "${PID:-}" ] || ! kill -0 "$PID" 2>/dev/null; then
  echo "· PID $PID 프로세스가 없음 — stale PID 파일 정리."
  rm -f "$PID_FILE"
  exit 0
fi

if [ "$FORCE" -eq 1 ]; then
  echo "→ SIGKILL $PID (--force)"
  kill -9 "$PID" 2>/dev/null || true
  rm -f "$PID_FILE"
  echo "✓ 강제 종료."
  exit 0
fi

echo "→ SIGTERM $PID (graceful)..."
kill -TERM "$PID" 2>/dev/null || true

# 최대 10초 대기
i=0
while [ "$i" -lt 50 ]; do
  if ! kill -0 "$PID" 2>/dev/null; then
    rm -f "$PID_FILE"
    echo "✓ 종료 완료 (PID $PID)."
    exit 0
  fi
  sleep 0.2
  i=$((i + 1))
done

echo "⚠ graceful 종료 실패 — SIGKILL 시도." >&2
kill -9 "$PID" 2>/dev/null || true
rm -f "$PID_FILE"
echo "✓ 강제 종료 (PID $PID)."
