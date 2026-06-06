# clopsctl OS 호환성 노트

> Linux 우선 개발·검증. macOS / Windows 동작은 아래 가이드라인과 알려진 차이점을 참고.

## 마스터 머신 (clopsctl 가 도는 곳)

### 공통 요건
- Python 3.11+ (TOML stdlib `tomllib`, `Annotated[..., Form()]` 등 의존)
- 사용자 PATH 에 `claude` / `gemini` / `codex` 중 **하나 이상** 설치·인증
- 인터넷 (LLM CLI 가 외부 API 호출)
- SSH agent 가 떠있고 키가 추가돼 있을 것 (또는 `auth=pem`/`password` 사용)

### Linux (1차 지원, 검증됨)
- 표준 OpenSSH `sshd`/`ssh-agent`
- `chmod 600 .env` / `chmod 700 secrets/` 가 권장 + 실행 가능
- paramiko 의 `look_for_keys=True` 가 `~/.ssh/` 자동 탐지
- 본 프로젝트의 모든 단위/스모크 테스트는 Linux (Ubuntu 22.04+) 에서 검증

### macOS (2차 지원, 코드 호환)
- Apple Silicon / Intel 모두 동작 가정 (paramiko, FastAPI 등 휠 제공)
- ssh-agent 는 macOS Keychain 통합 — `ssh-add --apple-load-keychain` 또는 `~/.ssh/config` 의 `UseKeychain yes` 권장
- `chmod` 동일하게 작동
- 알려진 차이: Rosetta + arm64 혼합 환경에서 paramiko cryptography wheel 빌드가 까다로울 수 있음 → `pip install --upgrade pip wheel` 후 재시도

### Windows (3차 지원, 추가 점검 필요)
- WSL2 사용을 가장 강력히 권장 — 안에서는 Linux 와 동일하게 동작
- 네이티브 Windows 사용 시 알려진 차이:
  - `chmod 600 .env` 는 동작 안 함. NTFS ACL 로 운영자 외 읽기 차단 필요
    (`icacls .env /inheritance:r /grant:r %USERNAME%:R`)
  - ssh-agent 는 OpenSSH for Windows 의 `ssh-agent.exe` (서비스 시작 필요) 또는 Pageant 사용
  - 경로 separator: 인벤토리 `pem_path` 는 슬래시(`/`) 사용 권장 (paramiko 가 정규화)
  - PowerShell 실행 정책으로 `clopsctl.exe` entry script 실행이 막힐 수 있음 → `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`
  - **검증 미완료** — 실제 PoC 후 issue 로 보고 환영

## 원격 서버 (SSH 대상)

마스터에서 실행되는 명령은 결국 원격 OS 의 셸에서 동작. 권한 모드 allowlist (`read-only` allowlist) 는 Linux 명령(`df`, `journalctl`, `systemctl` …) 가정.

### Linux 원격 — 1차 지원
모든 allowlist 명령이 표준.

### macOS 원격
- `journalctl` 없음 → `log show` 사용 (allowlist 에 미포함, role 을 `shell` 로 두거나 추후 macOS 별 allowlist 추가)
- `systemctl` 대신 `launchctl` (현재 차단되지 않음 — sudo/shell role 에서만 사용)

### Windows 원격
- OpenSSH Server 설치 시 동작. cmd/PowerShell 명령은 우리 read-only allowlist 와 거의 일치 안 함 — 권장: `role=shell` 로 두고 명시적 명령만 사용

## 알려진 한계

- LLM CLI subprocess timeout 기본 120s — 대형 cluster 는 늘려야 할 수 있음 (`llm.CLIBackend.invoke(..., timeout=N)` 직접 호출 시)
- in-memory job dict (web SSE) 는 단일 프로세스 가정. multi-worker uvicorn 으로 띄우면 SSE 가 깨짐 — `--workers 1` 유지 권장
- pem 파일은 마스터 로컬에만 보관 — pem 자체는 원격에 절대 업로드되지 않지만, `chmod 600` 권한은 OS 가 책임짐

## 검증 매트릭스 (2026-04 기준)

| 환경 | 단위 테스트 | exec 스모크 | ask 스모크 | web SSE | install/start/stop |
| --- | --- | --- | --- | --- | --- |
| Ubuntu 22.04 + Python 3.12 | ✅ | ✅ | ✅ | ✅ | ✅ (JAE-107) |
| macOS 14 (arm64) | 미검증 | 미검증 | 미검증 | 미검증 | 미검증 (스크립트는 portable 작성) |
| Windows 11 (WSL2) | 미검증 (Linux와 동일 예상) | — | — | — | 미검증 (Linux와 동일 예상) |
| Windows 11 native | 미검증 | — | — | — | 미지원 (.sh — WSL2 권장) |

호환성 검증 결과는 PR 또는 issue 로 본 표를 갱신해 주시면 좋겠습니다.

## 빠른 시작 스크립트 (JAE-107)

`scripts/install.sh`, `bin/clopsctl-start.sh`, `bin/clopsctl-stop.sh` 는 macOS / Linux 공용으로
작성했다 (검증된 portability 포인트):

- `#!/usr/bin/env bash`, bash 3.2 호환 (macOS 기본 셸) — 연관배열·`${var,,}` 등 bash4 기능 미사용
- 스크립트 루트 해석에 `readlink -f` (macOS 미지원) 대신 `cd "$(dirname …)" && pwd` 사용
- 포트 점검은 `lsof`(mac/linux 공통) 우선, 없으면 venv python 소켓 fallback
- `chmod` 실패는 경고만 (Windows/특수 FS 대비), venv 경로는 `bin/`·`Scripts/` 양쪽 탐색

**Linux(Ubuntu 22.04, Python 3.12) 실검증 완료** — install → start(HTTP 200) → idempotent
재기동 → 포트 충돌 감지 → `--restart` → graceful stop 전 경로 통과.

**macOS 실검증은 Mac 하드웨어 필요로 미완료** — 위 매트릭스의 macOS install/start/stop 칸은
실제 Mac 에서 1회 스모크 후 PR/issue 로 갱신 필요. 부팅 자동 기동은 [service.md](service.md) 의
launchd LaunchAgent 템플릿 참고.
