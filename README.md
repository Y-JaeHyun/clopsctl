# clopsctl

> 마스터(로컬) 한 대에서 다수 SSH 원격 서버를 자연어 + 단순 커맨드로 통합 관리. 원격 서버에는 AI 미설치 — sshd 만 가동.

- **언어**: Python 3.11+
- **CLI**: Typer / Rich
- **SSH**: paramiko (병렬 fan-out)
- **LLM**: 로컬에 설치된 `claude` / `gemini` / `codex` CLI 를 subprocess 로 호출 — API key 직접 관리 불필요
- **Web**: FastAPI — 인벤토리/히스토리 + ask 실행 폼 (localhost-only)
- **히스토리**: SQLite append-only
- **상태**: 0.0.2 — `exec` + `ask`(Plan→Execute→Summarize) 동작, web UI 폼은 Phase 2-c

## 빠른 시작

### 1. 사전 요건

마스터 머신에 다음 중 **하나 이상**의 LLM CLI 가 설치·인증되어 있어야 합니다:

```bash
which claude   # https://docs.claude.com/code (권장)
which gemini   # https://github.com/google-gemini/gemini-cli
which codex    # https://github.com/openai/codex
```

`clopsctl backend` 로 가용성 확인 가능. 미설치 백엔드는 자동으로 건너뜀.

### 2. 의존성

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

### 3. 설정

```bash
cp .env.example .env
chmod 600 .env
# .env 에서 CLOPSCTL_LLM_BACKEND 등 환경 조정 (기본은 PATH 자동 감지)

cp inventory/servers.example.toml inventory/servers.toml
# 실서버 정보 입력. pem 은 secrets/ 또는 안전한 경로에
chmod 700 secrets 2>/dev/null || true
```

### 4. 사용

```bash
# 가용한 LLM 백엔드 확인
clopsctl backend

# 인벤토리
clopsctl server list
clopsctl server check web-1

# 단일/멀티 서버에 명령 fan-out (LLM 비경유)
clopsctl exec web-1 -- "df -h"
clopsctl exec web-1,web-2,db-stage -- "uptime"

# 자연어 (LLM CLI 활용 — Plan→Execute→Summarize)
clopsctl ask web-1,web-2 "최근 1시간 5xx 비율 알려줘"
clopsctl ask --backend gemini web-1 "디스크 80% 넘는 마운트 찾아줘"

# 히스토리 (자연어 프롬프트와 실제 실행 명령 모두 기록됨)
clopsctl history --limit 30
clopsctl history --server web-1
clopsctl history --grep "디스크"

# 웹 UI — 인벤토리/히스토리 조회 + ask 실행 폼 (반드시 localhost only)
clopsctl web
# → http://127.0.0.1:8765
#   - 서버 체크박스 + 프롬프트 + 백엔드 선택 + dry-run
#   - POST /ask 로 실행 → 결과 페이지 렌더
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

- [x] Phase 1 PoC: 마스터 환경 검증, LLM CLI 가용성
- [x] Phase 2-a: 스캐폴드 + `exec` fan-out + history + web stub
- [x] Phase 2-b: `ask` 모드 (claude/gemini/codex CLI Plan→Execute→Summarize)
- [x] Phase 2-c-1: web UI ask 실행 폼 (localhost-only, POST 폼)
- [x] Phase 2-c-2: 권한 모드 allowlist + `--dry-run` 옵션
- [x] Phase 2-c-3: web UI 실시간 스트리밍 (SSE 단계별 진행 표시)
- [x] Phase 3-a: per-server 권한 모드 (`--per-server` / `CLOPSCTL_PERMISSION_MODE`)
- [x] Phase 3-b: 호환성 노트 (`docs/compatibility.md`)
- [x] Phase 3-d-1: SSH ProxyJump 지원 (인벤토리 `jump` 필드, 최대 2 hop)
- [x] Phase 3-d-2: Web UI 디자인 정돈 (카드 레이아웃, 배지, step 이벤트 색상)
- [x] Phase 3-e: 인벤토리 CRUD web UI (모든 Server 필드 폼 + jump 드롭다운 + cycle 검증)
- [x] Phase 4-a: 대화형 follow-up (Conversation, prior_turns, 이전 turn 카드 누적)
- [ ] Phase 4-b: 인터랙티브 SSH 터미널 UI (xterm.js + WebSocket + paramiko PTY)
- [ ] Phase 3-c: Windows/macOS 실제 PoC 검증 (호환성 매트릭스 채우기)

## 라이선스

MIT
