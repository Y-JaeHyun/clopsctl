"""role 기반 명령 allowlist.

inventory 의 `role` 필드(`read-only` / `shell` / `sudo`)에 따라 실행 가능한
명령을 사전 차단한다. `safety.is_dangerous` 와 별개의 게이트:
- safety: 명백히 파괴적인 패턴 (rm -rf /, shutdown 등)
- permission: 서버별 권한 정책 (read-only 서버에 mv 명령 시도 등)
"""
from __future__ import annotations

import re
from typing import Sequence

from .config import RoleMode, Server

# 첫 단어(파이프/리다이렉션 직전까지의 시작 토큰) 추출
_FIRST_TOKEN = re.compile(r"^\s*(?:sudo\s+)?([A-Za-z0-9_./-]+)")

# read-only 역할에서 허용하는 명령 — 정보 조회 + 텍스트 가공만.
READ_ONLY_BINARIES: frozenset[str] = frozenset({
    # 파일/디렉토리 조회
    "ls", "ll", "cat", "less", "more", "head", "tail", "wc", "file", "stat",
    "find", "locate", "tree", "readlink", "realpath", "basename", "dirname",
    # 텍스트 가공 (read-only)
    "grep", "egrep", "fgrep", "rg", "awk", "gawk", "sed", "cut", "sort",
    "uniq", "tr", "tee", "xargs", "column", "diff", "cmp", "md5sum",
    "sha1sum", "sha256sum",
    # 시스템 상태
    "df", "du", "free", "top", "htop", "ps", "uptime", "vmstat", "iostat",
    "sar", "lsof", "fuser", "pidof", "pgrep",
    # 네트워크 조회 (read-only)
    "netstat", "ss", "ip", "ifconfig", "route", "arp", "ping", "traceroute",
    "host", "dig", "nslookup", "curl", "wget",
    # 사용자/시스템 정보
    "hostname", "uname", "whoami", "id", "groups", "date", "tty",
    "w", "who", "last", "uptime", "lscpu", "lsblk", "lsusb", "lspci",
    "lsmod", "dmesg",
    # systemd / 서비스 조회
    "journalctl", "systemctl", "service",  # systemctl 은 status/show/list 만 의도; 추가 검사는 아래
    # 컨테이너 조회
    "docker", "podman", "kubectl",
    # 셸 내장 / 기본
    "echo", "printf", "true", "false", "yes", "test", "[",
    "env", "printenv", "set", "type", "which", "whereis", "command", "alias",
    "pwd", "cd",
    # 기타
    "history", "help", "man", "info", "apropos",
})

# read-only 에서 절대 금지하는 서브명령 패턴 (binary 가 read-only 목록에 있어도)
# 주의: `-X` 같은 비-단어 문자는 `\b` 가 매칭되지 않으므로 공백/시작 매칭으로 처리.
READ_ONLY_FORBIDDEN_SUBPATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bsystemctl\s+(start|stop|restart|reload|enable|disable|mask|unmask|kill|edit|set-property)\b"),
    re.compile(r"\bservice\s+\S+\s+(start|stop|restart|reload)\b"),
    re.compile(r"\bdocker\s+(run|exec|rm|kill|stop|restart|start|build|push|pull|tag|commit|rmi|prune|system|volume|network|swarm|service|stack|node|secret|config)\b"),
    re.compile(r"\bpodman\s+(run|exec|rm|kill|stop|restart|start|build|push|pull|tag|commit|rmi|prune|system|volume|network)\b"),
    re.compile(r"\bkubectl\s+(apply|create|delete|edit|patch|replace|rollout|scale|drain|cordon|uncordon|taint|exec|port-forward|cp|run|expose|set|label|annotate)\b"),
    # curl/wget 의 POST/PUT/DELETE 같은 변경 요청
    re.compile(r"\bcurl\b.*(?:^|\s)(?:-X|--request)\s+(?:POST|PUT|DELETE|PATCH)"),
    re.compile(r"\bcurl\b.*(?:^|\s)(?:-d|--data|--data-binary|--data-urlencode|-F|--form|--upload-file|-T)(?:[=\s]|$)"),
    re.compile(r"\bwget\b.*(?:^|\s)(?:--post-data|--post-file|--method=POST|--method=PUT|--method=DELETE)"),
    # 리다이렉션으로 파일 쓰기 시도 (단순한 휴리스틱)
    re.compile(r"(?:^|[^>&])>\s*[^&\s]"),  # > file (>> 와 2>&1 는 제외)
    re.compile(r">>\s*\S"),  # >> file (append)
    re.compile(r"\btee\s+(?!-a\b)(?!--append\b)\S"),  # tee w/o -a 는 덮어쓰기
)


def _first_binary(command: str) -> str | None:
    """파이프 첫 토큰의 실행 파일 이름. sudo 접두사는 무시."""
    # 첫 파이프 이전만 검사 — 나머지는 일반적으로 read-only 가공
    head = command.split("|", 1)[0]
    head = head.split("&&", 1)[0].split(";", 1)[0]
    m = _FIRST_TOKEN.match(head)
    if not m:
        return None
    binary = m.group(1)
    # 절대경로면 basename
    return binary.rsplit("/", 1)[-1]


def is_allowed_for_role(command: str, role: RoleMode) -> str | None:
    """role 정책상 허용되지 않으면 차단 사유 문자열 반환, 허용되면 None.

    - sudo: 항상 허용 (단, safety 게이트가 별도로 차단)
    - shell: 항상 허용 (단, safety 게이트가 별도로 차단)
    - read-only: 첫 명령이 READ_ONLY_BINARIES 에 있어야 하고 forbidden subpattern 도 없어야 함
    """
    if role in ("sudo", "shell"):
        return None

    # read-only
    binary = _first_binary(command)
    if binary is None:
        return "could not parse command head"
    if binary not in READ_ONLY_BINARIES:
        return f"role 'read-only' rejects command starting with '{binary}'"
    for pat in READ_ONLY_FORBIDDEN_SUBPATTERNS:
        if pat.search(command):
            return f"role 'read-only' forbids subpattern: {pat.pattern}"
    return None


def strictest_role(servers: Sequence[Server]) -> RoleMode:
    """fan-out 시 대상 중 가장 엄격한 role 을 정책 기준으로 사용 (안전 우선)."""
    roles = {s.role for s in servers}
    if "read-only" in roles:
        return "read-only"
    if "shell" in roles:
        return "shell"
    return "sudo"
