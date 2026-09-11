#!/usr/bin/env python3
"""capctl -- the agent's interface to the capability kernel.

Runs *inside* a sandbox.  Standard library only, single file, no imports from
the capwrap package: the guest tools directory is bind-mounted read-only into an
otherwise unrelated filesystem, so it cannot rely on capwrap being installed
there.

Everything an agent can do to the outside world it does through here, by way of
``/run/capwrap.sock``.  Slot numbers are local names in this container's own
capability table -- your slot 3 and another agent's slot 3 are unrelated, and
there is no way to refer to something you were not given.

    capctl caps                        what am I allowed to do?
    capctl send 4 "build is green"     message the holder of slot 4
    capctl send 4 "..." --sign         sign it; `recv` checks it on the far side
    capctl send 3,4,7 "build is green" message all three at once
    capctl recv --wait                 read my mailbox
    capctl ask "may I install curl?"   ask the human, and block for an answer
    capctl net                         where may I connect to?
    capctl board post standup "done"   write to a shared board
    capctl board read standup          read it; everyone sees every post
    capctl board post standup "x" --sign   sign it, so it can be checked later
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from typing import Any

DEFAULT_SOCKET = os.environ.get("CAPWRAP_SOCKET", "/run/capwrap.sock")


class _SubParser(argparse.ArgumentParser):
    """Subparser that also understands the global flags.

    `add_subparsers(parser_class=...)` makes every subcommand inherit --json, so
    it works before or after the subcommand name.
    """

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("add_help", True)
        super().__init__(*args, **kwargs)
        self.add_argument("--json", action="store_true", help="raw JSON output")


class CapctlError(Exception):
    pass


def call(
    op: str, args: dict[str, Any] | None = None, timeout: float | None = 30.0
) -> Any:
    """One request, one response, over the container's control socket."""
    path = DEFAULT_SOCKET
    if not os.path.exists(path):
        raise CapctlError(
            f"no capability socket at {path}. "
            "Either this is not a capwrap container, or the daemon is not running."
        )

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(path)
    except OSError as exc:
        raise CapctlError(f"cannot reach the capwrap daemon: {exc}") from None

    try:
        sock.sendall(
            json.dumps({"id": 1, "op": op, "args": args or {}}).encode() + b"\n"
        )
        buffer = b""
        while b"\n" not in buffer:
            chunk = sock.recv(65536)
            if not chunk:
                raise CapctlError("the daemon closed the connection")
            buffer += chunk
    except socket.timeout:
        raise CapctlError(f"timed out waiting for the daemon ({timeout}s)") from None
    finally:
        sock.close()

    reply = json.loads(buffer.split(b"\n", 1)[0])
    if not reply.get("ok"):
        error = reply.get("error") or {}
        raise CapctlError(f"{error.get('code', 'error')}: {error.get('message', '?')}")
    return reply.get("result")


