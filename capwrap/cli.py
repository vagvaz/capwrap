"""``capwrap`` -- the host-side command line."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
from pathlib import Path
from typing import Any

from . import container_files
from .config import load_config
from .errors import CapwrapError
from .paths import ContainerPaths, force_rmtree, state_root
from .runtime import bwrap as bwrap_mod
from .runtime import fsprep, probe


def _guest_tools_dir() -> Path:
    return Path(__file__).resolve().parent / "guest"


def cmd_doctor(args: argparse.Namespace) -> int:
    report = probe.run_all()
    print("capwrap doctor\n")
    print(probe.format_report(report, color=sys.stdout.isatty()))
    return 0 if report.ok else 1


def _resolve_backend(requested: str) -> str:
    if requested != "auto":
        return requested
    backend = probe.run_all().overlay_backend
    if backend is None:
        raise CapwrapError(
            "no overlay backend available on this host; run `capwrap doctor`"
        )
    return backend


def _needs_overlay(config) -> bool:
    return any(m.mode == "overlay" for m in config.mounts)


def cmd_run(args: argparse.Namespace) -> int:
    """Prepare a container's filesystem and exec into it.

    The foreground, no-daemon path: useful for developing a config and for
    poking around inside a sandbox by hand.  `capwrap up` is the managed
    equivalent that the capability kernel and the web UI drive.
    """
    config = load_config(args.config)
    if args.name:
        config.name = args.name
    config.validate_sources()

    backend = (
        _resolve_backend(args.overlay_backend) if _needs_overlay(config) else "kernel"
    )

    bwrap_bin, why_not = probe.find_working_bwrap()
    if not bwrap_bin:
        raise CapwrapError(
            f"no usable bwrap on this host ({why_not}); run `capwrap doctor`"
        )

    paths = ContainerPaths(config.name)
    prepared = fsprep.prepare(config, paths, overlay_backend=backend)

    if args.command:
        config.runtime.command = list(args.command)

    argv = bwrap_mod.build_argv(
        config,
        prepared,
        paths,
        bwrap=bwrap_bin,
        guest_tools=_guest_tools_dir(),
    )

    env = bwrap_mod.build_env(config)

    if args.dry_run:
        print(f"# container: {config.name}")
        print(f"# state:     {paths.root}")
        print(f"# overlay:   {backend}")
        for line in fsprep.describe(prepared):
            print(f"# mount:     {line}")
        print("#")
        # Values of secret-looking names are masked: the point of passing the
        # environment this way is that tokens do not end up somewhere readable.
        for key, value in sorted(bwrap_mod.redact(env).items()):
            print(f"# env:       {key}={value}")
        print()
        print(bwrap_mod.render(argv))
        prepared.cleanup()
        return 0

    if not args.quiet:
        print(
            f"capwrap: {config.name} -> {' '.join(config.runtime.command)}",
            file=sys.stderr,
        )

    try:
        # execve, not execv: the container's environment is inherited rather
        # than passed as --setenv, so it never appears in argv.
        os.execve(argv[0], argv, env)
    except OSError as exc:
        prepared.cleanup()
        raise CapwrapError(f"failed to exec {argv[0]}: {exc}") from None
    return 0  # pragma: no cover - execv does not return


def cmd_show(args: argparse.Namespace) -> int:
    """Validate a config and print what it resolves to."""
    config = load_config(args.config)
    if not args.no_check:
        config.validate_sources()
    paths = ContainerPaths(config.name)

    print(f"name:     {config.name}")
    print(f"command:  {' '.join(config.runtime.command)}")
    print(f"cwd:      {config.runtime.cwd}")
    if config.proxied_network:
        print("network:  through the capability proxy")
    else:
        print(f"network:  {'host, unrestricted' if config.sandbox.network else 'none'}")
    print(f"state:    {paths.root}")
    print("mounts:")
    for m in config.mounts:
        detail = f"{m.mode:9s} {m.dest}"
        if m.src:
            detail += f"  <- {m.src}"
        if m.mode == "worktree":
            detail += f"  [branch={m.branch or 'detached'} share={m.share}]"
        print(f"  {detail}")
    if config.files:
        print("files:")
        for f in config.files:
            origin = str(f.src) if f.src else "<inline>"
            print(f"  {f.dest}  <- {origin}")
    print("caps:")
    print(f"  parent: {', '.join(config.caps.parent) or 'none'}")
    if config.caps.factory:
        q = config.caps.factory.quota.containers
        print(f"  factory: {', '.join(config.caps.factory.rights)} (quota {q})")
    for p in config.caps.peers:
        print(f"  peer {p.container}: {', '.join(p.rights)}")
    for d in config.caps.dataspaces:
        print(f"  dataspace {d.path}: {', '.join(d.rights)}")
    for n in config.caps.network:
        print(f"  net {n.name}: {n.pattern}  ({', '.join(n.rights)})")
    return 0


def cmd_clean(args: argparse.Namespace) -> int:
    """Delete a container's host-side state directory."""
    paths = ContainerPaths(args.name)
    if not paths.root.exists():
        print(f"nothing to clean for {args.name}")
        return 0
    if not args.yes:
        print(f"would remove {paths.root}")
        print("re-run with --yes to actually delete it")
        return 1
    force_rmtree(paths.root)
    print(f"removed {paths.root}")
    return 0


