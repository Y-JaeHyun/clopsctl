# clopsctl 아키텍처

> 마스터(로컬) 한 대에서 다수 SSH 서버를 통합 관리하는 CLI/Web 도구. 원격 서버에는 AI 미설치 — sshd 만 가동.

## 컴포넌트

```
[로컬 마스터]
├─ LLM CLI (claude / gemini / codex 중 하나)   ← 사용자 환경에서 이미 인증됨
└─ clopsctl (이 repo)
   ├─ llm        백엔드 추상 (subprocess 로 위 CLI 호출)
   ├─ inventory  servers.toml
   ├─ secrets    .env, *.pem  (gitignore, chmod 600)
   ├─ history    SQLite (append-only)
   ├─ CLI        Typer  — server / exec / ask / backend / history / web
   └─ Web UI     FastAPI — 인벤토리/히스토리 read-only (Phase 2 1차)

[원격 서버 N대]
└─ sshd 만 가동, AI 미설치
```

> ANTHROPIC_API_KEY 같은 키를 직접 관리하지 않습니다. 사용자 머신에 이미 인증돼 있는 `claude`/`gemini`/`codex` CLI 를 그대로 활용 — 인증·요금·모델 정책은 각 CLI 의 책임.

## 주요 흐름

### exec 모드 (LLM 비경유)
1. 사용자: `clopsctl exec web-1,web-2 -- "df -h"`
2. `safety.is_dangerous` 체크 → 위험 시 confirm 게이트
3. `ssh.fan_out` 으로 paramiko 병렬 실행
4. 결과를 콘솔 panel + history SQLite 에 기록

### ask 모드 (LLM CLI Plan→Execute→Summarize)
1. 사용자: `clopsctl ask web-1,web-2 "디스크 80% 초과 경로 찾아줘"`
2. **Plan**: 인벤토리 + 사용자 질문을 LLM CLI(stdin)로 전달, JSON `{"steps": [...]}` 응답 받음
3. **Execute**: 우리가 safety 게이트 통과시키고 paramiko 로 실행 (단일/fan-out 모두)
4. **Summarize**: 실행 결과를 다시 LLM CLI에 보내 한국어 답변 생성
5. 자연어 프롬프트 + 실제 실행 명령/결과 모두 history(`mode='ask'`) append-only 기록

> 단순 텍스트 in/out 만 쓰므로 claude, gemini, codex 어느 CLI 라도 동등하게 동작.

### web 모드 (구현됨)
- `clopsctl web` → 127.0.0.1:8765 바인드 FastAPI (외부 노출 금지, 페이지에 경고 배너)
- `GET /` — 인벤토리, 히스토리 20건, ask 폼 (서버 체크박스 + 프롬프트 + 백엔드 선택 + dry-run)
- `POST /ask` — `Job` 생성 + 백그라운드 thread 로 `agent.ask()` 시작, **즉시 streaming 페이지 반환**
  - 페이지에 `EventSource('/ask/stream/<job_id>')` 임베드 → 브라우저가 단계별 이벤트 수신
- `GET /ask/stream/{job_id}` — **SSE 스트림** (`text/event-stream`, keep-alive 코멘트 포함)
  - 이벤트 타입: `started` / `plan_start` / `plan_done` / `step_start` / `step_result` / `step_blocked` / `step_failed` / `step_dry_run` / `summarize_start` / `summarize_done` / `done` / `error`
  - 종료: `event: eof` 후 `data: end`
- `GET /healthz` — 버전/백엔드 가용성 JSON
- 모든 동적 값은 서버측 `html.escape()` + 클라이언트측 `escapeHtml()` 두 단계 escape (XSS 방지)
- Job 은 in-memory dict 로 관리, 종료 후 10분 TTL 로 자동 청소 (단일 프로세스 가정)

`agent.ask()` 는 옵셔널 `on_event` 콜백을 받아 phase 별 dict 이벤트를 푸시 — CLI는 `console.print`, web은 SSE 큐 push 로 동일 콜백 인터페이스 활용.

## 디자인 결정

- **Python 3.11+**: paramiko, keyring 미사용(.env), Typer/Rich, FastAPI 모두 한 언어
- **MCP 서버는 Node**: `npx -y mcp-ssh-manager` — 별도 언어 차이는 subprocess 경계로 격리
- **broad fan-out 우선**: ThreadPoolExecutor 로 병렬 실행, deep 시나리오는 단일 서버 fan-out 1대 케이스로 자연스럽게 흡수
- **append-only history**: 운영자 회고/감사 가능
- **safety regex**: 1차 룰셋 + `--yes` 강제 옵션, Phase 3 에서 권한 모드(read-only/shell/sudo) 별 정책 분리

## 디렉토리 책임

| 디렉토리 | 책임 |
| --- | --- |
| `src/clopsctl/cli.py` | Typer 앱 정의, 명령 라우팅 |
| `src/clopsctl/config.py` | `.env` + TOML 인벤토리 로딩 |
| `src/clopsctl/ssh.py` | paramiko 래퍼, fan-out |
| `src/clopsctl/safety.py` | 위험 명령 정규식 게이트 |
| `src/clopsctl/history.py` | SQLite 스키마/조회 |
| `src/clopsctl/web.py` | FastAPI 앱 |
| `inventory/` | TOML 인벤토리 (실서버는 `servers.toml`, 샘플은 `servers.example.toml`) |
| `secrets/` | pem 등 (gitignore) |
| `history/` | SQLite 파일 (gitignore) |
| `tests/` | 단위 테스트 |
| `docs/` | 아키텍처/보안 문서 |
