#!/usr/bin/env python3
"""Build a capwrap config for a role crossed with a persona.

Roles and personas are orthogonal, and they are not the same *kind* of thing:

- A **role** is a job and a set of capabilities. It is enforced. A reviewer with
  `Write` denied cannot write, whatever it decides about itself mid-session.
- A **persona** is a disposition. It is a prompt and nothing else, and it cannot
  be enforced at all. A "lazy" agent that you actually need to be unable to do
  something needs a different *role*, not a stronger adjective.

The capability shape of each role lives in the table below, which is the part
worth reading. The personas contribute nothing to it -- deliberately, because
pretending otherwise would be the whole mistake this example exists to avoid.

    ./compose.py architect:idealist architect:pragmatist
    capwrap up examples/roles-and-personas/built/*.toml

    ./compose.py --list
    ./compose.py --all-personas reviewer      # one role, every disposition
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys
import urllib.parse

HERE = pathlib.Path(__file__).resolve().parent
BUILT = HERE / "built"

#: The opencode config that decides which provider endpoints an agent actually
#: calls. `network = "auto"` reads this to derive the exact holes to punch in
#: an otherwise closed network, instead of handing over the host's whole stack.
OPCODE_CONFIG_DIR = pathlib.Path("~/.config/opencode").expanduser()

#: What a role may do, as capwrap and Claude both enforce it.
#:
#: `permissions` are Claude's own rules, so a denied tool is refused inside the
#: agent. `write` says what the container gets of the repository. `factory`
#: gives it the authority to create other containers at all.
ROLES: dict[str, dict] = {
    # -- decide, do not implement -------------------------------------
    "architect": {
        "summary": "decides the shape; writes notes, not code",
        "allow": [
            "Read",
            "Glob",
            "Grep",
            "Write",
            "Bash(git log:*)",
            "Bash(git diff:*)",
        ],
        "deny": ["Edit", "Bash(sudo *)"],
        "work": "worktree",
    },
    "designer": {
        "summary": "owns how it is used; source is read-only",
        "allow": ["Read", "Glob", "Grep", "Write"],
        "deny": ["Edit", "Bash(sudo *)"],
        "work": "worktree",
    },
    "product-owner": {
        "summary": "decides what is built and why; no repository at all",
        "allow": ["Read", "Write", "TodoWrite"],
        "deny": ["Edit", "Bash(sudo *)"],
        "work": "none",
    },
    "api-owner": {
        "summary": "owns the public surface; writes a decision log",
        "allow": [
            "Read",
            "Glob",
            "Grep",
            "Write",
            "Bash(git log:*)",
            "Bash(git diff:*)",
        ],
        "deny": ["Edit", "Bash(sudo *)"],
        "work": "worktree",
    },
    "domain-expert": {
        "summary": "knows the problem, not the code; no repository at all",
        "allow": ["Read", "Write", "TodoWrite"],
        "deny": ["Edit", "Bash(sudo *)"],
        "work": "none",
    },
    # -- read, report, change nothing ---------------------------------
    "reviewer": {
        "summary": "reviews a diff; cannot write, by construction",
        "allow": [
            "Read",
            "Glob",
            "Grep",
            "Bash(git log:*)",
            "Bash(git diff:*)",
            "Bash(git show:*)",
        ],
        "deny": ["Write", "Edit", "Bash(sudo *)"],
        "work": "worktree",
    },
    "security-reviewer": {
        "summary": "reviews for hostile input; no network, so no unsourced claims",
        "allow": ["Read", "Glob", "Grep", "Bash(git log:*)", "Bash(git diff:*)"],
        "deny": ["Write", "Edit", "WebFetch", "WebSearch", "Bash(sudo *)"],
        "work": "worktree",
        "network": False,
    },
    "accessibility-reviewer": {
        "summary": "reviews for people using it without a mouse or without sight",
        "allow": ["Read", "Glob", "Grep"],
        "deny": ["Write", "Edit", "Bash(sudo *)"],
        "work": "worktree",
    },
    "archaeologist": {
        "summary": "works out why the code is the way it is; writes nothing",
        "allow": [
            "Read",
            "Glob",
            "Grep",
            "Bash(git log:*)",
            "Bash(git blame:*)",
            "Bash(git show:*)",
            "Bash(git diff:*)",
        ],
        "deny": ["Write", "Edit", "Bash(sudo *)"],
        "work": "worktree",
    },
    "user": {
        "summary": "not on the team; sees the built artifact, never the source",
        "allow": ["Read", "Bash(ls:*)"],
        "deny": ["Write", "Edit", "Glob", "Grep", "Bash(sudo *)"],
        "work": "none",
    },
    # -- write code ----------------------------------------------------
    "implementer": {
        "summary": "makes it work, in the smallest change that does",
        "allow": ["Read", "Glob", "Grep", "Write", "Edit"],
        "deny": ["Bash(sudo *)"],
        "work": "worktree",
        "network": "auto",
    },
    "refactorer": {
        "summary": "changes how it is written, not what it does",
        "allow": ["Read", "Glob", "Grep", "Write", "Edit"],
        "deny": ["Bash(sudo *)"],
        "work": "worktree",
        "network": "auto",
    },
    "debugger": {
        "summary": "finds out why; may watch and drive another agent's terminal",
        "allow": ["Read", "Glob", "Grep", "Write", "Edit"],
        "deny": ["Bash(sudo *)"],
        "work": "worktree",
        "network": "auto",
    },
    "performance-engineer": {
        "summary": "measures first; every claim has a before and an after",
        "allow": ["Read", "Glob", "Grep", "Write", "Edit"],
        "deny": ["Bash(sudo *)"],
        "work": "worktree",
        "network": "auto",
    },
    "integrator": {
        "summary": "merges branches and keeps main working",
        "allow": ["Read", "Glob", "Grep", "Write", "Edit"],
        "deny": ["Bash(sudo *)"],
        "work": "worktree",
        "network": "auto",
    },
    "build-engineer": {
        "summary": "owns the toolchain; needs the network, so says what it pulled in",
        "allow": ["Read", "Glob", "Grep", "Write", "Edit"],
        "deny": ["Bash(sudo *)"],
        "work": "worktree",
        "network": "auto",
    },
    "technical-writer": {
        "summary": "writes the documentation; reads the source, changes none of it",
        "allow": ["Read", "Glob", "Grep", "Write", "Bash(git log:*)"],
        "deny": ["Edit", "Bash(sudo *)"],
        "work": "worktree",
    },
    # -- tests ---------------------------------------------------------
    "test-writer": {
        "summary": "writes tests, not fixes",
        "allow": ["Read", "Glob", "Grep", "Write", "Edit"],
        "deny": ["Bash(sudo *)"],
        "work": "worktree",
        "network": "auto",
    },
    "tester": {
        "summary": "runs it and tries to break it; changes no source",
        "allow": ["Read", "Glob", "Grep"],
        "deny": ["Write", "Edit", "Bash(sudo *)"],
        "work": "worktree",
        "network": "auto",
    },
    # -- hold a factory ------------------------------------------------
    "manager": {
        "summary": "decides what is worked on and by whom; writes no code",
        "allow": ["Read", "Glob", "Grep", "Write", "TodoWrite"],
        "deny": ["Edit", "Bash(sudo *)"],
        "work": "worktree",
        "network": "auto",
        "factory": {
            "containers": 3,
            "child_rights": ["send", "inspect", "read_output"],
        },
    },
    "tech-lead": {
        "summary": "accountable for the code, and writes it; can steer its agents",
        "allow": ["Read", "Glob", "Grep", "Write", "Edit"],
        "deny": ["Bash(sudo *)"],
        "work": "worktree",
        "network": "auto",
        "factory": {
            "containers": 3,
            "child_rights": ["send", "inspect", "read_output", "write_input", "signal"],
        },
    },
    "orchestrator": {
        "summary": "splits the task; may spawn another orchestrator for a big piece",
        "allow": ["Read", "Glob", "Grep", "Write", "TodoWrite"],
        "deny": ["Edit", "Bash(sudo *)"],
        "work": "worktree",
        "network": "auto",
        # Generous child_rights on purpose. A child's factory may only hand on
        # rights contained in its parent's, so this list is the ceiling for the
        # whole tree below this agent -- a sub-orchestrator cannot give its own
        # children anything that is not in here, and its quota is capped at
        # whatever remains of this one. Recursion is bounded by the kernel, not
        # by the orchestrator behaving itself.
        "factory": {
            "containers": 4,
            "child_rights": ["send", "inspect", "read_output", "write_input", "signal"],
        },
    },
}

PERSONAS = sorted(p.stem for p in (HERE / "personas").glob("*.md"))

TEMPLATE = """\
# {role} \u00b7 {persona}
#
# {summary}
#
# Generated by compose.py -- edit the role and persona prompts, not this file.
#
#   capwrap up examples/roles-and-personas/built/{fname}.toml

