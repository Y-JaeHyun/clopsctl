"""SSH jump host 체인 해석 테스트 (실제 paramiko 연결 없이 _resolve_jump_chain 만)."""
from __future__ import annotations

import pytest

from clopsctl.config import Server
from clopsctl.ssh import MAX_JUMP_DEPTH, _resolve_jump_chain


def _srv(name: str, **kw) -> Server:
    return Server(name=name, host=f"{name}.test", user="u", **kw)


def test_no_jump_returns_self():
    srv = _srv("a")
    assert _resolve_jump_chain(srv, {"a": srv}) == [srv]


def test_single_hop_chain_order_is_bastion_then_target():
    bastion = _srv("bastion")
    target = _srv("target", jump="bastion")
    chain = _resolve_jump_chain(target, {"bastion": bastion, "target": target})
    assert [s.name for s in chain] == ["bastion", "target"]


def test_unknown_jump_raises():
    target = _srv("target", jump="ghost")
    with pytest.raises(ValueError, match="unknown jump 'ghost'"):
        _resolve_jump_chain(target, {"target": target})


def test_self_referential_jump_detected():
    s = _srv("loop", jump="loop")
    with pytest.raises(ValueError, match="cycle"):
        _resolve_jump_chain(s, {"loop": s})


def test_two_hop_chain_exceeds_max_depth():
    """target → mid → bastion 이면 chain depth 2 → MAX_JUMP_DEPTH(=1) 초과."""
    bastion = _srv("bastion")
    mid = _srv("mid", jump="bastion")
    target = _srv("target", jump="mid")
    inv = {"bastion": bastion, "mid": mid, "target": target}
    with pytest.raises(ValueError, match=f"max depth {MAX_JUMP_DEPTH}"):
        _resolve_jump_chain(target, inv)


def test_cycle_through_bastion_detected():
    a = _srv("a", jump="b")
    b = _srv("b", jump="a")
    with pytest.raises(ValueError, match="cycle"):
        _resolve_jump_chain(a, {"a": a, "b": b})


def test_inventory_loader_parses_jump(tmp_path):
    """servers.toml 의 jump 필드가 Server.jump 로 잘 옮겨지는지."""
    from clopsctl.config import load_inventory

    p = tmp_path / "s.toml"
    p.write_text("""
[server.bastion]
host = "203.0.113.1"
user = "u"

[server.app]
host = "10.0.0.5"
user = "u"
jump = "bastion"
""")
    inv = load_inventory(p)
    assert inv["bastion"].jump is None
    assert inv["app"].jump == "bastion"
