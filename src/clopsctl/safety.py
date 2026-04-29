"""파괴적 명령 사전 차단 게이트."""
from __future__ import annotations

import re

DANGEROUS_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\brm\s+-rf?\s+/(?!\w)"),
    re.compile(r"\bshutdown\b"),
    re.compile(r"\breboot\b"),
    re.compile(r"\bhalt\b"),
    re.compile(r"\bmkfs\."),
    re.compile(r">\s*/dev/sd[a-z]"),
    re.compile(r"\bdd\s+if=.*of=/dev/"),
    re.compile(r":\s*\(\s*\)\s*\{[^}]*:\|\s*:[^}]*&[^}]*\}\s*;\s*:"),  # fork bomb
    re.compile(r"\bchmod\s+-R\s+777\s+/"),
)


def is_dangerous(command: str) -> str | None:
    """위험 명령이면 매칭된 패턴의 설명을 반환, 안전하면 None."""
    for pat in DANGEROUS_PATTERNS:
        if pat.search(command):
            return pat.pattern
    return None
