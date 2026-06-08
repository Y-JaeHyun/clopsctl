# clopsctl 웹 UI 상시 기동 (systemd / launchd)

`bin/clopsctl-start.sh` / `bin/clopsctl-stop.sh` 는 수동 백그라운드 기동/종료용이다.
부팅 시 자동 기동이나 비정상 종료 시 자동 재시작이 필요하면 OS 서비스 매니저에 등록한다.

> ⚠ 이 UI 는 인증이 없고 SSH 명령 실행을 트리거할 수 있다. 반드시 `127.0.0.1` 바인드를
> 유지하고, 원격 접근이 필요하면 `ssh -L` 포트포워딩이나 VPN 을 사용한다. 시스템 전역
> 데몬(root)으로 띄우지 말고 **사용자 단위**(systemd `--user`, launchd `LaunchAgents`)로 운영한다.

아래 템플릿의 경로(`/home/USER/clopsctl`, `/Users/USER/clopsctl`)와 사용자명을 실제 값으로 바꾼다.

---

## Linux — systemd (`--user` 유닛)

`~/.config/systemd/user/clopsctl-web.service`:

```ini
[Unit]
Description=clopsctl web UI (localhost)
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/home/USER/clopsctl
# .env 의 CLOPSCTL_WEB_HOST/PORT 를 그대로 사용하려면 EnvironmentFile 사용
EnvironmentFile=-/home/USER/clopsctl/.env
ExecStart=/home/USER/clopsctl/.venv/bin/python -m uvicorn clopsctl.web:app \
  --host 127.0.0.1 --port 8765 --workers 1
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
```

등록 / 운영:

```bash
systemctl --user daemon-reload
systemctl --user enable --now clopsctl-web.service
systemctl --user status clopsctl-web.service
journalctl --user -u clopsctl-web.service -f      # 로그
systemctl --user restart clopsctl-web.service
systemctl --user disable --now clopsctl-web.service
```

로그아웃 후에도 계속 돌게 하려면 linger 활성화:

```bash
sudo loginctl enable-linger USER
```

---

## macOS — launchd (LaunchAgent)

`~/Library/LaunchAgents/com.clopsctl.web.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.clopsctl.web</string>

  <key>ProgramArguments</key>
  <array>
    <string>/Users/USER/clopsctl/.venv/bin/python</string>
    <string>-m</string>
    <string>uvicorn</string>
    <string>clopsctl.web:app</string>
    <string>--host</string><string>127.0.0.1</string>
    <string>--port</string><string>8765</string>
    <string>--workers</string><string>1</string>
  </array>

  <key>WorkingDirectory</key>
  <string>/Users/USER/clopsctl</string>

  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <dict>
    <key>SuccessfulExit</key>
    <false/>
  </dict>

  <key>StandardOutPath</key>
  <string>/Users/USER/clopsctl/.run/web.log</string>
  <key>StandardErrorPath</key>
  <string>/Users/USER/clopsctl/.run/web.log</string>
</dict>
</plist>
```

등록 / 운영 (macOS 11+ `launchctl` 권장 문법):

```bash
mkdir -p /Users/USER/clopsctl/.run
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.clopsctl.web.plist
launchctl print gui/$(id -u)/com.clopsctl.web        # 상태
launchctl kickstart -k gui/$(id -u)/com.clopsctl.web # 재시작
launchctl bootout gui/$(id -u)/com.clopsctl.web      # 중지·해제
```

(구버전 macOS 는 `launchctl load -w ...` / `launchctl unload -w ...` 사용.)

launchd 는 `.env` 를 자동 로드하지 않는다. host/port 외 환경변수가 필요하면 plist 의
`EnvironmentVariables` dict 에 명시하거나 `.venv/bin/clopsctl web` 진입점(내부에서 `.env` 로드)을
`ProgramArguments` 로 사용한다.
