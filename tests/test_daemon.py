"""The daemon: IPC attribution, message delivery, and live containers.

The first test here is the important one.  Everything else in the capability
model rests on the daemon knowing *who is calling*, and it establishes that from
the socket a connection arrived on rather than from anything the caller says.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path

import pytest

from capwrap.config import load_config_data
from capwrap.daemon import Daemon
from capwrap.errors import CapwrapError
from capwrap.ipc.protocol import Request, Response
from capwrap.runtime import supervisor


@pytest.fixture
async def daemon(state_dir):
    d = Daemon(audit_path=Path(state_dir) / "audit.db")
    yield d
    await d.shutdown()


def config(name: str, base_dir: Path, **extra):
    return load_config_data({"name": name, **extra}, base_dir=base_dir)


async def request(socket_path: Path, op: str, args: dict | None = None) -> Response:
    """Talk to a container's control socket the way capctl does."""
    reader, writer = await asyncio.open_unix_connection(str(socket_path))
    try:
        writer.write(Request(op=op, args=args or {}, id=1).encode())
        await writer.drain()
        line = await asyncio.wait_for(reader.readuntil(b"\n"), timeout=10)
        return Response.parse(line)
    finally:
        writer.close()


# ==========================================================================
# identity
# ==========================================================================


async def test_identity_comes_from_the_socket_not_the_request(daemon, tmp_path):
    """An agent cannot claim to be another container, because it never claims at all.

    Both containers run the same request with the same bytes on the wire; the
    answers differ purely because the sockets differ.
    """
    for name in ("alpha", "beta"):
        daemon.register(config(name, tmp_path))
        container = daemon.containers[name]
        container.server = await daemon._serve_container(container)

    alpha = await request(daemon.containers["alpha"].paths.socket, "whoami")
    beta = await request(daemon.containers["beta"].paths.socket, "whoami")

    assert alpha.result["container"] == "alpha"
    assert beta.result["container"] == "beta"


async def test_a_request_cannot_smuggle_in_an_actor(daemon, tmp_path):
    daemon.register(config("alpha", tmp_path))
    container = daemon.containers["alpha"]
    container.server = await daemon._serve_container(container)

    # There is no field for it, and adding one changes nothing.
    reader, writer = await asyncio.open_unix_connection(str(container.paths.socket))
    writer.write(
        json.dumps(
            {"id": 1, "op": "whoami", "actor": "root", "container": "beta"}
        ).encode()
        + b"\n"
    )
    await writer.drain()
    reply = Response.parse(await asyncio.wait_for(reader.readuntil(b"\n"), timeout=10))
    writer.close()

    assert reply.result["container"] == "alpha"


async def test_unknown_operations_are_refused(daemon, tmp_path):
    daemon.register(config("alpha", tmp_path))
    container = daemon.containers["alpha"]
    container.server = await daemon._serve_container(container)

    reply = await request(container.paths.socket, "kernel.destroy_container")
    assert not reply.ok
    assert reply.code == "protocol_error"


async def test_malformed_json_does_not_kill_the_connection(daemon, tmp_path):
    daemon.register(config("alpha", tmp_path))
    container = daemon.containers["alpha"]
    container.server = await daemon._serve_container(container)

    reader, writer = await asyncio.open_unix_connection(str(container.paths.socket))
    writer.write(b"{not json\n")
    await writer.drain()
    bad = Response.parse(await asyncio.wait_for(reader.readuntil(b"\n"), timeout=10))
    assert not bad.ok and bad.code == "protocol_error"

    writer.write(Request(op="whoami", id=2).encode())
    await writer.drain()
    good = Response.parse(await asyncio.wait_for(reader.readuntil(b"\n"), timeout=10))
    writer.close()
    assert good.ok, "one bad request should not poison the session"


async def test_internal_errors_do_not_leak_details_to_an_agent(daemon, tmp_path):
    daemon.register(config("alpha", tmp_path))
    container = daemon.containers["alpha"]
    container.server = await daemon._serve_container(container)

    # Missing required argument -> a KeyError inside the daemon.
    reply = await request(container.paths.socket, "cap.info", {})
    assert not reply.ok
    assert reply.code == "internal_error"
    assert "KeyError" not in (reply.message or "")


# ==========================================================================
# capability operations over the wire
# ==========================================================================


async def test_caps_are_listed_without_exposing_object_ids(daemon, tmp_path):
    daemon.register(config("alpha", tmp_path))
    container = daemon.containers["alpha"]
    container.server = await daemon._serve_container(container)

    reply = await request(container.paths.socket, "cap.list")
    assert reply.ok
    labels = {c["label"] for c in reply.result}
    assert labels == {"self", "operator"}
    for cap in reply.result:
        assert "oid" not in cap and "oid" not in cap["detail"]


async def test_messages_flow_between_containers(daemon, tmp_path):
    peers = {"caps": {"peers": [{"container": "beta", "rights": ["send"]}]}}
    daemon.register(config("alpha", tmp_path, **peers))
    daemon.register(config("beta", tmp_path))
    daemon.link_all_peers()

    for name in ("alpha", "beta"):
        c = daemon.containers[name]
        c.server = await daemon._serve_container(c)

    caps = (await request(daemon.containers["alpha"].paths.socket, "cap.list")).result
    peer_slot = [c["slot"] for c in caps if c["label"] == "peer:beta"][0]

    sent = await request(
        daemon.containers["alpha"].paths.socket,
        "msg.send",
        {"slot": peer_slot, "payload": "the build is green"},
    )
    assert sent.ok and sent.result["delivered_to"] == "beta"

    got = await request(
        daemon.containers["beta"].paths.socket, "msg.recv", {"timeout": 0}
    )
    assert got.ok
    assert got.result[0]["from"] == "alpha"
    assert got.result[0]["payload"] == "the build is green"


async def test_sending_without_a_capability_is_denied_and_audited(daemon, tmp_path):
    daemon.register(config("alpha", tmp_path))
    daemon.register(config("beta", tmp_path))
    for name in ("alpha", "beta"):
        c = daemon.containers[name]
        c.server = await daemon._serve_container(c)

    reply = await request(
        daemon.containers["alpha"].paths.socket,
        "msg.send",
        {"slot": 77, "payload": "hello?"},
    )
    assert not reply.ok
    assert reply.code == "no_such_cap"

    denied = daemon.audit.tail(denied_only=True)
    assert any(d["actor"] == "alpha" and d["op"] == "msg.send" for d in denied)


async def test_messages_are_mirrored_into_shared_inbox(daemon, tmp_path):
    """An agent that never polls the socket still trips over its mail."""
    peers = {"caps": {"peers": [{"container": "beta", "rights": ["send"]}]}}
    daemon.register(config("alpha", tmp_path, **peers))
    daemon.register(config("beta", tmp_path, runtime={"notify": "file"}))
    daemon.link_all_peers()
    for name in ("alpha", "beta"):
        c = daemon.containers[name]
        c.paths.ensure()
        c.server = await daemon._serve_container(c)

    caps = (await request(daemon.containers["alpha"].paths.socket, "cap.list")).result
    slot = [c["slot"] for c in caps if c["label"] == "peer:beta"][0]
    await request(
        daemon.containers["alpha"].paths.socket,
        "msg.send",
        {"slot": slot, "payload": "check your inbox"},
    )

    inbox = daemon.containers["beta"].paths.shared / "inbox"
    files = list(inbox.glob("*.json"))
    assert files, "no inbox file was written"
    assert "check your inbox" in files[0].read_text()


async def test_delegation_over_the_wire_notifies_the_recipient(daemon, tmp_path):
    peers = {
        "caps": {
            "peers": [
                {"container": "beta", "rights": ["send", "inspect", "delegate"]},
            ]
        }
    }
    daemon.register(config("alpha", tmp_path, **peers))
    daemon.register(config("beta", tmp_path))
    daemon.link_all_peers()
    for name in ("alpha", "beta"):
        c = daemon.containers[name]
        c.server = await daemon._serve_container(c)

    caps = (await request(daemon.containers["alpha"].paths.socket, "cap.list")).result
    slot = [c["slot"] for c in caps if c["label"] == "peer:beta"][0]

    reply = await request(
        daemon.containers["alpha"].paths.socket,
        "cap.delegate",
        {"target_slot": slot, "cap_slot": slot, "rights": ["send"]},
    )
    assert reply.ok

    beta_caps = (
        await request(daemon.containers["beta"].paths.socket, "cap.list")
    ).result
    handed = [c for c in beta_caps if c["slot"] == reply.result["slot"]][0]
    assert handed["rights"] == ["send"]

    mail = (
        await request(
            daemon.containers["beta"].paths.socket, "msg.recv", {"timeout": 0}
        )
    ).result
    assert any(m["kind"] == "capability" for m in mail)


