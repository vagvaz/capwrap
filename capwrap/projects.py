"""Projects: named, predefined configurations for where agents work.

A Project records the decisions every real spawn needed to hand-patch before
this existed: which repository the worktree forks from, which branch it forks
from, any extra mounts the agent needs, extra host env vars to pass through,
and a default question-routing position. Spawning "the capwrap project" then
means picking it in a dropdown instead of hand-editing a generated config.

Projects live as one TOML file each in a projects directory (default
``~/.local/state/capwrap/projects/``, overridable with ``--projects-dir`` on
``up`` or the ``CAPWRAP_PROJECTS`` env var) -- file-based and reviewable, like
every other capwrap config. The shape::

    name = "capwrap"
    source = "/home/vagvaz/Projects/ai/capwrap"   # repo the worktree forks from
    base = "generalize_agents"                    # branch it forks from

    [[extra_mounts]]
    src  = "~/.foo"
    dest = "/foo"
    mode = "ro"

    env = ["MY_TOKEN"]        # extra env_from_host vars
    routing = "forward"       # optional question-routing default

Validation is eager for what is cheap (name, modes, routing, env names, that
``source`` exists and is a git repo) and lazy for what is slow: ``base`` is
only resolved with git at spawn time, not at save.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ConfigError
from .paths import state_root

#: Where projects live when nothing overrides it. Under the state root, beside
#: teams.json -- runtime state the operator curates, not a cache.
DEFAULT_DIRNAME = "projects"

#: A project name is also its filename and the key everything addresses it by,
#: so it stays slug-safe.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")

#: An env_from_host entry is a variable name, not an assignment.
_VAR_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: Question-routing positions, same vocabulary as [runtime] question_routing.
ROUTINGS = ("forward", "block", "auto")

#: Extra mounts are plain binds; the worktree/overlay machinery is compose's
#: own business and a project only adds ordinary binds.
MOUNT_MODES = ("ro", "rw")


@dataclass
class ProjectMount:
    """One extra bind mount a project adds to every spawn that uses it."""

    src: str
    dest: str
    mode: str = "ro"

    def to_dict(self) -> dict:
        return {"src": self.src, "dest": self.dest, "mode": self.mode}


@dataclass
class Project:
    """A named, predefined spawn configuration."""

    name: str
    source: str
    base: str = "main"
    extra_mounts: list[ProjectMount] = field(default_factory=list)
    env: list[str] = field(default_factory=list)
    #: "" means "no opinion" -- compose's own default applies. One of ROUTINGS
    #: otherwise, and it is only a default: an explicit routing at spawn wins.
    routing: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "source": self.source,
            "base": self.base,
            "extra_mounts": [m.to_dict() for m in self.extra_mounts],
            "env": list(self.env),
            "routing": self.routing,
        }

    def compose_kwargs(self) -> dict:
        """The keyword arguments compose() takes for this project."""
        return {
            "source": self.source,
            "base": self.base,
            "extra_mounts": [m.to_dict() for m in self.extra_mounts],
            "extra_env": list(self.env),
        }

    def validate_base(self) -> None:
        """Check that ``base`` resolves in ``source`` with git.

        Deliberately not part of `parse_project_data`: a git rev-parse is slow
        enough that saving a project should not block on it, but a spawn that
        forks from a branch that does not exist should fail with a clear
        message rather than deep inside worktree creation.
        """
        result = subprocess.run(
            ["git", "rev-parse", "--verify", f"{self.base}^{{commit}}"],
            cwd=self.source,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            detail = (result.stderr or "").strip().splitlines()
            hint = detail[-1] if detail else "git rev-parse failed"
            raise ConfigError(
                f"project {self.name!r}: base {self.base!r} does not resolve "
                f"in {self.source}: {hint}"
            )


def _validate_name(name: str) -> str:
    if not name or not _NAME_RE.match(name):
        raise ConfigError(
            f"project name {name!r} must start with an alphanumeric and use "
            "only alphanumerics, '-', '_' or '.'"
        )
    return name


def _validate_source(source: str) -> str:
    path = Path(source).expanduser()
    if not source:
        raise ConfigError("a project needs a source: the repo the worktree forks from")
    if not path.is_dir():
        raise ConfigError(f"project source {source!r} does not exist")
    if not (path / ".git").exists():
        raise ConfigError(f"project source {source!r} is not a git repository")
    return str(path)


def _validate_mounts(raw: Any) -> list[ProjectMount]:
    if not isinstance(raw, list):
        raise ConfigError("extra_mounts must be a list of tables")
    mounts: list[ProjectMount] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ConfigError(f"extra_mounts[{index}] must be a table")
        src = str(entry.get("src", "")).strip()
        dest = str(entry.get("dest", "")).strip()
        mode = str(entry.get("mode", "ro")).strip()
        if not src or not dest:
            raise ConfigError(f"extra_mounts[{index}] needs both src and dest")
        if mode not in MOUNT_MODES:
            raise ConfigError(
                f"extra_mounts[{index}]: mode {mode!r} must be one of "
                f"{', '.join(MOUNT_MODES)}"
            )
        mounts.append(ProjectMount(src=src, dest=dest, mode=mode))
    return mounts


def _validate_env(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        raise ConfigError("env must be a list of variable names")
    names: list[str] = []
    for entry in raw:
        name = str(entry).strip()
        if not _VAR_RE.match(name):
            raise ConfigError(
                f"env entry {name!r} is not a variable name "
                "(alphanumerics and '_', starting with a letter or '_')"
            )
        names.append(name)
    return names


def parse_project_data(raw: dict[str, Any]) -> Project:
    """Validate an already-parsed project mapping.

    Split out from `parse_project_file` so the daemon can validate a project
    that arrived over the wire (from the web console or the CLI) without it
    touching the filesystem.
    """
    if not isinstance(raw, dict):
        raise ConfigError("a project must be a TOML table")
    name = _validate_name(str(raw.get("name", "")))
    source = _validate_source(str(raw.get("source", "")))
    base = str(raw.get("base", "main")).strip()
    if not base:
        raise ConfigError(f"project {name!r}: base cannot be empty")
    routing = str(raw.get("routing", "")).strip()
    if routing and routing not in ROUTINGS:
        raise ConfigError(
            f"project {name!r}: routing {routing!r} must be one of "
            f"{', '.join(ROUTINGS)}"
        )
    return Project(
        name=name,
        source=source,
        base=base,
        extra_mounts=_validate_mounts(raw.get("extra_mounts", [])),
        env=_validate_env(raw.get("env", [])),
        routing=routing,
    )


def parse_project(path: str | Path) -> Project:
    """Parse and validate a project TOML file."""
    path = Path(path).expanduser()
    try:
        raw = tomllib.loads(path.read_text())
    except FileNotFoundError:
        raise ConfigError(f"no such project file: {path}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from None
    return parse_project_data(raw)


def default_projects_dir() -> Path:
    """The projects directory: ``$CAPWRAP_PROJECTS`` or the state root's
    ``projects/``."""
    env = os.environ.get("CAPWRAP_PROJECTS")
    if env:
        return Path(env).expanduser().resolve()
    return state_root() / DEFAULT_DIRNAME


def load_projects(directory: str | Path) -> dict[str, Project]:
    """Every valid project in a directory, keyed by name.

    A missing directory is simply no projects -- the daemon boots empty on a
    fresh install, not with an error. A file that does not parse is skipped
    rather than fatal, the same leniency `_load_teams` shows a bad team: one
    typoed file must not take the whole console's project list down.
    """
    directory = Path(directory).expanduser()
    projects: dict[str, Project] = {}
    if not directory.is_dir():
        return projects
    for path in sorted(directory.glob("*.toml")):
        try:
            project = parse_project(path)
        except ConfigError:
            continue
        projects[project.name] = project
    return projects


def _toml_str(value: str) -> str:
    """A TOML basic string. json.dumps produces valid TOML for the strings
    that end up here (paths, branch names, var names)."""
    return json.dumps(value)


def project_toml(project: Project) -> str:
    """Serialize a project back to the TOML form it is reviewed in."""
    lines = [
        f"name = {_toml_str(project.name)}",
        f"source = {_toml_str(project.source)}",
        f"base = {_toml_str(project.base)}",
    ]
    if project.routing:
        lines.append(f"routing = {_toml_str(project.routing)}")
    if project.env:
        items = ", ".join(_toml_str(name) for name in project.env)
        lines.append(f"env = [{items}]")
    for mount in project.extra_mounts:
        lines.append("")
        lines.append("[[extra_mounts]]")
        lines.append(f"src  = {_toml_str(mount.src)}")
        lines.append(f"dest = {_toml_str(mount.dest)}")
        lines.append(f"mode = {_toml_str(mount.mode)}")
    return "\n".join(lines) + "\n"


def save_project(project: Project, directory: str | Path) -> Path:
    """Write one project file (named after the project) and return its path."""
    directory = Path(directory).expanduser()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{project.name}.toml"
    path.write_text(project_toml(project))
    return path


def delete_project(name: str, directory: str | Path) -> bool:
    """Remove a project's file. False when there was nothing to remove."""
    path = Path(directory).expanduser() / f"{name}.toml"
    if not path.exists():
        return False
    path.unlink()
    return True
