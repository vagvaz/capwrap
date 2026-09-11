"""The operator web interface.

One pane of glass over every container: the parent tree, every agent's live
terminal, one approval queue covering all of them, a message composer, and the
capability graph with a revoke button.

The daemon and the web server share a process and an event loop.  That is not
laziness -- it means an approval clicked in the browser resolves the very
`asyncio.Future` that a blocked agent is waiting on, with no polling, no second
store of truth, and no chance of the UI and the kernel disagreeing about what a
container is allowed to do.

Assets are vendored under `static/vendor`, so the interface works with no
network at all -- which matters, since the whole point is running agents that
themselves have no network.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import os
import tempfile
import tomllib
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import container_files
from ..config import ContainerConfig, load_config, load_config_data
from ..daemon import OPERATOR, Daemon
from ..errors import CapabilityError, CapwrapError
from ..explain import ExplainError
from ..kernel.kernel import ROOT
from ..kernel.rights import parse_rights
from ..teams import load_compose as load_team_compose
from ..teams import parse_team_data

STATIC = Path(__file__).resolve().parent / "static"

#: The repo root, which is where examples/roles-and-personas/compose.py lives.
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
COMPOSE_PATH = REPO_ROOT / "examples" / "roles-and-personas" / "compose.py"


def _load_compose():
    """Import examples/roles-and-personas/compose.py without running its CLI.

    The module is a script with a `main()` that calls `sys.exit()` on bad input,
    so the web layer validates against its tables *before* calling `compose()`,
    which is the only function that writes files.
    """
    spec = importlib.util.spec_from_file_location("capwrap_compose", COMPOSE_PATH)
    if spec is None or spec.loader is None:
        raise HTTPException(500, "compose.py could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------
# authority view
# --------------------------------------------------------------------------


def _pattern_inner(pattern: str) -> str | None:
    """The glob inside a ``Tool(glob)`` permission pattern, or None.

    ``Bash(git log:*)`` -> ``git log:*``; a bare tool name like ``Read`` has no
    inner glob and returns None.
    """
    if pattern.endswith(")"):
        open_paren = pattern.find("(")
        if open_paren > 0:
            return pattern[open_paren + 1 : -1]
    return None


def authority_view(daemon: Daemon, container: Any, compose: Any) -> dict:
    """One assembled view of what a container may actually do.

    The capability graph shows only kernel capability objects, which is nearly
    empty for operator-spawned containers: their real authority lives in the
    config's permission lists, the daemon-side grant table, the network rules,
    the peer caps and the team boards. This assembles all of it, lazily, per
    request -- nothing here is cached, because grants and routing change live.
    """
    config = container.config
    runtime = config.runtime

    def dedupe(items: list[str]) -> list[str]:
        return list(dict.fromkeys(items))

    allow = dedupe([*runtime.auto_allow, *runtime.permissions.allow])
    ambient_set = set(compose.AMBIENT_SHELL)
    work_set = set(compose.WORK_SHELL) | set(compose.GIT_WORK)
    ambient: list[str] = []
    work_shell: list[str] = []
    role: list[str] = []
    for pattern in allow:
        inner = _pattern_inner(pattern)
        # compose.py emits shell rules as exactly ``Tool(<table entry>)``, so
        # membership in the tables is the classification; everything else --
        # bare tool names, role-specific Bash globs, retagged bash_tool rules
        # shown as they appear in the config -- is role authority.
        if inner is not None and inner in ambient_set:
            ambient.append(pattern)
        elif inner is not None and inner in work_set:
            work_shell.append(pattern)
        else:
            role.append(pattern)

    deny = dedupe([*runtime.auto_deny, *runtime.permissions.deny])

    peers = []
    seen_peers: set[str] = set()
    for cap in daemon.kernel.cap_list(container.name):
        if cap.kind == "container" and cap.label.startswith("peer:"):
            target = cap.label[len("peer:") :]
            peers.append({"container": target, "rights": cap.rights})
            seen_peers.add(target)
    # Peer caps that named a container never registered stay config-only.
    for peer in config.caps.peers:
        if peer.container not in seen_peers:
            peers.append({"container": peer.container, "rights": peer.rights})

    boards = [
        {
            "topic": cap.detail.get("topic") or cap.label,
            "rights": cap.rights,
        }
        for cap in daemon.kernel.cap_list(container.name)
        if cap.kind == "board"
    ]

    return {
        "container": container.name,
        "allow": {"ambient": ambient, "work_shell": work_shell, "role": role},
        "deny": deny,
        "grants": daemon.container_grants(container.name),
        "network": {
            # sandbox.network = true hands over the host's network namespace
            # wholesale: open and unproxied, which no rule list can constrain.
            "open": config.sandbox.network,
            "rules": [
                {"name": rule.name, "pattern": rule.pattern}
                for rule in config.caps.network
            ],
        },
        "peers": peers,
        "boards": boards,
        "routing": {
            "effective": container.effective_question_routing,
            "config": runtime.question_routing,
            "override": container.question_routing,
        },
    }


# --------------------------------------------------------------------------
# request bodies
# --------------------------------------------------------------------------


class SendBody(BaseModel):
    """One message, to one container or to several.

    `target` is kept alongside `targets` because it is the shape every existing
    caller uses; a broadcast is the same operation with more than one name in it.
    """

    message: str
    target: str | None = None
    targets: list[str] | None = None

    def recipients(self) -> list[str]:
        names = list(self.targets or [])
        if self.target:
            names.append(self.target)
        # Order-preserving dedupe: picking a container twice in the composer
        # must not post to it twice.
        return list(dict.fromkeys(names))


class TraceBody(BaseModel):
    enabled: bool


class AddBody(BaseModel):
    """A container to bring into a capwrap that is already running.

    The config arrives already parsed and with its paths resolved, because the
    CLI that sends it is the thing sitting in the directory the config's relative
    paths are written against.
    """

    config: dict
    start: bool = True


class ApprovalBody(BaseModel):
    decision: str
    reason: str = ""
    #: Only for a capability request: grant narrower rights than were asked for.
    rights: list[str] | None = None


class RevokeBody(BaseModel):
    container: str
    slot: int
    include_self: bool = True


class GrantBody(BaseModel):
    """The operator granting one container a capability on another."""

    holder: str
    target_container: str
    rights: list[str]
    label: str | None = None


class InputBody(BaseModel):
    data: str


class SpawnBody(BaseModel):
    """A role×persona×agent to build and run, as the operator.

    Two shapes share this endpoint: the form shape (role/persona/agent,
    composed daemon-side exactly as before) and an edited config -- `config`
    holding TOML text, e.g. from the spawn dialog's editable preview -- which
    is validated through the same loader and spawned as-is.
    """

    role: str | None = None
    persona: str | None = None
    agent: str = "claude"
    #: Where the spawned agent's plain questions surface; compose.py emits it
    #: into the generated config's [runtime] section. None means "no opinion":
    #: a selected Project's routing is the default, else compose's own.
    routing: str | None = None
    #: Optional model pin for this spawn only. Empty/None = the agent's own
    #: current configuration decides (the live config is copied at spawn and
    #: capwrap merges only policy on top). No capwrap-side model logic.
    model: str | None = None
    #: An optional Project: where the worktree forks from, extra mounts, extra
    #: env, and the routing default. Resolved daemon-side; unknown → 400.
    project: str | None = None
    #: Custom instructions appended to the role+persona prompt. Form shape
    #: only: an edited config carries its own prompt file already.
    extra_prompt: str = ""
    #: An edited config, as TOML text. When present it is the whole request:
    #: role/persona/agent are ignored.
    config: str | None = None


class RoutingBody(BaseModel):
    """A live override of one container's question routing."""

    routing: str


