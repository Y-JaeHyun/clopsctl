"""permissions 모듈 단위 테스트."""
from __future__ import annotations

import pytest

from clopsctl.config import Server
from clopsctl.permissions import is_allowed_for_role, strictest_role


# --- is_allowed_for_role -----------------------------------------------------

def test_sudo_allows_anything():
    assert is_allowed_for_role("ls -la", "sudo") is None
    assert is_allowed_for_role("systemctl restart nginx", "sudo") is None
    assert is_allowed_for_role("docker run -it ubuntu", "sudo") is None


def test_shell_allows_anything_safety_handles_destructive():
    # role 게이트는 "권한 정책" — 파괴적 명령 차단은 별도 safety 게이트의 역할
    assert is_allowed_for_role("vim /etc/nginx/nginx.conf", "shell") is None
    assert is_allowed_for_role("git pull", "shell") is None


@pytest.mark.parametrize("cmd", [
    "ls /var/log",
    "df -h",
    "free -m",
    "ps aux",
    "cat /etc/hostname",
    "tail -f /var/log/syslog",
    "journalctl -u nginx --since '1 hour ago'",
    "systemctl status nginx",
    "docker ps",
    "kubectl get pods",
    "grep ERROR /var/log/app.log | head -20",
    "find /var/log -name '*.log' -mtime -1",
    "echo hello",
    "uptime",
    "ss -tnlp",
])
def test_read_only_allows_inspection_commands(cmd):
    assert is_allowed_for_role(cmd, "read-only") is None, cmd


@pytest.mark.parametrize("cmd", [
    "rm /tmp/foo",
    "mv a b",
    "cp a b",
    "mkdir /tmp/x",
    "chmod 644 /etc/hosts",
    "chown root /tmp/x",
    "vim /etc/hosts",
    "nano file.txt",
    "git pull",
    "apt install nginx",
    "yum update",
    "pip install requests",
    "npm i lodash",
])
def test_read_only_rejects_mutating_commands(cmd):
    assert is_allowed_for_role(cmd, "read-only") is not None, cmd


@pytest.mark.parametrize("cmd", [
    "systemctl restart nginx",
    "systemctl start mysql",
    "systemctl stop apache2",
    "service nginx restart",
    "docker run -d nginx",
    "docker exec -it foo bash",
    "kubectl apply -f deploy.yaml",
    "kubectl delete pod foo",
])
def test_read_only_rejects_state_changing_subcommands(cmd):
    """binary 가 read-only 목록에 있어도 서브명령이 변경이면 거부."""
    assert is_allowed_for_role(cmd, "read-only") is not None, cmd


@pytest.mark.parametrize("cmd", [
    "curl -X POST https://api.example.com/foo",
    "curl --request DELETE https://api.example.com/foo",
    "curl -d '{}' https://api.example.com/foo",
    "curl -F file=@x.bin https://api.example.com/upload",
    "wget --post-data='x=1' https://example.com/",
])
def test_read_only_rejects_curl_wget_writes(cmd):
    assert is_allowed_for_role(cmd, "read-only") is not None, cmd


@pytest.mark.parametrize("cmd", [
    "ls > /tmp/files",
    "ls > /tmp/x.txt",
    "echo x >> /var/log/foo",
    "ls | tee /tmp/out",
])
def test_read_only_rejects_write_redirections(cmd):
    assert is_allowed_for_role(cmd, "read-only") is not None, cmd


def test_read_only_accepts_stderr_redirect():
    # 2>&1 같은 stderr 리다이렉션은 허용 (파일 쓰기가 아님)
    assert is_allowed_for_role("ls -la 2>&1", "read-only") is None


def test_read_only_accepts_pipe_with_read_only_tools():
    assert is_allowed_for_role("cat /var/log/syslog | grep ERROR | head -10", "read-only") is None


def test_read_only_accepts_sudo_prefixed_read_only():
    # sudo 접두사는 무시되고 첫 실행 파일이 read-only 목록에 있으면 허용
    assert is_allowed_for_role("sudo journalctl -u nginx", "read-only") is None


def test_read_only_accepts_absolute_path():
    assert is_allowed_for_role("/usr/bin/ls -la", "read-only") is None


# --- strictest_role -----------------------------------------------------------

def _srv(name: str, role: str) -> Server:
    return Server(name=name, host="h", user="u", role=role)  # type: ignore[arg-type]


def test_strictest_picks_read_only_if_present():
    assert strictest_role([_srv("a", "sudo"), _srv("b", "read-only"), _srv("c", "shell")]) == "read-only"


def test_strictest_picks_shell_over_sudo():
    assert strictest_role([_srv("a", "sudo"), _srv("b", "shell")]) == "shell"


def test_strictest_all_sudo():
    assert strictest_role([_srv("a", "sudo"), _srv("b", "sudo")]) == "sudo"


def test_strictest_single_server():
    assert strictest_role([_srv("a", "read-only")]) == "read-only"
