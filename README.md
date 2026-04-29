# clopsctl

> 마스터(로컬) 한 대에서 다수 SSH 원격 서버를 자연어 + 단순 커맨드로 통합 관리. 원격 서버에는 AI 미설치 — sshd 만 가동.

- **언어**: Python 3.11+
- **CLI**: Typer / Rich
- **SSH**: paramiko (병렬 fan-out)
- **LLM**: Anthropic Claude (`claude-opus-4-7`) + `mcp-ssh-manager` (Node MCP 서버)
- **Web**: FastAPI (Phase 2)
- **히스토리**: SQLite append-only
- **상태**: 0.0.1 — 스캐폴드 + `exec` 모드 동작, `ask`/`web` 은 Phase 2

## 빠른 시작

### 1. 의존성

```bash
# Python 패키지
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

# Node MCP 서버 (ask 모드용, 마스터 전역 1회)
npm i -g mcp-ssh-manager
```

### 2. 설정

```bash
cp .env.example .env
chmod 600 .env
# .env 안에서 ANTHROPIC_API_KEY 등 채우기

cp inventory/servers.example.toml inventory/servers.toml
# 실서버 정보 입력. pem 은 secrets/ 또는 안전한 경로에
chmod 700 secrets 2>/dev/null || true
```

### 3. 사용

```bash
# 인벤토리 확인
clopsctl server list
clopsctl server check web-1

# 단일/멀티 서버에 명령 fan-out (LLM 비경유)
clopsctl exec web-1 -- "df -h"
clopsctl exec web-1,web-2,db-stage -- "uptime"

# 자연어 (Phase 2 — 현재 stub)
clopsctl ask web-1,web-2 "최근 1시간 5xx 비율 알려줘"

# 히스토리
clopsctl history --limit 30
clopsctl history --server web-1
clopsctl history --grep "디스크"

# 웹 UI (Phase 2 1차 — 인벤토리/히스토리 read-only)
clopsctl web
# → http://127.0.0.1:8765
```

## 보안

- `.env`, `*.pem`, `secrets/`, `history/*.sqlite` 는 모두 `.gitignore` 대상
- `.env` 는 반드시 `chmod 600`, 원격 저장소·동기화 폴더에 절대 업로드 금지
- 위험 명령(`rm -rf /`, `shutdown`, `dd ... of=/dev/...` 등)은 자동 confirm 게이트
- 자세한 정책: [docs/security.md](docs/security.md)

## 아키텍처

[docs/architecture.md](docs/architecture.md) 참조.

## 개발

```bash
pip install -e ".[dev]"
pytest
ruff check .
```

## 로드맵

- [x] Phase 1 PoC: 마스터 환경 검증, mcp-ssh-manager 호환성
- [x] Phase 2-a: 스캐폴드 + `exec` fan-out + history + web stub
- [ ] Phase 2-b: `ask` 모드 (Claude Code + mcp-ssh-manager subprocess 통합)
- [ ] Phase 2-c: web UI 실행 폼/스트리밍
- [ ] Phase 3: Windows/macOS 호환 검증, 권한 모드 분리, allowlist 정책

## 라이선스

MIT
