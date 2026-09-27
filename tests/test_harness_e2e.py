"""End-to-end harness scenarios: does the feature actually work in a sandbox?

Tier 2 in `docs/harness-compatibility.md` §4: a real bubblewrap container, a
real daemon, the real guest hook, and a deterministic trigger -- no model, no
network, no reliance on an agent choosing to call a tool.

The question scenarios exist because claude's question routing was shipped
once as "working" on the strength of unit tests alone. Every test here asserts
the observable outcome an operator would see: a card in the inbox, an answer
reaching the container, and the harness's own verdict on stdout.

Four harnesses, one scenario shape each: claude (the real hook), pi
(`capctl ask` -- its only question path), and opencode v1/v2 (the real
TypeScript shims, run by bun inside the container).

Run:  .venv/bin/python -m pytest tests/test_harness_e2e.py -q
      .venv/bin/python -m pytest tests/test_harness_e2e.py -q -m sandbox
"""

from __future__ import annotations

import asyncio
import json
import shutil
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


# --------------------------------------------------------------------------
# the pi question scenario -- `capctl ask` IS pi's question path
# --------------------------------------------------------------------------


def capctl_ask_command() -> list[str]:
    """The ask itself is the trigger: pi has no question tool to intercept."""
    return [
        "/bin/bash",
        "-c",
        'capctl ask "E2E: which branch?" --options main,release',
    ]


def pending_questions(daemon, name: str) -> list[dict]:
    """Pending question-kind cards for `name`, the console's queue shape."""
    return [
        p
        for p in daemon.pending_approvals()
        if p.get("container") == name and p.get("kind") == "question"
    ]


async def wait_for_pending_question(daemon, name: str, timeout: float = 15.0):
    """Poll the approval queue for a question card from `name`.

    `capctl ask` sends no context.container -- the daemon attributes the ask
    from the socket -- so the card is matched on what the daemon stamped it
    with, which is the same attribution the operator's console resolves by.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        cards = pending_questions(daemon, name)
        if cards:
            return cards[0]
        await asyncio.sleep(0.05)
    return None


async def wait_for_verdict(container, timeout: float = 45.0) -> tuple[int, str]:
    """The container's exit code plus everything it printed, bounded."""
    code = await asyncio.wait_for(container.session.wait(), timeout=timeout)
    return code, container.session.scrollback().decode(errors="replace")


@pytest.mark.sandbox
async def test_pi_capctl_ask_reaches_the_operator_with_the_options(
    daemon, tmp_path, require_sandbox
):
    """routing=forward: the card lands with the options as answer chips, and
    the operator's reply is handed to the still-blocked `capctl ask`.

    capctl exits 0 on an explanation -- guidance is an answer, not a denial.
    """
    container = await start_scenario(
        daemon,
        tmp_path,
        "pi-forward",
        agent="pi",
        approvals="capwrap",
        command=capctl_ask_command(),
    )

    card = await wait_for_pending_question(daemon, "pi-forward")
    assert card is not None, "the question never reached the approval queue"
    assert card["kind"] == "question"
    assert "tool" not in card["context"], "a question must not look like an approval"
    assert card["context"]["options"] == ["main", "release"]

    daemon.resolve_approval(card["id"], "explain", "release")
    code, verdict = await wait_for_verdict(container)
    assert "explain: release" in verdict, verdict
    assert code == 0, f"an explanation is an answer, not a denial: {verdict}"


@pytest.mark.sandbox
async def test_pi_capctl_ask_block_answers_in_the_agents_terminal(
    daemon, tmp_path, require_sandbox
):
    """routing=block: no card anywhere; the guidance is printed on stdout."""
    container = await start_scenario(
        daemon,
        tmp_path,
        "pi-block",
        agent="pi",
        approvals="capwrap",
        question_routing="block",
        command=capctl_ask_command(),
    )

    code, verdict = await wait_for_verdict(container)
    assert "State your questions as plain text" in verdict, verdict
    assert code == 0, f"block guidance is an answer, not a denial: {verdict}"

    assert not pending_questions(daemon, "pi-block"), (
        "block must not leave a pending card"
    )
    leaked = [
        m
        for m in daemon.mailboxes.get("operator").recent(20)
        if getattr(m, "kind", None) == "question"
        and getattr(m, "sender", "") == "pi-block"
    ]
    assert not leaked, f"block must not ping the operator: {leaked!r}"