def resolve_slot(value: str) -> int:
    """Accept either a slot number or a capability label.

    `capctl send peer:beta "hi"` reads far better than making an agent look up a
    number first, and it is not a weakening of the model: the label is matched
    only against capabilities this container already holds, so it can still
    name nothing it was not given.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        pass

    caps = call("cap.list")
    exact = [c for c in caps if c["label"] == value]
    if len(exact) == 1:
        return int(exact[0]["slot"])
    if len(exact) > 1:
        raise CapctlError(
            f"{value!r} is ambiguous: slots {', '.join(str(c['slot']) for c in exact)}"
        )

    # `beta` should find `peer:beta`, which is what an agent will actually type.
    partial = [
        c
        for c in caps
        if c["label"].endswith(f":{value}") or c["label"].startswith(f"{value}:")
    ]
    if len(partial) == 1:
        return int(partial[0]["slot"])
    if len(partial) > 1:
        names = ", ".join(c["label"] for c in partial)
        raise CapctlError(f"{value!r} is ambiguous: matches {names}")

    known = ", ".join(c["label"] for c in caps) or "none"
    raise CapctlError(f"no capability called {value!r}; you hold: {known}")


def resolve_slots(value: str) -> list[int]:
    """Resolve a comma-separated list of slots or labels, keeping their order.

    `capctl send 3,4,7 ...` is the shape an agent reaches for when it has been
    told to report to three peers, and it is worth supporting directly: three
    separate commands means three chances to get a partial send and no obvious
    record that the other two were meant to happen too.
    """
    names = [part.strip() for part in value.split(",") if part.strip()]
    if not names:
        raise CapctlError("name at least one slot")
    slots: list[int] = []
    for name in names:
        slot = resolve_slot(name)
        if slot not in slots:
            slots.append(slot)
    return slots


def container_slots() -> list[dict]:
    """Every capability I hold that can carry a message to another container.

    Excludes me. Not by label: the `self` capability is only the obvious way to
    end up pointing at yourself, and a config that lists its own container among
    its peers gives you a second one under a different name. The object's own
    name is what actually settles it. The operator gate is excluded too, being a
    different kind -- `capctl ask` is how you reach a human.
    """
    me = os.environ.get("CAPWRAP_CONTAINER")
    return [
        cap
        for cap in call("cap.list")
        if cap["kind"] == "container"
        and "send" in cap["rights"]
        and (cap.get("detail") or {}).get("name") != me
        and cap["label"] != "self"
    ]


# --------------------------------------------------------------------------
# keystrokes
# --------------------------------------------------------------------------

# Byte sequences a terminal actually delivers for keys that are not characters.
# Needed to drive another agent's TUI -- a selection prompt is answered with
# arrows and Enter, and none of those can be expressed as text.
KEYS = {
    "up": "\x1b[A",
    "down": "\x1b[B",
    "right": "\x1b[C",
    "left": "\x1b[D",
    "home": "\x1b[H",
    "end": "\x1b[F",
    "pageup": "\x1b[5~",
    "pagedown": "\x1b[6~",
    "insert": "\x1b[2~",
    "delete": "\x1b[3~",
    # Enter is a carriage return, not a newline. A TTY in raw mode -- which is
    # what any full-screen TUI puts itself in -- receives \r when you press it,
    # and many prompts ignore \n entirely.
    "enter": "\r",
    "return": "\r",
    "cr": "\r",
    "newline": "\n",
    "tab": "\t",
    "backtab": "\x1b[Z",
    "shift-tab": "\x1b[Z",
    "space": " ",
    "backspace": "\x7f",
    "escape": "\x1b",
    "esc": "\x1b",
}
KEYS.update(
    {
        f"f{n}": seq
        for n, seq in enumerate(
            [
                "\x1bOP",
                "\x1bOQ",
                "\x1bOR",
                "\x1bOS",
                "\x1b[15~",
                "\x1b[17~",
                "\x1b[18~",
                "\x1b[19~",
                "\x1b[20~",
                "\x1b[21~",
                "\x1b[23~",
                "\x1b[24~",
            ],
            start=1,
        )
    }
)


def key_sequence(name: str) -> str:
    """Translate a key name into what a terminal would send.

    Also accepts `ctrl-c` style names, and `\x03`-ish escapes for anything the
    table does not cover.
    """
    key = name.strip().lower()
    if key in KEYS:
        return KEYS[key]
    if key.startswith(("ctrl-", "c-", "^")):
        letter = key.split("-", 1)[-1].lstrip("^")
        if len(letter) == 1 and letter.isalpha():
            return chr(ord(letter.lower()) - 96)
    if key.startswith("alt-") and len(key) == 5:
        return "\x1b" + key[-1]
    try:
        # Last resort: a literal escape, so unusual keys stay reachable.
        return name.encode().decode("unicode_escape")
    except UnicodeDecodeError:
        raise CapctlError(
            f"unknown key {name!r}; known: {', '.join(sorted(KEYS))}, "
            "ctrl-<letter>, alt-<letter>"
        ) from None


# --------------------------------------------------------------------------
# output helpers
# --------------------------------------------------------------------------


def emit(value: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(value, indent=2))
    elif isinstance(value, str):
        print(value)
    else:
        print(json.dumps(value))


def print_caps(caps: list[dict]) -> None:
    if not caps:
        print("(no capabilities)")
        return
    width = max(len(c["label"]) for c in caps)
    print(f"{'SLOT':<5} {'KIND':<10} {'LABEL':<{width}}  RIGHTS")
    for cap in caps:
        detail = cap.get("detail") or {}
        extra = ""
        if cap["kind"] == "container":
            extra = f"  [{detail.get('state', '?')}]"
        elif cap["kind"] == "factory":
            extra = f"  [{detail.get('remaining', 0)} left]"
        print(
            f"{cap['slot']:<5} {cap['kind']:<10} {cap['label']:<{width}}  "
            f"{','.join(cap['rights'])}{extra}"
        )


def print_messages(messages: list[dict]) -> None:
    if not messages:
        print("(no messages)")
        return
    for m in messages:
        payload = m["payload"]
        body = payload if isinstance(payload, str) else json.dumps(payload)
        # Verified here, by the reader, rather than reported from the flag the
        # sender's side set -- which is the only version of the claim that means
        # anything.
        print(f"[{m['id']}] from {m['from']}{_verify_message(m)} ({m['kind']}): {body}")


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------


def cmd_whoami(args):
    me = call("whoami")
    if args.json:
        emit(me, True)
        return
    print(f"container: {me['container']}")
    print(f"caps:      {me['caps']}")
    if me.get("fingerprint"):
        print(f"key:       {me['fingerprint']}  (sign posts with `board post --sign`)")


def cmd_caps(args):
    caps = call("cap.list")
    if args.json:
        emit(caps, True)
    else:
        print_caps(caps)


def cmd_info(args):
    emit(call("cap.info", {"slot": resolve_slot(args.slot)}), True)


def cmd_send(args):
    payload: Any = args.message
    if args.json_payload:
        payload = json.loads(args.message)
    slots = resolve_slots(args.slot)
    signature = _sign_payload(payload) if args.sign else ""

    if len(slots) == 1:
        request = {"slot": slots[0], "payload": payload}
        if signature:
            request["signature"] = signature
        result = call("msg.send", request)
        if args.json:
            emit(result, True)
        else:
            print(
                f"delivered to {result['delivered_to']}"
                + (" (signed)" if signature else "")
            )
        return
    _deliver(slots, payload, args.json, signature)


def _sign_payload(payload: Any) -> str:
    me = call("whoami")["container"]
    return _sign(_message_bytes(me, payload))


def cmd_broadcast(args):
    payload: Any = args.message
    if args.json_payload:
        payload = json.loads(args.message)

    if args.to:
        slots = resolve_slots(args.to)
    else:
        caps = container_slots()
        if not caps:
            raise CapctlError(
                "you hold no capability that can send to another container"
            )
        slots = [int(c["slot"]) for c in caps]
    _deliver(slots, payload, args.json, _sign_payload(payload) if args.sign else "")


def _deliver(
    slots: list[int], payload: Any, as_json: bool, signature: str = ""
) -> None:
    """One broadcast, reported so a partial delivery is impossible to miss.

    A refusal on one slot does not stop the others, so the interesting part of
    the answer is which ones did *not* go -- printing only the successes would
    let an agent believe it had told everybody.
    """
    request = {"slots": slots, "payload": payload}
    if signature:
        # One signature for the whole broadcast: it covers the author and the
        # payload, not who it went to.
        request["signature"] = signature
    result = call("msg.broadcast", request)
    if as_json:
        emit(result, True)
    else:
        recipients = ", ".join(result["recipients"]) or "nobody"
        print(f"delivered to {recipients}" + (" (signed)" if signature else ""))
        for refusal in result["refused"]:
            print(f"  slot {refusal['slot']}: {refusal['error']}", file=sys.stderr)
    if not result["delivered"]:
        sys.exit(1)


def cmd_recv(args):
    timeout = None if args.wait and args.timeout is None else (args.timeout or 0)
    messages = call(
        "msg.recv",
        {"timeout": timeout, "limit": args.limit},
        timeout=None if timeout is None else max(timeout + 5, 30),
    )
    if args.json:
        emit(messages, True)
    else:
        print_messages(messages)


def cmd_grant(args):
    result = call(
        "cap.delegate",
        {
            "target_slot": resolve_slot(args.target),
            "cap_slot": resolve_slot(args.cap),
            "rights": args.rights.split(",") if args.rights else None,
        },
    )
    if args.json:
        emit(result, True)
    else:
        print(
            f"{result['recipient']} now holds it in slot {result['slot']} "
            f"with {','.join(result['rights'])}"
        )


def cmd_revoke(args):
    result = call(
        "cap.revoke", {"slot": resolve_slot(args.slot), "include_self": args.self_too}
    )
    if args.json:
        emit(result, True)
    else:
        holders = ", ".join(result["holders"]) or "nobody"
        print(f"revoked {result['revoked']} mapping(s); affected: {holders}")


def cmd_status(args):
    emit(call("ctr.status", {"slot": resolve_slot(args.slot)}), True)


def cmd_kill(args):
    emit(
        call("ctr.kill", {"slot": resolve_slot(args.slot), "signal": args.signal}),
        args.json,
    )


def cmd_interrupt(args):
    emit(
        call("ctr.signal", {"slot": resolve_slot(args.slot), "signal": args.signal}),
        args.json,
    )


def cmd_type(args):
    data = args.data
    if args.enter:
        data += "\r"
    emit(call("ctr.input", {"slot": resolve_slot(args.slot), "data": data}), args.json)


def cmd_keys(args):
    """Send named keys, so a TUI can be driven rather than only typed at."""
    data = "".join(key_sequence(k) for k in args.keys)
    result = call("ctr.input", {"slot": resolve_slot(args.slot), "data": data})
    if args.json:
        emit(result, True)
    else:
        print(f"sent {len(args.keys)} key(s) to {result['to']}")


def cmd_screen(args):
    """Read what another container is showing."""
    result = call("ctr.output", {"slot": resolve_slot(args.slot), "rows": args.rows})
    if args.json:
        emit(result, True)
        return
    if not result.get("running"):
        print(f"({result.get('container', '?')} is not running)")
    for line in result.get("lines", []):
        print(line.rstrip())


def cmd_spawn(args):
    if args.config == "-":
        raw = json.loads(sys.stdin.read())
    else:
        with open(args.config) as fh:
            text = fh.read()
        try:
            raw = json.loads(text)
        except json.JSONDecodeError:
            try:
                import tomllib
            except ImportError:  # pragma: no cover - python < 3.11 in the sandbox
                raise CapctlError("config must be JSON on this python") from None
            raw = tomllib.loads(text)
    if args.name:
        raw["name"] = args.name
    result = call(
        "ctr.spawn", {"factory_slot": resolve_slot(args.factory), "config": raw}
    )
    if args.json:
        emit(result, True)
        return
    print(
        f"spawned {result['spawned']} ({result['remaining_quota']} left in the factory)"
    )
    if result.get("slot"):
        print(
            f"  you hold it in slot {result['slot']} "
            f"as child:{result['spawned']} with {','.join(result['rights'])}"
        )
    else:
        print(
            "  note: this factory grants no rights over what it creates, so you "
            "cannot reach it. Ask the operator with `capctl request`."
        )


def cmd_map(args):
    result = call(
        "ds.map",
        {
            "target_slot": resolve_slot(args.target),
            "ds_slot": resolve_slot(args.dataspace),
            "dest": args.dest,
            "mode": args.mode,
        },
    )
    if args.json:
        emit(result, True)
    else:
        print(f"{result['recipient']} can now read it at {result['path']}")


def cmd_request(args):
    """Ask the operator for a capability. Approval performs the grant."""
    result = call(
        "cap.request",
        {
            "kind": args.kind,
            "target": args.target or "",
            "rights": args.rights.split(",") if args.rights else [],
            "quota": args.quota,
            "reason": args.reason or "",
            "timeout": args.timeout,
        },
        timeout=None,
    )
    if args.json:
        emit(result, True)
    elif result.get("granted"):
        print(
            f'granted: slot {result["slot"]} "{result["label"]}" '
            f"with {','.join(result['rights'])}"
        )
    else:
        reason = result.get("reason") or ""
        print(f"{result.get('decision', 'denied')}{': ' + reason if reason else ''}")
    if not result.get("granted"):
        sys.exit(1)


# Signing lives in this directory too, so the guest can reach it: capctl may not
# import from capwrap, and the host and the sandbox have to derive the signed
# bytes identically or nothing verifies.
SIGNING_KEY = os.environ.get("CAPWRAP_SIGNING_KEY", "/run/capwrap-key")


def _canonical(tag: bytes, fields: dict) -> bytes:
    """Must match capwrap/kernel/signing.py exactly. Change both or neither."""
    document = json.dumps(
        fields,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    )
    return tag + b"\n" + document.encode()


def _signing_bytes(board: str, author: str, payload):
    return _canonical(
        b"capwrap-board-v1", {"board": board, "from": author, "payload": payload}
    )


def _message_bytes(author: str, payload):
    # No recipient: one signature covers a whole broadcast, and it survives the
    # message being forwarded, which is where it is worth most.
    return _canonical(b"capwrap-message-v1", {"from": author, "payload": payload})


def _sign(message: bytes) -> str:
    import ed25519

    return ed25519.sign(_load_seed(), message).hex()


def _verify_message(m: dict) -> str:
    """Check a received message's signature here, rather than believe a flag."""
    if not m.get("signature"):
        return ""
    try:
        import ed25519

        ok = ed25519.verify(
            bytes.fromhex(m.get("public_key") or ""),
            _message_bytes(m["from"], m["payload"]),
            bytes.fromhex(m["signature"]),
        )
    except (ImportError, ValueError):
        return " [signed, unverified]"
    return " [signed]" if ok else " [BAD SIGNATURE]"


