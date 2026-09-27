"""End-to-end harness scenarios: does the feature actually work in a sandbox?

Tier 2 in `docs/harness-compatibility.md` §4: a real bubblewrap container, a
real daemon, the real guest hook, and a deterministic trigger -- no model, no
network, no reliance on an agent choosing to call a tool.

The question scenarios exist because claude's question routing was shipped
once as "working" on the strength of unit tests alone. Every test here asserts
the observable outcome an operator would see: a card in the inbox, an answer
reaching the container, and the harness's own verdict on stdout.

Run:  .venv/bin/python -m pytest tests/test_harness_e2e.py -q
      .venv/bin/python -m pytest tests/test_harness_e2e.py -q -m sandbox
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from capwrap.daemon import Daemon
from tests.test_daemon import config, hook_command

pytestmark = pytest.mark.sandbox


@pytest.fixture
async def daemon(state_dir):
    """A real daemon over a tmp state dir -- same shape as test_daemon's.

    Defined here rather than imported so the fixture name is not shadowed by
    its use as a test parameter (ruff F811).
    """
    instance = Daemon(audit_path=Path(state_dir) / "audit.db")
    yield instance
    await instance.shutdown()


# --------------------------------------------------------------------------
# the claude question scenario
# --------------------------------------------------------------------------

ASK_USER_QUESTION = json.dumps(
    {
        "hook_event_name": "PreToolUse",
        "tool_name": "AskUserQuestion",
        "tool_input": {
            "questions": [
                {
                    "question": "Which database should the migration target?",
                    "header": "Database",
                    "options": [
                        {"label": "postgres"},
                        {"label": "sqlite"},
                    ],
                }
            ]
        },
        "cwd": "/work",
        "session_id": "scenario-1",
    }
)


def question_command() -> list[str]:
    """Drive AskUserQuestion through the real hook, the way Claude Code does."""
    return ["/bin/bash", "-c", f"echo '{ASK_USER_QUESTION}' | /opt/capwrap/hook.py"]


async def start_scenario(daemon, tmp_path, name: str, **runtime) -> object:
    daemon.register(config(name, tmp_path, runtime=runtime))
    return await daemon.start(name)


async def wait_for_question(daemon, name: str, timeout: float = 15.0):
    """Poll the operator's inbox for a question-kind card from `name`."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        for message in daemon.mailboxes.get("operator").recent(20):
            payload = getattr(message, "payload", {}) or {}
            if (
                getattr(message, "kind", None) == "question"
                and (payload.get("context") or {}).get("container") == name
            ):
                return message
        await asyncio.sleep(0.05)
    return None


async def wait_for_exit(container, timeout: float = 45.0) -> str:
    await asyncio.wait_for(container.session.wait(), timeout=timeout)
    return container.session.scrollback().decode(errors="replace")


@pytest.mark.sandbox
async def test_claude_question_forward_reaches_the_operator_and_returns(
    daemon, tmp_path, require_sandbox
):
    """routing=forward (the default): the console gets the card, the answer
    comes back into the container, and the hook says do-not-retry.

    This is the scenario that proves claude's question routing works -- the
    claim that previously rested on nothing.
    """
    container = await start_scenario(
        daemon,
        tmp_path,
        "claude-forward",
        approvals="capwrap",
        command=question_command(),
    )

    message = await wait_for_question(daemon, "claude-forward")
    assert message is not None, "the question never reached the operator's inbox"

    payload = message.payload
    assert "Which database should the migration target?" in payload["question"]
    context = payload["context"]
    # Classified as conversation, not permission: explicit kind, no tool key.
    assert context["kind"] == "question"
    assert "tool" not in context, "a question must not look like an approval"
    # The options render as answer chips, exactly as `capctl ask --options` does.
    assert context["options"] == ["postgres", "sqlite"]

    # A question card is still resolvable through the approval channel the
    # console already uses; explain is how a question gets an answer.
    daemon.resolve_approval(payload["id"], "explain", "postgres")

    verdict = await wait_for_exit(container)
    assert '"permissionDecision": "deny"' in verdict, verdict
    assert "postgres" in verdict, verdict
    # The model must not treat the denial as a failure and re-ask forever.
    assert "do not call the question tool again" in verdict, verdict


