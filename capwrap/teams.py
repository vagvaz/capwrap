"""Team definitions: parse and validate a team TOML, and know how to build it.

A Team is a named set of containers with complementary roles, a shared goal,
peer messaging granted among members, and one shared board. Members are
selected by role (plus persona/agent); the goal and optional success criteria
are recorded at creation and visible to every member.

The team TOML shape::

    name = "feature-x"
    goal = "ship the parser rewrite"
    success_criteria = "all tests green, ADR merged"   # optional

    [[members]]
    role = "implementer"
    persona = "pragmatist"    # required; any persona in compose.PERSONAS
    agent = "pi"              # optional, default "claude"

Validation happens up front against compose.py's tables (roles/personas/agents),
so a bad team is refused before anything is spawned.

A team definition can also live as a file the operator edits: the state
root's ``team-files/<name>.toml`` (see `team_files_dir`, `save_team_file`,
`load_team_file`). The file is the editable source the console loads and
saves -- spawning itself still goes through `parse_team_data` on a mapping,
never through this store; the daemon's persisted teams are a different file.
"""

from __future__ import annotations

import importlib.util
import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import paths
from .errors import ConfigError
from .roles import install_custom_roles

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_PATH = REPO_ROOT / "examples" / "roles-and-personas" / "compose.py"


def load_compose():
    """Import examples/roles-and-personas/compose.py without running its CLI.

    The module is a script with a `main()` that calls `sys.exit()` on bad input,
    so callers validate against its tables before calling `compose()`, which is
    the only function that writes files.

    The import also merges the state dir's custom roles
    (``$CAPWRAP_STATE/roles/*.toml``, see capwrap/roles.py) into this fresh
    instance's ROLES, so team validation and member generation accept them on
    the same files spawn and preview read. Built-ins cannot be shadowed;
    broken or shadowing files are skipped, never fatal.
    """
    spec = importlib.util.spec_from_file_location("capwrap_compose", COMPOSE_PATH)
    if spec is None or spec.loader is None:
        raise ConfigError("compose.py could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    install_custom_roles(module)
    return module


@dataclass
class TeamMember:
    """One container in a team, selected by role (plus persona/agent)."""

    role: str
    persona: str
    agent: str = "claude"

    @property
    def name(self) -> str:
        """The container name compose() will generate for this member.

        Mirrors compose.compose(): claude agents are named ``role-persona``,
        everyone else ``agent-role-persona`` (the agent is part of the name so
        the same role-persona pair can run for several agents without the
        worktree branches colliding).
        """
        if self.agent == "claude":
            return f"{self.role}-{self.persona}"
        return f"{self.agent}-{self.role}-{self.persona}"

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "role": self.role,
            "persona": self.persona,
            "agent": self.agent,
        }


@dataclass
class Team:
    """A named set of containers with a shared goal and one shared board."""

    name: str
    goal: str
    success_criteria: str = ""
    members: list[TeamMember] = field(default_factory=list)

    @property
    def board_topic(self) -> str:
        return f"team/{self.name}"

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "goal": self.goal,
            "success_criteria": self.success_criteria,
            "members": [m.to_dict() for m in self.members],
        }


def validate_name(name: str) -> str:
    """A team name obeys the container name rules (see config._name_safe).

    Same alphabet as roles.py's ``validate_name``: non-empty, alphanumerics,
    ``-``, ``_`` or ``.`` -- the name becomes half of a generated container
    name at spawn and the file name of a saved definition here.
    """
    if not name or not all(c.isalnum() or c in "-_." for c in name):
        raise ConfigError(
            f"team name {name!r} must be non-empty and use only "
            "alphanumerics, '-', '_' or '.'"
        )
    return name


def parse_team_data(raw: dict[str, Any], compose: Any) -> Team:
    """Validate an already-parsed team mapping against compose's tables.

    Split out from `parse_team` so the daemon can validate a team that arrived
    over the wire (from the web UI or the CLI) without it touching the
    filesystem.
    """
    if not isinstance(raw, dict):
        raise ConfigError("a team must be a TOML table")
    name = validate_name(str(raw.get("name", "")))
    goal = str(raw.get("goal", "")).strip()
    if not goal:
        raise ConfigError("a team needs a goal")
    success_criteria = str(raw.get("success_criteria", "")).strip()

    members_raw = raw.get("members")
    if not isinstance(members_raw, list) or not members_raw:
        raise ConfigError("a team needs at least one [[members]] entry")

    members: list[TeamMember] = []
    seen_names: set[str] = set()
    for entry in members_raw:
        if not isinstance(entry, dict):
            raise ConfigError("each [[members]] entry must be a table")
        role = str(entry.get("role", ""))
        persona = str(entry.get("persona", ""))
        agent = str(entry.get("agent", "claude"))
        if role not in compose.ROLES:
            raise ConfigError(
                f"unknown role {role!r}; choose from {', '.join(sorted(compose.ROLES))}"
            )
        if persona not in compose.PERSONAS:
            raise ConfigError(
                f"unknown persona {persona!r}; choose from {', '.join(compose.PERSONAS)}"
            )
        if agent not in compose.AGENT_SETUP:
            raise ConfigError(
                f"unknown agent {agent!r}; choose from {', '.join(sorted(compose.AGENT_SETUP))}"
            )
        member = TeamMember(role=role, persona=persona, agent=agent)
        if member.name in seen_names:
            raise ConfigError(
                f"two members both generate the container name {member.name!r}; "
                "pick distinct role/persona/agent combinations"
            )
        seen_names.add(member.name)
        members.append(member)

    return Team(
        name=name, goal=goal, success_criteria=success_criteria, members=members
    )


