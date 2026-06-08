"""인벤토리 / 환경 설정 로더."""
from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

from dotenv import load_dotenv

AuthMode = Literal["pem", "password", "agent"]
RoleMode = Literal["read-only", "shell", "sudo"]

# 서버 입력 검증 규칙 — web 인벤토리 CRUD 와 `clopsctl init` 마법사가 공유.
NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]*$")
VALID_AUTH = ("agent", "pem", "password")
VALID_ROLE = ("read-only", "shell", "sudo")


@dataclass(slots=True, frozen=True)
class Server:
    name: str
    host: str
    user: str
    port: int = 22
    auth: AuthMode = "agent"
    pem_path: str | None = None
    password_env: str | None = None
    role: RoleMode = "read-only"
    tags: tuple[str, ...] = field(default_factory=tuple)
    jump: str | None = None  # 다른 server name 참조 (bastion). 최대 1단계 chain (총 2 hop)
    tmux: bool = False  # True 면 웹 터미널 접속 시 tmux 세션 자동 attach (세션 지속성)
    legacy: bool = False  # True 면 구식 SSH 알고리즘(ssh-rsa 등) 협상 허용. HP-UX/Solaris 등 노후 sshd 용


@dataclass(slots=True, frozen=True)
class Settings:
    inventory_path: Path
    history_db: Path
    model: str
    safety_confirm: bool
    web_host: str
    web_port: int
    llm_backend: str | None  # "claude" | "gemini" | "codex" | None(자동 감지)
    permission_mode: str  # "strict" (기본, 안전 우선) | "per_server"


def load_settings(env_file: Path | None = None) -> Settings:
    if env_file and env_file.exists():
        load_dotenv(env_file)
    else:
        load_dotenv()
    return Settings(
        inventory_path=Path(os.getenv("CLOPSCTL_INVENTORY", "inventory/servers.toml")),
        history_db=Path(os.getenv("CLOPSCTL_HISTORY_DB", "history/clopsctl.sqlite")),
        model=os.getenv("CLOPSCTL_MODEL", "claude-opus-4-7"),
        safety_confirm=os.getenv("CLOPSCTL_SAFETY_CONFIRM", "true").lower() == "true",
        web_host=os.getenv("CLOPSCTL_WEB_HOST", "127.0.0.1"),
        web_port=int(os.getenv("CLOPSCTL_WEB_PORT", "8765")),
        llm_backend=os.getenv("CLOPSCTL_LLM_BACKEND"),
        permission_mode=os.getenv("CLOPSCTL_PERMISSION_MODE", "strict"),
    )


def load_inventory(path: Path) -> dict[str, Server]:
    if not path.exists():
        return {}
    with path.open("rb") as fp:
        raw = tomllib.load(fp)
    servers: dict[str, Server] = {}
    for name, cfg in raw.get("server", {}).items():
        servers[name] = Server(
            name=name,
            host=cfg["host"],
            user=cfg["user"],
            port=int(cfg.get("port", 22)),
            auth=cfg.get("auth", "agent"),
            pem_path=cfg.get("pem_path"),
            password_env=cfg.get("password_env"),
            role=cfg.get("role", "read-only"),
            tags=tuple(cfg.get("tags", [])),
            jump=cfg.get("jump"),
            tmux=bool(cfg.get("tmux", False)),
            legacy=bool(cfg.get("legacy", False)),
        )
    return servers


def server_to_dict(s: Server) -> dict[str, object]:
    """Server → TOML-serializable dict (None/빈값 제외)."""
    out: dict[str, object] = {
        "host": s.host,
        "port": s.port,
        "user": s.user,
        "auth": s.auth,
        "role": s.role,
    }
    if s.pem_path:
        out["pem_path"] = s.pem_path
    if s.password_env:
        out["password_env"] = s.password_env
    if s.tags:
        out["tags"] = list(s.tags)
    if s.jump:
        out["jump"] = s.jump
    if s.tmux:
        out["tmux"] = True
    if s.legacy:
        out["legacy"] = True
    return out