# ==========================================================================
# dataspace mapping
# ==========================================================================


async def test_mapping_a_dataspace_materialises_it_and_revoking_removes_it(
    daemon, tmp_path
):
    source = tmp_path / "notes"
    source.mkdir()
    (source / "finding.md").write_text("the bug is in the parser\n")

    alpha_caps = {
        "caps": {
            "peers": [{"container": "beta", "rights": ["send"]}],
            "dataspaces": [
                {"path": str(source), "rights": ["read", "copy", "delegate"]}
            ],
        }
    }
    daemon.register(config("alpha", tmp_path, **alpha_caps))
    daemon.register(config("beta", tmp_path))
    daemon.link_all_peers()
    for name in ("alpha", "beta"):
        c = daemon.containers[name]
        c.paths.ensure()
        c.server = await daemon._serve_container(c)

    caps = (await request(daemon.containers["alpha"].paths.socket, "cap.list")).result
    peer_slot = [c["slot"] for c in caps if c["label"] == "peer:beta"][0]
    ds_slot = [c["slot"] for c in caps if c["kind"] == "dataspace"][0]

    reply = await request(
        daemon.containers["alpha"].paths.socket,
        "ds.map",
        {"target_slot": peer_slot, "ds_slot": ds_slot, "dest": "notes", "mode": "copy"},
    )
    assert reply.ok

    landed = daemon.containers["beta"].paths.shared / "notes" / "finding.md"
    assert landed.exists(), "the dataspace never reached beta's /shared"
    assert "parser" in landed.read_text()

    # Revoking the mapping takes the files back too, not just the authority.
    granted_slot = reply.result["slot"]
    node = daemon.kernel.tasks["beta"].slots[granted_slot].node
    killed = daemon.kernel.mapdb.revoke(node)
    daemon.kernel._apply_revocations(killed)
    assert not landed.parent.exists(), "revocation left the data behind"


async def test_map_mode_requires_the_stronger_right(daemon, tmp_path):
    source = tmp_path / "notes"
    source.mkdir()
    alpha_caps = {
        "caps": {
            "peers": [{"container": "beta", "rights": ["send"]}],
            # copy but not map
            "dataspaces": [{"path": str(source), "rights": ["read", "copy"]}],
        }
    }
    daemon.register(config("alpha", tmp_path, **alpha_caps))
    daemon.register(config("beta", tmp_path))
    daemon.link_all_peers()
    for name in ("alpha", "beta"):
        c = daemon.containers[name]
        c.paths.ensure()
        c.server = await daemon._serve_container(c)

    caps = (await request(daemon.containers["alpha"].paths.socket, "cap.list")).result
    peer_slot = [c["slot"] for c in caps if c["label"] == "peer:beta"][0]
    ds_slot = [c["slot"] for c in caps if c["kind"] == "dataspace"][0]

    reply = await request(
        daemon.containers["alpha"].paths.socket,
        "ds.map",
        {"target_slot": peer_slot, "ds_slot": ds_slot, "dest": "n", "mode": "map"},
    )
    assert not reply.ok and reply.code == "insufficient_rights"


# ==========================================================================
# operator approvals
# ==========================================================================


async def test_ask_blocks_until_the_operator_answers(daemon, tmp_path):
    daemon.register(config("alpha", tmp_path))
    c = daemon.containers["alpha"]
    c.server = await daemon._serve_container(c)

    asking = asyncio.ensure_future(
        request(c.paths.socket, "ask", {"question": "may I install curl?"})
    )
    await asyncio.sleep(0.05)

    pending = daemon.pending_approvals()
    assert len(pending) == 1
    assert pending[0]["question"] == "may I install curl?"
    assert pending[0]["container"] == "alpha"

    assert daemon.resolve_approval(pending[0]["id"], "allow", "go ahead")
    reply = await asyncio.wait_for(asking, timeout=5)
    assert reply.ok and reply.result["decision"] == "allow"


async def test_ask_times_out_rather_than_hanging_forever(daemon, tmp_path):
    daemon.register(config("alpha", tmp_path))
    c = daemon.containers["alpha"]
    c.server = await daemon._serve_container(c)

    reply = await request(
        c.paths.socket, "ask", {"question": "anyone there?", "timeout": 0.1}
    )
    assert reply.ok and reply.result["decision"] == "timeout"


async def test_pending_approvals_carry_a_kind_for_the_inbox_tabs(daemon, tmp_path):
    """The console splits its inbox on `kind`; the daemon tags each card.

    A permission request (a tool in its context) and every structured card --
    capability request, escalation, permission escalation -- is an *approval*;
    a plain question is conversation.
    """
    daemon.register(config("alpha", tmp_path))
    c = daemon.containers["alpha"]
    c.server = await daemon._serve_container(c)

    # A plain question: conversation, not a permission decision.
    asking = asyncio.ensure_future(
        request(c.paths.socket, "ask", {"question": "tea or coffee?"})
    )
    await asyncio.sleep(0.05)
    pending = daemon.pending_approvals()[0]
    assert pending["kind"] == "question"
    daemon.resolve_approval(pending["id"], "explain", "tea")
    await asyncio.wait_for(asking, timeout=5)

    # A permission request (a tool in its context): an approval.
    asking = asyncio.ensure_future(
        request(
            c.paths.socket,
            "ask",
            {
                "question": "Bash: git push",
                "context": {"tool": "Bash", "input": {"command": "git push"}},
            },
        )
    )
    await asyncio.sleep(0.05)
    pending = daemon.pending_approvals()[0]
    assert pending["kind"] == "approval"
    daemon.resolve_approval(pending["id"], "reject", "")
    await asyncio.wait_for(asking, timeout=5)

    # An escalation card: an approval.
    asking = asyncio.ensure_future(
        request(
            c.paths.socket,
            "escalate",
            {"capability": "network", "pattern": "pypi\\.org:443", "reason": ""},
        )
    )
    await asyncio.sleep(0.05)
    pending = daemon.pending_approvals()[0]
    assert pending["kind"] == "approval"
    daemon.resolve_approval(pending["id"], "reject", "")
    await asyncio.wait_for(asking, timeout=5)


async def test_an_ask_with_options_lands_them_in_the_context(daemon, tmp_path):
    """`capctl ask --options a,b` reaches the operator as clickable chips."""
    daemon.register(config("alpha", tmp_path))
    c = daemon.containers["alpha"]
    c.server = await daemon._serve_container(c)

    asking = asyncio.ensure_future(
        request(
            c.paths.socket,
            "ask",
            {
                "question": "which branch?",
                "context": {"options": ["main", "release"]},
            },
        )
    )
    await asyncio.sleep(0.05)
    pending = daemon.pending_approvals()[0]
    assert pending["context"]["options"] == ["main", "release"]
    assert pending["kind"] == "question"

    daemon.resolve_approval(pending["id"], "explain", "main")
    reply = await asyncio.wait_for(asking, timeout=5)
    assert reply.ok and reply.result["message"] == "main"


async def test_a_question_answered_via_explain_gets_the_text_back(daemon, tmp_path):
    """Answering a plain question rides the explain path: the operator's text
    goes straight back to the agent as the ask's result, deciding nothing."""
    daemon.register(config("alpha", tmp_path))
    c = daemon.containers["alpha"]
    c.server = await daemon._serve_container(c)

    asking = asyncio.ensure_future(
        request(c.paths.socket, "ask", {"question": "which port?"})
    )
    await asyncio.sleep(0.05)
    pending = daemon.pending_approvals()[0]
    assert pending["kind"] == "question"

    assert daemon.resolve_approval(pending["id"], "explain", "use 8080")
    reply = await asyncio.wait_for(asking, timeout=5)

    assert reply.ok
    assert reply.result["decision"] == "explain"
    assert reply.result["message"] == "use 8080"
    assert not daemon.pending_approvals(), "a resolved question leaves the queue"


# ==========================================================================
# question routing -- where plain questions surface
# ==========================================================================