def parse_team_toml(text: str) -> Team:
    """Parse and validate a team's TOML *text* into a Team."""
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid team TOML: {exc}") from None
    return parse_team_data(raw, load_compose())


def parse_team(path: str | Path) -> Team:
    """Parse and validate a team TOML file."""
    path = Path(path).expanduser().resolve()
    try:
        text = path.read_text()
    except FileNotFoundError:
        raise ConfigError(f"no such team file: {path}") from None
    return parse_team_toml(text)


def team_preamble(team: Team) -> str:
    """The team context appended to every member's role prompt.

    The goal and success criteria are recorded at creation and must be visible
    to every member, so they are folded into the generated prompt file rather
    than left to the agent to discover.
    """
    lines = [
        f"# Team: {team.name}",
        f"Goal: {team.goal}",
    ]
    if team.success_criteria:
        lines.append(f"Success criteria: {team.success_criteria}")
    lines.append(
        f"Shared board: {team.board_topic} -- post progress with "
        f"`capctl board post {team.board_topic} <message>` and read it with "
        f"`capctl board read {team.board_topic}`."
    )
    lines.append(
        "You are a member of this team. Coordinate with your peers with "
        "`capctl send <peer> <message>`; every member can message every other."
    )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# file-backed definitions: team-files/<name>.toml
# --------------------------------------------------------------------------


def team_files_dir() -> Path:
    """Where saved team definition files live: the state root's ``team-files/``.

    Nothing else in the state dir is written from here -- in particular not
    the daemon's persisted ``teams.json``: a definition file is the editable
    source an operator keeps, and spawning remains a separate, explicit act.
    """
    return paths.state_root() / "team-files"


def _toml_str(value: str) -> str:
    """A TOML basic string; json.dumps output is valid TOML for the short
    strings that end up here, the way roles.py and projects.py do it."""
    return json.dumps(value)


def team_toml(team: Team) -> str:
    """Serialize a team back into the TOML the operator edits and saves.

    The shape of the module docstring: scalar fields first, then one
    ``[[members]]`` table per member. The agent line is omitted when it is
    the default, so a hand-written file and a saved one stay comparable.
    """
    lines = [
        f"name = {_toml_str(team.name)}",
        f"goal = {_toml_str(team.goal)}",
    ]
    if team.success_criteria:
        lines.append(f"success_criteria = {_toml_str(team.success_criteria)}")
    for member in team.members:
        lines.append("")
        lines.append("[[members]]")
        lines.append(f"role = {_toml_str(member.role)}")
        lines.append(f"persona = {_toml_str(member.persona)}")
        if member.agent != "claude":
            lines.append(f"agent = {_toml_str(member.agent)}")
    return "\n".join(lines) + "\n"


def _parse_team_file(path: Path) -> Team:
    """Parse one definition file, insisting the file's name matches the team's.

    The file stem is the identity the console addresses
    (``team-files/<name>.toml`` for ``<name>``); a file that defines a
    differently-named team would otherwise be listable under one name and
    unreadable under it.
    """
    team = parse_team_toml(path.read_text())
    if team.name != path.stem:
        raise ConfigError(
            f"{path.name}: it defines team {team.name!r}; rename the file "
            f"to {team.name}.toml or the team to {path.stem!r}"
        )
    return team


def list_team_files() -> list[dict]:
    """Every saved team definition: name, goal, and member count.

    Lenient like roles.load_roles: a file that does not parse is skipped
    rather than fatal, so one typoed file cannot take the dialog's list down.
    """
    out: list[dict] = []
    directory = team_files_dir()
    if not directory.is_dir():
        return out
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.suffix != ".toml":
            continue
        try:
            team = _parse_team_file(path)
        except (ConfigError, FileNotFoundError):
            continue
        out.append({"name": team.name, "goal": team.goal, "members": len(team.members)})
    return out


def load_team_file(name: str) -> Team:
    """One saved team definition, validated against the compose tables
    (custom roles included, via `load_compose`).

    ConfigError when the name is unusable, the file is missing, misnamed, or
    a member names an unknown role/persona/agent -- the caller decides
    between 400 and 404.
    """
    validate_name(name)
    path = team_files_dir() / f"{name}.toml"
    try:
        return _parse_team_file(path)
    except FileNotFoundError:
        raise ConfigError(f"no such team file: {name!r}") from None


def save_team_file(team: Team) -> Path:
    """Write one definition file, named after the team. Returns the path.

    The caller has already validated the team (`parse_team_data`) and checked
    the name against the address the save arrived on; the file written is the
    canonical serialization, so a saved file reads back as what was meant.
    """
    directory = team_files_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{team.name}.toml"
    path.write_text(team_toml(team))
    return path


def delete_team_file(name: str) -> bool:
    """Remove one definition file. False when there was nothing to remove.

    Only the file goes: a team already spawned from it keeps running, and the
    daemon's record of it is untouched.
    """
    validate_name(name)
    path = team_files_dir() / f"{name}.toml"
    if not path.exists():
        return False
    path.unlink()
    return True