name = "{fname}"

[runtime]
agent = "{agent}"
command = [{command}]
# Role and persona are one system prompt: it survives compaction, and the
# agent cannot talk itself out of either halfway through a session.  capwrap
# binds the file and delivers it natively for whichever agent this runs.
role_prompt = "{fname}.md"
cwd       = "/work"
tty       = true
approvals = "{approvals}"
# Where the agent's plain questions surface: "forward" (the operator's console
# Questions tab), "block" (no console card; the agent states its question in
# its own terminal and ends its turn), or "auto" (autonomous mode: the
# question is auto-answered "use best judgment, note it" and recorded for
# later review).  Permission requests and escalations always create cards.
question_routing = "{routing}"

auto_allow = [{auto_allow}]
auto_deny  = [{auto_deny}]

env_from_host = [{env}]

{permissions}[sandbox]
network  = {network}
hostname = "{fname}"
unshare  = ["pid", "ipc", "uts", "cgroup"]

{mounts}{work}{files}[caps]
parent = ["send"]
{caps}"""

#: The role's capability table, for agents that enforce one natively. This is
#: the half of the role that is *enforced*; the persona is a disposition and
#: enforces nothing.
NATIVE_PERMISSIONS = """\
# The role's capability table, enforced by the agent's own permission system.
[runtime.permissions]
allow = [{allow}]
ask   = ["WebFetch"]
deny  = [{deny}]
"""

#: Shell posture for generated configs. Roles are the pre-decision layer: the
#: operator encodes intent once, in the role table, and the approval queue
#: should be quiet by role design -- not because the operator rubber-stamps.
#: So every role gets an *ambient* baseline (read-only shell, git reads, the
#: capctl comms verbs: the ability to function, not authority), the role table
#: carries the distinctive powers on top, a hard denylist bounds the shell,
#: and the queue is left for genuine outliers. Only the roles whose value IS
#: read-only-ness deny the mutating verbs outright.
SHELL_DENYLIST = ["sudo *", "curl *", "wget *", "rm -rf *"]
SHELL_READONLY = [
    "ls*",
    "find*",
    "cat*",
    "grep*",
    "rg*",
    "head*",
    "tail*",
    "wc*",
    "sort*",
    "uniq*",
    "diff*",
    "stat*",
    "file*",
    "tree*",
    "which*",
    "pwd",
    "echo*",
    "cd*",
    "mkdir*",
    "node *",
]
GIT_READONLY = [
    "git status*",
    "git log*",
    "git diff*",
    "git show*",
    "git branch --list*",
]
GIT_MUTATING = [
    "git commit*",
    "git push*",
    "git reset*",
    "git clean*",
    "git checkout*",
    "git rebase*",
    "git merge*",
]
CAPCTL_COMMS = ["capctl recv*", "capctl send*", "capctl ask*"]
AMBIENT_SHELL = SHELL_READONLY + GIT_READONLY + CAPCTL_COMMS

#: Roles whose real work is building and changing the tree: their shell grant
#: adds the dev toolchain and the git work verbs. Publishing (push) and
#: history rewriting (reset, clean) stay un-listed on purpose -- they prompt,
#: which is the conservative default for the irreversible. Everything here is
#: worktree-contained: the wall keeps the rest of the filesystem read-only.
WORK_SHELL_ROLES = {
    "implementer",
    "refactorer",
    "debugger",
    "integrator",
    "build-engineer",
    "performance-engineer",
    "test-writer",
    "tester",
    "tech-lead",
}
WORK_SHELL = [
    "make*",
    "cargo*",
    "go*",
    "just*",
    "cmake*",
    "ninja*",
    "meson*",
    "gradle*",
    "./gradlew*",
    "mvn*",
    "./mvnw*",
    "dotnet*",
    "mix*",
    "./scripts/*",
    "pytest*",
    "python*",
    "python3*",
    "pip*",
    "pip3*",
    "npx*",
    "npm*",
    "yarn*",
    "pnpm*",
    "bun*",
    "ruff*",
    "mypy*",
    "pyright*",
    "tsc*",
    "prettier*",
    "eslint*",
    "touch*",
    "cp*",
    "mv*",
    "sed*",
    "rm*",
    "chmod*",
    "ln*",
    "tar*",
    "unzip*",
    "env",
    "date",
    "sleep*",
]
GIT_WORK = [
    "git add*",
    "git commit*",
    "git stash*",
    "git checkout*",
    "git merge*",
    "git rebase*",
    "git branch*",
]

#: Roles whose guarantee IS read-only-ness; their shell stays enumerated and
#: mutating verbs are denied, not asked.
READONLY_SHELL_ROLES = {"reviewer", "security-reviewer"}


#: What each agent needs to run: the command that starts it, the tool name its
#: shell gate answers to (they disagree: claude says Bash, opencode v2 renamed
#: it Shell, pi is lowercase), the mounts that bring its binary and config into
#: the sandbox, and the host env vars that carry its credentials.  The role
#: table above is agent-agnostic -- this is the only per-agent part, and
#: `--agent` switches it.
#:
def find_pi_package() -> pathlib.Path | None:
    """Locate the installed pi-coding-agent package, wherever npm put it.

    The install moves between prefixes (a system update landed in /usr while
    ~/.local held a stale copy, then the system copy was removed again), so
    the config resolves the package at generation time instead of hard-coding
    one location.
    """
    candidates = [
        pathlib.Path(
            "~/.local/lib/node_modules/@earendil-works/pi-coding-agent"
        ).expanduser(),
        pathlib.Path("/usr/lib/node_modules/@earendil-works/pi-coding-agent"),
    ]
    try:
        root = subprocess.run(
            ["npm", "root", "-g"], capture_output=True, text=True, timeout=10
        ).stdout.strip()
        if root:
            candidates.insert(0, pathlib.Path(root) / "@earendil-works/pi-coding-agent")
    except (OSError, subprocess.SubprocessError):
        pass
    for candidate in candidates:
        if (candidate / "package.json").is_file():
            return candidate
    return None


def _pi_entry(package: pathlib.Path) -> str:
    """The CLI entry from the package's own ``bin`` field."""
    try:
        bin_field = json.loads((package / "package.json").read_text()).get("bin", {})
        entry = bin_field.get("pi") if isinstance(bin_field, dict) else bin_field
        return entry or "dist/bundle/cli.js"
    except (OSError, ValueError):
        return "dist/bundle/cli.js"


