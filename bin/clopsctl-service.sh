#!/usr/bin/env bash
# clopsctl 웹 UI 를 macOS launchd LaunchAgent 로 상시 서비스화.
#
# 로그인 시 자동 기동(RunAtLoad) + 비정상 종료 시 자동 재시작(KeepAlive).
# plist 는 이 스크립트가 절대경로를 채워 생성한다 (편집 불필요).
#
# 사용:
#   ./bin/clopsctl-service.sh install     # plist 생성 + 등록 + 기동
#   ./bin/clopsctl-service.sh start       # 기동(load)
#   ./bin/clopsctl-service.sh stop        # 중지(unload) — KeepAlive 무시하고 완전 정지
#   ./bin/clopsctl-service.sh restart     # 재기동
#   ./bin/clopsctl-service.sh status      # 상태 + healthz 확인
#   ./bin/clopsctl-service.sh logs        # 로그 tail -f
#   ./bin/clopsctl-service.sh uninstall   # 중지 + plist 삭제
#
# 비-macOS(Linux systemd) 는 docs/service.md 참고.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

LABEL="com.clopsctl.web"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"
SERVICE_TARGET="$DOMAIN/$LABEL"
RUN_DIR="$ROOT_DIR/.run"
LOG_FILE="$RUN_DIR/web.log"

# clopsctl 진입점 (.env 를 내부에서 로드 → 인벤토리/비밀번호/바인드 host·port 적용)
CLOPSCTL_BIN="$ROOT_DIR/.venv/bin/clopsctl"

# .env 에서 host/port 읽기 (상태 출력용)
read_env() {
  key="$1"; default="$2"
  if [ -f "$ROOT_DIR/.env" ]; then
    val="$(grep -E "^[[:space:]]*${key}=" "$ROOT_DIR/.env" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"' | tr -d "'" | xargs 2>/dev/null || true)"
    [ -n "${val:-}" ] && { echo "$val"; return; }
  fi
  echo "$default"
}
HOST="$(read_env CLOPSCTL_WEB_HOST 127.0.0.1)"
PORT="$(read_env CLOPSCTL_WEB_PORT 8765)"

[ "$(uname -s)" = "Darwin" ] || {
  echo "✗ 이 스크립트는 macOS(launchd) 전용입니다. Linux 는 docs/service.md(systemd) 참고." >&2
  exit 1
}

if [ ! -x "$CLOPSCTL_BIN" ]; then
  echo "✗ $CLOPSCTL_BIN 가 없습니다. 먼저 ./scripts/install.sh 를 실행하세요." >&2
  exit 1
fi

write_plist() {
  mkdir -p "$RUN_DIR" "$HOME/Library/LaunchAgents"
  # LaunchAgent 는 최소 PATH 를 받으므로, claude CLI(~/.local/bin)·brew(/opt/homebrew/bin)
  # 경로를 명시한다. ask(LLM) 가 claude 를 subprocess 로 호출하기 때문.
  cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$CLOPSCTL_BIN</string>
    <string>web</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$ROOT_DIR</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>HOME</key>
    <string>$HOME</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <dict>
    <key>SuccessfulExit</key>
    <false/>
  </dict>
  <key>StandardOutPath</key>
  <string>$LOG_FILE</string>
  <key>StandardErrorPath</key>
  <string>$LOG_FILE</string>
</dict>
</plist>
PLIST_EOF
  echo "✓ plist 작성: $PLIST"
}

is_loaded() { launchctl print "$SERVICE_TARGET" >/dev/null 2>&1; }

do_start() {
  [ -f "$PLIST" ] || { echo "✗ plist 없음 — 먼저 'install' 을 실행하세요." >&2; exit 1; }
  if is_loaded; then
    echo "→ 이미 등록됨 — kickstart 로 재기동."
    launchctl kickstart -k "$SERVICE_TARGET"
  else
    launchctl bootstrap "$DOMAIN" "$PLIST"
    echo "✓ 기동(load) 완료."
  fi
  show_url
}

do_stop() {
  if is_loaded; then
    launchctl bootout "$SERVICE_TARGET" 2>/dev/null || launchctl bootout "$DOMAIN" "$PLIST" 2>/dev/null || true
    echo "✓ 중지(unload) 완료 — KeepAlive 와 무관하게 완전 정지."
  else
    echo "· 등록돼 있지 않음 (이미 정지)."
  fi
}

show_url() {
  echo "  URL : http://$HOST:$PORT"
  echo "  로그: $LOG_FILE   ( ./bin/clopsctl-service.sh logs )"
}

case "${1:-}" in
  install)
    write_plist
    do_stop  # 기존 등록이 있으면 정리 후 재등록
    launchctl bootstrap "$DOMAIN" "$PLIST"
    echo "✓ 등록 완료 — 로그인 시 자동 기동 + 크래시 시 자동 재시작."
    show_url
    ;;
  start)   do_start ;;
  stop)    do_stop ;;
  restart)
    if is_loaded; then launchctl kickstart -k "$SERVICE_TARGET"; echo "✓ 재기동."; else do_start; fi
    show_url
    ;;
  status)
    if is_loaded; then
      echo "● 등록됨 ($SERVICE_TARGET)"
      launchctl print "$SERVICE_TARGET" 2>/dev/null | grep -E "state =|pid =|program =|last exit" | sed 's/^/  /' || true
    else
      echo "○ 미등록/정지"
    fi
    echo "--- healthz ---"
    curl -fsS "http://$HOST:$PORT/healthz" 2>/dev/null && echo || echo "(응답 없음)"
    ;;
  logs)
    [ -f "$LOG_FILE" ] || { echo "로그 없음: $LOG_FILE"; exit 0; }
    tail -f "$LOG_FILE"
    ;;
  uninstall)
    do_stop
    [ -f "$PLIST" ] && rm -f "$PLIST" && echo "✓ plist 삭제: $PLIST" || true
    ;;
  -h|--help|"")
    grep '^#' "$0" | sed 's/^# \{0,1\}//'
    ;;
  *)
    echo "알 수 없는 명령: $1 (install|start|stop|restart|status|logs|uninstall)" >&2
    exit 2
    ;;
esac
