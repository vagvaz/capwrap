"""``capwrap doctor`` -- the pre-flight guard against config/host drift.

Twice in one session, containers ran a stale agent install because a config
hard-coded (or resolved, once, at generation time) an agent package path that
had since drifted from the binary on PATH.  This module is the guard: a
host-side preflight that catches the drift *before* a container is spawned.

Every check is a pure function returning a `Result`, so they are testable
without a daemon, a container or a network.  The CLI in `cli.cmd_doctor`
renders them one line each and exits 1 only on a FAIL; warnings never fail.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

#: Where the daemon answers by default; `capwrap up` binds here.
DAEMON_HOST = "127.0.0.1"
DAEMON_PORT = 8420

#: The exact wording of the incident this doctor exists to prevent.  Do not
#: paraphrase it in tests -- it is the message an operator greps for.
DRIFT_WARNING = (
    "{agent} configured at {path} is {configured} but `{bin}` on PATH is "
    "{path_resolved} \u2014 containers will run the old one"
)

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_PATH = REPO_ROOT / "examples" / "roles-and-personas" / "compose.py"

_COMPOSE_MODULE: Any = None


def load_compose() -> Any:
    """Import examples/roles-and-personas/compose.py for its AGENT_SETUP table.

    Same trick the web layer uses (see `web.app._load_compose`): the module is
    a script, so it is loaded by path rather than as a package.  Loading runs
    `find_pi_package()` once, which shells out to `npm root -g` locally -- no
    network.
    """
    global _COMPOSE_MODULE
    if _COMPOSE_MODULE is None:
        spec = importlib.util.spec_from_file_location(
            "capwrap_doctor_compose", COMPOSE_PATH
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load {COMPOSE_PATH}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _COMPOSE_MODULE = module
    return _COMPOSE_MODULE


# ---------------------------------------------------------------------------
# results
# ---------------------------------------------------------------------------


@dataclass
class Result:
    """One check's outcome.  ``info`` is exit-neutral, like ``ok``."""

    name: str
    status: str  # "ok" | "warn" | "fail" | "info"
    detail: str = ""


def exit_code(results: list[Result]) -> int:
    """0 unless any check FAILED; warnings and info never fail."""
    return 1 if any(r.status == "fail" for r in results) else 0


# ---------------------------------------------------------------------------
# version comparison
# ---------------------------------------------------------------------------

_VERSION_RE = re.compile(r"v?(\d+(?:\.\d+)*)")


def parse_version(text: str) -> tuple[int, ...] | None:
    """The leading ``X.Y.Z`` prefix of a version string, as integers.

    Lenient on purpose: agents print things like ``1.0.58 (Claude Code)``,
    ``0.85.1-beta`` or ``v2.3``.  Anything with no numeric prefix at all is
    unparseable (``None``).
    """
    match = _VERSION_RE.search(text)
    if match is None:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def compare_versions(a: str, b: str) -> int | None:
    """Semver-style compare of two version strings: -1, 0, 1 -- or None.

    None means at least one side had no parseable version, and no verdict can
    be reached; callers treat that as "cannot compare", never as "equal".
    Missing components count as zero, so ``1.2`` equals ``1.2.0``.
    """
    va, vb = parse_version(a), parse_version(b)
    if va is None or vb is None:
        return None
    width = max(len(va), len(vb))
    va += (0,) * (width - len(va))
    vb += (0,) * (width - len(vb))
    return (va > vb) - (va < vb)


# ---------------------------------------------------------------------------
# agent drift
# ---------------------------------------------------------------------------


def _run_version(argv: list[str]) -> str | None:
    """``<path> --version`` output, leniently parsed; None when it will not say."""
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    out = (proc.stdout or "") + (proc.stderr or "")
    return out.strip() or None


def _configured_binary(compose_mod: Any, agent: str) -> Path | None:
    """The host path the agent's AGENT_SETUP mounts or runs.

    Derived from the table rather than hard-coded, so it stays right when
    compose.py changes: the command's first element is the in-sandbox path,
    and the mount whose dest covers it carries the host source.  For pi the
    "install" is the npm package directory, resolved by find_pi_package.
    """
    if agent == "pi":
        return compose_mod.find_pi_package()
    setup = compose_mod.AGENT_SETUP[agent]
    command = setup["command"]
    if not command:
        return None
    guest_path = command[0]
    if not guest_path.startswith("/"):
        return None
    guest = guest_path
    best: tuple[str, str] | None = None
    best_len = -1
    for src, dest, _mode in setup["mounts"]:
        dest_norm = dest.rstrip("/") or "/"
        if guest == dest_norm or guest.startswith(dest_norm + "/"):
            if len(dest_norm) > best_len:
                best, best_len = (src, dest_norm), len(dest_norm)
    if best is None:
        return None
    src, dest_norm = best
    host = Path(src).expanduser()
    if guest != dest_norm:
        host = host / guest[len(dest_norm) + 1 :]
    return host