async def test_question_routing_defaults_to_forward(daemon, tmp_path):
    """The default position is today's behaviour: a card in the Questions tab."""
    daemon.register(config("alpha", tmp_path))
    c = daemon.containers["alpha"]
    c.server = await daemon._serve_container(c)

    asking = asyncio.ensure_future(
        request(c.paths.socket, "ask", {"question": "tea or coffee?"})
    )
    await asyncio.sleep(0.05)
    pending = daemon.pending_approvals()
    assert len(pending) == 1
    assert pending[0]["kind"] == "question"
    daemon.resolve_approval(pending[0]["id"], "explain", "tea")
    await asyncio.wait_for(asking, timeout=5)


async def test_block_routing_returns_guidance_without_a_card(daemon, tmp_path):
    """block: no operator ping, no card -- the agent is told to state its
    question in its own terminal and end its turn."""
    daemon.register(config("alpha", tmp_path, runtime={"question_routing": "block"}))
    c = daemon.containers["alpha"]
    c.server = await daemon._serve_container(c)

    reply = await request(c.paths.socket, "ask", {"question": "which port?"})

    assert reply.ok
    assert reply.result["decision"] == "block"
    assert "operator" in reply.result["message"]
    assert "terminal" in reply.result["message"]
    assert not daemon.pending_approvals(), "block must not create a card"

    blocked = daemon.audit.tail(limit=20)
    assert any(
        e["op"] == "ask" and "blocked" in str(e.get("detail", "")) for e in blocked
    ), "the blocked question should be in the audit log"


async def test_auto_routing_answers_and_records_without_a_card(daemon, tmp_path):
    """auto: the question is auto-answered, recorded as already answered for
    later review, and never becomes a pending card."""
    daemon.register(config("alpha", tmp_path, runtime={"question_routing": "auto"}))
    c = daemon.containers["alpha"]
    c.server = await daemon._serve_container(c)

    reply = await request(c.paths.socket, "ask", {"question": "which port?"})

    assert reply.ok
    assert reply.result["decision"] == "explain"
    assert "best judgment" in reply.result["message"]
    assert not daemon.pending_approvals(), "auto must not create a pending card"

    inbox = daemon.overview()["operator_inbox"]
    recorded = [m for m in inbox if m["kind"] == "question"][-1]
    assert recorded["payload"]["decision"] == "auto"
    assert "best judgment" in recorded["payload"]["reason"]
    assert recorded["payload"]["question"] == "which port?"


async def test_auto_routing_never_auto_answers_a_permission_request(daemon, tmp_path):
    """The invariant: a permission request in auto mode still creates a card.

    Routing governs where *questions* surface; a request with a tool in its
    context is a permission decision and always reaches the operator.
    """
    daemon.register(config("alpha", tmp_path, runtime={"question_routing": "auto"}))
    c = daemon.containers["alpha"]
    c.server = await daemon._serve_container(c)

    asking = asyncio.ensure_future(
        request(
            c.paths.socket,
            "ask",
            {
                "question": "Bash: git push",
                "context": {"tool": "Bash", "input": {"command": "git push"}},
            },
        )
    )
    await asyncio.sleep(0.05)
    pending = daemon.pending_approvals()
    assert len(pending) == 1, "a permission request must still reach the operator"
    assert pending[0]["kind"] == "approval"
    daemon.resolve_approval(pending[0]["id"], "reject", "")
    await asyncio.wait_for(asking, timeout=5)


async def test_auto_routing_never_auto_answers_an_escalation(daemon, tmp_path):
    """Same invariant for escalations: crossing a boundary always needs a card."""
    daemon.register(config("alpha", tmp_path, runtime={"question_routing": "auto"}))
    c = daemon.containers["alpha"]
    c.server = await daemon._serve_container(c)

    asking = asyncio.ensure_future(
        request(
            c.paths.socket,
            "escalate",
            {"capability": "network", "pattern": "pypi\\.org:443", "reason": ""},
        )
    )
    await asyncio.sleep(0.05)
    pending = daemon.pending_approvals()
    assert len(pending) == 1, "an escalation must still reach the operator"
    assert pending[0]["kind"] == "approval"
    daemon.resolve_approval(pending[0]["id"], "reject", "")
    await asyncio.wait_for(asking, timeout=5)


