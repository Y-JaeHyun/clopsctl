# clopsctl 아키텍처

> 마스터(로컬) 한 대에서 다수 SSH 서버를 통합 관리하는 CLI/Web 도구. 원격 서버에는 AI 미설치 — sshd 만 가동.

## 컴포넌트

```
[로컬 마스터]
├─ Claude Code CLI (claude-opus-4-7)            ← 자연어 명령 진입점
├─ MCP server: mcp-ssh-manager (npm 글로벌)     ← LLM ↔ SSH 다리
└─ clopsctl (이 repo)
   ├─ inventory  servers.toml
   ├─ secrets    .env, *.pem  (gitignore, chmod 600)
   ├─ history    SQLite (append-only)
   ├─ CLI        Typer  — server / exec / ask / history / web
   └─ Web UI     FastAPI — 인벤토리/히스토리 read-only (Phase 2 1차)

[원격 서버 N대]
└─ sshd 만 가동, AI 미설치
```

## 주요 흐름

### exec 모드 (LLM 비경유)
1. 사용자: `clopsctl exec web-1,web-2 -- "df -h"`
2. `safety.is_dangerous` 체크 → 위험 시 confirm 게이트
3. `ssh.fan_out` 으로 paramiko 병렬 실행
4. 결과를 콘솔 panel + history SQLite 에 기록

### ask 모드 (LLM 경유, Phase 2)
1. 사용자: `clopsctl ask web-1,web-2 "디스크 80% 초과 경로 찾아줘"`
2. wrapper 가 `claude code` 세션을 실행 (mcp-ssh-manager 활성)
3. LLM 이 mcp-ssh-manager 도구로 N대 서버에 질의 fan-out
4. LLM 응답 + 실제 실행 명령/결과를 history 에 기록

### web 모드 (Phase 2)
- `clopsctl web` → 로컬 127.0.0.1 바인드 FastAPI
- 1차: 인벤토리/히스토리 read-only
- 후속: 실행 폼, WebSocket 스트리밍, 히스토리 검색

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
