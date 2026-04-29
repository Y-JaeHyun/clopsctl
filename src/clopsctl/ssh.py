"""paramiko 기반 SSH 실행기 — broad fan-out 우선."""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

import paramiko

from .config import Server


@dataclass(slots=True)
class ExecResult:
    server: str
    host: str
    exit_code: int
    stdout: str
    stderr: str
    error: str | None = None


def _client_for(server: Server) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    kwargs: dict[str, object] = {
        "hostname": server.host,
        "port": server.port,
        "username": server.user,
        "timeout": 10,
        "auth_timeout": 10,
        "banner_timeout": 10,
    }
    if server.auth == "pem":
        if not server.pem_path:
            raise ValueError(f"server '{server.name}' has auth=pem but no pem_path")
        kwargs["key_filename"] = os.path.expanduser(server.pem_path)
    elif server.auth == "password":
        if not server.password_env:
            raise ValueError(f"server '{server.name}' has auth=password but no password_env")
        password = os.getenv(server.password_env)
        if not password:
            raise ValueError(f"env {server.password_env} is empty for server '{server.name}'")
        kwargs["password"] = password
    elif server.auth == "agent":
        kwargs["allow_agent"] = True
        kwargs["look_for_keys"] = True
    else:
        raise ValueError(f"unknown auth '{server.auth}' for server '{server.name}'")

    client.connect(**kwargs)  # type: ignore[arg-type]
    return client


def run(server: Server, command: str) -> ExecResult:
    try:
        client = _client_for(server)
    except Exception as exc:  # noqa: BLE001 — 사용자가 봐야 하는 연결 오류 메시지
        return ExecResult(server.name, server.host, -1, "", "", str(exc))

    try:
        stdin, stdout_stream, stderr_stream = client.exec_command(command, timeout=60)
        stdout = stdout_stream.read().decode(errors="replace")
        stderr = stderr_stream.read().decode(errors="replace")
        exit_code = stdout_stream.channel.recv_exit_status()
        return ExecResult(server.name, server.host, exit_code, stdout, stderr)
    except Exception as exc:  # noqa: BLE001
        return ExecResult(server.name, server.host, -1, "", "", str(exc))
    finally:
        client.close()


def fan_out(servers: list[Server], command: str, max_workers: int = 8) -> list[ExecResult]:
    """다수 서버에 동일 명령을 병렬 실행."""
    results: list[ExecResult] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(run, s, command): s for s in servers}
        for fut in as_completed(futures):
            results.append(fut.result())
    return results