def write_inventory(path: Path, servers: dict[str, Server]) -> None:
    """servers dict 를 TOML 로 atomic write. 기존 파일 덮어씀.

    파일 권한 600 (소유자만) 으로 설정. 파일 위에 안내 주석 포함.
    """
    import tomli_w

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"server": {name: server_to_dict(s) for name, s in servers.items()}}
    body = tomli_w.dumps(payload)
    header = (
        "# clopsctl 인벤토리 — clopsctl web 또는 직접 편집으로 수정 가능.\n"
        "# pem 파일은 secrets/ 등 안전한 경로에 두고 path 만 기록 (커밋 금지).\n"
        "\n"
    )
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(header + body, encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass  # Windows 등에서 chmod 무시
    tmp.replace(path)


def validate_server_input(
    form: dict[str, str],
    inventory: dict[str, Server],
    *,
    is_edit: bool,
) -> tuple[Server | None, list[str]]:
    """폼 dict 를 검증하고 (Server | None, errors) 반환.

    web 인벤토리 CRUD 와 `clopsctl init` 마법사가 공유하는 단일 검증 경로.
    jump cycle / 깊이 검증은 ssh._resolve_jump_chain 을 재사용한다.
    """
    from .ssh import _resolve_jump_chain  # 지연 import — 순환 의존 방지

    errors: list[str] = []
    name = (form.get("name") or "").strip()
    host = (form.get("host") or "").strip()
    user = (form.get("user") or "").strip()
    port_raw = (form.get("port") or "22").strip()
    auth = (form.get("auth") or "agent").strip()
    pem_path = (form.get("pem_path") or "").strip() or None
    password_env = (form.get("password_env") or "").strip() or None
    role = (form.get("role") or "read-only").strip()
    tags_raw = (form.get("tags") or "").strip()
    jump = (form.get("jump") or "").strip() or None
    tmux = (form.get("tmux") or "").strip().lower() in ("true", "on", "1", "yes")
    legacy = (form.get("legacy") or "").strip().lower() in ("true", "on", "1", "yes")

    if not name:
        errors.append("name 이 비어있습니다.")
    elif not NAME_RE.match(name):
        errors.append(f"name '{name}' 은 영숫자·_·-·. 만 허용합니다.")
    elif not is_edit and name in inventory:
        errors.append(f"name '{name}' 가 이미 존재합니다.")

    if not host:
        errors.append("host 가 비어있습니다.")
    if not user:
        errors.append("user 가 비어있습니다.")

    try:
        port = int(port_raw)
        if not (1 <= port <= 65535):
            raise ValueError
    except ValueError:
        errors.append(f"port '{port_raw}' 가 1-65535 범위를 벗어납니다.")
        port = 22

    if auth not in VALID_AUTH:
        errors.append(f"auth '{auth}' 는 {list(VALID_AUTH)} 중 하나여야 합니다.")
    if role not in VALID_ROLE:
        errors.append(f"role '{role}' 는 {list(VALID_ROLE)} 중 하나여야 합니다.")

    if auth == "pem" and not pem_path:
        errors.append("auth=pem 인 경우 pem_path 가 필요합니다.")
    if auth == "password" and not password_env:
        errors.append("auth=password 인 경우 password_env (환경변수 이름) 가 필요합니다.")

    tags: tuple[str, ...] = tuple(t.strip() for t in tags_raw.split(",") if t.strip())

    if jump:
        if jump == name:
            errors.append("jump 가 자기 자신을 가리킵니다.")
        elif jump not in inventory:
            errors.append(f"jump '{jump}' 는 인벤토리에 없는 서버입니다.")

    if errors:
        return None, errors

    server = Server(
        name=name, host=host, user=user, port=port, auth=auth,  # type: ignore[arg-type]
        pem_path=pem_path, password_env=password_env, role=role,  # type: ignore[arg-type]
        tags=tags, jump=jump, tmux=tmux, legacy=legacy,
    )

    # cycle / 깊이 검증 — 미리 인벤토리에 넣고 _resolve_jump_chain 호출
    new_inv = dict(inventory)
    new_inv[name] = server
    try:
        _resolve_jump_chain(server, new_inv)
    except ValueError as exc:
        errors.append(f"jump 검증 실패: {exc}")
        return None, errors

    return server, []


def write_env(path: Path, values: dict[str, str], *, overwrite: bool = False) -> list[str]:
    """KEY=VALUE 들을 .env 에 기록. 기본은 append-only (기존 키 보존).

    - 파일이 없으면 안내 헤더와 함께 새로 생성.
    - overwrite=False(기본): 이미 존재하는 키는 건드리지 않고, 없는 키만 추가.
      비밀번호 등 사용자가 직접 채워둔 값을 덮어쓰지 않기 위함.
    - 파일 권한 600 으로 설정.

    반환: 실제로 추가된 키 목록.
    """
    existing_keys: set[str] = set()
    lines: list[str] = []
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()
        for ln in lines:
            stripped = ln.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                existing_keys.add(stripped.split("=", 1)[0].strip())
    else:
        lines = [
            "# clopsctl 환경 변수 — `clopsctl init` 로 생성. 절대 커밋·업로드 금지.",
            "",
        ]

    added: list[str] = []
    appended: list[str] = []
    for key, val in values.items():
        if key in existing_keys and not overwrite:
            continue
        appended.append(f"{key}={val}")
        added.append(key)

    if appended:
        if lines and lines[-1].strip() != "":
            lines.append("")
        lines.extend(appended)

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass  # Windows 등에서 chmod 무시
    tmp.replace(path)
    return added
