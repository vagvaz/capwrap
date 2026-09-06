"""capwrap's terminal console.

The same job as the web console, for the case the web console is bad at: you are
already on the box over SSH, or on a machine where opening a browser to watch
agents work is the wrong shape.

What it is for, in order:

1. **Answering approvals.** Five agents blocked on prompts is the problem the
   whole project exists to fix, and needing a browser to unblock them puts a
   graphical session in the middle of it.
2. **Seeing what every agent is doing at once**, without five terminals.
3. **Attaching to one properly**, when watching is no longer enough.

Built on `curses` from the standard library rather than a TUI framework, for the
same reason the web console has no framework: this has to run in the sandboxed,
minimal places capwrap itself runs.

It speaks to the daemon over HTTP, like the browser does, so it works against an
instance somebody else started and needs no shared process.
"""

from __future__ import annotations

import curses
import json
import os
import select
import sys
import termios
import textwrap
import time
import tty

from .client import Client, ConsoleError

#: How often the console re-fetches. Unhurried on purpose: the box drawing this
#: is also running the agents.
REFRESH_SECONDS = 1.5

#: Key that gets you out of an attached terminal. Ctrl-] , as telnet has used
#: since forever -- and unlike Ctrl-C it is not something an agent's TUI wants.
DETACH_KEY = b"\x1d"

VIEWS = ["agents", "approvals", "boards", "audit"]


