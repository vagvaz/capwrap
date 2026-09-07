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
"""

from __future__ import annotations

import importlib.util
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ConfigError

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_PATH = REPO_ROOT / "examples" / "roles-and-personas" / "compose.py"


def load_compose():
    """Import examples/roles-and-personas/compose.py without running its CLI.

    The module is a script with a `main()` that calls `sys.exit()` on bad input,
    so callers validate against its tables before calling `compose()`, which is
    the only function that writes files.
    """
    spec = importlib.util.spec_from_file_location("capwrap_compose", COMPOSE_PATH)
    if spec is None or spec.loader is None:
        raise ConfigError("compose.py could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
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


def _validate_name(name: str) -> str:
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
    name = _validate_name(str(raw.get("name", "")))
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


def parse_team(path: str | Path) -> Team:
    """Parse and validate a team TOML file."""
    path = Path(path).expanduser().resolve()
    try:
        raw = tomllib.loads(path.read_text())
    except FileNotFoundError:
        raise ConfigError(f"no such team file: {path}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from None
    return parse_team_data(raw, load_compose())


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