_PI_PACKAGE = find_pi_package()


def _pi_setup() -> dict:
    """The pi agent adapter, pointed at wherever the package actually lives."""
    if _PI_PACKAGE is None:
        return {
            "command": [],
            "native_permissions": False,
            "bash_tool": "bash",
            "mounts": [("~/.pi/agent", "/home/agent/.pi/agent", "copy")],
            "env": ["OPENCODE_API_KEY"],
            "files": False,
        }
    return {
        "command": [
            "node",
            str(pathlib.Path("/opt/pi/pi-agent") / _pi_entry(_PI_PACKAGE)),
            "--thinking",
            "high",
        ],
        "native_permissions": False,
        "bash_tool": "bash",
        "mounts": [
            (str(_PI_PACKAGE), "/opt/pi/pi-agent", "ro"),
            ("~/.pi/agent", "/home/agent/.pi/agent", "copy"),
        ],
        "env": ["OPENCODE_API_KEY"],
        "files": False,
    }


#: `command` is required: `runtime.command` defaults to a bare shell, which is
#: never what a role container means.  `native_permissions` marks agents whose
#: CLI enforces allow/deny itself (claude, opencode); the others have none, so
#: the role's rules fold into `auto_allow`/`auto_deny` and the
#: `[runtime.permissions]` block is omitted -- a permissions block on such an
#: agent is a config error.
AGENT_SETUP: dict[str, dict] = {
    "claude": {
        "command": ["/opt/claude/claude"],
        "approvals": "capwrap",
        "native_permissions": True,
        "bash_tool": "Bash",
        "mounts": [
            ("~/.local/bin/claude", "/opt/claude/claude", "ro"),
            ("~/.claude", "/home/agent/.claude", "copy"),
        ],
        "env": ["ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"],
        "files": True,  # house rules land at /work/CLAUDE.md
    },
    "opencode": {
        "command": ["/opt/opencode/opencode"],
        "approvals": "native",
        "native_permissions": True,
        "bash_tool": "Bash",
        "mounts": [
            ("~/.opencode/bin", "/opt/opencode", "ro"),
            ("~/.config/opencode", "/home/agent/.config/opencode", "copy"),
            ("~/.local/share/opencode", "/home/agent/.local/share/opencode", "rw"),
        ],
        "env": ["OPENCODE_API_KEY"],
        "files": False,
    },
    "opencode2": {
        "command": ["/opt/opencode/opencode2", "--standalone"],
        "approvals": "capwrap",
        "native_permissions": True,
        "bash_tool": "Shell",
        "mounts": [
            ("~/.opencode/bin", "/opt/opencode", "ro"),
            ("~/.config/opencode2", "/home/agent/.config/opencode2", "copy"),
            ("~/.local/share/opencode", "/home/agent/.local/share/opencode", "rw"),
        ],
        "env": ["OPENCODE_API_KEY"],
        "files": False,
    },
    "pi": _pi_setup(),
}

