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


@dataclass(slots=True, frozen=True)
class Settings:
    inventory_path: Path
    history_db: Path
    model: str
    safety_confirm: bool
    web_host: str
    web_port: int
    llm_backend: str | None  # "claude" | "gemini" | "codex" | None(자동 감지)


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
        )
    return servers