@pytest.mark.sandbox
async def test_claude_question_block_stays_in_the_agents_terminal(
    daemon, tmp_path, require_sandbox
):
    """routing=block: the operator is not pinged, the native picker runs.

    The hook must return allow immediately -- block is the behaviour the old
    local bypass was defending, and it has to survive the redesign.
    """
    container = await start_scenario(
        daemon,
        tmp_path,
        "claude-block",
        approvals="capwrap",
        question_routing="block",
        command=question_command(),
    )

    verdict = await wait_for_exit(container)
    assert '"permissionDecision": "allow"' in verdict, verdict

    # Nothing in the operator's inbox for this container.
    leaked = [
        m
        for m in daemon.mailboxes.get("operator").recent(20)
        if getattr(m, "kind", None) == "question"
        and (getattr(m, "payload", {}).get("context") or {}).get("container")
        == "claude-block"
    ]
    assert not leaked, f"block must not ping the operator: {leaked!r}"
    assert not daemon.pending_approvals(), "block must not leave a pending card"


@pytest.mark.sandbox
async def test_claude_question_auto_answers_without_the_operator(
    daemon, tmp_path, require_sandbox
):
    """routing=auto: answered autonomously, recorded for later review.

    No card waits on a human, and the container is told to use best judgment.
    """
    container = await start_scenario(
        daemon,
        tmp_path,
        "claude-auto",
        approvals="capwrap",
        question_routing="auto",
        command=question_command(),
    )

    verdict = await wait_for_exit(container)
    assert '"permissionDecision": "deny"' in verdict, verdict
    assert "best judgment" in verdict, verdict
    assert not daemon.pending_approvals(), "auto must not leave a pending card"

    # The question is recorded as already answered, so it stays reviewable but
    # out of the pending count.
    recorded = [
        m
        for m in daemon.mailboxes.get("operator").recent(20)
        if getattr(m, "kind", None) == "question"
        and (getattr(m, "payload", {}).get("context") or {}).get("container")
        == "claude-auto"
    ]
    assert recorded, "auto must still record the question for later review"


@pytest.mark.sandbox
async def test_claude_permission_routing_is_unchanged_by_the_question_work(
    daemon, tmp_path, require_sandbox
):
    """Regression guard: approvals still block, deny and explain the answer.

    The question path was rewired through the same `ask` op; this proves the
    permission path still behaves exactly as before.
    """
    container = await start_scenario(
        daemon,
        tmp_path,
        "claude-approve",
        approvals="capwrap",
        auto_allow=["Read"],
        command=hook_command("Bash", '{"command":"rm -rf /work"}'),
    )

    for _ in range(200):
        if daemon.pending_approvals():
            break
        await asyncio.sleep(0.05)

    pending = daemon.pending_approvals()
    assert pending, "the approval never reached the operator"
    assert pending[0]["kind"] == "approval"
    assert pending[0]["context"]["tool"] == "Bash"

    daemon.resolve_approval(pending[0]["id"], "deny", "that would delete it")
    verdict = await wait_for_exit(container)
    assert '"permissionDecision": "deny"' in verdict, verdict
    assert "delete it" in verdict, verdict


@pytest.mark.sandbox
async def test_claude_auto_allow_still_short_circuits(
    daemon, tmp_path, require_sandbox
):
    """A policy allow must never reach the human -- including now that
    AskUserQuestion takes a different path through the same hook."""
    container = await start_scenario(
        daemon,
        tmp_path,
        "claude-quiet",
        approvals="capwrap",
        auto_allow=["Read"],
        command=hook_command("Read", '{"file_path":"/work/a.py"}'),
    )
    verdict = await wait_for_exit(container)
    assert '"permissionDecision": "allow"' in verdict, verdict
    assert not daemon.pending_approvals(), "a Read should never reach the human"