class TeamBody(BaseModel):
    """A whole team to spawn: the team TOML, as a mapping.

    `project` is an optional Project name applied to every member; it may also
    be carried inside the team mapping itself (the CLI's shape).
    """

    team: dict
    project: str | None = None


class ProjectBody(BaseModel):
    """A project to create or update: the project TOML, as a mapping."""

    project: dict


class ResizeBody(BaseModel):
    cols: int
    rows: int


def create_app(daemon: Daemon, shutdown: Callable[[], None] | None = None) -> FastAPI:
    app = FastAPI(
        title="capwrap", docs_url="/api/docs", openapi_url="/api/openapi.json"
    )

    @app.exception_handler(CapwrapError)
    async def _capwrap_error(_request, exc: CapwrapError):
        status = 403 if isinstance(exc, CapabilityError) else 400
        return JSONResponse({"error": str(exc)}, status_code=status)

    # ------------------------------------------------------------------
    # views
    # ------------------------------------------------------------------

    @app.get("/api/overview")
    async def overview() -> dict:
        return daemon.overview()

    @app.get("/api/instance")
    async def instance() -> dict:
        """What this capwrap is called, for the page title and the header."""
        return {"name": daemon.instance_name}

    @app.get("/api/containers")
    async def containers() -> list[dict]:
        return [
            {
                **c.status(),
                "pending_mail": daemon.mailboxes.get(c.name).pending,
            }
            for c in daemon.containers.values()
        ]

    @app.get("/api/containers/{name}")
    async def container(name: str) -> dict:
        c = daemon.containers.get(name)
        if c is None:
            raise HTTPException(404, f"no such container: {name}")
        return {
            **c.status(),
            "config": {
                "command": c.config.runtime.command,
                "cwd": c.config.runtime.cwd,
                "network": c.config.sandbox.network,
                "mounts": [
                    {
                        "dest": m.dest,
                        "mode": m.mode,
                        "src": str(m.src) if m.src else None,
                        "branch": m.branch,
                    }
                    for m in c.config.mounts
                ],
            },
            "caps": [cap.to_dict() for cap in daemon.kernel.cap_list(name)],
            "mailbox": [m.to_dict() for m in daemon.mailboxes.get(name).recent(50)],
            "queued": [m.to_dict() for m in daemon.mailboxes.get(name).queued()],
            # The screen snapshot, not the byte log: the overview tiles need
            # what a TUI currently *shows*, which a replayed byte stream cannot
            # give you without a terminal emulator on the client side.
            "session": (
                {**c.session.status(), "screen": c.session.snapshot().to_dict()}
                if c.session
                else None
            ),
        }

    @app.get("/api/containers/{name}/grants")
    async def grants(name: str) -> list[dict]:
        """A container's grant table: the "always allow" entries."""
        return daemon.container_grants(name)

    @app.get("/api/containers/{name}/authority")
    async def authority(name: str) -> dict:
        """One assembled view of a container's real authority.

        The capability graph renders kernel objects only, which is nearly empty
        for operator-spawned containers; this adds the config's permission
        allow/deny lists (grouped ambient / work-shell / role), the grant
        table, the network rules, peer messaging, team boards and the effective
        question routing. Computed lazily per request; never cached.
        """
        container = daemon.containers.get(name)
        if container is None:
            raise HTTPException(404, f"no such container: {name}")
        return authority_view(daemon, container, _load_compose())

    @app.delete("/api/containers/{name}/grants/{grant_id}")
    async def revoke_grant(name: str, grant_id: str) -> dict:
        """Revoke one grant, so the request prompts again."""
        if not daemon.revoke_grant(name, grant_id):
            raise HTTPException(404, f"no grant {grant_id} for {name}")
        return {"container": name, "revoked": grant_id}

    @app.get("/api/containers/{name}/screen")
    async def screen(name: str, rows: int = 12) -> dict:
        """The tail of a container's screen, for the all-agents overview.

        Separate from the container detail endpoint so refreshing a grid of
        tiles does not also pull every container's capability table and mailbox.
        """
        c = daemon.containers.get(name)
        if c is None:
            raise HTTPException(404, f"no such container: {name}")
        if c.session is None:
            return {"container": name, "running": False, "styled": [], "lines": []}
        snap = c.session.snapshot(tail=max(1, min(rows, 200)))
        return {"container": name, "running": c.running, **snap.to_dict()}

    @app.get("/api/containers/{name}/diff")
    async def container_diff(name: str) -> dict:
        """The diff that will be merged: base...branch over the worktree.

        git runs in a thread: the daemon and the web server share an event
        loop, and a large diff must not stall approvals and terminals.
        """
        c = daemon.containers.get(name)
        if c is None:
            raise HTTPException(404, f"no such container: {name}")
        return await asyncio.to_thread(
            container_files.container_diff, c.config, c.paths
        )

    @app.get("/api/containers/{name}/files")
    async def container_files_listing(name: str) -> dict:
        """What the container produced outside git, grouped by area."""
        c = daemon.containers.get(name)
        if c is None:
            raise HTTPException(404, f"no such container: {name}")
        return await asyncio.to_thread(
            container_files.list_container_files, c.config, c.paths
        )

    @app.get("/api/containers/{name}/files/content")
    async def file_content(name: str, path: str) -> dict:
        """One text file's content, capped, binaries refused."""
        c = daemon.containers.get(name)
        if c is None:
            raise HTTPException(404, f"no such container: {name}")
        return await asyncio.to_thread(
            container_files.read_container_file, c.config, c.paths, path
        )

    @app.get("/api/containers/{name}/files/raw")
    async def file_raw(name: str, path: str) -> FileResponse:
        """One file's bytes, for `capwrap get` and the console's copy-out.

        Same traversal gate as the content endpoint; binaries are fine here,
        which is the point -- the preview refuses them, the copy does not.
        """
        c = daemon.containers.get(name)
        if c is None:
            raise HTTPException(404, f"no such container: {name}")
        _area, host_path = await asyncio.to_thread(
            container_files.resolve_container_path, c.config, c.paths, path
        )
        if host_path.is_dir():
            raise HTTPException(
                400, f"{path!r} is a directory; use the files listing instead"
            )
        return FileResponse(host_path, filename=host_path.name)

    @app.get("/api/screens")
    async def screens(rows: int = 12) -> dict:
        """Every running container's screen tail, in one response.

        The overview polls this. One request rather than one per container: on a
        Pi with several agents, a fan-out of requests every tick costs more in
        connection churn than the payload is worth, and they arrive interleaved.
        """
        limit = max(1, min(rows, 200))
        return {
            "screens": [
                {
                    "container": c.name,
                    "running": True,
                    **c.session.snapshot(tail=limit).to_dict(),
                }
                for c in daemon.containers.values()
                if c.running and c.session is not None
            ],
        }

    @app.get("/api/boards")
    async def boards(limit: int = 100) -> dict:
        """Every board and its recent posts.

        The operator sees all of them regardless of who created one: the root
        capability is the ancestor of every mapping in the system, and a board
        that agents are coordinating on is exactly what a human overseeing them
        needs to be able to read.
        """
        return {
            "boards": [
                {
                    **board.describe(),
                    "holders": daemon.kernel.board_holders(board.oid),
                    "recent": board.posts[-max(1, limit) :],
                    # No per-operator "last seen" state exists, so unread is
                    # pragmatically the whole topic: a dot that says "there is
                    # something here you have not necessarily read".
                    "unread": len(board.posts),
                }
                for board in daemon.kernel.boards()
            ],
        }

    @app.get("/api/caps/{name}")
    async def caps(name: str) -> list[dict]:
        if name not in daemon.kernel.tasks:
            raise HTTPException(404, f"no such task: {name}")
        return [c.to_dict() for c in daemon.kernel.cap_list(name)]

    @app.get("/api/capgraph")
    async def capgraph() -> dict:
        return daemon.kernel.cap_graph()

    @app.get("/api/audit")
    async def audit(
        limit: int = 100, actor: str | None = None, denied: bool = False
    ) -> list[dict]:
        return daemon.audit.tail(limit=limit, actor=actor, denied_only=denied)

    @app.get("/api/inbox")
    async def inbox(limit: int = 100) -> list[dict]:
        return [m.to_dict() for m in daemon.mailboxes.get(OPERATOR).recent(limit)]

    # ------------------------------------------------------------------
    # control
    # ------------------------------------------------------------------

    @app.post("/api/containers")
    async def add(body: AddBody) -> dict:
        """Register a new container, and start it unless told not to.

        The point of a running capwrap is that agents come and go: a review
        needs a reviewer, a build needs a tester, and stopping everything to
        restart with one more config file wastes whatever the others were in the
        middle of.

        It registers under the *operator*, not under any existing container, so
        this is an authority grant from the human rather than a spawn -- an agent
        wanting a child still goes through a factory capability and its quota.
        """
        config = load_config_data(
            dict(body.config), base_dir=Path.cwd(), origin="operator:add"
        )
        if config.name in daemon.containers:
            raise HTTPException(409, f"a container named {config.name} already exists")

        container = daemon.register(config)
        # Both directions: the newcomer's peer references resolve, and any
        # existing container that named it before it existed gets its capability
        # filled in now rather than never.
        daemon.link_all_peers()
        if body.start:
            await daemon.start(config.name)
        return {**container.status(), "started": body.start}

    # ------------------------------------------------------------------
    # compose & spawn -- build a role×persona config and run it
    # ------------------------------------------------------------------

    def _compose_options(module: Any) -> dict:
        """The role/persona/agent choices compose.py knows about."""
        return {
            "roles": sorted(module.ROLES),
            "personas": list(module.PERSONAS),
            "agents": sorted(module.AGENT_SETUP),
        }

    def _validate_compose(module: Any, role: str, persona: str, agent: str) -> None:
        """Check inputs against compose.py's tables before calling into it.

        compose.compose() calls sys.exit() on bad input, which would take the
        whole web process down with it, so the web layer validates first and
        returns a clean 400 instead.
        """
        if role not in module.ROLES:
            raise HTTPException(
                400,
                f"unknown role {role!r}; choose from {', '.join(sorted(module.ROLES))}",
            )
        if persona not in module.PERSONAS:
            raise HTTPException(
                400,
                f"unknown persona {persona!r}; choose from {', '.join(module.PERSONAS)}",
            )
        if agent not in module.AGENT_SETUP:
            raise HTTPException(
                400,
                f"unknown agent {agent!r}; choose from {', '.join(sorted(module.AGENT_SETUP))}",
            )

    @app.get("/api/compose/options")
    async def compose_options() -> dict:
        """What the spawn dialog can offer: every role, persona and agent."""
        return _compose_options(_load_compose())

    def _resolve_project(name: str | None):
        """Look a named project up daemon-side; unknown names are a clean 400."""
        if not name:
            return None
        try:
            return daemon.resolve_project(name)
        except CapwrapError as exc:
            raise HTTPException(400, str(exc)) from None

    def _compose_generated(
        module: Any,
        role: str,
        persona: str,
        agent: str,
        routing: str | None,
        project: str | None,
        extra_prompt: str = "",
        model: str | None = None,
    ) -> tuple[Path, str]:
        """Run compose() for a role×persona×agent; return (config path, routing).

        The one place preview and the form-path spawn build a config, so the
        two cannot drift. An optional project supplies the worktree
        source/base, extra mounts, extra env and the routing default;
        extra_prompt is custom instructions appended to the role+persona prompt.
        """
        resolved = _resolve_project(project)
        chosen = routing or (resolved.routing if resolved else "") or "forward"
        kwargs = resolved.compose_kwargs() if resolved else {}
        path = module.compose(
            role,
            persona,
            agent,
            routing=chosen,
            extra_prompt=extra_prompt,
            model=model,
            **kwargs,
        )
        return path, chosen

    def _config_from_text(module: Any, text: str) -> ContainerConfig:
        """Parse an edited TOML body into a validated config.

        The text is written to a temp file inside compose's BUILT directory
        before `load_config` reads it back, so the config's relative paths --
        [[files]] srcs like pi-mcp.json, the role_prompt markdown -- resolve
        against the same directory compose wrote them to, exactly as they would
        for a config compose() itself had generated. The temp file is always
        removed again; parse errors surface as a clean 400 with the parser's
        message, validation errors as a 400 from the config loader.
        """
        try:
            tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise HTTPException(400, f"invalid TOML: {exc}") from None
        built = Path(module.BUILT)
        built.mkdir(exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=built, prefix=".spawn-", suffix=".toml")
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w") as handle:
                handle.write(text)
            return load_config(tmp_path)
        finally:
            tmp_path.unlink(missing_ok=True)

    async def _spawn_registered(config: ContainerConfig) -> dict:
        """Register and start a config through the operator path.

        The one spawn core, shared by the form shape and the edited-config
        shape of POST /api/spawn: both are the same operator path as
        POST /api/containers -- an authority grant from the human, not a
        kernel factory spawn.
        """
        if config.name in daemon.containers:
            raise HTTPException(
                409,
                f"a container named {config.name} already exists; "
                "dismiss it or pick a different role/persona/agent",
            )
        container = daemon.register(config)
        daemon.link_all_peers()
        await daemon.start(config.name)
        return {**container.status(), "started": True}

    @app.get("/api/compose/preview")
    async def compose_preview(
        role: str,
        persona: str,
        agent: str = "claude",
        routing: str | None = None,
        project: str | None = None,
        extra_prompt: str = "",
        model: str | None = None,
    ) -> dict:
        """The TOML compose.py would generate, without starting anything.

        compose() writes the config into examples/roles-and-personas/built/
        (gitignored) and returns the path to it; we read that back for preview.
        An optional project supplies the worktree source/base, extra mounts,
        extra env and the routing default. extra_prompt is custom instructions
        appended to the role+persona prompt; it changes the generated prompt
        file, which is returned alongside the TOML so the preview shows what
        would actually run.
        """
        module = _load_compose()
        _validate_compose(module, role, persona, agent)
        path, routing = _compose_generated(
            module, role, persona, agent, routing, project, extra_prompt, model
        )
        return {
            "role": role,
            "persona": persona,
            "agent": agent,
            "routing": routing,
            "project": project or "",
            "name": path.stem,
            "toml": path.read_text(),
            "prompt": path.with_suffix(".md").read_text(),
        }

    @app.get("/api/compose/role/{role}")
    async def compose_role(role: str) -> dict:
        """One role's markdown, for the spawn dialog's library reading."""
        module = _load_compose()
        if role not in module.ROLES:
            raise HTTPException(404, f"no such role: {role!r}")
        path = Path(module.HERE) / "roles" / f"{role}.md"
        if not path.is_file():
            raise HTTPException(404, f"no markdown for role {role!r}")
        return {"name": role, "markdown": path.read_text()}

    @app.get("/api/compose/persona/{persona}")
    async def compose_persona(persona: str) -> dict:
        """One persona's markdown, for the spawn dialog's library reading."""
        module = _load_compose()
        if persona not in module.PERSONAS:
            raise HTTPException(404, f"no such persona: {persona!r}")
        path = Path(module.HERE) / "personas" / f"{persona}.md"
        if not path.is_file():
            raise HTTPException(404, f"no markdown for persona {persona!r}")
        return {"name": persona, "markdown": path.read_text()}

    @app.post("/api/spawn")
    async def spawn(body: SpawnBody) -> dict:
        """Build a role×persona config and run it, as the operator.

        Two shapes: the form shape composes role×persona×agent daemon-side
        exactly as before; an edited config (`config` holding TOML text, from
        the spawn dialog's editable preview) is validated and spawned as-is.
        Both register and start through the same operator path as
        POST /api/containers -- an authority grant from the human, not a
        kernel factory spawn.
        """
        module = _load_compose()
        if body.config is not None:
            if not body.config.strip():
                raise HTTPException(
                    400, "config is empty; paste TOML or use the form fields"
                )
            return await _spawn_registered(_config_from_text(module, body.config))

        if not body.role or not body.persona:
            raise HTTPException(
                400, "spawn needs either a config (TOML text) or a role and persona"
            )
        _validate_compose(module, body.role, body.persona, body.agent)
        path, _ = _compose_generated(
            module,
            body.role,
            body.persona,
            body.agent,
            body.routing,
            body.project,
            body.extra_prompt,
            body.model,
        )
        return await _spawn_registered(load_config(path))

    # ------------------------------------------------------------------
    # projects
    # ------------------------------------------------------------------

    @app.get("/api/projects")
    async def list_projects() -> list[dict]:
        """Every project: named, predefined spawn configurations."""
        return daemon.projects_view()

    @app.post("/api/projects")
    async def save_project(body: ProjectBody) -> dict:
        """Create or update a project: validate, write its TOML file, keep it.

        The file lands in the daemon's projects directory, named after the
        project, so it stays reviewable like every other capwrap config.
        """
        project = daemon.save_project(body.project)
        return {"name": project.name, "saved": True}

    @app.delete("/api/projects/{name}")
    async def delete_project(name: str) -> dict:
        daemon.delete_project(name)
        return {"name": name, "deleted": True}

    # ------------------------------------------------------------------
    # teams
    # ------------------------------------------------------------------

    @app.get("/api/teams")
    async def teams() -> list[dict]:
        """Every team, with each member's running state."""
        return daemon.teams_view()

    @app.post("/api/teams/spawn")
    async def spawn_team(body: TeamBody) -> dict:
        """Spawn a whole team: validate, generate each member, register and
        start them, create the shared board and record the team.

        A name collision on any member refuses the whole team with no partial
        spawns. An optional project (top-level in the body, or inside the team
        mapping) applies to every member.
        """
        team = parse_team_data(body.team, load_team_compose())
        project_name = body.project or str(body.team.get("project", "") or "")
        return await daemon.spawn_team(team, project_name=project_name)

    @app.post("/api/teams/{name}/edit")
    async def edit_team(name: str, body: TeamBody) -> dict:
        """Edit a team: replace, add or remove members, restate goal/criteria.

        Same body shape as spawn. Validation runs first, so a refused edit
        comes back as `ok: false` with a per-member error list and nothing
        applied; a successful edit returns a per-member result summary
        (replaced/added/removed/kept). An optional project applies to every
        member this edit spawns.
        """
        project_name = body.project or str(body.team.get("project", "") or "")
        return await daemon.edit_team(name, body.team, project_name=project_name)

    @app.post("/api/teams/{name}/stop")
    async def stop_team(name: str) -> dict:
        """Stop every member of a team."""
        return await daemon.stop_team(name)

    @app.post("/api/containers/{name}/start")
    async def start(name: str) -> dict:
        container = await daemon.start(name)
        return container.status()

    @app.post("/api/containers/{name}/stop")
    async def stop(name: str) -> dict:
        code = await daemon.stop(name)
        return {"container": name, "exit_code": code}

    @app.post("/api/down")
    async def down() -> dict:
        """Stop every running container, then exit the server.

        `capwrap down` calls this. The shutdown hook is the uvicorn server's
        should_exit flag, passed in by `up`; without it (tests, embedded
        uses) the containers still stop and the response reports the count.
        """
        stopped = await daemon.down()
        if shutdown is not None:
            shutdown()
        return {"stopped": stopped, "shutdown": shutdown is not None}

    @app.post("/api/containers/{name}/signal")
    async def signal(name: str, sig: int = 2) -> dict:
        daemon.signal_container(name, sig)
        return {"container": name, "signal": sig}

    @app.post("/api/containers/{name}/routing")
    async def set_routing(name: str, body: RoutingBody) -> dict:
        """Override where this container's plain questions surface, live.

        In-memory only: the override lives on the container object, so a
        respawn reverts to the TOML value.
        """
        return daemon.set_question_routing(name, body.routing)

    @app.delete("/api/containers/{name}")
    async def destroy(
        name: str, remove_state: bool = False, force: bool = False
    ) -> dict:
        """Dismiss a container: forget it entirely, tree entry included.

        `remove_state=true` also deletes its host-side directory -- overlay
        writes, private copies, and the git worktree with whatever the agent
        committed. Off by default, because that work usually outlives the
        container that produced it.
        """
        return await daemon.destroy(name, remove_state=remove_state, force=force)

    @app.post("/api/containers/dismiss-finished")
    async def dismiss_finished(remove_state: bool = False) -> dict:
        """Clear away every container that has already exited."""
        dismissed = []
        for name in daemon.dismissable():
            with contextlib.suppress(CapwrapError):
                await daemon.destroy(name, remove_state=remove_state)
                dismissed.append(name)
        return {"dismissed": dismissed}

    @app.post("/api/containers/{name}/input")
    async def write_input(name: str, body: InputBody) -> dict:
        daemon.write_input(name, body.data)
        return {"wrote": len(body.data)}

    @app.post("/api/containers/{name}/mailbox/{message_id}/discard")
    async def discard_mail(name: str, message_id: int) -> dict:
        """Drop one queued message without delivering it.

        The operator's console offers this next to "nudge": a message posted by
        mistake, or already handled out of band, should not keep sitting in the
        agent's queue.
        """
        if not daemon.discard_mail(name, message_id):
            raise HTTPException(404, f"no queued message {message_id} for {name}")
        return {"container": name, "discarded": message_id}

    @app.post("/api/send")
    async def send(body: SendBody) -> dict:
        """Send a message as the operator, to one container or to several.

        The operator holds root capabilities on every container, so this is a
        normal `msg.send`/`msg.broadcast` through the kernel rather than a back
        door -- it is audited exactly like an agent's message would be.
        """
        names = body.recipients()
        if not names:
            raise HTTPException(400, "name at least one container to send to")

        slots: list[int] = []
        for name in names:
            target = daemon.kernel.find_container(name)
            if target is None:
                raise HTTPException(404, f"no such container: {name}")
            slot = daemon.kernel.root.find(target.oid)
            if slot is None:
                raise HTTPException(500, f"the operator holds no capability on {name}")
            slots.append(slot)

        if len(slots) == 1:
            return daemon.kernel.msg_send(ROOT, slots[0], body.message)
        return daemon.kernel.msg_broadcast(ROOT, slots, body.message)

    # ------------------------------------------------------------------
    # message tracing
    # ------------------------------------------------------------------

    @app.get("/api/trace")
    async def trace_state() -> dict:
        return daemon.trace_state()

    @app.post("/api/trace")
    async def set_trace(body: TraceBody) -> dict:
        """Turn the inter-container message trace on or off.

        Off by default and cleared when turned off: a trace keeps whole message
        payloads, which is the agents' working content rather than metadata.
        """
        return daemon.set_message_trace(body.enabled)

    @app.get("/api/messages")
    async def traced_messages(limit: int = 200) -> dict:
        return {
            **daemon.trace_state(),
            "messages": daemon.traced_messages(limit=limit),
        }

    @app.delete("/api/messages")
    async def clear_trace() -> dict:
        """Drop what has been recorded so far, leaving tracing on."""
        daemon.message_trace.clear()
        return daemon.trace_state()

    # ------------------------------------------------------------------
    # approvals
    # ------------------------------------------------------------------

    @app.get("/api/approvals")
    async def approvals() -> list[dict]:
        return daemon.pending_approvals()

    @app.post("/api/approvals/{approval_id}")
    async def resolve(approval_id: int, body: ApprovalBody) -> dict:
        if body.decision not in ("allow", "reject", "deny", "grant", "explain"):
            raise HTTPException(
                400,
                "decision must be 'allow', 'reject', 'grant' or 'explain' "
                "('deny' is accepted as an alias for 'reject')",
            )
        if not daemon.resolve_approval(
            approval_id, body.decision, body.reason, body.rights
        ):
            raise HTTPException(404, "no such pending approval")
        return {"id": approval_id, "decision": body.decision}

    @app.post("/api/approvals/{approval_id}/explain")
    async def explain(approval_id: int) -> dict:
        """What would this request actually do?

        Advisory, and labelled as such wherever it is shown: it is one model's
        reading of another model's request. It decides nothing -- the operator
        still clicks the button.
        """
        pending = daemon.approvals.get(approval_id)
        if pending is None or pending.future.done():
            raise HTTPException(404, "no such pending approval")

        container = daemon.containers.get(pending.container)
        detail = None
        if container is not None:
            detail = {
                "config": {
                    "agent": container.config.runtime.agent,
                    "model": container.config.runtime.model,
                    "command": container.config.runtime.command,
                    "cwd": container.config.runtime.cwd,
                    "network": container.config.sandbox.network,
                    "mounts": [
                        {"dest": m.dest, "mode": m.mode}
                        for m in container.config.mounts
                    ],
                }
            }

        try:
            result = await daemon.explainer.explain(pending.to_dict(), detail)
        except ExplainError as exc:
            raise HTTPException(503, str(exc)) from None
        daemon.audit.record(
            OPERATOR,
            "approval.explain",
            allowed=True,
            target=pending.container,
            detail={"model": result["model"]},
        )
        return result

    # ------------------------------------------------------------------
    # capability administration
    # ------------------------------------------------------------------

    @app.post("/api/caps/revoke")
    async def revoke(body: RevokeBody) -> dict:
        """Revoke a capability, and everything derived from it.

        Available for any container's slot because the operator's root
        capability is the ancestor of every mapping in the system.
        """
        return daemon.kernel.cap_revoke(
            body.container, body.slot, include_self=body.include_self
        )

    @app.post("/api/caps/grant")
    async def grant(body: GrantBody) -> dict:
        """Hand a container a new capability on another container, at runtime."""
        return daemon.kernel.operator_grant(
            body.holder,
            "container",
            body.target_container,
            parse_rights(body.rights),
            label=body.label,
        )

    # ------------------------------------------------------------------
    # live streams
    # ------------------------------------------------------------------

    @app.websocket("/ws/events")
    async def ws_events(socket: WebSocket) -> None:
        """Everything happening across the whole system, as one stream."""
        await socket.accept()
        queue = daemon.subscribe_events()
        try:
            await socket.send_json({"event": "overview", **daemon.overview()})
            while True:
                event = await queue.get()
                await socket.send_json(event)
        except (WebSocketDisconnect, RuntimeError, asyncio.CancelledError):
            pass
        finally:
            daemon.unsubscribe_events(queue)

    @app.websocket("/ws/terminal/{name}")
    async def ws_terminal(socket: WebSocket, name: str) -> None:
        """A container's terminal, both directions.

        Raw PTY bytes out, keystrokes in.  The scrollback is replayed first so a
        browser that connects late still sees the session rather than a blank
        screen waiting for the next redraw.
        """
        await socket.accept()
        container = daemon.containers.get(name)
        if container is None or container.session is None:
            await socket.send_json(
                {"type": "error", "message": f"{name} is not running"}
            )
            await socket.close()
            return

        session = container.session
        loop = asyncio.get_running_loop()
        outbound: asyncio.Queue[bytes] = asyncio.Queue(maxsize=512)

        def on_output(data: bytes) -> None:
            # Called from the reader callback; hop back onto the loop safely.
            with contextlib.suppress(asyncio.QueueFull):
                outbound.put_nowait(data)

        unsubscribe = session.subscribe(on_output)

        async def pump() -> None:
            while True:
                data = await outbound.get()
                await socket.send_bytes(data)

        pump_task = loop.create_task(pump())
        try:
            # Replay everything retained, in the order the terminal originally
            # received it. That is what fills the browser's scrollback: sending
            # only the current frame -- which is all a full-screen program's
            # repaint can give you -- left the operator able to see what an agent
            # is doing now and nothing of what it did to get there, and threw the
            # session away again every time they clicked another container.
            if history := session.scrollback():
                if session.truncated:
                    await socket.send_bytes(
                        b"\x1b[90m[capwrap: earlier output has aged out of the "
                        b"buffer; raise CAPWRAP_SCROLLBACK_BYTES to keep more]"
                        b"\x1b[0m\r\n"
                    )
                await socket.send_bytes(history)

            # Then re-assert the program's modes and, for a full-screen one, its
            # current frame. The replay above may have been trimmed part-way
            # through a redraw, so this is what guarantees the live screen is
            # right regardless of where the ring happened to start.
            if preamble := session.mode_preamble():
                await socket.send_bytes(preamble)
            if session.alternate_screen:
                await socket.send_bytes(session.repaint())

            while True:
                message = await socket.receive()
                if message["type"] == "websocket.disconnect":
                    break
                if (text := message.get("text")) is not None:
                    await _handle_terminal_message(session, text)
                elif (data := message.get("bytes")) is not None:
                    session.write(data)
        except (WebSocketDisconnect, RuntimeError, asyncio.CancelledError):
            pass
        finally:
            unsubscribe()
            pump_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pump_task

    async def _handle_terminal_message(session: Any, text: str) -> None:
        """Text frames are control JSON; keystrokes arrive as binary."""
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            session.write(text)
            return
        kind = payload.get("type")
        if kind == "input":
            session.write(payload.get("data", ""))
        elif kind == "resize":
            session.resize(int(payload.get("cols", 80)), int(payload.get("rows", 24)))

    # ------------------------------------------------------------------
    # static assets
    # ------------------------------------------------------------------

    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    return app