def _load_seed() -> bytes:
    try:
        with open(SIGNING_KEY, "rb") as fh:
            seed = fh.read()
    except OSError as exc:
        raise CapctlError(
            f"no signing key at {SIGNING_KEY}: {exc}. "
            "Only a container started by capwrap has one."
        ) from None
    if len(seed) != 32:
        raise CapctlError(f"the signing key at {SIGNING_KEY} is malformed")
    return seed


def _board_topic(slot: int) -> str:
    """The topic the signature is bound to, read from the capability itself.

    Via `cap.list` rather than `cap.info`, because `cap.info` needs `inspect`
    and signing a post must not: an agent given exactly `send` on a board should
    be able to sign what it posts there. Listing your own table needs no right,
    which is the correct level of authority for reading the name of something
    you already hold.
    """
    for cap in call("cap.list"):
        if cap["slot"] == slot:
            if cap.get("kind") != "board":
                raise CapctlError(f"slot {slot} does not name a board")
            return (cap.get("detail") or {}).get("topic", "")
    raise CapctlError(f"you hold nothing in slot {slot}")


def cmd_board(args):
    """Boards: shared, readable by everyone who holds them, consumed by nobody."""
    if args.action == "create":
        result = call(
            "board.create",
            {
                "factory_slot": resolve_slot(args.factory),
                "topic": args.topic,
            },
        )
        if args.json:
            emit(result, True)
        else:
            print(
                f"board '{result['board']}' is slot {result['slot']} "
                f"({','.join(result['rights'])})"
            )
            print(
                "  hand it to a worker with: "
                f"capctl grant <their-slot> {result['slot']} --rights send,read"
            )
        return

    if args.action == "post":
        slot = resolve_slot(args.target)
        request = {"slot": slot, "payload": args.message}
        if args.sign:
            import ed25519

            me = call("whoami")["container"]
            topic = _board_topic(slot)
            request["signature"] = ed25519.sign(
                _load_seed(), _signing_bytes(topic, me, args.message)
            ).hex()
        result = call("board.post", request)
        if args.json:
            emit(result, True)
        else:
            mark = " (signed)" if result.get("signed") else ""
            print(f"posted to {result['board']} as #{result['id']}{mark}")
        return

    # read
    result = call(
        "board.read",
        {
            "slot": resolve_slot(args.target),
            "since": args.since,
            "limit": args.limit,
        },
    )
    if args.json:
        emit(result, True)
        return
    posts = result["posts"]
    if not posts:
        print(f"({result['board']}: nothing since #{args.since})")
        return
    for post in posts:
        payload = post["payload"]
        body = payload if isinstance(payload, str) else json.dumps(payload)
        print(f"#{post['id']} {post['from']}{_signature_mark(result, post)}: {body}")
    # The cursor to pass next time, so following a board is a loop over --since.
    print(f"(latest #{result['latest']})", file=sys.stderr)