def check_agent_drift(
    agent: str,
    compose_mod: Any,
    which: Callable[[str], str | None] = shutil.which,
    version_of: Callable[[list[str]], str | None] = _run_version,
    exists: Callable[[Path], bool] = lambda p: p.exists(),
) -> Result:
    """Does the install the config would use match the binary on PATH?

    The core check.  A mismatch is a WARN, not a FAIL: the container will run
    *something*, just not the version the operator thinks it is.  A missing
    binary on either side is a FAIL -- the container would not start at all.
    """
    configured = _configured_binary(compose_mod, agent)
    if configured is None:
        return Result(
            f"agent {agent}",
            "fail",
            "no install found for this agent (pi: npm install -g "
            "@earendil-works/pi-coding-agent)",
        )
    if not exists(configured):
        return Result(
            f"agent {agent}", "fail", f"configured install {configured} does not exist"
        )

    bin_name = (
        "pi"
        if agent == "pi"
        else Path(compose_mod.AGENT_SETUP[agent]["command"][0]).name
    )
    on_path = which(bin_name)
    if on_path is None:
        return Result(f"agent {agent}", "fail", f"`{bin_name}` not found on PATH")

    # Configured version: pi's package.json; the others, the binary itself.
    if agent == "pi":
        try:
            data = json.loads((configured / "package.json").read_text())
        except (OSError, ValueError):
            configured_version = None
        else:
            configured_version = data.get("version") if isinstance(data, dict) else None
    else:
        configured_version = version_of([str(configured), "--version"])
    path_version = version_of([on_path, "--version"])

    if configured_version is None or path_version is None:
        return Result(
            f"agent {agent}",
            "ok",
            f"configured {configured}; `{bin_name}` at {on_path} "
            "(version not reported; cannot compare)",
        )

    verdict = compare_versions(str(configured_version), str(path_version))
    if verdict is None:
        return Result(
            f"agent {agent}",
            "ok",
            f"configured {configured} ({configured_version}); "
            f"`{bin_name}` at {on_path} ({path_version}) -- versions unparseable",
        )
    if verdict != 0:
        return Result(
            f"agent {agent}",
            "warn",
            DRIFT_WARNING.format(
                agent=agent,
                path=configured,
                configured=configured_version,
                bin=bin_name,
                path_resolved=path_version,
            ),
        )
    return Result(
        f"agent {agent}",
        "ok",
        f"{configured_version} at {configured} matches `{on_path}`",
    )


# ---------------------------------------------------------------------------
# per-config checks
# ---------------------------------------------------------------------------


def check_command_paths(config) -> list[Result]:
    """Every absolute path in runtime.command exists, directly or via a mount.

    ``/opt/pi/pi-agent/dist/bundle/cli.js`` is not on the host -- but the
    config binds the pi package there, so the check resolves it through the
    mount table instead of failing it.
    """
    results: list[Result] = []
    mounts = sorted(
        (
            (m.dest.rstrip("/") or "/", m.src)
            for m in config.mounts
            if m.src is not None
        ),
        key=lambda pair: -len(pair[0]),
    )
    for token in config.runtime.command:
        if not token.startswith("/"):
            continue
        if Path(token).exists():
            results.append(Result(f"command path {token}", "ok", "exists on host"))
            continue
        resolved = None
        for dest, src in mounts:
            if token == dest or token.startswith(dest + "/"):
                resolved = Path(src).expanduser() / token[len(dest) + 1 :]
                break
        if resolved is not None and resolved.exists():
            results.append(
                Result(f"command path {token}", "ok", f"via mount -> {resolved}")
            )
        else:
            results.append(
                Result(
                    f"command path {token}",
                    "fail",
                    "does not exist on host and no mount provides it",
                )
            )
    return results


def check_mounts(config) -> list[Result]:
    """Every mount src exists (and is readable, for copy mode)."""
    results: list[Result] = []
    for mount in config.mounts:
        if mount.src is None:
            continue
        src = Path(os.path.expanduser(mount.src))
        if not src.exists():
            results.append(
                Result(f"mount {mount.dest}", "fail", f"source {src} does not exist")
            )
        elif mount.mode == "copy" and not os.access(src, os.R_OK):
            results.append(
                Result(
                    f"mount {mount.dest}",
                    "fail",
                    f"copy source {src} is not readable",
                )
            )
        else:
            results.append(
                Result(f"mount {mount.dest}", "ok", f"{mount.mode} <- {src}")
            )
    return results


def check_files(config) -> list[Result]:
    """Every [[files]] src exists (already resolved against the TOML's dir)."""
    results: list[Result] = []
    for spec in config.files:
        if spec.src is None:
            continue
        if spec.src.is_file():
            results.append(Result(f"file {spec.dest}", "ok", f"<- {spec.src}"))
        else:
            results.append(
                Result(f"file {spec.dest}", "fail", f"source {spec.src} does not exist")
            )
    return results


def check_env(config, environ: dict[str, str] | None = None) -> list[Result]:
    """Every env_from_host var is present in the host environment.

    A missing var is a WARN, not a FAIL: the container starts, just without
    the token -- which usually shows up as the agent failing to authenticate.
    """
    env = os.environ if environ is None else environ
    return [
        Result(
            f"env {name}",
            "ok" if name in env else "warn",
            "present" if name in env else f"containers will start without {name}",
        )
        for name in config.runtime.env_from_host
    ]


