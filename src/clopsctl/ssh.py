"""paramiko 기반 SSH 실행기 — broad fan-out + jump host (ProxyJump) 지원.

jump host 체인은 최대 1단계 (총 2 hop). target.jump → bastion → target.
"""
from __future__ import annotations

import os
import socket
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterable

import paramiko

from .config import Server

MAX_JUMP_DEPTH = 1  # target 외 chain 의 최대 길이 (1 = bastion 1대까지)

# paramiko 5.x 가 기본에서 제거한 구식 SSH 알고리즘 — legacy=True 서버에서만 되살린다.
# 노후 sshd(HP-UX 등)는 host key 로 ssh-rsa(SHA-1) 만 제시하므로 협상이 "(no match)" 로 실패한다.
# 주의: ssh-rsa(SHA-1)·DSS 는 약한 알고리즘이다. 신뢰된 내부망 노후 장비에 한해서만 사용한다.
_LEGACY_HOST_KEY_ALGOS = ("ssh-rsa",)
_CONNECT_TIMEOUT = 15


def _enable_legacy_rsa_sha1() -> None:
    """paramiko 5.x 가 RSAKey.HASHES 에서 뺀 ssh-rsa(SHA-1) 검증을 되살린다.

    노후 sshd 가 host key 를 ssh-rsa(SHA-1) 로만 서명하면, HASHES 에 'ssh-rsa' 가 없어
    verify_ssh_sig 가 즉시 False → "Signature verification (ssh-rsa) failed" 로 끊긴다.
    SHA-1 매핑을 추가한다. 전역이지만 'ssh-rsa' 협상 시에만 쓰이므로 현대 연결엔 영향 없음.
    """
    from cryptography.hazmat.primitives import hashes

    if "ssh-rsa" not in paramiko.RSAKey.HASHES:
        paramiko.RSAKey.HASHES["ssh-rsa"] = hashes.SHA1

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


def _legacy_auth(transport: paramiko.Transport, server: Server) -> None:
    """legacy Transport 에 대해 server.auth 방식대로 인증."""
    if server.auth == "password":
        if not server.password_env:
            raise ValueError(f"server '{server.name}' has auth=password but no password_env")
        password = os.getenv(server.password_env)
        if not password:
            raise ValueError(f"env {server.password_env} is empty for server '{server.name}'")
        transport.auth_password(server.user, password)
    elif server.auth == "pem":
        if not server.pem_path:
            raise ValueError(f"server '{server.name}' has auth=pem but no pem_path")
        key = paramiko.RSAKey.from_private_key_file(os.path.expanduser(server.pem_path))
        transport.auth_publickey(server.user, key)
    elif server.auth == "agent":
        keys = paramiko.Agent().get_keys()
        if not keys:
            raise ValueError(f"no ssh-agent keys available for legacy server '{server.name}'")
        last_exc: Exception | None = None
        for key in keys:
            try:
                transport.auth_publickey(server.user, key)
                return
            except paramiko.SSHException as exc:  # noqa: PERF203
                last_exc = exc
        raise last_exc or ValueError(f"agent auth failed for '{server.name}'")
    else:
        raise ValueError(f"unknown auth '{server.auth}' for server '{server.name}'")


def _legacy_client_for(server: Server, *, sock: object | None = None) -> paramiko.SSHClient:
    """구식 sshd 용 수동 Transport 구성.

    paramiko 5.x 는 ssh-rsa(SHA-1) host key 알고리즘을 _key_info/_preferred_keys 에서
    제거했다. SSHClient.connect 는 알고리즘 '추가' 훅이 없으므로, Transport 를 직접
    만들어 ssh-rsa 를 되살린 뒤 인증하고 SSHClient 로 감싼다 (AutoAddPolicy 와 동일하게
    host key 검증은 생략).
    """
    _enable_legacy_rsa_sha1()
    if sock is None:
        sock = socket.create_connection((server.host, server.port), timeout=_CONNECT_TIMEOUT)
    transport = paramiko.Transport(sock)  # type: ignore[arg-type]
    # ssh-rsa 를 host key 알고리즘 목록과 이름→키클래스 매핑 양쪽에 되살린다.
    key_info = dict(transport._key_info)  # type: ignore[attr-defined]
    for algo in _LEGACY_HOST_KEY_ALGOS:
        key_info.setdefault(algo, paramiko.RSAKey)
    transport._key_info = key_info  # type: ignore[attr-defined]
    transport._preferred_keys = (  # type: ignore[attr-defined]
        tuple(_LEGACY_HOST_KEY_ALGOS) + tuple(transport._preferred_keys)  # type: ignore[attr-defined]
    )
    transport.start_client(timeout=_CONNECT_TIMEOUT)
    _legacy_auth(transport, server)
    client = paramiko.SSHClient()
    client._transport = transport  # type: ignore[attr-defined]
    return client


def _client_for(server: Server, *, sock: object | None = None) -> paramiko.SSHClient:
    if server.legacy:
        return _legacy_client_for(server, sock=sock)
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