WORKTREE = """
[[mounts]]
src        = "{src}"
dest       = "/work"
mode       = "worktree"
branch     = "capwrap/{name}"
base       = "{base}"
on_destroy = "keep"
"""

NO_REPO = """
# No repository. This role's judgement is worth something *because* it is not
# based on reading the source.
[[mounts]]
dest = "/work"
mode = "tmpfs"
size = "64m"
"""

FACTORY = """
[caps.factory]
rights       = ["create"]
quota        = {{ containers = {containers} }}
child_rights = [{child_rights}]
"""


def _strip_comments(text: str) -> str:
    """Remove // and /* */ comments from JSONC, leaving strings intact."""
    out: list[str] = []
    i, n = 0, len(text)
    in_string = False
    escape = False
    while i < n:
        ch = text[i]
        if escape:
            out.append(ch)
            escape = False
            i += 1
            continue
        if in_string:
            out.append(ch)
            if ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def parse_jsonc(text: str) -> dict:
    """Parse JSONC (JSON with // and /* */ comments and trailing commas).

    Tolerant on purpose: opencode configs are hand-edited and routinely carry
    comments and trailing commas that strict JSON rejects. No new dependency --
    comments are stripped and trailing commas removed before the stdlib parser
    runs.
    """
    text = _strip_comments(text)
    text = re.sub(r",\s*([}\]])", r"\1", text)
    return json.loads(text)