# --------------------------------------------------------------------------
# the opencode scenarios -- the real shims, run by bun inside the container
# --------------------------------------------------------------------------

#: Driver scripts live here; each is staged into its container at the same
#: name under /tmp via `[[files]]`.
E2E_DIR = Path(__file__).resolve().parent / "e2e"

#: The driver prints exactly one machine-readable line with this marker.
MARKER = "CAPWRAP_E2E_RESULT:"

#: bun on the host. The shim sources are TypeScript, so the operations
#: containers need bun visible inside: its directory is bound read-only at
#: /opt/bun and PATH is not relied on.
BUN = shutil.which("bun")


@pytest.fixture
def bun_in_sandbox(require_sandbox):
    """The bun path inside the container, or a skip when the host has none."""
    if BUN is None:
        pytest.skip(
            "bun is not on this host's PATH; the opencode shims are "
            "TypeScript and need it to run inside the sandbox"
        )
    return "/opt/bun/bun"


def driver_command(bun: str, driver: str, env: str = "") -> list[str]:
    """Run one driver with bun, optionally with env overrides in front.

    The per-process CAPWRAP_SOCKET override is what drives a shim against a
    daemon that does not exist -- the sandbox always sets the real one.
    """
    return ["/bin/bash", "-c", f"{env} {bun} /tmp/{driver}"]


async def start_shim_scenario(
    daemon, tmp_path, name: str, driver: str, agent: str, command: list[str]
) -> object:
    """A container for driving one real shim: bun's dir bound read-only at
    /opt/bun, the driver staged at /tmp/<driver>, and the agent's profile for
    guest-side injection (the shims ship in the guest dir, which binds at
    /opt/capwrap in every container)."""
    daemon.register(
        config(
            name,
            tmp_path,
            runtime={
                "agent": agent,
                "approvals": "capwrap" if agent == "opencode2" else "native",
                "command": command,
            },
            # opencode2 keeps its approval policy on file; the v1 hook protocol
            # is machinery for approvals only and the question shim needs none.
            mounts=[
                {
                    "src": str(Path(BUN).resolve().parent),
                    "dest": "/opt/bun",
                    "mode": "ro",
                }
            ],
            files=[
                {"src": str(E2E_DIR / driver), "dest": f"/tmp/{driver}", "mode": "0644"}
            ],
        )
    )
    return await daemon.start(name)


def driver_result(verdict: str) -> dict:
    """Parse the single CAPWRAP_E2E_RESULT line the driver printed."""
    lines = [
        line.split(":", 1)[1].strip()
        for line in verdict.splitlines()
        if line.startswith(MARKER)
    ]
    assert lines, f"the driver never reported an outcome: {verdict!r}"
    assert len(lines) == 1, f"the driver reported twice: {lines!r}"
    return json.loads(lines[0])


@pytest.mark.sandbox
async def test_opencode_v1_question_delivers_the_answer_as_a_prefixed_error(
    daemon, tmp_path, require_sandbox, bun_in_sandbox
):
    """A question through opencode v1's real shim reaches the operator, and
    the answer rides back as the prefixed Error the model reads."""
    container = await start_shim_scenario(
        daemon,
        tmp_path,
        "oc1-forward",
        "opencode_v1_driver.ts",
        "opencode",
        command=driver_command(bun_in_sandbox, "opencode_v1_driver.ts"),
    )

    message = await wait_for_question(daemon, "oc1-forward")
    assert message is not None, "the question never reached the operator's inbox"
    assert getattr(message, "kind", None) == "question"
    assert "E2E question?" in message.payload["question"]
    context = message.payload["context"]
    assert context["container"] == "oc1-forward"
    assert "tool" not in context, "a question must not look like an approval"

    daemon.resolve_approval(message.payload["id"], "explain", "42")

    code, verdict = await wait_for_verdict(container)
    result = driver_result(verdict)
    assert result["outcome"] == "threw", result
    # The shim and the sandbox were wired by the daemon env, not by any test
    # fault -- assert the socket the shim actually dialed.
    assert result["env"]["socket"] == "/run/capwrap.sock", result
    assert result["env"]["container"] == "oc1-forward", result
    assert "capwrap: this is the answer to your question" in result["message"], result
    assert "do not call the question tool again" in result["message"], result
    assert "42" in result["message"], result
    assert code == 0, f"the driver must report cleanly: {verdict}"


