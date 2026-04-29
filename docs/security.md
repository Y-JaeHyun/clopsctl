# clopsctl 보안 정책

## 시크릿 관리

- `.env` 는 평문이지만 **반드시** 다음 조건을 충족해야 한다:
  - `chmod 600 .env`
  - `.gitignore` 에 명시 (이미 포함됨)
  - 원격 저장소·공유 폴더·백업 시스템 어디에도 절대 업로드 금지
  - 클라우드 동기화 폴더(Dropbox/iCloud/OneDrive) 외부 경로에 저장
- pem 파일은 `secrets/` 또는 사용자 홈 외 안전한 경로에 두고 인벤토리에는 path 만 기록
- 비밀번호 인증 사용 시 `password_env` 로 `.env` 변수명을 지정 (TOML 자체에는 비밀번호 평문 금지)

## 위험 명령 게이트

`safety.is_dangerous` 가 1차 정규식으로 차단:
- `rm -rf /`, `shutdown`, `reboot`, `halt`, `mkfs.*`, `dd ... of=/dev/...`, `chmod -R 777 /`, fork bomb 등
- `CLOPSCTL_SAFETY_CONFIRM=true` (기본) → confirm 프롬프트
- `--yes` 플래그로 강제 가능 (의도적 운영 시)
- ask 모드에서 LLM 이 위험 명령 생성 시에도 동일 게이트 적용 예정 (Phase 2)

## 감사 추적

- 모든 명령(exec/ask)은 SQLite history 에 append-only 기록
- 기록 항목: ts, server, mode, prompt, command, exit_code, stdout, stderr, llm_model, tokens
- history DB 자체도 `.gitignore` 대상 (운영 정보 포함)

## 권한 모드 (Phase 3)

`inventory.role` 별 차등 정책:
- `read-only`: ls, cat, df, top 류만 허용 (allowlist)
- `shell`: 일반 운영 명령 허용, destructive 차단 강화
- `sudo`: 전체 허용, 단 destructive 시 항상 confirm

## 점검 체크리스트

- [ ] `.env` 권한 600 인지 (`stat -c %a .env`)
- [ ] `secrets/` 안 pem 파일 권한 600 인지
- [ ] `git status` 가 비밀 파일을 untracked 로조차 노출하지 않는지 (`.gitignore` 확인)
- [ ] 백업/동기화 도구가 본 디렉토리를 제외하는지
- [ ] 운영자 외 다른 사용자가 디렉토리 읽기 권한을 갖지 않는지 (`chmod 700 .`)