def _signature_mark(result: dict, post: dict) -> str:
    """How a post's signature checked out, verified here rather than taken on trust.

    The daemon says a post is signed; this is the reader doing the arithmetic
    itself, which is the only version of the claim worth anything.
    """
    if not post.get("signature"):
        return ""
    try:
        import ed25519
    except ImportError:  # pragma: no cover
        return " [signed, unverified]"
    ok = ed25519.verify(
        bytes.fromhex(post.get("public_key", "") or ""),
        _signing_bytes(result["board"], post["from"], post["payload"]),
        bytes.fromhex(post["signature"]),
    )
    return " [signed]" if ok else " [BAD SIGNATURE]"


def cmd_net(args):
    """What this container may reach, and through which rule.

    Worth having as its own command: an agent that has just been refused a
    connection needs to know what it *does* hold before it asks for more, and
    reading that out of `capctl caps` means knowing that net rules are a kind.
    """
    rules = [c for c in call("cap.list") if c["kind"] == "net_rule"]
    if args.json:
        emit(rules, True)
        return
    if not rules:
        print("(no network access)")
        return
    width = max(len(c["label"]) for c in rules)
    for cap in rules:
        usable = "connect" in cap["rights"]
        detail = cap.get("detail") or {}
        mark = " " if usable else "  (cannot connect: no `connect` right)"
        print(
            f"{cap['slot']:<5} {cap['label']:<{width}}  {detail.get('pattern', '')}{mark}"
        )