async def test_the_routing_override_endpoint_validates_and_applies(daemon, tmp_path):
    """POST /api/containers/{name}/routing overrides the running container's
    routing in memory, and refuses anything that is not a routing position."""
    from capwrap.web.app import create_app
    from fastapi.testclient import TestClient

    daemon.register(config("alpha", tmp_path))
    client = TestClient(create_app(daemon))

    ok = client.post("/api/containers/alpha/routing", json={"routing": "block"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["routing"] == "block"

    # The override takes effect immediately, without touching the config.
    assert daemon.containers["alpha"].effective_question_routing == "block"
    assert daemon.containers["alpha"].config.runtime.question_routing == "forward"

    bad = client.post("/api/containers/alpha/routing", json={"routing": "sideways"})
    assert bad.status_code == 400, bad.text

    missing = client.post("/api/containers/ghost/routing", json={"routing": "auto"})
    assert missing.status_code == 400, missing.text


def test_capctl_ask_options_flag_lands_in_the_context(monkeypatch):
    """`capctl ask --options a,b` becomes an `options` list in the context.

    The guest CLI is the only way an agent can attach answer choices to a
    question; the console renders them as chips that fill the reply box.
    """
    from capwrap.guest import capctl

    seen = {}

    def fake_call(op, args, timeout=None):
        seen["op"] = op
        seen["args"] = args
        return {"decision": "allow", "reason": "ok"}

    monkeypatch.setattr(capctl, "call", fake_call)
    args = capctl.build_parser().parse_args(
        ["ask", "which branch?", "--options", "main, release, hotfix"]
    )
    capctl.cmd_ask(args)

    assert seen["op"] == "ask"
    assert seen["args"]["context"]["options"] == ["main", "release", "hotfix"]


# ==========================================================================
# live containers
# ==========================================================================


@pytest.mark.sandbox
async def test_a_running_container_can_use_capctl(daemon, tmp_path, require_sandbox):
    """The whole stack: sandbox, socket, guest CLI, kernel, mailbox."""
    peers = {"caps": {"peers": [{"container": "beta", "rights": ["send"]}]}}
    daemon.register(
        config(
            "alpha",
            tmp_path,
            runtime={
                "command": ["/bin/bash", "-c", "capctl caps && capctl whoami"],
                "tty": True,
            },
            **peers,
        )
    )
    daemon.register(config("beta", tmp_path))
    daemon.link_all_peers()

    await daemon.start("beta")
    container = await daemon.start("alpha")
    code = await asyncio.wait_for(container.session.wait(), timeout=60)

    output = container.session.scrollback().decode(errors="replace")
    assert code == 0, output
    assert "peer:beta" in output, output
    # `whoami` prints for a person, not for a parser -- `--json` is the parser's
    # form. It also names the container's signing key, which is how an agent
    # finds out it has one.
    assert "container: alpha" in output, output
    assert "key:" in output, output


@pytest.mark.sandbox
async def test_two_live_agents_message_each_other(daemon, tmp_path, require_sandbox):
    peers = {"caps": {"peers": [{"container": "beta", "rights": ["send"]}]}}
    daemon.register(
        config(
            "beta",
            tmp_path,
            runtime={"command": ["/bin/bash", "-c", "capctl recv --wait --timeout 30"]},
        )
    )
    daemon.register(
        config(
            "alpha",
            tmp_path,
            # Addressed by label rather than slot number -- the way an agent would.
            runtime={
                "command": [
                    "/bin/bash",
                    "-c",
                    "sleep 0.5; capctl send peer:beta 'hello from alpha'",
                ]
            },
            **peers,
        )
    )
    daemon.link_all_peers()

    beta = await daemon.start("beta")
    alpha = await daemon.start("alpha")

    await asyncio.wait_for(alpha.session.wait(), timeout=60)
    await asyncio.wait_for(beta.session.wait(), timeout=60)

    received = beta.session.scrollback().decode(errors="replace")
    assert "hello from alpha" in received, received


# ==========================================================================
# the PreToolUse hook -- bundling permission prompts
# ==========================================================================

HOOK_EVENT = (
    '{{"hook_event_name":"PreToolUse","tool_name":"{tool}","tool_input":{input}}}'
)


def hook_command(tool: str, tool_input: str) -> list[str]:
    """Feed one PreToolUse event to the hook, the way Claude Code would."""
    event = HOOK_EVENT.format(tool=tool, input=tool_input)
    return ["/bin/bash", "-c", f"echo '{event}' | /opt/capwrap/hook.py"]


@pytest.mark.sandbox
async def test_hook_routes_a_tool_prompt_to_the_operator(
    daemon, tmp_path, require_sandbox
):
    """A blocked agent's prompt shows up in the operator's one queue.

    This is the feature that stops five agents meaning five terminals to watch:
    the agent is stuck inside the hook until someone answers here.
    """
    daemon.register(
        config(
            "hooked",
            tmp_path,
            runtime={
                "approvals": "capwrap",
                "auto_allow": ["Read"],
                "command": hook_command("Bash", '{"command":"rm -rf /work"}'),
            },
        )
    )
    container = await daemon.start("hooked")

    for _ in range(200):
        if daemon.pending_approvals():
            break
        await asyncio.sleep(0.05)

    pending = daemon.pending_approvals()
    assert pending, "the hook never reached the operator"
    assert pending[0]["container"] == "hooked"
    assert "rm -rf /work" in pending[0]["question"]
    assert pending[0]["context"]["tool"] == "Bash"

    daemon.resolve_approval(pending[0]["id"], "deny", "that would delete your branch")
    await asyncio.wait_for(container.session.wait(), timeout=30)

    verdict = container.session.scrollback().decode(errors="replace")
    assert '"permissionDecision": "deny"' in verdict, verdict
    assert "delete your branch" in verdict


@pytest.mark.sandbox
async def test_hook_auto_allows_without_troubling_the_operator(
    daemon, tmp_path, require_sandbox
):
    """Nobody wants to approve every Read; the policy decides those locally."""
    daemon.register(
        config(
            "quiet",
            tmp_path,
            runtime={
                "approvals": "capwrap",
                "auto_allow": ["Read"],
                "command": hook_command("Read", '{"file_path":"/work/a.py"}'),
            },
        )
    )
    container = await daemon.start("quiet")
    await asyncio.wait_for(container.session.wait(), timeout=30)

    output = container.session.scrollback().decode(errors="replace")
    assert '"permissionDecision": "allow"' in output, output
    assert not daemon.pending_approvals(), "a Read should never reach the human"


@pytest.mark.sandbox
async def test_hook_policy_is_read_only_to_the_agent(daemon, tmp_path, require_sandbox):
    """An agent must not be able to widen its own permissions.

    It has a shell, so if the policy file were writable the entire approval
    mechanism would be advisory.
    """
    daemon.register(
        config(
            "sneaky",
            tmp_path,
            runtime={
                "approvals": "capwrap",
                "auto_deny": ["Bash(sudo *)"],
                "command": [
                    "/bin/bash",
                    "-c",
                    'echo \'{"allow":["*"]}\' > $CAPWRAP_POLICY 2>&1; '
                    "cat $CAPWRAP_POLICY",
                ],
            },
        )
    )
    container = await daemon.start("sneaky")
    await asyncio.wait_for(container.session.wait(), timeout=30)

    output = container.session.scrollback().decode(errors="replace")
    assert "Read-only file system" in output or "Permission denied" in output, output
    assert '"allow": []' in output or "sudo" in output, output


# ==========================================================================
# teardown -- nothing may outlive its container
# ==========================================================================


def live_sleepers() -> list[str]:
    """PIDs of `sleep 999x` processes, matched on exact argv.

    Scanned from /proc rather than with `pgrep -f`, which would also match the
    shell running the check -- the false positive that makes "did it actually
    die?" impossible to answer honestly.
    """
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            argv = [a for a in (entry / "cmdline").read_bytes().split(b"\0") if a]
        except OSError:
            continue
        if len(argv) == 2 and argv[0].endswith(b"sleep") and argv[1].startswith(b"999"):
            found.append(entry.name)
    return found


@pytest.mark.sandbox
async def test_stopping_a_container_kills_everything_it_started(
    daemon, tmp_path, require_sandbox
):
    """Including processes that deliberately escape the process group.

    `setsid` puts a process in its own session, so killing the container's
    process group misses it. The PID namespace is what actually guarantees
    teardown: bwrap is pid 1 inside it, and the kernel SIGKILLs the rest of the
    namespace when pid 1 exits.
    """
    before = set(live_sleepers())
    daemon.register(
        config(
            "victim",
            tmp_path,
            runtime={
                "command": [
                    "/bin/bash",
                    "-c",
                    "sleep 9999 & "
                    "nohup sleep 9998 >/dev/null 2>&1 & "
                    "setsid sleep 9997 >/dev/null 2>&1 </dev/null & "
                    "sleep 9996",
                ]
            },
        )
    )
    await daemon.start("victim")
    await asyncio.sleep(2)

    started = set(live_sleepers()) - before
    assert len(started) == 4, f"expected 4 background processes, saw {started}"

    await daemon.stop("victim")
    await asyncio.sleep(1.5)

    survivors = set(live_sleepers()) & started
    assert not survivors, f"processes outlived their container: {survivors}"


def test_a_config_cannot_opt_out_of_the_pid_namespace(tmp_path):
    """The guarantee above depends on it, so it is not configurable."""
    from capwrap.errors import ConfigError

    with pytest.raises(ConfigError, match="must include 'pid'"):
        load_config_data(
            {"name": "leaky", "sandbox": {"unshare": ["ipc", "uts"]}},
            base_dir=tmp_path,
        )


# ==========================================================================
# permission escalation through spawning
# ==========================================================================


def spawn_request(name: str, permissions: dict) -> dict:
    return {
        "factory_slot": 3,
        "config": {"name": name, "runtime": {"permissions": permissions}},
    }


async def parent_with_factory(daemon, tmp_path, *, permissions, envelope=None):
    runtime = {"permissions": permissions}
    if envelope is not None:
        runtime["permission_envelope"] = envelope
    daemon.register(
        config(
            "boss",
            tmp_path,
            runtime=runtime,
            caps={"factory": {"rights": ["create"], "quota": {"containers": 5}}},
        )
    )
    container = daemon.containers["boss"]
    container.server = await daemon._serve_container(container)
    # Spawning must not actually launch a sandbox in these tests.
    daemon.spawn_container = lambda cfg, parent: daemon.register(cfg, parent=parent).obj
    return container


async def test_a_narrower_child_spawns_without_asking_anyone(daemon, tmp_path):
    container = await parent_with_factory(
        daemon,
        tmp_path,
        permissions={"allow": ["Read", "Bash(git *)"], "deny": ["Bash(sudo *)"]},
    )
    reply = await request(
        container.paths.socket,
        "ctr.spawn",
        spawn_request(
            "child",
            {"allow": ["Read"], "deny": ["Bash(sudo *)"]},
        ),
    )
    assert reply.ok, reply.message
    assert not daemon.pending_approvals(), "narrowing must never reach the operator"


async def test_a_child_inherits_the_parents_permissions_when_it_asks_for_none(
    daemon, tmp_path
):
    permissions = {"allow": ["Read"], "deny": ["Bash(sudo *)"]}
    container = await parent_with_factory(daemon, tmp_path, permissions=permissions)

    reply = await request(
        container.paths.socket,
        "ctr.spawn",
        {
            "factory_slot": 3,
            "config": {"name": "child"},
        },
    )
    assert reply.ok, reply.message
    child = daemon.containers["child"]
    assert child.config.runtime.permissions.allow == ["Read"]
    assert child.config.runtime.permissions.deny == ["Bash(sudo *)"]


async def test_a_wider_child_is_blocked_until_the_operator_agrees(daemon, tmp_path):
    """The hole this whole mechanism exists to close.

    A container confined to Read tries to spawn a child that can run anything --
    which would let it act through the child. The factory capability alone says
    nothing about tool permissions, so without this check the spawn succeeds.
    """
    container = await parent_with_factory(
        daemon,
        tmp_path,
        permissions={"allow": ["Read"], "deny": ["Bash(sudo *)"]},
    )

    spawning = asyncio.ensure_future(
        request(
            container.paths.socket,
            "ctr.spawn",
            spawn_request(
                "overreach", {"allow": ["Read", "Bash(*)"], "deny": ["Bash(sudo *)"]}
            ),
        )
    )
    await asyncio.sleep(0.1)

    pending = daemon.pending_approvals()
    assert len(pending) == 1, "the escalation did not reach the operator"
    assert pending[0]["context"]["kind"] == "permission_escalation"
    assert any("Bash(*)" in r for r in pending[0]["context"]["reasons"])

    daemon.resolve_approval(pending[0]["id"], "deny", "no")
    reply = await asyncio.wait_for(spawning, timeout=5)

    assert not reply.ok
    assert "operator declined" in (reply.message or "")
    assert "overreach" not in daemon.containers


async def test_the_operator_can_approve_an_escalation(daemon, tmp_path):
    container = await parent_with_factory(
        daemon,
        tmp_path,
        permissions={"allow": ["Read"]},
    )
    spawning = asyncio.ensure_future(
        request(
            container.paths.socket,
            "ctr.spawn",
            spawn_request("wider", {"allow": ["Read", "Write"]}),
        )
    )
    await asyncio.sleep(0.1)

    pending = daemon.pending_approvals()
    daemon.resolve_approval(pending[0]["id"], "allow", "this one is fine")
    reply = await asyncio.wait_for(spawning, timeout=5)

    assert reply.ok, reply.message
    assert "wider" in daemon.containers


async def test_an_envelope_pre_authorises_a_range(daemon, tmp_path):
    """The less human-invasive route: decide once in the config, not per spawn."""
    container = await parent_with_factory(
        daemon,
        tmp_path,
        permissions={"allow": ["Read"], "deny": ["Bash(sudo *)"]},
        envelope={"allow": ["Read", "Bash(git *)"], "deny": ["Bash(sudo *)"]},
    )

    # Inside the envelope, though beyond the parent's own policy: no prompt.
    reply = await request(
        container.paths.socket,
        "ctr.spawn",
        spawn_request(
            "helper",
            {"allow": ["Read", "Bash(git status)"], "deny": ["Bash(sudo *)"]},
        ),
    )
    assert reply.ok, reply.message
    assert not daemon.pending_approvals()

    # Beyond the envelope: still stops.
    spawning = asyncio.ensure_future(
        request(
            container.paths.socket,
            "ctr.spawn",
            spawn_request("greedy", {"allow": ["Bash(*)"], "deny": ["Bash(sudo *)"]}),
        )
    )
    await asyncio.sleep(0.1)
    assert daemon.pending_approvals()
    daemon.resolve_approval(daemon.pending_approvals()[0]["id"], "deny", "")
    await asyncio.wait_for(spawning, timeout=5)


async def test_escalation_attempts_are_audited(daemon, tmp_path):
    container = await parent_with_factory(
        daemon,
        tmp_path,
        permissions={"allow": ["Read"]},
    )
    spawning = asyncio.ensure_future(
        request(
            container.paths.socket,
            "ctr.spawn",
            spawn_request("nope", {"allow": ["Bash"]}),
        )
    )
    await asyncio.sleep(0.1)
    daemon.resolve_approval(daemon.pending_approvals()[0]["id"], "deny", "")
    await asyncio.wait_for(spawning, timeout=5)

    entries = daemon.audit.tail(limit=40)
    assert any(e["op"] == "policy.escalation" and not e["allowed"] for e in entries)


# ==========================================================================
# capability requests -- approval that actually grants
# ==========================================================================


async def test_ask_alone_grants_nothing(daemon, tmp_path):
    """`capctl ask` is a question, not a request.

    Approving it tells the agent "yes" and changes nothing, which is exactly the
    confusion `cap.request` exists to remove.
    """
    daemon.register(config("solo", tmp_path))
    c = daemon.containers["solo"]
    c.server = await daemon._serve_container(c)

    before = len(daemon.kernel.tasks["solo"])
    asking = asyncio.ensure_future(
        request(c.paths.socket, "ask", {"question": "may I have a factory?"})
    )
    await asyncio.sleep(0.05)
    daemon.resolve_approval(daemon.pending_approvals()[0]["id"], "allow", "sure")
    reply = await asyncio.wait_for(asking, timeout=5)

    assert reply.result["decision"] == "allow"
    assert len(daemon.kernel.tasks["solo"]) == before, (
        "ask must not change the capability table"
    )


async def test_a_granted_request_lands_in_the_cap_table(daemon, tmp_path):
    daemon.register(config("solo", tmp_path))
    daemon.register(config("peer", tmp_path))
    c = daemon.containers["solo"]
    c.server = await daemon._serve_container(c)

    asking = asyncio.ensure_future(
        request(
            c.paths.socket,
            "cap.request",
            {
                "kind": "container",
                "target": "peer",
                "rights": ["send", "inspect"],
                "reason": "need to report results",
            },
        )
    )
    await asyncio.sleep(0.05)

    pending = daemon.pending_approvals()[0]
    assert pending["context"]["kind"] == "capability_request"
    assert pending["context"]["request"]["target"] == "peer"
    assert pending["context"]["request"]["reason"] == "need to report results"

    daemon.resolve_approval(pending["id"], "allow")
    reply = await asyncio.wait_for(asking, timeout=5)

    assert reply.ok and reply.result["granted"] is True
    slot = reply.result["slot"]

    # The agent can use it immediately, without restarting or being told twice.
    caps = (await request(c.paths.socket, "cap.list")).result
    granted = [x for x in caps if x["slot"] == slot][0]
    assert granted["label"] == "peer:peer"
    assert sorted(granted["rights"]) == ["inspect", "send"]

    sent = await request(c.paths.socket, "msg.send", {"slot": slot, "payload": "hello"})
    assert sent.ok


async def test_the_operator_can_grant_less_than_was_asked_for(daemon, tmp_path):
    """Asking for kill should not mean getting kill."""
    daemon.register(config("solo", tmp_path))
    daemon.register(config("peer", tmp_path))
    c = daemon.containers["solo"]
    c.server = await daemon._serve_container(c)

    asking = asyncio.ensure_future(
        request(
            c.paths.socket,
            "cap.request",
            {
                "kind": "container",
                "target": "peer",
                "rights": ["send", "inspect", "kill"],
            },
        )
    )
    await asyncio.sleep(0.05)
    daemon.resolve_approval(
        daemon.pending_approvals()[0]["id"],
        "allow",
        "",
        rights=["send"],
    )
    reply = await asyncio.wait_for(asking, timeout=5)

    assert reply.result["rights"] == ["send"]
    with pytest.raises(Exception):
        daemon.kernel.ctr_kill("solo", reply.result["slot"])


async def test_a_denied_request_grants_nothing(daemon, tmp_path):
    daemon.register(config("solo", tmp_path))
    daemon.register(config("peer", tmp_path))
    c = daemon.containers["solo"]
    c.server = await daemon._serve_container(c)
    before = len(daemon.kernel.tasks["solo"])

    asking = asyncio.ensure_future(
        request(
            c.paths.socket,
            "cap.request",
            {
                "kind": "container",
                "target": "peer",
                "rights": ["send"],
            },
        )
    )
    await asyncio.sleep(0.05)
    daemon.resolve_approval(daemon.pending_approvals()[0]["id"], "deny", "no")
    reply = await asyncio.wait_for(asking, timeout=5)

    assert reply.ok and reply.result["granted"] is False
    assert len(daemon.kernel.tasks["solo"]) == before


async def test_requesting_a_factory_makes_spawning_work(daemon, tmp_path):
    """The exact case that prompted this: an agent with no factory asks for one."""
    daemon.register(config("solo", tmp_path))
    c = daemon.containers["solo"]
    c.server = await daemon._serve_container(c)
    daemon.spawn_container = lambda cfg, parent: daemon.register(cfg, parent=parent).obj

    assert not any(x.kind == "factory" for x in daemon.kernel.cap_list("solo"))

    asking = asyncio.ensure_future(
        request(
            c.paths.socket,
            "cap.request",
            {
                "kind": "factory",
                "rights": ["create"],
                "quota": 2,
                "reason": "I need a helper to run the test suite",
            },
        )
    )
    await asyncio.sleep(0.05)
    daemon.resolve_approval(
        daemon.pending_approvals()[0]["id"], "allow", rights=["create"]
    )
    reply = await asyncio.wait_for(asking, timeout=5)
    assert reply.result["granted"]

    factory_slot = reply.result["slot"]
    spawned = await request(
        c.paths.socket,
        "ctr.spawn",
        {
            "factory_slot": factory_slot,
            "config": {"name": "helper"},
        },
    )
    assert spawned.ok, spawned.message
    assert "helper" in daemon.containers


async def test_a_requested_capability_is_still_revocable(daemon, tmp_path):
    """Nothing granted this way escapes the mapping database."""
    daemon.register(config("solo", tmp_path))
    daemon.register(config("peer", tmp_path))
    c = daemon.containers["solo"]
    c.server = await daemon._serve_container(c)

    asking = asyncio.ensure_future(
        request(
            c.paths.socket,
            "cap.request",
            {
                "kind": "container",
                "target": "peer",
                "rights": ["send"],
            },
        )
    )
    await asyncio.sleep(0.05)
    daemon.resolve_approval(daemon.pending_approvals()[0]["id"], "allow")
    slot = (await asyncio.wait_for(asking, timeout=5)).result["slot"]

    node = daemon.kernel.tasks["solo"].slots[slot].node
    daemon.kernel._apply_revocations(daemon.kernel.mapdb.revoke(node))
    assert slot not in daemon.kernel.tasks["solo"].slots


async def test_an_unknown_request_kind_is_refused(daemon, tmp_path):
    daemon.register(config("solo", tmp_path))
    c = daemon.containers["solo"]
    c.server = await daemon._serve_container(c)

    reply = await request(
        c.paths.socket, "cap.request", {"kind": "root", "target": "everything"}
    )
    assert not reply.ok
    assert not daemon.pending_approvals(), "a bad kind must not reach the operator"


# ==========================================================================
# dismissing a finished container
# ==========================================================================


def tree_names(daemon) -> set[str]:
    """Every container the operator's tree actually shows, at any depth."""
    found: set[str] = set()

    def walk(nodes):
        for node in nodes:
            found.add(node["name"])
            walk(node.get("children", []))

    walk(daemon.kernel.container_tree())
    return found


async def test_a_stopped_container_stays_until_dismissed(daemon, tmp_path):
    """Keeping it is deliberate -- you usually want its exit code first."""
    daemon.register(config("gone", tmp_path))
    daemon.kernel.find_container("gone").state = "exited"

    assert "gone" in tree_names(daemon)

    await daemon.destroy("gone")
    assert "gone" not in tree_names(daemon), "dismissing must clear the tree entry"
    assert "gone" not in daemon.containers


async def test_a_running_container_is_not_dismissed_by_accident(daemon, tmp_path):
    daemon.register(config("busy", tmp_path))
    container = daemon.containers["busy"]

    class Fake:
        running = True

        async def terminate(self, grace=5.0):
            return 0

    container.session = Fake()
    with pytest.raises(CapwrapError, match="still running"):
        await daemon.destroy("busy")
    assert "busy" in daemon.containers

    container.session = None  # stopped; now it goes
    await daemon.destroy("busy")
    assert "busy" not in daemon.containers


async def test_dismissing_revokes_capabilities_others_held_on_it(daemon, tmp_path):
    """Otherwise a peer keeps a slot pointing at an object that no longer exists.

    `cap.list` would quietly skip it while the slot stayed occupied, and using it
    would surface as an internal error rather than a clean denial.
    """
    peers = {"caps": {"peers": [{"container": "doomed", "rights": ["send"]}]}}
    daemon.register(config("watcher", tmp_path, **peers))
    daemon.register(config("doomed", tmp_path))
    daemon.link_all_peers()
    daemon.kernel.find_container("doomed").state = "exited"

    held = [c for c in daemon.kernel.cap_list("watcher") if c.label == "peer:doomed"]
    assert held, "precondition: watcher holds a capability on doomed"
    slot = held[0].slot

    await daemon.destroy("doomed")

    assert slot not in daemon.kernel.tasks["watcher"].slots
    assert not any(c.label == "peer:doomed" for c in daemon.kernel.cap_list("watcher"))


async def test_dismissing_a_parent_does_not_hide_its_children(daemon, tmp_path):
    """The tree is walked down from the roots.

    A child whose parent has been removed would be unreachable -- it would
    vanish from the UI while still being a real container.
    """
    daemon.register(
        config(
            "boss",
            tmp_path,
            caps={"factory": {"rights": ["create"], "quota": {"containers": 2}}},
        )
    )
    daemon.register(config("worker", tmp_path), parent="boss")
    assert {"boss", "worker"} <= tree_names(daemon)

    daemon.kernel.find_container("boss").state = "exited"
    result = await daemon.destroy("boss")

    assert result["reparented"] == ["worker"]
    assert "worker" in tree_names(daemon), "the child disappeared from the tree"
    assert daemon.kernel.find_container("worker").parent is None


async def test_dismissing_keeps_host_state_by_default(daemon, tmp_path, state_dir):
    daemon.register(config("keeper", tmp_path))
    container = daemon.containers["keeper"]
    container.paths.ensure()
    (container.paths.root / "evidence.txt").write_text("the agent's work\n")
    daemon.kernel.find_container("keeper").state = "exited"

    await daemon.destroy("keeper")
    assert (container.paths.root / "evidence.txt").exists(), (
        "dismissing must not throw away the work the container produced"
    )


async def test_dismissing_can_also_remove_state_when_asked(daemon, tmp_path, state_dir):
    daemon.register(config("tidy", tmp_path))
    container = daemon.containers["tidy"]
    container.paths.ensure()
    daemon.kernel.find_container("tidy").state = "exited"

    await daemon.destroy("tidy", remove_state=True)
    assert not container.paths.root.exists()


async def test_dismissing_frees_the_name_for_reuse(daemon, tmp_path):
    daemon.register(config("recycled", tmp_path))
    daemon.kernel.find_container("recycled").state = "exited"
    await daemon.destroy("recycled")

    daemon.register(config("recycled", tmp_path))  # must not clash
    assert "recycled" in daemon.containers


async def test_dismissing_an_unknown_container_is_an_error(daemon, tmp_path):
    with pytest.raises(CapwrapError, match="no such container"):
        await daemon.destroy("never-existed")


# ==========================================================================
# one agent driving another
# ==========================================================================


async def linked_pair(daemon, tmp_path, rights, child_command):
    """A driver holding `rights` on a child that is running `child_command`."""
    daemon.register(
        config(
            "driver",
            tmp_path,
            caps={"peers": [{"container": "child", "rights": rights}]},
        )
    )
    daemon.register(
        config(
            "child",
            tmp_path,
            runtime={"command": child_command, "tty": True},
        )
    )
    daemon.link_all_peers()
    for name in ("driver", "child"):
        c = daemon.containers[name]
        c.server = await daemon._serve_container(c)
    return daemon.containers["driver"], daemon.containers["child"]


async def peer_slot(driver) -> int:
    caps = (await request(driver.paths.socket, "cap.list")).result
    return [c["slot"] for c in caps if c["label"] == "peer:child"][0]


async def test_reading_output_needs_its_own_right(daemon, tmp_path):
    """INSPECT is not enough.

    Knowing a container exists is a far smaller thing than reading everything on
    its screen, which for an agent is its prompts, file contents and whatever it
    has been told.
    """
    driver, _ = await linked_pair(
        daemon, tmp_path, ["send", "inspect"], ["/bin/sleep", "5"]
    )
    slot = await peer_slot(driver)

    status = await request(driver.paths.socket, "ctr.status", {"slot": slot})
    assert status.ok, "inspect should still work"

    denied = await request(driver.paths.socket, "ctr.output", {"slot": slot})
    assert not denied.ok
    assert denied.code == "insufficient_rights"


async def test_typing_needs_its_own_right(daemon, tmp_path):
    driver, _ = await linked_pair(
        daemon, tmp_path, ["send", "inspect", "read_output"], ["/bin/sleep", "5"]
    )
    slot = await peer_slot(driver)

    denied = await request(
        driver.paths.socket, "ctr.input", {"slot": slot, "data": "x"}
    )
    assert not denied.ok
    assert denied.code == "insufficient_rights"


@pytest.mark.sandbox
async def test_an_agent_can_read_and_drive_another_agents_terminal(
    daemon, tmp_path, require_sandbox
):
    """The parent-drives-child case: read the screen, answer with keystrokes.

    The child runs a selection prompt that only responds to arrow keys and
    Enter -- exactly the shape of an interactive tool prompt, and impossible to
    answer by sending text.
    """
    script = r"""
        choice=1
        draw() {
            printf '\033[2J\033[H'
            echo 'Pick one:'
            [ $choice = 1 ] && echo '> alpha' || echo '  alpha'
            [ $choice = 2 ] && echo '> beta'  || echo '  beta'
        }
        draw
        while true; do
            IFS= read -rsn1 c || exit 1
            if [ "$c" = "$(printf '\033')" ]; then
                IFS= read -rsn2 rest
                case "$rest" in
                    '[A') choice=1 ;;
                    '[B') choice=2 ;;
                esac
                draw
            elif [ "$c" = "" ]; then
                printf '\033[2J\033[H'
                [ $choice = 1 ] && echo 'CHOSE-ALPHA' || echo 'CHOSE-BETA'
                sleep 3; exit 0
            fi
        done
    """
    driver, child = await linked_pair(
        daemon,
        tmp_path,
        ["send", "inspect", "read_output", "write_input"],
        ["/bin/bash", "-c", script],
    )
    await daemon.start("child")
    await asyncio.sleep(1.5)
    slot = await peer_slot(driver)

    # 1. Read the child's screen through the capability.
    screen = await request(
        driver.paths.socket, "ctr.output", {"slot": slot, "rows": 10}
    )
    assert screen.ok, screen.message
    rendered = "\n".join(screen.result["lines"])
    assert "Pick one:" in rendered
    assert "> alpha" in rendered, rendered

    # 2. Answer it with keystrokes that are not text at all.
    down = await request(
        driver.paths.socket, "ctr.input", {"slot": slot, "data": "\x1b[B"}
    )
    assert down.ok, down.message
    await asyncio.sleep(0.8)

    moved = await request(driver.paths.socket, "ctr.output", {"slot": slot, "rows": 10})
    assert "> beta" in "\n".join(moved.result["lines"]), moved.result["lines"]

    # 3. Enter is a carriage return; a newline would not be seen.
    await request(driver.paths.socket, "ctr.input", {"slot": slot, "data": "\r"})
    await asyncio.sleep(1.0)

    final = await request(driver.paths.socket, "ctr.output", {"slot": slot, "rows": 10})
    assert "CHOSE-BETA" in "\n".join(final.result["lines"]), final.result["lines"]


async def test_an_answered_question_is_not_replayed_as_open(daemon, tmp_path):
    """The operator inbox is history and survives a page reload.

    Without recording the outcome, a question you already decided comes back
    looking exactly like one still waiting on you.
    """
    daemon.register(config("asker", tmp_path))
    c = daemon.containers["asker"]
    c.server = await daemon._serve_container(c)

    asking = asyncio.ensure_future(
        request(c.paths.socket, "ask", {"question": "may I push?"})
    )
    await asyncio.sleep(0.05)

    inbox = daemon.overview()["operator_inbox"]
    question = [m for m in inbox if m["kind"] == "question"][-1]
    assert "decision" not in question["payload"], "not answered yet"

    daemon.resolve_approval(daemon.pending_approvals()[0]["id"], "deny", "not yet")
    await asyncio.wait_for(asking, timeout=5)

    assert not daemon.pending_approvals(), "the queue must be empty"
    replayed = [
        m for m in daemon.overview()["operator_inbox"] if m["kind"] == "question"
    ][-1]
    assert replayed["payload"]["decision"] == "deny"
    assert replayed["payload"]["reason"] == "not yet"


# ==========================================================================
# reconnecting to a full-screen TUI
# ==========================================================================


@pytest.mark.sandbox
async def test_reconnecting_restores_the_programs_terminal_modes(
    daemon, tmp_path, require_sandbox, monkeypatch
):
    """A long-lived TUI's startup sequences fall out of the ring buffer.

    Claude Code enters the alternate screen and turns on mouse reporting when it
    starts. Replaying only the tail leaves the browser in the normal buffer
    rendering alt-screen output, which is what made a session opened from the
    overview look scrambled and refuse to scroll.

    The ring is shrunk here to force the overflow. In normal use it holds
    megabytes, so this takes a long-running agent rather than a moment -- but it
    is exactly the case the preamble exists for, and the one that only shows up
    after an agent has been working for a while.
    """
    monkeypatch.setattr(supervisor, "SCROLLBACK_BYTES", 64 * 1024)
    daemon.register(
        config(
            "tui",
            tmp_path,
            runtime={
                "command": [
                    "/bin/bash",
                    "-c",
                    # enter alt screen + mouse reporting, then emit
                    # more than the ring buffer holds
                    "printf '\\033[?1049h\\033[?1000h\\033[?1006h'; "
                    "head -c 200000 /dev/zero | tr '\\0' 'x'; "
                    "printf '\\033[H\\033[2Jready'; sleep 20",
                ]
            },
        )
    )
    container = await daemon.start("tui")
    await asyncio.sleep(2.5)
    session = container.session

    assert session.alternate_screen, "the program owns the screen"

    # The enables are long gone from the raw buffer...
    assert b"\x1b[?1049h" not in session.scrollback()
    # ...but the daemon still knows about them.
    preamble = session.mode_preamble()
    for mode in (b"?1049h", b"?1000h", b"?1006h"):
        assert mode in preamble, preamble

    # And a joining client gets the current screen rather than stale redraws.
    painted = session.repaint()
    assert b"ready" in painted
    assert painted.startswith(b"\x1b[H\x1b[2J")


# ==========================================================================
# approvals that outlive their asker
# ==========================================================================


async def _serve(daemon, name, tmp_path, **extra):
    daemon.register(config(name, tmp_path, **extra))
    c = daemon.containers[name]
    c.server = await daemon._serve_container(c)
    return c


async def _wait_for_approval(daemon, timeout: float = 5.0):
    """Wait until the container's blocking `ask` has reached the queue."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if daemon.pending_approvals():
            return daemon.pending_approvals()[0]
        await asyncio.sleep(0.01)
    raise AssertionError("no approval was queued")


async def test_a_question_dies_with_the_agent_that_asked_it(daemon, tmp_path):
    """The queue must not outlive the process that is waiting on it.

    A hook whose agent is killed mid-prompt leaves nothing to receive an answer.
    If the question stays queued, it comes back on every page load and every
    reconnect, and clicking Allow resolves a future nobody is waiting on -- which
    reads as a stuck UI rather than a departed agent.
    """
    c = await _serve(daemon, "alpha", tmp_path)

    reader, writer = await asyncio.open_unix_connection(str(c.paths.socket))
    writer.write(
        Request(
            op="ask",
            id=1,
            args={
                "question": "may I install curl?",
                "block": True,
                "timeout": 60,
            },
        ).encode()
    )
    await writer.drain()

    pending = await _wait_for_approval(daemon)
    assert pending["container"] == "alpha"

    writer.close()  # the agent goes away, still blocked
    with contextlib.suppress(Exception):
        await writer.wait_closed()

    deadline = asyncio.get_running_loop().time() + 5
    while daemon.pending_approvals():
        assert asyncio.get_running_loop().time() < deadline, (
            "question was never retired"
        )
        await asyncio.sleep(0.01)

    answered = [
        m for m in daemon.mailboxes.get("operator").recent(10) if m.kind == "question"
    ]
    assert answered[-1].payload["decision"] == "abandoned"
    reader.feed_eof()


async def test_a_question_dies_with_its_container(daemon, tmp_path):
    """Same again, but the connection is still open: the container itself ends."""
    c = await _serve(daemon, "alpha", tmp_path)

    reader, writer = await asyncio.open_unix_connection(str(c.paths.socket))
    writer.write(
        Request(
            op="ask",
            id=1,
            args={
                "question": "shall I push?",
                "block": True,
                "timeout": 60,
            },
        ).encode()
    )
    await writer.drain()
    await _wait_for_approval(daemon)

    assert daemon.abandon_approvals("alpha", "the container exited") == 1
    assert daemon.pending_approvals() == []

    reply = Response.parse(await asyncio.wait_for(reader.readuntil(b"\n"), timeout=5))
    assert reply.ok and reply.result["decision"] == "abandoned"
    writer.close()


async def test_answering_a_question_still_reaches_the_agent(daemon, tmp_path):
    """The abandonment machinery must not have broken the ordinary path."""
    c = await _serve(daemon, "alpha", tmp_path)

    reader, writer = await asyncio.open_unix_connection(str(c.paths.socket))
    writer.write(
        Request(
            op="ask",
            id=1,
            args={
                "question": "may I write bench.c?",
                "block": True,
                "timeout": 60,
            },
        ).encode()
    )
    await writer.drain()

    pending = await _wait_for_approval(daemon)
    assert daemon.resolve_approval(pending["id"], "allow", "go ahead")

    reply = Response.parse(await asyncio.wait_for(reader.readuntil(b"\n"), timeout=5))
    assert reply.ok
    assert reply.result["decision"] == "allow"
    assert reply.result["reason"] == "go ahead"
    writer.close()


async def test_a_second_request_on_the_same_connection_still_works(daemon, tmp_path):
    """Framing moved into the daemon to make the disconnect race possible.

    Two requests down one connection is the thing that would break if the reader
    watching for a hang-up ate bytes belonging to the request after it.
    """
    c = await _serve(daemon, "alpha", tmp_path)
    reader, writer = await asyncio.open_unix_connection(str(c.paths.socket))
    try:
        # Both at once, so the second is already buffered while the first runs.
        writer.write(
            Request(op="whoami", id=1).encode() + Request(op="cap.list", id=2).encode()
        )
        await writer.drain()

        first = Response.parse(await asyncio.wait_for(reader.readuntil(b"\n"), 5))
        second = Response.parse(await asyncio.wait_for(reader.readuntil(b"\n"), 5))
        assert first.ok and first.result["container"] == "alpha"
        assert second.ok and {c["label"] for c in second.result} == {"self", "operator"}
    finally:
        writer.close()


# ==========================================================================
# broadcast, over the wire
# ==========================================================================


async def test_an_agent_can_broadcast_to_several_peers_at_once(daemon, tmp_path):
    peers = {
        "caps": {
            "peers": [
                {"container": "beta", "rights": ["send"]},
                {"container": "gamma", "rights": ["send"]},
            ]
        }
    }
    await _serve(daemon, "alpha", tmp_path, **peers)
    await _serve(daemon, "beta", tmp_path)
    await _serve(daemon, "gamma", tmp_path)
    daemon.link_all_peers()

    caps = (await request(daemon.containers["alpha"].paths.socket, "cap.list")).result
    slots = [c["slot"] for c in caps if c["label"].startswith("peer:")]

    sent = await request(
        daemon.containers["alpha"].paths.socket,
        "msg.broadcast",
        {"slots": slots, "payload": "build is green"},
    )
    assert sent.ok
    assert sorted(sent.result["recipients"]) == ["beta", "gamma"]

    for name in ("beta", "gamma"):
        got = await request(
            daemon.containers[name].paths.socket, "msg.recv", {"timeout": 0}
        )
        assert got.result[0]["payload"] == "build is green"


async def test_broadcast_needs_a_non_empty_slot_list(daemon, tmp_path):
    await _serve(daemon, "alpha", tmp_path)
    reply = await request(
        daemon.containers["alpha"].paths.socket, "msg.broadcast", {"slots": []}
    )
    assert not reply.ok and reply.code == "protocol_error"


# ==========================================================================
# message tracing
# ==========================================================================


async def test_message_payloads_are_only_recorded_when_asked_for(daemon, tmp_path):
    """Off by default: a trace holds the agents' working content, not metadata."""
    peers = {"caps": {"peers": [{"container": "beta", "rights": ["send"]}]}}
    await _serve(daemon, "alpha", tmp_path, **peers)
    await _serve(daemon, "beta", tmp_path)
    daemon.link_all_peers()

    caps = (await request(daemon.containers["alpha"].paths.socket, "cap.list")).result
    slot = [c["slot"] for c in caps if c["label"] == "peer:beta"][0]
    socket_path = daemon.containers["alpha"].paths.socket

    assert daemon.trace_state()["enabled"] is False
    await request(socket_path, "msg.send", {"slot": slot, "payload": "unrecorded"})
    assert daemon.traced_messages() == []

    daemon.set_message_trace(True)
    await request(socket_path, "msg.send", {"slot": slot, "payload": "recorded"})

    trace = daemon.traced_messages()
    assert [t["payload"] for t in trace] == ["recorded"]
    assert trace[0]["from"] == "alpha" and trace[0]["to"] == "beta"

    # Turning it off discards what was collected, or "off" would mean less than
    # it says.
    daemon.set_message_trace(False)
    assert daemon.traced_messages() == []


async def test_the_instance_can_be_named(state_dir, tmp_path):
    """Several capwraps run at once; the name is how their tabs stay tellable apart."""
    d = Daemon(
        audit_path=Path(state_dir) / "audit.db", instance_name="  FastPath HashTable  "
    )
    try:
        assert d.instance_name == "FastPath HashTable"
        assert d.overview()["instance"] == "FastPath HashTable"
    finally:
        await d.shutdown()


# ==========================================================================
# adding a container to a running capwrap
# ==========================================================================


async def test_a_container_can_be_added_while_others_are_running(daemon, tmp_path):
    """The point of a running capwrap is that agents come and go.

    A review needs a reviewer; stopping everything to restart with one more
    config wastes whatever the other agents were in the middle of.
    """
    from capwrap.web.app import create_app
    from fastapi.testclient import TestClient

    daemon.register(config("alpha", tmp_path))
    client = TestClient(create_app(daemon))

    late = config(
        "late",
        tmp_path,
        caps={
            "peers": [
                {"container": "alpha", "rights": ["send"]},
            ]
        },
    )
    body = {
        "config": json.loads(late.model_dump_json(exclude={"source_dir"})),
        "start": False,
    }

    response = client.post("/api/containers", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["name"] == "late"
    assert "late" in daemon.containers

    # Its peer capability resolved, rather than being deferred forever.
    labels = {c.label for c in daemon.kernel.cap_list("late")}
    assert "peer:alpha" in labels

    # And a second attempt under the same name is refused rather than clobbering.
    assert client.post("/api/containers", json=body).status_code == 409


async def test_a_container_added_late_is_reachable_from_the_ones_already_there(
    daemon, tmp_path
):
    """A config that named a container before it existed gets filled in.

    `link_all_peers` runs both ways on add, so an agent configured to talk to a
    reviewer that had not been created yet is not left holding nothing.
    """
    from capwrap.web.app import create_app
    from fastapi.testclient import TestClient

    daemon.register(
        config(
            "dev",
            tmp_path,
            caps={
                "peers": [
                    {"container": "reviewer", "rights": ["send"]},
                ]
            },
        )
    )
    assert "peer:reviewer" not in {c.label for c in daemon.kernel.cap_list("dev")}

    client = TestClient(create_app(daemon))
    reviewer = config("reviewer", tmp_path)
    client.post(
        "/api/containers",
        json={
            "config": json.loads(reviewer.model_dump_json(exclude={"source_dir"})),
            "start": False,
        },
    )

    assert "peer:reviewer" in {c.label for c in daemon.kernel.cap_list("dev")}


@pytest.mark.sandbox
async def test_the_whole_session_is_replayed_to_a_browser_that_connects(
    daemon, tmp_path, require_sandbox
):
    """Scrollback is the operator's history, and it has to survive reconnecting.

    Sending only the current frame -- which is all a full-screen program's
    repaint can give you -- meant an operator could see what an agent was doing
    now and nothing of how it got there, and lost even that every time they
    clicked another container and back.
    """
    from capwrap.web.app import create_app
    from fastapi.testclient import TestClient

    daemon.register(
        config(
            "chatty",
            tmp_path,
            runtime={
                "command": [
                    "/bin/bash",
                    "-c",
                    'for i in $(seq 1 400); do echo "line $i"; done; sleep 20',
                ]
            },
        )
    )
    await daemon.start("chatty")
    await asyncio.sleep(2.0)

    client = TestClient(create_app(daemon))
    with client.websocket_connect("/ws/terminal/chatty") as socket:
        replayed = b""
        for _ in range(40):
            message = socket.receive()
            if "bytes" in message and message["bytes"] is not None:
                replayed += message["bytes"]
            if b"line 400" in replayed:
                break

    # The beginning, not just the tail: 400 lines is far inside the ring.
    assert b"line 1\r\n" in replayed
    assert b"line 200" in replayed
    assert b"line 400" in replayed


@pytest.mark.sandbox
async def test_a_replay_that_lost_its_head_says_so(
    daemon, tmp_path, require_sandbox, monkeypatch
):
    """Silently showing a partial session as if it were the whole one is worse
    than showing less: the operator draws conclusions from what is not there."""
    monkeypatch.setattr(supervisor, "SCROLLBACK_BYTES", 8 * 1024)
    daemon.register(
        config(
            "noisy",
            tmp_path,
            runtime={
                "command": [
                    "/bin/bash",
                    "-c",
                    "head -c 60000 /dev/zero | tr '\\0' 'y'; sleep 20",
                ]
            },
        )
    )
    container = await daemon.start("noisy")
    await asyncio.sleep(2.0)
    assert container.session.truncated

    from capwrap.web.app import create_app
    from fastapi.testclient import TestClient

    client = TestClient(create_app(daemon))
    with client.websocket_connect("/ws/terminal/noisy") as socket:
        first = None
        while first is None:
            message = socket.receive()
            first = message.get("bytes")
    assert b"aged out of the buffer" in first
