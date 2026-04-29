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

## 권한 모드 (구현됨)

`inventory.role` 별 차등 정책. `safety` 게이트와 별개로 작동하는 두 번째 게이트:
- `read-only`: 정보 조회·텍스트 가공 명령만 allowlist 통과
  (ls, cat, df, du, free, ps, top, journalctl, systemctl status, docker ps, kubectl get, grep, awk, find, …)
  - 상태 변경 서브명령 차단: `systemctl restart/start/stop/reload/enable/disable`, `docker run/exec/rm/build`, `kubectl apply/delete/patch` …
  - 쓰기 리다이렉션 차단: `> file`, `>> file`, `tee` (without `-a`)
  - 변경 HTTP 메서드 차단: `curl -X POST/PUT/DELETE`, `curl -d`, `wget --post-data` …
- `shell`: 권한 게이트 통과(safety 게이트만 작동)
- `sudo`: 권한 게이트 통과(safety 게이트만 작동)

**기본(strict): fan-out 시 가장 엄격한 role 기준** — 대상 중 하나라도 `read-only` 면 그 정책으로 전체 차단. 안전 우선.

**per_server 모드** (`CLOPSCTL_PERMISSION_MODE=per_server` 또는 `--per-server`): 서버별 개별 검사 — 통과한 서버에만 실행하고 차단된 서버는 history 에 사유와 함께 기록. 혼합 role 클러스터에서 안전한 명령은 가능한 곳까지 굴리고 싶을 때 사용.

`exec`/`ask` 모두 적용. `--dry-run` 옵션으로 게이트만 검사하고 실제 SSH 실행은 건너뜀.

## 점검 체크리스트

- [ ] `.env` 권한 600 인지 (`stat -c %a .env`)
- [ ] `secrets/` 안 pem 파일 권한 600 인지
- [ ] `git status` 가 비밀 파일을 untracked 로조차 노출하지 않는지 (`.gitignore` 확인)
- [ ] 백업/동기화 도구가 본 디렉토리를 제외하는지
- [ ] 운영자 외 다른 사용자가 디렉토리 읽기 권한을 갖지 않는지 (`chmod 700 .`)
