"""인벤토리 / 환경 설정 로더."""
from __future__ import annotations

import os
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