class Console:
    """State, drawing, and keys."""

    def __init__(self, client: Client) -> None:
        self.client = client
        self.instance = ""
        self.view = "agents"
        self.containers: list[dict] = []
        self.approvals: list[dict] = []
        self.boards: list[dict] = []
        self.audit: list[dict] = []
        self.screens: dict[str, list[str]] = {}
        self.selected = 0
        self.approval_index = 0
        self.status = ""
        self.status_until = 0.0
        self.error = ""
        self.last_fetch = 0.0
        self.attach_request: str | None = None
        self.running = True

    # ------------------------------------------------------------------
    # data
    # ------------------------------------------------------------------

    @property
    def current(self) -> dict | None:
        if not self.containers:
            return None
        return self.containers[min(self.selected, len(self.containers) - 1)]

    def refresh(self, rows: int) -> None:
        try:
            overview = self.client.overview()
            self.instance = overview.get("instance", "")
            self.containers = overview.get("containers", [])
            self.approvals = overview.get("approvals", [])
            self.error = ""
        except ConsoleError as exc:
            self.error = str(exc)
            return

        self.selected = max(0, min(self.selected, max(0, len(self.containers) - 1)))
        self.approval_index = max(
            0, min(self.approval_index, max(0, len(self.approvals) - 1))
        )

        try:
            if self.view == "agents":
                self.screens = {
                    s["container"]: s.get("lines", [])
                    for s in self.client.screens(rows=max(6, rows))
                }
            elif self.view == "boards":
                self.boards = self.client.boards()
            elif self.view == "audit":
                self.audit = self.client.audit(limit=200)
        except ConsoleError as exc:
            self.error = str(exc)
        self.last_fetch = time.monotonic()

    def say(self, message: str, seconds: float = 4.0) -> None:
        self.status = message
        self.status_until = time.monotonic() + seconds

    # ------------------------------------------------------------------
    # keys
    # ------------------------------------------------------------------

    def handle(self, key: int) -> None:
        if key in (ord("q"), 27):  # q, Esc
            self.running = False
        elif key == ord("\t"):
            self.view = VIEWS[(VIEWS.index(self.view) + 1) % len(VIEWS)]
            self.last_fetch = 0.0
        elif key in (curses.KEY_DOWN, ord("j")):
            self._move(1)
        elif key in (curses.KEY_UP, ord("k")):
            self._move(-1)
        elif key in (ord("r"), curses.KEY_F5):
            self.last_fetch = 0.0
        elif key in (ord("\n"), curses.KEY_ENTER, 10, 13):
            self._attach()
        elif self.view == "approvals":
            self._approval_key(key)
        else:
            self._container_key(key)

    def _move(self, delta: int) -> None:
        if self.view == "approvals":
            if self.approvals:
                self.approval_index = (self.approval_index + delta) % len(
                    self.approvals
                )
        elif self.containers:
            self.selected = (self.selected + delta) % len(self.containers)

    def _approval_key(self, key: int) -> None:
        if not self.approvals:
            return
        pending = self.approvals[self.approval_index]
        if key in (ord("y"), ord("a")):
            self._answer(pending, "allow")
        elif key in (ord("n"), ord("d")):
            self._answer(pending, "deny")
        elif key in (ord("g"), ord("o")):
            # Go to the asker. For an AskUserQuestion this is the only way to
            # answer it at all: allowing the tool call just draws the picker in
            # that agent's own terminal.
            name = pending.get("container")
            for index, container in enumerate(self.containers):
                if container["name"] == name:
                    self.selected = index
                    self.view = "agents"
                    self._attach()
                    return
            self.say(f"{name} is no longer here")

    def _answer(self, pending: dict, decision: str) -> None:
        context = pending.get("context") or {}
        rights = None
        if context.get("kind") == "capability_request" and decision == "allow":
            # Grant what was asked for. Trimming the rights is a judgement call
            # that wants the rights picker, which is a browser affordance --
            # so the terminal console grants as asked or denies, and says so.
            rights = (context.get("request") or {}).get("rights")
        try:
            self.client.answer(pending["id"], decision, rights=rights)
        except ConsoleError as exc:
            self.say(f"could not answer: {exc}")
            return
        self.approvals = [a for a in self.approvals if a["id"] != pending["id"]]
        self.approval_index = max(0, min(self.approval_index, len(self.approvals) - 1))
        extra = f" ({', '.join(rights)})" if rights else ""
        self.say(f"{decision}ed {pending.get('container', '?')}{extra}")

    def _container_key(self, key: int) -> None:
        container = self.current
        if container is None:
            return
        name = container["name"]
        try:
            if key == ord("s"):
                self.client.start(name)
                self.say(f"started {name}")
            elif key == ord("x"):
                self.client.stop(name)
                self.say(f"stopped {name}")
            elif key == ord("i"):
                self.client.interrupt(name)
                self.say(f"interrupted {name}")
            else:
                return
        except ConsoleError as exc:
            self.say(str(exc))
        self.last_fetch = 0.0

    def _attach(self) -> None:
        container = self.current
        if container is None:
            return
        if not container.get("running"):
            self.say(f"{container['name']} is not running")
            return
        # Curses has to be torn down before the agent's own TUI can have the
        # terminal; the caller does that and calls back in.
        self.attach_request = container["name"]

    # ------------------------------------------------------------------
    # drawing
    # ------------------------------------------------------------------

    def draw(self, stdscr: "curses.window") -> None:
        stdscr.erase()
        height, width = stdscr.getmaxyx()
        if height < 8 or width < 40:
            _put(stdscr, 0, 0, "terminal too small", width)
            stdscr.noutrefresh()
            return

        self._draw_header(stdscr, width)
        body_top, body_height = 2, height - 4
        if self.view == "agents":
            self._draw_agents(stdscr, body_top, body_height, width)
        elif self.view == "approvals":
            self._draw_approvals(stdscr, body_top, body_height, width)
        elif self.view == "boards":
            self._draw_boards(stdscr, body_top, body_height, width)
        else:
            self._draw_audit(stdscr, body_top, body_height, width)
        self._draw_footer(stdscr, height, width)
        stdscr.noutrefresh()

    def _draw_header(self, stdscr, width: int) -> None:
        name = self.instance or "capwrap"
        waiting = len(self.approvals)
        left = f" {name} "
        _put(stdscr, 0, 0, left.ljust(width), width, curses.A_REVERSE | curses.A_BOLD)

        tabs = []
        for view in VIEWS:
            label = view
            if view == "approvals" and waiting:
                label = f"approvals({waiting})"
            tabs.append(label)
        column = 0
        for view, label in zip(VIEWS, tabs):
            attr = curses.A_BOLD | (
                _colour(3) if view == "approvals" and waiting else curses.A_NORMAL
            )
            if view == self.view:
                attr = curses.A_REVERSE | curses.A_BOLD
            text = f" {label} "
            _put(stdscr, 1, column, text, width - column, attr)
            column += len(text) + 1

    def _draw_agents(self, stdscr, top: int, height: int, width: int) -> None:
        list_width = min(28, max(16, width // 4))
        for index, container in enumerate(self.containers[:height]):
            running = container.get("running")
            mark = "●" if running else "○"
            attr = curses.A_REVERSE if index == self.selected else curses.A_NORMAL
            if running:
                attr |= _colour(2)
            exit_code = container.get("exit_code")
            tail = (
                f"exit {exit_code}"
                if exit_code is not None
                else ("running" if running else container.get("state", ""))
            )
            line = f"{mark} {container['name']}"[: list_width - 2]
            _put(stdscr, top + index, 0, line.ljust(list_width - 1), list_width, attr)
            _put(
                stdscr,
                top + index,
                list_width - len(tail) - 1,
                tail,
                len(tail) + 1,
                curses.A_DIM,
            )

        if not self.containers:
            _put(stdscr, top, 0, "no containers", width, curses.A_DIM)
            return

        # The selected agent's screen, live. (`selected`, not `container`: the
        # loop above already bound that name to each listed container.)
        selected = self.current
        assert selected is not None
        pane_left = list_width + 1
        pane_width = width - pane_left
        for row in range(height):
            _put(stdscr, top + row, list_width, "│", 1, curses.A_DIM)

        title = f"{selected['name']} — Enter to attach"
        _put(stdscr, top, pane_left, title[:pane_width], pane_width, curses.A_BOLD)

        lines = self.screens.get(selected["name"], [])
        if not lines:
            note = "not running" if not selected.get("running") else "(no output yet)"
            _put(stdscr, top + 2, pane_left, note, pane_width, curses.A_DIM)
            return
        for row, line in enumerate(lines[-(height - 2) :], start=top + 2):
            _put(stdscr, row, pane_left, line.rstrip()[:pane_width], pane_width)

    def _draw_approvals(self, stdscr, top: int, height: int, width: int) -> None:
        if not self.approvals:
            _put(stdscr, top, 0, "Nothing waiting on you.", width, curses.A_DIM)
            return

        row = top
        for index, pending in enumerate(self.approvals):
            if row >= top + height - 1:
                break
            selected = index == self.approval_index
            marker = "▸ " if selected else "  "
            attr = curses.A_BOLD if selected else curses.A_NORMAL
            context = pending.get("context") or {}
            kind = context.get("kind")

            heading = f"{marker}{pending.get('container', '?')}"
            if kind == "capability_request":
                heading += " · capability request"
            elif kind == "user_question":
                heading += " · asking you"
            _put(stdscr, row, 0, heading, width, attr | _colour(3))
            row += 1

            for line in textwrap.wrap(pending.get("question", ""), max(20, width - 4))[
                :3
            ]:
                if row >= top + height - 1:
                    break
                _put(stdscr, row, 4, line, width - 4, attr)
                row += 1

            if kind == "user_question" and selected:
                _put(
                    stdscr,
                    row,
                    4,
                    "a question, not a permission — g goes to its terminal",
                    width - 4,
                    curses.A_DIM,
                )
                row += 1
            row += 1

    def _draw_boards(self, stdscr, top: int, height: int, width: int) -> None:
        if not self.boards:
            _put(
                stdscr,
                top,
                0,
                "No boards. An agent with a factory makes one: "
                "capctl board create <slot> <topic>",
                width,
                curses.A_DIM,
            )
            return
        row = top
        for board in self.boards:
            if row >= top + height:
                break
            _put(stdscr, row, 0, f"{board['topic']}", width, curses.A_BOLD | _colour(4))
            holders = ", ".join(
                f"{h['container']}:"
                + (
                    "+".join(
                        x
                        for x, y in (("post", h["may_post"]), ("read", h["may_read"]))
                        if y
                    )
                    or "none"
                )
                for h in board.get("holders", [])
            )
            row += 1
            _put(stdscr, row, 2, holders[: width - 2], width - 2, curses.A_DIM)
            row += 1
            for post in reversed(board.get("recent", [])[-5:]):
                if row >= top + height:
                    break
                payload = post["payload"]
                body = payload if isinstance(payload, str) else json.dumps(payload)
                _put(
                    stdscr,
                    row,
                    2,
                    f"#{post['id']} {post['from']}: {body}"[: width - 2],
                    width - 2,
                )
                row += 1
            row += 1

    def _draw_audit(self, stdscr, top: int, height: int, width: int) -> None:
        if not self.audit:
            _put(stdscr, top, 0, "Nothing logged yet.", width, curses.A_DIM)
            return
        for row, entry in enumerate(self.audit[:height], start=top):
            when = time.strftime("%H:%M:%S", time.localtime(entry["ts"]))
            allowed = entry.get("allowed")
            verdict = "ok  " if allowed else "DENY"
            line = (
                f"{when} {verdict} {entry.get('actor', ''):<10} "
                f"{entry.get('op', ''):<18} {entry.get('target') or ''}"
            )
            _put(
                stdscr,
                row,
                0,
                line[:width],
                width,
                curses.A_NORMAL if allowed else _colour(1) | curses.A_BOLD,
            )

    def _draw_footer(self, stdscr, height: int, width: int) -> None:
        if self.error:
            _put(
                stdscr,
                height - 2,
                0,
                self.error[:width].ljust(width),
                width,
                _colour(1) | curses.A_BOLD,
            )
        elif self.status and time.monotonic() < self.status_until:
            _put(
                stdscr,
                height - 2,
                0,
                self.status[:width].ljust(width),
                width,
                _colour(2),
            )

        # Only the keys that do something in this view: offering "Enter attach"
        # on the audit log invites a keypress that goes nowhere.
        keys = {
            "approvals": "y allow  n deny  g go to it",
            "agents": "Enter attach  s start  x stop  i interrupt",
        }.get(self.view, "")
        keys = f"{keys}  " if keys else ""
        keys += "Tab view  r refresh  q quit"
        _put(stdscr, height - 1, 0, f" {keys}".ljust(width), width, curses.A_REVERSE)


# --------------------------------------------------------------------------
# drawing helpers
# --------------------------------------------------------------------------


def _put(window, y: int, x: int, text: str, width: int, attr: int = 0) -> None:
    """Write text, clipped, swallowing the bottom-right-corner error.

    curses raises when a write ends exactly at the last cell, which is a corner
    case every curses program has to swallow rather than a condition worth
    reporting.
    """
    if width <= 0 or x < 0:
        return
    try:
        window.addnstr(y, x, text, max(0, width), attr)
    except curses.error:
        pass


_COLOURS_READY = False


def _colour(pair: int) -> int:
    return curses.color_pair(pair) if _COLOURS_READY else 0


def _init_colours() -> None:
    global _COLOURS_READY
    try:
        curses.start_color()
        curses.use_default_colors()
        for index, colour in enumerate(
            (
                curses.COLOR_RED,
                curses.COLOR_GREEN,
                curses.COLOR_YELLOW,
                curses.COLOR_CYAN,
            ),
            start=1,
        ):
            curses.init_pair(index, colour, -1)
        _COLOURS_READY = True
    except curses.error:
        _COLOURS_READY = False


# --------------------------------------------------------------------------
# attaching
# --------------------------------------------------------------------------


def attach(client: Client, name: str) -> str:
    """Hand the real terminal to one agent until Ctrl-] takes it back.

    Raw bytes both ways over the same WebSocket the browser uses, so the agent
    gets a real terminal -- its TUI, its colours, its keys -- rather than a
    line-at-a-time approximation.

    Written against the socket module rather than a WebSocket library because
    the framing needed here is small and the alternative is a dependency the
    guest side could not carry either.
    """
    from .ws import WebSocket

    try:
        ws = WebSocket.connect(client.host, client.port, f"/ws/terminal/{name}")
    except OSError as exc:
        return f"could not attach to {name}: {exc}"

    stdin_fd = sys.stdin.fileno()
    try:
        saved = termios.tcgetattr(stdin_fd)
    except termios.error:
        saved = None

    try:
        if saved is not None:
            tty.setraw(stdin_fd)
        _send_resize(ws)
        sys.stdout.write("\x1b[2J\x1b[H")
        sys.stdout.flush()

        while True:
            readable, _, _ = select.select([stdin_fd, ws.sock], [], [], 0.2)

            if stdin_fd in readable:
                data = os.read(stdin_fd, 4096)
                if not data:
                    break
                if DETACH_KEY in data:
                    before = data.split(DETACH_KEY, 1)[0]
                    if before:
                        ws.send_text(
                            json.dumps(
                                {
                                    "type": "input",
                                    "data": before.decode("utf-8", "replace"),
                                }
                            )
                        )
                    break
                ws.send_text(
                    json.dumps(
                        {"type": "input", "data": data.decode("utf-8", "replace")}
                    )
                )

            if ws.sock in readable:
                for opcode, payload in ws.receive():
                    if opcode == 0x8:  # close
                        return f"{name} closed the connection"
                    if opcode == 0x2:  # binary: terminal output
                        sys.stdout.buffer.write(payload)
                        sys.stdout.buffer.flush()
                    elif opcode == 0x1:  # text: control JSON
                        with _quiet():
                            note = json.loads(payload.decode())
                            if note.get("type") == "error":
                                return note.get("message", "")
    except OSError as exc:
        return f"attach to {name} ended: {exc}"
    finally:
        if saved is not None:
            termios.tcsetattr(stdin_fd, termios.TCSADRAIN, saved)
        ws.close()

    return f"detached from {name}"


def _send_resize(ws) -> None:
    """Tell the PTY the size of the terminal it has just been handed."""
    try:
        size = os.get_terminal_size()
    except OSError:
        return
    ws.send_text(
        json.dumps({"type": "resize", "cols": size.columns, "rows": size.lines})
    )


class _quiet:
    """Ignore a malformed control frame rather than dropping the session."""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return exc_type is not None and issubclass(
            exc_type, (json.JSONDecodeError, UnicodeDecodeError)
        )


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------


def run(host: str = "127.0.0.1", port: int = 8420) -> int:
    client = Client(host=host, port=port)
    try:
        client.instance()
    except ConsoleError as exc:
        print(f"capwrap: {exc}", file=sys.stderr)
        print("Start one with `capwrap up`, or pass --port.", file=sys.stderr)
        return 1

    console = Console(client)
    while console.running:
        curses.wrapper(_loop, console)
        # `_loop` returns when an attach is asked for, so that curses is fully
        # torn down before the agent's own full-screen program takes over.
        if console.attach_request:
            name, console.attach_request = console.attach_request, None
            console.say(attach(client, name))
            console.last_fetch = 0.0
    return 0


def _loop(stdscr, console: Console) -> None:
    _init_colours()
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.keypad(True)

    while console.running and console.attach_request is None:
        height, _width = stdscr.getmaxyx()
        if time.monotonic() - console.last_fetch >= REFRESH_SECONDS:
            console.refresh(rows=max(6, height - 6))

        console.draw(stdscr)
        curses.doupdate()

        # A short poll rather than blocking input: the console has to redraw as
        # agents produce output, not only when a key is pressed.
        key = stdscr.getch()
        if key == -1:
            time.sleep(0.05)
            continue
        if key == curses.KEY_RESIZE:
            continue
        console.handle(key)
