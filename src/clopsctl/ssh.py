"""paramiko 기반 SSH 실행기 — broad fan-out + jump host (ProxyJump) 지원.

jump host 체인은 최대 1단계 (총 2 hop). target.jump → bastion → target.
"""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterable

import paramiko

from .config import Server

MAX_JUMP_DEPTH = 1  # target 외 chain 의 최대 길이 (1 = bastion 1대까지)

# fan_out / run 호출 시 inventory 를 thread-local 로 전달 (jump 해석용)
_INVENTORY: ContextVar[dict[str, Server] | None] = ContextVar("_INVENTORY", default=None)


@dataclass(slots=True)
class ExecResult:
    server: str
    host: str
    exit_code: int
    stdout: str
    stderr: str
    error: str | None = None


def _build_kwargs(server: Server) -> dict[str, object]:
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
    return kwargs


def _resolve_jump_chain(server: Server, inventory: dict[str, Server]) -> list[Server]:
    """target 의 jump 체인을 bastion → target 순서로 반환.

    최대 길이 = MAX_JUMP_DEPTH + 1 (bastion 1 + target 1 = 2 hop).
    순환 참조나 깊이 초과는 ValueError.
    """
    chain: list[Server] = [server]
    seen = {server.name}
    cur = server
    depth = 0
    while cur.jump:
        # cycle 우선 감지 (max-depth 와 무관하게 의미 있는 메시지)
        if cur.jump in seen:
            raise ValueError(f"jump chain has cycle at '{cur.jump}'")
        depth += 1
        if depth > MAX_JUMP_DEPTH:
            raise ValueError(
                f"server '{server.name}' jump chain exceeds max depth {MAX_JUMP_DEPTH} "
                f"(only single-hop bastion is supported)"
            )
        nxt = inventory.get(cur.jump)
        if nxt is None:
            raise ValueError(f"server '{cur.name}' references unknown jump '{cur.jump}'")
        seen.add(nxt.name)
        chain.append(nxt)
        cur = nxt
    chain.reverse()  # bastion 먼저 → target
    return chain


def _client_for(server: Server, *, sock: object | None = None) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kwargs = _build_kwargs(server)
    if sock is not None:
        kwargs["sock"] = sock
    client.connect(**kwargs)  # type: ignore[arg-type]
    return client


def _open_chain(chain: Iterable[Server]) -> tuple[paramiko.SSHClient, list[paramiko.SSHClient]]:
    """체인을 따라 차례로 연결. 마지막 client (target) 와 모든 client 리스트(닫기용) 반환."""
    clients: list[paramiko.SSHClient] = []
    sock: object | None = None
    chain_list = list(chain)
    for srv in chain_list:
        if sock is None:
            client = _client_for(srv)
        else:
            client = _client_for(srv, sock=sock)
        clients.append(client)
        # 다음 hop 을 위한 channel 준비 (target 이 마지막이면 사용 안 됨)
        if srv is not chain_list[-1]:
            transport = client.get_transport()
            assert transport is not None
            next_srv = chain_list[chain_list.index(srv) + 1]
            sock = transport.open_channel(
                "direct-tcpip",
                (next_srv.host, next_srv.port),
                ("", 0),
            )
    return clients[-1], clients


def run(server: Server, command: str) -> ExecResult:
    inventory = _INVENTORY.get() or {server.name: server}
    try:
        chain = _resolve_jump_chain(server, inventory)
    except Exception as exc:  # noqa: BLE001
        return ExecResult(server.name, server.host, -1, "", "", str(exc))

    clients: list[paramiko.SSHClient] = []
    try:
        target_client, clients = _open_chain(chain)
        stdin, stdout_stream, stderr_stream = target_client.exec_command(command, timeout=60)
        stdout = stdout_stream.read().decode(errors="replace")
        stderr = stderr_stream.read().decode(errors="replace")
        exit_code = stdout_stream.channel.recv_exit_status()
        return ExecResult(server.name, server.host, exit_code, stdout, stderr)
    except Exception as exc:  # noqa: BLE001
        return ExecResult(server.name, server.host, -1, "", "", str(exc))
    finally:
        for c in clients:
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass


def open_shell(
    server: Server,
    inventory: dict[str, Server],
    *,
    term: str = "xterm-256color",
    cols: int = 80,
    rows: int = 24,
) -> tuple[paramiko.Channel, list[paramiko.SSHClient]]:
    """interactive PTY shell 채널 + 닫을 클라이언트 리스트 반환.

    jump host 체인 자동 적용. 호출자는 결과 channel 로 send/recv 하고,
    종료 시 모든 client 를 close 해야 한다.
    """
    chain = _resolve_jump_chain(server, inventory)
    target_client, clients = _open_chain(chain)
    transport = target_client.get_transport()
    if transport is None:
        for c in clients:
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass
        raise RuntimeError(f"failed to acquire transport for {server.name}")
    channel = transport.open_session()
    channel.get_pty(term=term, width=cols, height=rows)
    channel.invoke_shell()
    return channel, clients


def fan_out(
    servers: list[Server],
    command: str,
    max_workers: int = 8,
    inventory: dict[str, Server] | None = None,
) -> list[ExecResult]:
    """다수 서버에 동일 명령을 병렬 실행. jump 해석용 inventory 옵션."""
    results: list[ExecResult] = []
    inv = inventory or {s.name: s for s in servers}

    def _runner(s: Server) -> ExecResult:
        token = _INVENTORY.set(inv)
        try:
            return run(s, command)
        finally:
            _INVENTORY.reset(token)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_runner, s): s for s in servers}
        for fut in as_completed(futures):
            results.append(fut.result())
    return results
