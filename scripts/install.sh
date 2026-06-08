#!/usr/bin/env bash
# clopsctl 1-커맨드 부트스트랩 — venv 생성, 의존성 설치, 템플릿 복사, 권한 강화.
#
# macOS / Linux 모두 동작 (bash 3.2+, POSIX 도구만 사용).
#
# 사용:
#   ./scripts/install.sh            # 일반 설치 (clopsctl 본체만)
#   ./scripts/install.sh --dev      # dev 의존성 (pytest, ruff, mypy) 포함
#   PYTHON=python3.11 ./scripts/install.sh   # 특정 python 지정
set -euo pipefail

# ── 프로젝트 루트 해석 (macOS 에 readlink -f 없으므로 portable 방식) ──
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR"

WANT_DEV=0
for arg in "$@"; do
  case "$arg" in
    --dev) WANT_DEV=1 ;;
    -h|--help)
      grep '^#' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) echo "알 수 없는 옵션: $arg" >&2; exit 2 ;;
  esac
done

# ── Python 인터프리터 선택 ──
PYTHON="${PYTHON:-}"
if [ -z "$PYTHON" ]; then
  for cand in python3.13 python3.12 python3.11 python3; do
    if command -v "$cand" >/dev/null 2>&1; then PYTHON="$cand"; break; fi
  done
fi
if [ -z "$PYTHON" ]; then
  echo "✗ python3 를 찾을 수 없습니다. Python 3.11+ 을 설치하세요." >&2
  exit 1
fi

# ── Python 버전 검증 (>= 3.11) ──
if ! "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
  ver="$("$PYTHON" -c 'import sys; print(".".join(map(str, sys.version_info[:3])))')"
  echo "✗ Python 3.11+ 필요 (현재 $PYTHON = $ver)." >&2
  echo "  PYTHON=python3.11 ./scripts/install.sh 처럼 지정하세요." >&2
  exit 1
fi
echo "→ Python: $("$PYTHON" --version) ($PYTHON)"

# ── venv 생성 ──
if [ ! -d ".venv" ]; then
  echo "→ .venv 생성 중..."
  "$PYTHON" -m venv .venv
else
  echo "→ .venv 이미 존재 — 재사용"
fi
VENV_PY=".venv/bin/python"
if [ ! -x "$VENV_PY" ]; then
  # Windows venv 레이아웃 (Git-Bash 등) 대비
  if [ -x ".venv/Scripts/python.exe" ]; then VENV_PY=".venv/Scripts/python.exe"; fi
fi

# ── pip / 의존성 ──
echo "→ pip · wheel 업그레이드..."
"$VENV_PY" -m pip install --quiet --upgrade pip wheel
if [ "$WANT_DEV" -eq 1 ]; then
  echo "→ clopsctl 설치 (editable + dev 의존성)..."
  "$VENV_PY" -m pip install --quiet -e ".[dev]"
else
  echo "→ clopsctl 설치 (editable)..."
  "$VENV_PY" -m pip install --quiet -e .
fi

# ── 설정 템플릿 복사 (기존 파일은 보존) ──
copy_template() {
  src="$1"; dst="$2"
  if [ -f "$dst" ]; then
    echo "  · $dst 이미 존재 — 건너뜀"
  elif [ -f "$src" ]; then
    cp "$src" "$dst"
    echo "  · $src → $dst 복사"
  else
    echo "  · ⚠ 템플릿 없음: $src — 건너뜀"
  fi
}
echo "→ 설정 템플릿 복사..."
copy_template ".env.example" ".env"
copy_template "inventory/servers.example.toml" "inventory/servers.toml"

# ── 권한 강화 (chmod 미지원 OS 는 무시) ──
echo "→ 권한 강화..."
mkdir -p secrets history .run
chmod 600 .env 2>/dev/null && echo "  · chmod 600 .env" || echo "  · ⚠ chmod 600 .env 실패 (Windows 는 NTFS ACL 사용)"
chmod 700 secrets 2>/dev/null && echo "  · chmod 700 secrets/" || true
chmod +x bin/clopsctl-start.sh bin/clopsctl-stop.sh 2>/dev/null || true

echo ""
echo "✓ 설치 완료."
echo ""
echo "다음 단계:"
echo "  1. .env 와 inventory/servers.toml 를 실제 값으로 편집"
echo "  2. LLM CLI 확인:   .venv/bin/clopsctl backend"
echo "  3. 웹 UI 기동:     ./bin/clopsctl-start.sh   (종료: ./bin/clopsctl-stop.sh)"
echo "  4. 또는 직접 실행: source .venv/bin/activate && clopsctl --help"
