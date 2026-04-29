from clopsctl.safety import is_dangerous


def test_safe_commands():
    assert is_dangerous("ls -la /var/log") is None
    assert is_dangerous("df -h") is None
    assert is_dangerous("rm -rf /tmp/foo") is None  # / + 단어 경계, /tmp 는 단어 안


def test_dangerous_rm_root():
    assert is_dangerous("rm -rf /") is not None
    assert is_dangerous("rm -rf / --no-preserve-root") is not None


def test_dangerous_shutdown_reboot():
    assert is_dangerous("sudo shutdown now") is not None
    assert is_dangerous("reboot") is not None


def test_dangerous_dd_to_disk():
    assert is_dangerous("dd if=/dev/zero of=/dev/sda bs=1M") is not None


def test_dangerous_fork_bomb():
    assert is_dangerous(":(){ :|:& };:") is not None