def check_worktrees(config, runner: Callable = subprocess.run) -> list[Result]:
    """Worktree mounts: repo exists, base resolves, branch not taken elsewhere.

    The branch conflict is the sneaky one: `git worktree add` refuses a branch
    that is already checked out in another worktree, so the spawn dies after
    the operator walked away.  Caught here, before the spawn.
    """
    results: list[Result] = []
    for mount in config.mounts:
        if mount.mode != "worktree" or mount.src is None:
            continue
        src = Path(os.path.expanduser(mount.src))
        if not src.exists():
            results.append(
                Result(f"worktree {mount.dest}", "fail", f"repo {src} does not exist")
            )
            continue
        base = mount.base or "HEAD"
        try:
            proc = runner(
                ["git", "-C", str(src), "rev-parse", "--verify", base],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            results.append(
                Result(f"worktree {mount.dest}", "fail", f"git failed: {exc}")
            )
            continue
        if proc.returncode != 0:
            results.append(
                Result(
                    f"worktree {mount.dest}",
                    "fail",
                    f"base {base!r} does not resolve in {src}: "
                    f"{(proc.stderr or '').strip()}",
                )
            )
            continue
        results.append(
            Result(f"worktree {mount.dest}", "ok", f"base {base} resolves in {src}")
        )

        branch = mount.branch
        if not branch:
            continue
        try:
            listing = runner(
                ["git", "-C", str(src), "worktree", "list", "--porcelain"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if listing.returncode != 0:
            continue
        checked_out = _worktree_branches(listing.stdout)
        if branch in checked_out:
            results.append(
                Result(
                    f"worktree {mount.dest}",
                    "warn",
                    f"branch {branch} is checked out at {checked_out[branch]}; "
                    "spawn will fail until it is released",
                )
            )
    return results


def _worktree_branches(porcelain: str) -> dict[str, str]:
    """branch -> worktree path, from `git worktree list --porcelain` output."""
    out: dict[str, str] = {}
    path: str | None = None
    for line in porcelain.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree ") :]
        elif line.startswith("branch ") and path is not None:
            ref = line[len("branch ") :]
            out[ref.removeprefix("refs/heads/")] = path
    return out


# ---------------------------------------------------------------------------
# daemon
# ---------------------------------------------------------------------------


def check_daemon(
    host: str = DAEMON_HOST, port: int = DAEMON_PORT, timeout: float = 2.0
) -> Result:
    """Is a capwrap daemon answering?  If so, how many containers run?

    Unreachable is INFO, not a warning: doctor runs pre-spawn, so no daemon
    is the normal state, not a problem.
    """
    url = f"http://{host}:{port}/api/overview"
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url), timeout=timeout
        ) as resp:
            data = json.load(resp)
    except (urllib.error.URLError, OSError, ValueError):
        return Result(
            "daemon",
            "info",
            f"no capwrap answering on {host}:{port} (doctor runs pre-spawn)",
        )
    containers = data.get("containers") or []
    running = sum(1 for c in containers if isinstance(c, dict) and c.get("running"))
    return Result(
        "daemon",
        "ok",
        f"capwrap on {host}:{port}: {running} container(s) running "
        f"of {len(containers)} registered",
    )


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------


def agent_kinds(configs) -> list[str]:
    """The agent kinds to check: every agent when no configs, else those used.

    With no --config the doctor still guards the whole environment: every
    agent compose.py knows about is checked, since any of them could be
    spawned next.
    """
    compose_mod = load_compose()
    known = list(compose_mod.AGENT_SETUP)
    if not configs:
        return known
    used = {c.runtime.agent for c in configs if c.runtime.agent in known}
    return [a for a in known if a in used]


def run_checks(configs) -> list[tuple[str, list[Result]]]:
    """Run everything, grouped: environment first, then one group per config."""
    compose_mod = load_compose()
    groups: list[tuple[str, list[Result]]] = []

    env_results: list[Result] = []
    for agent in agent_kinds(configs):
        env_results.append(check_agent_drift(agent, compose_mod))
    env_results.append(check_daemon())
    groups.append(("environment", env_results))

    for config in configs:
        results: list[Result] = []
        results.extend(check_command_paths(config))
        results.extend(check_mounts(config))
        results.extend(check_files(config))
        results.extend(check_env(config))
        results.extend(check_worktrees(config))
        groups.append((f"config {config.name}", results))
    return groups


def render(groups: list[tuple[str, list[Result]]], color: bool = True) -> str:
    """One line per check: ``OK/WARN/FAIL  check-name  detail``."""

    def paint(text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if color else text

    marks = {
        "ok": ("OK", "32"),
        "warn": ("WARN", "33"),
        "fail": ("FAIL", "31"),
        "info": ("INFO", "36"),
    }
    lines: list[str] = []
    for header, results in groups:
        lines.append(paint(f"== {header}", "1"))
        for result in results:
            mark, code = marks.get(result.status, ("?", "0"))
            lines.append(f"{paint(mark, code):<6} {result.name:<28} {result.detail}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