def cmd_up(args: argparse.Namespace) -> int:
    """Run the daemon, the containers and the web interface together.

    All in one process and one event loop, so an approval clicked in the browser
    resolves the future a blocked agent is waiting on directly.
    """
    import asyncio
    import socket

    import uvicorn

    from .daemon import Daemon
    from .web.app import create_app

    configs = [load_config(path) for path in args.configs]
    for config in configs:
        config.validate_sources()

    # Bind before starting anything. uvicorn only reports a bind failure once
    # it is already serving, which would otherwise leave containers running
    # behind a web interface that never came up.
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listener.bind((args.host, args.port))
    except OSError as exc:
        listener.close()
        raise CapwrapError(
            f"cannot bind {args.host}:{args.port}: {exc}. "
            "Another capwrap may already be running -- check with "
            f"`ss -ltn | grep {args.port}`, or pass a different --port."
        ) from None
    listener.listen(2048)

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(
            f"capwrap: WARNING -- serving on {args.host}, and the web interface has\n"
            "         no authentication. Anyone who can reach this port can type\n"
            "         into any agent's terminal. Prefer binding to a VPN address,\n"
            "         or an SSH tunnel from the client.\n",
            file=sys.stderr,
        )

    instance_name = args.name or os.environ.get("CAPWRAP_NAME", "")

    async def run() -> None:
        daemon = Daemon(trace_messages=args.trace, instance_name=instance_name)
        for config in configs:
            daemon.register(config)
        # Two passes, so configs may refer to each other in any order.
        daemon.link_all_peers()
        # Re-establish team membership (shared boards) for any teams persisted
        # from a previous run whose members have just been re-registered.
        daemon.link_team_membership()

        if not args.no_start:
            for config in configs:
                await daemon.start(config.name)

        app = create_app(daemon)
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                log_level="warning",
                access_log=False,
            )
        )

        label = f"capwrap[{instance_name}]" if instance_name else "capwrap"
        print(f"{label}: {len(configs)} container(s) registered")
        for config in configs:
            print(f"  - {config.name}")
        if args.trace:
            print(
                "\n  message tracing is ON -- every message between containers "
                "is recorded,\n  payloads included. Turn it off in the "
                "Messages tab when you are done."
            )
        shown = "127.0.0.1" if args.host == "0.0.0.0" else args.host
        print(f"\n  web interface: http://{shown}:{args.port}\n", flush=True)

        try:
            await server.serve(sockets=[listener])
        finally:
            await daemon.shutdown()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    """Bring a new container into a capwrap that is already running.

    The config is loaded and validated *here*, in the directory its relative
    paths are written against, and sent over resolved. That also means a typo
    is reported against the file you just edited, before anything is registered.
    """
    import json
    import urllib.error
    import urllib.request

    configs = [load_config(path) for path in args.configs]
    for config in configs:
        config.validate_sources()

    base = f"http://{args.host}:{args.port}"
    added = []
    for config in configs:
        payload = json.dumps(
            {
                "config": json.loads(config.model_dump_json(exclude={"source_dir"})),
                "start": not args.no_start,
            }
        ).encode()
        request = urllib.request.Request(
            f"{base}/api/containers",
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                added.append(json.load(response))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            with contextlib.suppress(Exception):
                body = json.loads(detail)
                detail = body.get("error") or body.get("detail") or detail
            raise CapwrapError(f"{config.name}: {detail}") from None
        except urllib.error.URLError as exc:
            raise CapwrapError(
                f"no capwrap answering on {base} ({exc.reason}). "
                "Start one with `capwrap up`, or pass --port."
            ) from None

    for entry in added:
        state = "started" if entry.get("started") else "registered, not started"
        print(f"{entry['name']}: {state}")
    return 0


def cmd_team(args: argparse.Namespace) -> int:
    """Spawn a whole team of containers from a team TOML file.

    The team is parsed and validated here, then sent to a running capwrap which
    generates each member's config, registers and starts them, creates the
    shared board and records the team.
    """
    import json
    import urllib.error
    import urllib.request

    from .teams import parse_team

    team = parse_team(args.file)

    base = f"http://{args.host}:{args.port}"
    payload = json.dumps({"team": team.to_dict()}).encode()
    request = urllib.request.Request(
        f"{base}/api/teams/spawn",
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        with contextlib.suppress(Exception):
            body = json.loads(detail)
            detail = body.get("error") or body.get("detail") or detail
        raise CapwrapError(f"team {team.name}: {detail}") from None
    except urllib.error.URLError as exc:
        raise CapwrapError(
            f"no capwrap answering on {base} ({exc.reason}). "
            "Start one with `capwrap up`, or pass --port."
        ) from None

    print(f"team {result['team']}: spawned {', '.join(result['members'])}")
    return 0


def cmd_tui(args: argparse.Namespace) -> int:
    """The terminal console, against a capwrap that is already running."""
    from .tui import run

    return run(host=args.host, port=args.port)


def cmd_state(args: argparse.Namespace) -> int:
    root = state_root()
    print(root)
    containers = root / "containers"
    if containers.is_dir():
        for child in sorted(containers.iterdir()):
            print(f"  {child.name}")
    return 0


# --------------------------------------------------------------------------
# container files: diff, listing, copy-out
#
# These talk to a running capwrap over its web API, like `add` and `team` do:
# the daemon holds each container's config (base and branch of its worktree),
# and it is the one process that knows which containers exist. The heavy
# lifting lives in capwrap.container_files, shared with the web endpoints.
# --------------------------------------------------------------------------


def _daemon_url(args: argparse.Namespace, path: str) -> str:
    return f"http://{args.host}:{args.port}{path}"


def _fetch_json(args: argparse.Namespace, path: str) -> Any:
    import urllib.error
    import urllib.request

    request = urllib.request.Request(_daemon_url(args, path))
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        with contextlib.suppress(Exception):
            body = json.loads(detail)
            detail = body.get("error") or body.get("detail") or detail
        raise CapwrapError(detail) from None
    except urllib.error.URLError as exc:
        raise CapwrapError(
            f"no capwrap answering on http://{args.host}:{args.port} "
            f"({exc.reason}). Start one with `capwrap up`."
        ) from None


def _fetch_bytes(args: argparse.Namespace, path: str) -> bytes:
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(
            urllib.request.Request(_daemon_url(args, path)), timeout=120
        ) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        with contextlib.suppress(Exception):
            body = json.loads(detail)
            detail = body.get("error") or body.get("detail") or detail
        raise CapwrapError(detail) from None
    except urllib.error.URLError as exc:
        raise CapwrapError(
            f"no capwrap answering on http://{args.host}:{args.port} "
            f"({exc.reason}). Start one with `capwrap up`."
        ) from None


def _human_size(size: int | None) -> str:
    if size is None:
        return "?"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}GB"  # pragma: no cover - unreachable


def cmd_diff(args: argparse.Namespace) -> int:
    """What the container's worktree branch adds over its base."""
    result = _fetch_json(args, f"/api/containers/{args.name}/diff")
    if result.get("empty"):
        print(f"no changes vs {result['base']}")
        return 0
    if args.stat:
        print(result["stat"], end="" if result["stat"].endswith("\n") else "\n")
        return 0
    if result.get("stat"):
        print(result["stat"], end="" if result["stat"].endswith("\n") else "\n")
        print()
    print(result["diff"], end="" if result["diff"].endswith("\n") else "\n")
    if result.get("truncated"):
        print(f"[diff truncated at {container_files.DIFF_CAP // 1024} KB]")
    return 0


def cmd_files(args: argparse.Namespace) -> int:
    """What the container produced outside git, grouped by area."""
    result = _fetch_json(args, f"/api/containers/{args.name}/files")
    groups = result.get("groups") or []
    if not groups:
        print(f"{args.name}: nothing outside git yet")
        return 0
    for group in groups:
        print(f"== {group['label']} ({len(group['entries'])})")
        for entry in group["entries"]:
            status = f" [{entry['status']}]" if entry.get("status") else ""
            size = _human_size(entry.get("size"))
            print(f"  {size:>9}  {entry['path']}{status}")
        if group.get("truncated"):
            print(f"  ... truncated at {container_files.LISTING_CAP} entries")
    if result.get("truncated"):
        print(f"[listing truncated at {container_files.LISTING_CAP} entries]")
    return 0


def cmd_get(args: argparse.Namespace) -> int:
    """Copy one of the container's files out to the host."""
    from urllib.parse import quote

    quoted = quote(args.path)
    data = _fetch_bytes(args, f"/api/containers/{args.name}/files/raw?path={quoted}")
    dest = Path(args.dest) if args.dest else Path.cwd() / Path(args.path).name
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    print(f"{args.path} -> {dest} ({_human_size(len(data))})")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="capwrap",
        description="Capability-governed bubblewrap containers for AI agents.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("doctor", help="check that this host can run capwrap")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("run", help="prepare and enter a container in the foreground")
    p.add_argument("config", help="path to a container .toml")
    p.add_argument("--name", help="override the container name")
    p.add_argument(
        "--dry-run", action="store_true", help="print the bwrap command and exit"
    )
    p.add_argument("--quiet", "-q", action="store_true")
    p.add_argument(
        "--overlay-backend",
        choices=["auto", "kernel", "fuse"],
        default="auto",
    )
    p.set_defaults(func=cmd_run, command=[])

    p = sub.add_parser("show", help="validate a config and show what it resolves to")
    p.add_argument("config")
    p.add_argument(
        "--no-check", action="store_true", help="skip checking that source paths exist"
    )
    p.set_defaults(func=cmd_show)

    p = sub.add_parser(
        "up", help="run containers under the daemon, with the web interface"
    )
    p.add_argument("configs", nargs="+", help="one or more container .toml files")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8420)
    p.add_argument(
        "--no-start",
        action="store_true",
        help="register the containers but do not launch them",
    )
    p.add_argument(
        "--name",
        default="",
        help="what this capwrap is for, e.g. 'FastPath HashTable'. "
        "Shown in the header and the browser tab, so several "
        "instances stay tellable apart (env: CAPWRAP_NAME)",
    )
    p.add_argument(
        "--trace",
        action="store_true",
        help="record every message passed between containers, "
        "payloads included, for the Messages tab (also "
        "switchable there while running)",
    )
    p.set_defaults(func=cmd_up)

    p = sub.add_parser(
        "add", help="add a container to a capwrap that is already running"
    )
    p.add_argument("configs", nargs="+", help="one or more container .toml files")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8420)
    p.add_argument(
        "--no-start", action="store_true", help="register it but do not launch it"
    )
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("tui", help="the terminal console: approvals, screens, attach")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8420)
    p.set_defaults(func=cmd_tui)

    p = sub.add_parser("team", help="manage teams of containers")
    team_sub = p.add_subparsers(dest="team_command", required=True)
    tp = team_sub.add_parser("up", help="spawn a whole team from a team TOML file")
    tp.add_argument("file", help="path to a team .toml")
    tp.add_argument("--host", default="127.0.0.1")
    tp.add_argument("--port", type=int, default=8420)
    tp.set_defaults(func=cmd_team)

    p = sub.add_parser("clean", help="remove a container's host-side state")
    p.add_argument("name")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_clean)

    p = sub.add_parser("state", help="print the state directory and known containers")
    p.set_defaults(func=cmd_state)

    p = sub.add_parser(
        "diff", help="what a container's worktree branch adds over its base"
    )
    p.add_argument("name")
    p.add_argument("--stat", action="store_true", help="summary form only")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8420)
    p.set_defaults(func=cmd_diff)

    p = sub.add_parser("files", help="list what a container produced outside git")
    p.add_argument("name")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8420)
    p.set_defaults(func=cmd_files)

    p = sub.add_parser("get", help="copy one of a container's files out to the host")
    p.add_argument("name")
    p.add_argument("path", help="relative to the worktree, home or shared dir")
    p.add_argument("--dest", help="where on the host to put it (default: ./<name>)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8420)
    p.set_defaults(func=cmd_get)

    return parser


def _split_command(argv: list[str]) -> tuple[list[str], list[str]]:
    """Split argv at the first bare ``--``.

    Everything after it is the command to run inside the sandbox.  Done by hand
    rather than with `argparse.REMAINDER`, which starts hoovering at the first
    positional and would swallow capwrap's own flags:
    ``capwrap run cfg.toml --dry-run`` would treat ``--dry-run`` as the command.
    """
    try:
        cut = argv.index("--")
    except ValueError:
        return argv, []
    return argv[:cut], argv[cut + 1 :]


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    raw, inner_command = _split_command(raw)

    parser = build_parser()
    args = parser.parse_args(raw)
    if inner_command:
        args.command = inner_command
    try:
        return args.func(args)
    except CapwrapError as exc:
        print(f"capwrap: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
