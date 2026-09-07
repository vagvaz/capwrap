"""The grant table and escalation cards.

Phase 1: a per-container grant store, checked before a card is created, with
"grant" persisting so the same request never prompts again, and "explain"
returning the operator's text instead of deciding.

Phase 2: `capctl escalate` creates an escalation card; granting it applies the
capability live (network rule / spawn authority), structural limits are refused
with "respawn required".
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from capwrap.config import load_config_data
from capwrap.daemon import Daemon
from capwrap.ipc.protocol import Request, Response


@pytest.fixture
async def daemon(state_dir):
    d = Daemon(audit_path=Path(state_dir) / "audit.db")
    yield d
    await d.shutdown()


def config(name: str, base_dir: Path, **extra):
    return load_config_data({"name": name, **extra}, base_dir=base_dir)


async def request(socket_path: Path, op: str, args: dict | None = None) -> Response:
    reader, writer = await asyncio.open_unix_connection(str(socket_path))
    try:
        writer.write(Request(op=op, args=args or {}, id=1).encode())
        await writer.drain()
        line = await asyncio.wait_for(reader.readuntil(b"\n"), timeout=10)
        return Response.parse(line)
    finally:
        writer.close()


async def _serve(daemon, name, tmp_path, **extra):
    daemon.register(config(name, tmp_path, **extra))
    c = daemon.containers[name]
    c.server = await daemon._serve_container(c)
    return c


async def _wait_for_approval(daemon, timeout: float = 5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if daemon.pending_approvals():
            return daemon.pending_approvals()[0]
        await asyncio.sleep(0.01)
    raise AssertionError("no approval was queued")


def bash_ask(tool: str = "Bash", command: str = "git push origin main"):
    return {
        "question": f"{tool}: {command}",
        "context": {"tool": tool, "input": {"command": command}},
    }


# ==========================================================================
# grant table
# ==========================================================================


async def test_a_grant_auto_approves_and_skips_the_card(daemon, tmp_path):
    c = await _serve(daemon, "alpha", tmp_path)
    c.grants.add("bash(git push *)")

    reply = await request(c.paths.socket, "ask", bash_ask())
    assert reply.ok
    assert reply.result["decision"] == "allow"
    assert not daemon.pending_approvals(), (
        "a granted request must not reach the operator"
    )


async def test_deny_beats_grant(daemon, tmp_path):
    c = await _serve(
        daemon,
        "alpha",
        tmp_path,
        runtime={"auto_deny": ["Bash(sudo *)"]},
    )
    # A grant that would cover the request, but the deny list wins.
    c.grants.add("bash(sudo *)")

    reply = await request(c.paths.socket, "ask", bash_ask(command="sudo rm -rf /"))
    assert reply.ok
    assert reply.result["decision"] == "deny"
    assert not daemon.pending_approvals()


async def test_a_grant_persists_across_a_store_reload(daemon, tmp_path):
    c = await _serve(daemon, "alpha", tmp_path)
    c.grants.add("bash(git push *)")
    grants_path = c.paths.grants
    assert grants_path.exists()

    # A fresh store reading the same file still matches.
    from capwrap.grants import GrantStore
    from capwrap.kernel.policy import Rule

    reloaded = GrantStore(grants_path)
    assert reloaded.matches(Rule("bash", "git push origin main"))


async def test_granting_a_card_appends_to_the_table(daemon, tmp_path):
    c = await _serve(daemon, "alpha", tmp_path)

    asking = asyncio.ensure_future(
        request(c.paths.socket, "ask", bash_ask(command="git push origin main"))
    )
    pending = await _wait_for_approval(daemon)
    assert daemon.resolve_approval(pending["id"], "grant", "always fine")
    reply = await asyncio.wait_for(asking, timeout=5)

    assert reply.ok and reply.result["decision"] == "allow"
    grants = c.grants.list()
    assert grants, "granting must append to the grant table"
    assert grants[0]["pattern"] == "bash(git *)"

    # The same request now auto-approves without a card.
    again = await request(
        c.paths.socket, "ask", bash_ask(command="git push origin main")
    )
    assert again.result["decision"] == "allow"
    assert not daemon.pending_approvals()


async def test_reject_is_the_alias_for_deny(daemon, tmp_path):
    c = await _serve(daemon, "alpha", tmp_path)
    asking = asyncio.ensure_future(
        request(c.paths.socket, "ask", bash_ask(command="git push origin main"))
    )
    pending = await _wait_for_approval(daemon)
    assert daemon.resolve_approval(pending["id"], "reject", "no")
    reply = await asyncio.wait_for(asking, timeout=5)
    assert reply.result["decision"] == "reject"
    assert not c.grants.list(), "reject must not append to the grant table"


async def test_explain_returns_the_message_without_deciding(daemon, tmp_path):
    c = await _serve(daemon, "alpha", tmp_path)
    asking = asyncio.ensure_future(
        request(c.paths.socket, "ask", bash_ask(command="git push origin main"))
    )
    pending = await _wait_for_approval(daemon)
    assert daemon.resolve_approval(pending["id"], "explain", "use a PR instead")
    reply = await asyncio.wait_for(asking, timeout=5)

    assert reply.ok
    assert reply.result["decision"] == "explain"
    assert reply.result["message"] == "use a PR instead"
    assert not c.grants.list(), "explain must not grant anything"


async def test_revoking_a_grant_makes_it_ask_again(daemon, tmp_path):
    c = await _serve(daemon, "alpha", tmp_path)
    grant = c.grants.add("bash(git push *)")

    assert daemon.revoke_grant("alpha", grant["id"])
    assert not c.grants.list()

    asking = asyncio.ensure_future(
        request(c.paths.socket, "ask", bash_ask(command="git push origin main"))
    )
    await _wait_for_approval(daemon)
    daemon.resolve_approval(daemon.pending_approvals()[0]["id"], "reject", "")
    await asyncio.wait_for(asking, timeout=5)


# ==========================================================================
# escalation
# ==========================================================================


async def test_escalate_creates_an_escalation_card(daemon, tmp_path):
    c = await _serve(daemon, "alpha", tmp_path)
    asking = asyncio.ensure_future(
        request(
            c.paths.socket,
            "escalate",
            {
                "capability": "network",
                "pattern": "pypi\\.org:443",
                "reason": "need deps",
            },
        )
    )
    pending = await _wait_for_approval(daemon)
    assert pending["context"]["kind"] == "escalation"
    assert pending["context"]["capability"] == "network"
    assert pending["context"]["pattern"] == "pypi\\.org:443"
    assert pending["context"]["reason"] == "need deps"

    daemon.resolve_approval(pending["id"], "reject", "no")
    reply = await asyncio.wait_for(asking, timeout=5)
    assert reply.result["decision"] == "reject"


async def test_escalation_grant_appends_a_proxy_rule(daemon, tmp_path, monkeypatch):
    c = await _serve(daemon, "alpha", tmp_path)
    granted = []

    def fake_operator_grant(holder, kind, target, rights, **kw):
        if kind == "net_rule":
            granted.append(target)
        return {"slot": 1, "kind": kind, "label": "x", "rights": ["connect"]}

    monkeypatch.setattr(daemon.kernel, "operator_grant", fake_operator_grant)

    asking = asyncio.ensure_future(
        request(
            c.paths.socket,
            "escalate",
            {"capability": "network", "pattern": "pypi\\.org:443", "reason": ""},
        )
    )
    pending = await _wait_for_approval(daemon)
    assert daemon.resolve_approval(pending["id"], "grant", "ok")
    reply = await asyncio.wait_for(asking, timeout=5)

    assert reply.result["decision"] == "allow"
    assert granted == ["escalated=pypi\\.org:443"], "the network rule was not appended"


async def test_escalation_grant_records_spawn_authority(daemon, tmp_path):
    c = await _serve(daemon, "alpha", tmp_path)
    asking = asyncio.ensure_future(
        request(
            c.paths.socket,
            "escalate",
            {"capability": "spawn", "pattern": "Bash(*)", "reason": ""},
        )
    )
    pending = await _wait_for_approval(daemon)
    assert daemon.resolve_approval(pending["id"], "grant", "ok")
    reply = await asyncio.wait_for(asking, timeout=5)

    assert reply.result["decision"] == "allow"
    assert "Bash(*)" in c.spawn_grants


async def test_structural_escalation_is_refused_without_a_card(daemon, tmp_path):
    c = await _serve(daemon, "alpha", tmp_path)
    reply = await request(
        c.paths.socket,
        "escalate",
        {"capability": "worktree", "pattern": "/work", "reason": ""},
    )
    assert reply.ok
    assert reply.result["decision"] == "respawn_required"
    assert "respawn" in reply.result["message"]
    assert not daemon.pending_approvals(), "structural limits must not create a card"