def cmd_ask(args):
    context = json.loads(args.context) if args.context else {}
    if args.options:
        # Comma-separated answer choices, shown to the operator as clickable
        # chips that fill the reply box. Landed in the request context so the
        # console can render them without any new endpoint.
        context["options"] = [o.strip() for o in args.options.split(",") if o.strip()]
    result = call(
        "ask",
        {
            "question": args.question,
            "context": context,
            "block": not args.no_wait,
            "timeout": args.timeout,
        },
        timeout=None if not args.no_wait else 30,
    )
    if args.json:
        emit(result, True)
    else:
        decision = result.get("decision", "pending")
        # The operator's answer text rides in `message` (the explain path);
        # `reason` is the legacy field for allow/reject.  Print whichever is
        # present so a question's answer is not silently dropped.
        message = result.get("message") or result.get("reason") or ""
        print(f"{decision}{': ' + message if message else ''}")
    # Exit non-zero on a real denial or no-answer, so
    # `capctl ask ... && do-the-thing` works.  "explain" (the operator's
    # answer text) and routing guidance ("block"/"auto") are answers, not
    # denials — the agent should read them and carry on.
    if result.get("decision") in ("reject", "deny", "timeout", "abandoned"):
        sys.exit(1)


def cmd_escalate(args):
    """Ask the operator to cross a boundary: network or child-spawn.

    Unlike `capctl ask`, approving an escalation actually performs the grant:
    a network pattern becomes a live rule in the container's capability proxy,
    and a spawn pattern records spawn authority the child-spawn check consults.
    Structural capabilities (worktree writes, host mounts) are refused with
    "respawn required" -- no card is even created for those.
    """
    result = call(
        "escalate",
        {
            "capability": args.capability,
            "pattern": args.pattern,
            "reason": args.reason or "",
            "timeout": args.timeout,
        },
        timeout=None,
    )
    if args.json:
        emit(result, True)
    else:
        decision = result.get("decision", "pending")
        message = result.get("message") or result.get("reason") or ""
        print(f"{decision}{': ' + message if message else ''}")
    if result.get("decision") not in ("allow", None):
        sys.exit(1)