@pytest.mark.sandbox
async def test_opencode_v1_falls_through_to_its_native_question_ui(
    daemon, tmp_path, require_sandbox, bun_in_sandbox
):
    """v1's documented posture on an unreachable daemon: the hook returns
    undefined so v1's own question UI asks locally -- consulting nothing,
    blocking nobody."""
    container = await start_shim_scenario(
        daemon,
        tmp_path,
        "oc1-absent",
        "opencode_v1_driver.ts",
        "opencode",
        command=driver_command(
            bun_in_sandbox,
            "opencode_v1_driver.ts",
            env="CAPWRAP_SOCKET=/run/absent.sock",
        ),
    )

    code, verdict = await wait_for_verdict(container)
    result = driver_result(verdict)
    assert result["outcome"] == "fell-through", result
    assert result["env"]["socket"] == "/run/absent.sock", result
    assert result["env"]["container"] == "oc1-absent", result
    assert code == 0

    assert not pending_questions(daemon, "oc1-absent"), (
        "an unreachable daemon must not leave a card behind"
    )
    leaked = [
        m
        for m in daemon.mailboxes.get("operator").recent(20)
        if getattr(m, "kind", None) == "question"
        and getattr(m, "sender", "") == "oc1-absent"
    ]
    assert not leaked, f"nothing reached the operator: {leaked!r}"


@pytest.mark.sandbox
async def test_opencode2_question_tool_returns_the_answer_as_a_result(
    daemon, tmp_path, require_sandbox, bun_in_sandbox
):
    """A question through opencode v2's real shim (the tool override via
    `tool.transform`) reaches the operator with the options rendered, and the
    answer comes back as ordinary tool content the model reads."""
    container = await start_shim_scenario(
        daemon,
        tmp_path,
        "oc2-forward",
        "opencode2_driver.ts",
        "opencode2",
        command=driver_command(bun_in_sandbox, "opencode2_driver.ts"),
    )

    message = await wait_for_question(daemon, "oc2-forward")
    assert message is not None, "the question never reached the operator's inbox"
    assert getattr(message, "kind", None) == "question"
    assert "E2E question?" in message.payload["question"]
    context = message.payload["context"]
    assert context["container"] == "oc2-forward"
    assert "tool" not in context, "a question must not look like an approval"
    assert context["options"] == ["main", "release"]

    daemon.resolve_approval(message.payload["id"], "explain", "release")

    code, verdict = await wait_for_verdict(container)
    result = driver_result(verdict)
    assert result["outcome"] == "result", result
    assert result["env"]["socket"] == "/run/capwrap.sock", result
    assert result["env"]["container"] == "oc2-forward", result
    content = result["content"]
    assert "User has answered your questions" in content, result
    assert '"E2E question?"="release"' in content, result
    assert code == 0, f"the driver must report cleanly: {verdict}"


@pytest.mark.sandbox
async def test_opencode2_unreachable_tells_the_agent_to_ask_in_its_own_terminal(
    daemon, tmp_path, require_sandbox, bun_in_sandbox
):
    """The fail-toward-human posture: no decision is invented and no card is
    faked when the daemon is gone -- the tool result says the console is
    unreachable and hands the question to the agent's own terminal, never
    an auto-allow."""
    container = await start_shim_scenario(
        daemon,
        tmp_path,
        "oc2-absent",
        "opencode2_driver.ts",
        "opencode2",
        command=driver_command(
            bun_in_sandbox,
            "opencode2_driver.ts",
            env="CAPWRAP_SOCKET=/run/absent.sock",
        ),
    )

    code, verdict = await wait_for_verdict(container)
    result = driver_result(verdict)
    assert result["outcome"] == "result", result
    assert result["env"]["socket"] == "/run/absent.sock", result
    content = result["content"]
    assert "capwrap console unreachable" in content, result
    assert "State the question in your terminal" in content, result
    assert "Do not call the question tool again" in content, result
    assert code == 0

    assert not pending_questions(daemon, "oc2-absent"), (
        "an unreachable daemon must not leave a card behind"
    )
    leaked = [
        m
        for m in daemon.mailboxes.get("operator").recent(20)
        if getattr(m, "kind", None) == "question"
        and getattr(m, "sender", "") == "oc2-absent"
    ]
    assert not leaked, f"nothing reached the operator: {leaked!r}"