def _base_urls_from(data: dict) -> list[str]:
    """Pull every provider entry's ``options.baseURL`` out of a parsed config."""
    urls: list[str] = []
    providers = data.get("provider")
    if not isinstance(providers, dict):
        return urls
    for entry in providers.values():
        if not isinstance(entry, dict):
            continue
        options = entry.get("options")
        if not isinstance(options, dict):
            continue
        base = options.get("baseURL")
        if isinstance(base, str) and base:
            urls.append(base)
    return urls


def _referenced_presets(data: dict) -> list[str]:
    """Names of config presets the main config points at (e.g. the ``plugin``
    array), so their provider endpoints are folded in too."""
    refs: list[str] = []
    plugin = data.get("plugin")
    if isinstance(plugin, list):
        for p in plugin:
            if isinstance(p, str) and p:
                refs.append(p)
    return refs


def _opencode_config_path() -> pathlib.Path:
    """The opencode config to read for auto-inference: opencode.json or, if
    that is absent, opencode.jsonc."""
    for name in ("opencode.json", "opencode.jsonc"):
        candidate = OPCODE_CONFIG_DIR / name
        if candidate.exists():
            return candidate
    return OPCODE_CONFIG_DIR / "opencode.json"


def extract_base_urls(path: pathlib.Path) -> list[str]:
    """Provider ``options.baseURL`` values from an opencode config and any
    ``*.jsonc`` (or ``*.json``) presets it references.

    Returns an empty list when the config is missing or unparseable -- the
    caller treats that as "no endpoints found", never as "open the network".
    """
    path = pathlib.Path(path).expanduser()
    if not path.exists():
        return []
    try:
        data = parse_jsonc(path.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    urls = _base_urls_from(data)
    for ref in _referenced_presets(data):
        for candidate in (path.parent / f"{ref}.jsonc", path.parent / f"{ref}.json"):
            if candidate.exists():
                try:
                    urls.extend(_base_urls_from(parse_jsonc(candidate.read_text())))
                except (json.JSONDecodeError, OSError):
                    pass
    return urls


def _host_port(url: str) -> tuple[str | None, int]:
    """Split a base URL into (host, port), defaulting the port by scheme."""
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return None, 0
    if not parsed.hostname:
        return None, 0
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return parsed.hostname, port


def _anchored_pattern(host: str, port: int) -> str:
    """A NetRuleCap pattern: a regex over ``host:port``, anchored at both ends
    and with the host's metacharacters escaped, so ``api.example.com:443``
    cannot also match ``evil-api.example.com:443``."""
    return rf"^{re.escape(host)}:{port}$"


def endpoints_to_rules(base_urls: list[str]) -> list[dict]:
    """Turn provider base URLs into NetRuleCap dicts, one per unique host:port.

    Each rule is a single named hole in an otherwise closed network, matching
    NetRuleCap's exact semantics (see capwrap/config.py): ``name`` plus an
    anchored ``pattern`` over ``host:port``.
    """
    seen: set[tuple[str, int]] = set()
    rules: list[dict] = []
    for url in base_urls:
        host, port = _host_port(url)
        if host is None:
            continue
        key = (host, port)
        if key in seen:
            continue
        seen.add(key)
        rules.append(
            {
                "name": f"model-api-{len(rules) + 1}",
                "pattern": _anchored_pattern(host, port),
            }
        )
    return rules


def quote(items: list[str]) -> str:
    return ", ".join(f'"{item}"' for item in items)


def dedupe(items: list[str], key=lambda x: x) -> list[str]:
    """First occurrence wins, order preserved."""
    seen: set = set()
    return [x for x in items if not (key(x) in seen or seen.add(key(x)))]


def compose(
    role: str,
    persona: str,
    agent: str = "claude",
    generous: bool = False,
    extra_prompt: str = "",
    peers: "list[str] | None" = None,
    routing: str = "forward",
    source: str | None = None,
    base: str | None = None,
    extra_mounts: "list[dict] | None" = None,
    extra_env: "list[str] | None" = None,
) -> pathlib.Path:
    """Build one config.

    The optional project parameters (``source``, ``base``, ``extra_mounts``,
    ``extra_env``) come from a capwrap Project: where the worktree forks from
    instead of the demo default, extra binds appended after the standard
    mounts, and extra host env vars merged into ``env_from_host``. Every one
    is optional and the defaults are exactly today's behaviour.
    """
    if role not in ROLES:
        sys.exit(f"unknown role {role!r}; try --list")
    if persona not in PERSONAS:
        sys.exit(f"unknown persona {persona!r}; try --list")
    if agent not in AGENT_SETUP:
        sys.exit(f"unknown agent {agent!r}; try --list")
    if routing not in ("forward", "block", "auto"):
        sys.exit(f"unknown routing {routing!r}; try --routing forward|block|auto")
    if agent == "pi" and _PI_PACKAGE is None:
        sys.exit(
            "pi-coding-agent is not installed; "
            "npm install -g @earendil-works/pi-coding-agent"
        )

    spec = ROLES[role]
    setup = AGENT_SETUP[agent]
    # The agent is part of the generated name so the same role-persona pair
    # can be built for several agents without the worktree branches (which
    # carry the name) colliding.
    fname = name = (
        f"{role}-{persona}" if agent == "claude" else f"{agent}-{role}-{persona}"
    )
    BUILT.mkdir(exist_ok=True)

    # One prompt file, both halves, clearly separated. capwrap binds it at
    # /run/capwrap-role.md and delivers it natively for the agent -- claude
    # gets a system-prompt flag, opencode an instructions entry, pi a flag --
    # so the role and persona texts stay single copies editable on their own.
    prompt = (
        (HERE / "roles" / f"{role}.md").read_text().rstrip()
        + "\n\n---\n\n"
        + (HERE / "personas" / f"{persona}.md").read_text().rstrip()
        + "\n\n---\n\n"
        + "Your role is the job you are accountable for and is enforced by the\n"
        "capabilities you hold. Your persona is how you go about it. Where they\n"
        "pull against each other, the role wins: a reviewer with a lazy\n"
        "disposition still reviews, it just does not gold-plate the write-up.\n"
    )
    if extra_prompt:
        prompt += "\n\n---\n\n" + extra_prompt.rstrip() + "\n"
    (BUILT / f"{fname}.md").write_text(prompt)
    # Copied rather than referenced with `..`, so that `built/` can be moved or
    # shipped somewhere on its own and still work.
    (BUILT / "house.md").write_text((HERE / "house.md").read_text())

    mounts = "".join(
        f'\n[[mounts]]\nsrc  = "{src}"\ndest = "{dest}"\nmode = "{mode}"\n'
        for src, dest, mode in setup["mounts"]
    )
    # Extra binds a Project asked for, appended after the standard mounts and
    # the worktree mount, before any [[files]] entries.
    extra_mounts_toml = "".join(
        f'\n[[mounts]]\nsrc  = "{m["src"]}"\ndest = "{m["dest"]}"\nmode = "{m["mode"]}"\n'
        for m in extra_mounts or []
    )
    # Extra env_from_host vars, merged with the agent's own (first occurrence
    # wins, so an agent's own list keeps priority).
    env_vars = dedupe([*setup["env"], *(extra_env or [])])
    files = (
        '\n[[files]]\ndest = "/work/CLAUDE.md"\nsrc  = "house.md"\n\n'
        if setup["files"]
        else ""
    )

    factory = spec.get("factory")
    bash_tool = setup["bash_tool"]

    def retag(rules: list[str]) -> list[str]:
        """Role tables are written in claude's vocabulary; retag shell rules
        to whichever tool name this agent's gate answers to."""
        return [f"{bash_tool}{r[4:]}" if r.startswith("Bash(") else r for r in rules]

    readonly_shell = role in READONLY_SHELL_ROLES
    # `--auto-allow` trades role precision for a quiet queue: everything
    # except the denylist. Read-only roles keep their guarantee regardless --
    # read-only-ness is the point of those roles, not a queue-saving measure.
    generous_shell = generous and not readonly_shell
    if setup["native_permissions"]:
        auto_allow, auto_deny = ["Read", "Glob", "Grep", "TodoWrite"], ["Bash(sudo *)"]
        allow, deny = retag(spec["allow"]), retag(spec["deny"])
        if generous_shell:
            allow = [bash_tool, *allow]
        else:
            # The ambient baseline: read-only shell, git reads, capctl comms.
            allow += [f"{bash_tool}({p})" for p in AMBIENT_SHELL]
            if role in WORK_SHELL_ROLES:
                allow += [f"{bash_tool}({p})" for p in WORK_SHELL + GIT_WORK]
        deny += [f"{bash_tool}({p})" for p in SHELL_DENYLIST]
        if readonly_shell:
            # Read-only-ness is the point: mutations denied, not asked.
            deny += [f"{bash_tool}({p})" for p in GIT_MUTATING]
        permissions = NATIVE_PERMISSIONS.format(
            allow=quote(dedupe(allow)), deny=quote(dedupe(deny))
        )
    else:
        if generous_shell:
            auto_allow, auto_deny = (
                ["*"],
                dedupe([*(f"bash({p})" for p in SHELL_DENYLIST), *retag(spec["deny"])]),
            )
        else:
            auto_allow = dedupe(
                [
                    "read",
                    "glob",
                    "grep",
                    *retag(spec["allow"]),
                    *(f"bash({p})" for p in AMBIENT_SHELL),
                    *(
                        f"bash({p})"
                        for p in (WORK_SHELL + GIT_WORK)
                        if role in WORK_SHELL_ROLES
                    ),
                ],
                key=str.lower,
            )
            auto_deny = dedupe(
                [*(f"bash({p})" for p in SHELL_DENYLIST), *retag(spec["deny"])],
                key=str.lower,
            )
            if readonly_shell:
                auto_deny = dedupe(
                    [*auto_deny, *(f"bash({p})" for p in GIT_MUTATING)],
                    key=str.lower,
                )
        permissions = ""

    # -- network ---------------------------------------------------------
    # True/False keep today's exact behaviour: `network = true` hands over the
    # host's whole stack, `false` (or absent) is a closed network. "auto" opts
    # into the capability proxy instead: it derives the provider endpoints the
    # agent will actually call from the opencode config and emits one
    # [[caps.network]] rule per unique host:port -- visible in the TOML, never
    # applied silently. If nothing is found it emits no rules and a comment,
    # rather than silently opening the network.
    network_mode = spec.get("network", True)
    if network_mode == "auto":
        network_line = "false"
        rules = endpoints_to_rules(extract_base_urls(_opencode_config_path()))
        if rules:
            caps_network = "\n" + "\n".join(
                f'[[caps.network]]\nname    = "{r["name"]}"\n'
                f"pattern = '{r['pattern']}'\n"
                for r in rules
            )
        else:
            caps_network = (
                "\n# auto-inference found no provider endpoints in the opencode "
                "config, so no [[caps.network]] rules were emitted.\n"
            )
    else:
        network_line = str(network_mode).lower()
        caps_network = ""

    peers_toml = ""
    if peers:
        peers_toml = "\n" + "\n".join(
            f'[[caps.peers]]\ncontainer = "{p}"\nrights = ["send"]\n' for p in peers
        )

    config = TEMPLATE.format(
        role=role,
        persona=persona,
        fname=fname,
        agent=agent,
        routing=routing,
        approvals=setup.get("approvals", "capwrap"),
        command=quote(setup["command"]),
        summary=spec["summary"],
        env=quote(env_vars),
        auto_allow=quote(auto_allow),
        auto_deny=quote(auto_deny),
        permissions=permissions,
        network=network_line,
        mounts=mounts,
        files=files,
        work=(
            WORKTREE.format(
                name=name,
                src=source or "~/capwrap-demo/repo",
                base=base or "main",
            )
            + extra_mounts_toml
            if spec["work"] == "worktree"
            else NO_REPO + extra_mounts_toml
        ),
        caps=(
            (
                FACTORY.format(
                    containers=factory["containers"],
                    child_rights=quote(factory["child_rights"]),
                )
                if factory
                else ""
            )
            + caps_network
            + peers_toml
        ),
    )
    path = BUILT / f"{fname}.toml"
    # The host ~/.pi/agent copy carries host-specific breakage (a dead serena
    # MCP that kills a fresh boot, a stale defaultModel), patched by two
    # override files that live in built/ next to the generated configs. Emit
    # the overrides automatically so a regeneration cannot silently lose
    # them -- the recurring failure was hand-patches wiped by a re-run.
    if agent == "pi" and (BUILT / "pi-settings.json").is_file():
        config += (
            "\n# Local overrides (host-specific, kept out of this file):\n"
            "# - the host ~/.pi/agent copy references a dead local MCP (serena)\n"
            "#   that kills a fresh pi boot, and its defaultModel is failing\n"
            "#   provider-side.\n"
            '[[files]]\ndest = "/home/agent/.pi/agent/mcp.json"\n'
            'src  = "pi-mcp.json"\n\n'
            '[[files]]\ndest = "/home/agent/.pi/agent/settings.json"\n'
            'src  = "pi-settings.json"\n'
        )
    path.write_text(config)
    return path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build a capwrap config for a role crossed with a persona.",
        epilog="example: ./compose.py architect:idealist architect:pragmatist",
    )
    parser.add_argument("pairs", nargs="*", metavar="ROLE:PERSONA")
    parser.add_argument(
        "--list", action="store_true", help="show every role and persona"
    )
    parser.add_argument(
        "--all-personas", metavar="ROLE", help="build one role against every persona"
    )
    parser.add_argument(
        "--agent",
        default="claude",
        choices=sorted(AGENT_SETUP),
        help="which agent the generated configs run (default: claude)",
    )
    parser.add_argument(
        "--auto-allow",
        action="store_true",
        help="grant the generous shell (everything except the denylist) instead "
        "of the role's ambient baseline; read-only roles stay read-only",
    )
    parser.add_argument(
        "--routing",
        default="forward",
        choices=["forward", "block", "auto"],
        help="where the agent's plain questions surface (default: forward)",
    )
    args = parser.parse_args()

    if args.list:
        print(f"roles ({len(ROLES)}):")
        width = max(len(r) for r in ROLES)
        for role, spec in ROLES.items():
            mark = " [factory]" if spec.get("factory") else ""
            print(f"  {role:<{width}}  {spec['summary']}{mark}")
        print(f"\npersonas ({len(PERSONAS)}):")
        for persona in PERSONAS:
            print(f"  {persona}")
        print(f"\n{len(ROLES) * len(PERSONAS)} combinations.")
        return 0

    pairs = [p.split(":", 1) for p in args.pairs if ":" in p]
    if args.all_personas:
        pairs += [[args.all_personas, p] for p in PERSONAS]
    if not pairs:
        parser.error("give at least one ROLE:PERSONA, or --list")

    for role, persona in pairs:
        path = compose(
            role, persona, args.agent, generous=args.auto_allow, routing=args.routing
        )
        print(f"built {path.relative_to(HERE.parent.parent)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