def build_parser() -> argparse.ArgumentParser:
    # --json is accepted on both sides of the subcommand. `capctl caps --json`
    # is what anyone actually types, and argparse would otherwise only accept
    # `capctl --json caps` -- a pointless trap for a tool whose main users are
    # LLM agents reading --help.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="raw JSON output")

    parser = argparse.ArgumentParser(
        prog="capctl",
        parents=[common],
        description="Talk to the capwrap capability kernel from inside a container.",
        epilog="Slot numbers are local to this container; see `capctl caps`.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True, parser_class=_SubParser)

    sub.add_parser("whoami", help="which container am I?").set_defaults(func=cmd_whoami)
    sub.add_parser("caps", help="list my capabilities").set_defaults(func=cmd_caps)

    p = sub.add_parser("info", help="details of one capability")
    p.add_argument("slot")
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("send", help="send a message through one or more capabilities")
    p.add_argument("slot", help="slot or label; comma-separated for several at once")
    p.add_argument("message")
    p.add_argument(
        "--sign",
        action="store_true",
        help="sign it, so the recipient can check it came from here "
        "even after it has been forwarded",
    )
    p.add_argument(
        "--json-payload",
        action="store_true",
        help="parse the message as JSON before sending",
    )
    p.set_defaults(func=cmd_send)

    p = sub.add_parser(
        "broadcast",
        help="send one message to several containers, or to every one you can reach",
    )
    p.add_argument("message")
    p.add_argument(
        "--to",
        help="comma-separated slots or labels; "
        "omit to reach every container you may send to",
    )
    p.add_argument(
        "--sign", action="store_true", help="sign it once, for every recipient"
    )
    p.add_argument(
        "--json-payload",
        action="store_true",
        help="parse the message as JSON before sending",
    )
    p.set_defaults(func=cmd_broadcast)

    p = sub.add_parser("recv", help="read my mailbox")
    p.add_argument("--wait", action="store_true", help="block until something arrives")
    p.add_argument("--timeout", type=float, default=None)
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(func=cmd_recv)

    p = sub.add_parser("grant", help="delegate one of my capabilities to a peer")
    p.add_argument("target", help="slot of the container to give it to")
    p.add_argument("cap", help="slot of the capability to hand over")
    p.add_argument("--rights", help="comma-separated subset; defaults to all of mine")
    p.set_defaults(func=cmd_grant)

    p = sub.add_parser(
        "revoke",
        help="withdraw everything derived from one of my capabilities",
    )
    p.add_argument("slot")
    p.add_argument(
        "--self-too",
        action="store_true",
        help="also drop my own copy, not just what I delegated",
    )
    p.set_defaults(func=cmd_revoke)

    p = sub.add_parser("status", help="status of a container I hold a capability on")
    p.add_argument("slot")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("kill", help="terminate a container")
    p.add_argument("slot")
    p.add_argument("--signal", type=int, default=15)
    p.set_defaults(func=cmd_kill)

    p = sub.add_parser("interrupt", help="signal a container without killing it")
    p.add_argument("slot")
    p.add_argument("--signal", type=int, default=2)
    p.set_defaults(func=cmd_interrupt)

    p = sub.add_parser("type", help="type text at another container's terminal")
    p.add_argument("slot")
    p.add_argument("data")
    p.add_argument(
        "--enter",
        action="store_true",
        help="press Enter afterwards (sends CR, as a terminal does)",
    )
    p.set_defaults(func=cmd_type)

    p = sub.add_parser(
        "keys",
        help="send named keys: up down left right tab enter escape ctrl-c ...",
    )
    p.add_argument("slot")
    p.add_argument("keys", nargs="+")
    p.set_defaults(func=cmd_keys)

    p = sub.add_parser("screen", help="read another container's terminal")
    p.add_argument("slot")
    p.add_argument("--rows", type=int, default=24)
    p.set_defaults(func=cmd_screen)

    p = sub.add_parser("spawn", help="create a container through a factory capability")
    p.add_argument("factory", help="slot of the factory capability")
    p.add_argument("config", help="path to a config (TOML or JSON), or '-' for stdin")
    p.add_argument("--name", help="override the child's name")
    p.set_defaults(func=cmd_spawn)

    p = sub.add_parser("map", help="give a peer access to a dataspace I hold")
    p.add_argument("target", help="slot of the receiving container")
    p.add_argument("dataspace", help="slot of the dataspace")
    p.add_argument("dest", help="name it should appear under in their /shared")
    p.add_argument(
        "--mode",
        choices=["copy", "map"],
        default="copy",
        help="copy duplicates the bytes; map aliases them",
    )
    p.set_defaults(func=cmd_map)

    p = sub.add_parser(
        "request",
        help="ask the operator for a capability; approval grants it immediately",
    )
    p.add_argument("kind", choices=["container", "dataspace", "factory", "net_rule"])
    p.add_argument(
        "target",
        nargs="?",
        help="container name, host path, or for net_rule "
        "'name=<host:port regex>'; omit for a factory",
    )
    p.add_argument("--rights", help="comma-separated; defaults to what the kind needs")
    p.add_argument(
        "--quota",
        type=int,
        default=1,
        help="for a factory: how many containers it may create",
    )
    p.add_argument("--reason", help="why you need it -- the operator reads this")
    p.add_argument("--timeout", type=float, default=None)
    p.set_defaults(func=cmd_request)

    p = sub.add_parser(
        "board",
        help="a shared message board: several agents read and write, "
        "and reading takes nothing away",
    )
    board = p.add_subparsers(dest="action", required=True, parser_class=_SubParser)

    b = board.add_parser("create", help="set up a board (needs a factory capability)")
    b.add_argument("factory", help="slot of the factory capability")
    b.add_argument("topic", help="what the board is for, e.g. 'standup'")

    b = board.add_parser("post", help="put a message on a board")
    b.add_argument("target", help="slot or label of the board")
    b.add_argument("message")
    b.add_argument(
        "--sign",
        action="store_true",
        help="sign it with this container's key, so a reader can "
        "check afterwards that it really came from here",
    )

    b = board.add_parser("read", help="read a board without consuming it")
    b.add_argument("target", help="slot or label of the board")
    b.add_argument(
        "--since",
        type=int,
        default=0,
        help="only posts after this id; the last one is printed for you",
    )
    b.add_argument("--limit", type=int, default=50)

    p.set_defaults(func=cmd_board)

    sub.add_parser(
        "net", help="what may I reach on the network, and through which rule?"
    ).set_defaults(func=cmd_net)

    p = sub.add_parser("ask", help="ask the human operator, and wait for an answer")
    p.add_argument("question")
    p.add_argument("--context", help="JSON object of extra context")
    p.add_argument(
        "--options",
        help="comma-separated answer choices, shown to the operator as chips",
    )
    p.add_argument(
        "--no-wait", action="store_true", help="queue the question without blocking"
    )
    p.add_argument("--timeout", type=float, default=None)
    p.set_defaults(func=cmd_ask)

    p = sub.add_parser(
        "escalate",
        help="ask the operator to cross a boundary: network or child-spawn",
    )
    p.add_argument(
        "--capability",
        required=True,
        choices=["network", "spawn"],
        help="the boundary to cross; structural limits (worktree writes, "
        "host mounts) are refused with 'respawn required'",
    )
    p.add_argument(
        "--pattern", required=True, help="what to allow, e.g. a host:port regex"
    )
    p.add_argument("--reason", help="why you need it -- the operator reads this")
    p.add_argument("--timeout", type=float, default=None)
    p.set_defaults(func=cmd_escalate)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except CapctlError as exc:
        print(f"capctl: {exc}", file=sys.stderr)
        return 1
    except BrokenPipeError:
        return 0
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
