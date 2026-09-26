"""File-backed custom roles.

A custom role is one TOML file in the state root's ``roles/`` directory --
``$CAPWRAP_STATE/roles/<name>.toml`` -- written by the operator through the
console and read back the same way by spawn, preview and team validation.
A role file is the capability half of a role (``summary``, ``allow`` /
``deny``, ``work``, ``network``, ``shell``) plus the ``prompt`` that stands
in for the built-in role markdown when a config is composed:

    name = "parser-writer"
    summary = "writes parsers and their tests"
    work = "worktree"          # "worktree" or "none"
    network = false            # false, true, or "auto"
    shell = "work"             # "ambient", "work", or "readonly"
    allow = ["Read", "Write", "Edit"]
    deny = ["Bash(sudo *)"]

    prompt = \"\"\"
    # You are the parser writer
    ...
    \"\"\"

Built-ins live in compose.py's ``ROLES`` table and its ``roles/*.md``
markdown; they are read-only everywhere, and a save or delete naming one is
refused before any file changes hands.

Validation is eager: a role is checked against the same name rules a
container name obeys (``config.py``'s ``_name_safe`` and ``teams.py``'s
``validate_name`` share the alphabet -- non-empty, alphanumerics, ``-``,
``_`` or ``.``) and every enum field is checked against its values here,
never mid-compose. The *load* path stays lenient -- a broken file is
skipped, the way projects and persisted teams are -- so one typoed file
cannot take the role list down; the *save* path is strict.
"""

from __future__ import annotations

import json
import tomllib
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from . import paths
from .errors import ConfigError

#: Where a role may do its work: inside the repo it is working on, or nowhere.
WORK_MODES = ("worktree", "none")
#: Sandbox network posture, mirroring compose.py's ``network`` field:
#: open, closed, or derived from the opencode config through the proxy.
NETWORK_MODES = ("auto",)
#: Shell posture. ``ambient`` is compose's baseline (read-only shell, git
#: reads, capctl comms); ``work`` adds WORK_SHELL and the git work verbs;
#: ``readonly`` keeps the ambient baseline and *denies* the mutating git
#: verbs outright, like the read-only roles do.
SHELL_MODES = ("ambient", "work", "readonly")


def roles_dir() -> Path:
    """Where custom role files live: the state root's ``roles/``."""
    return paths.state_root() / "roles"


def validate_name(name: str) -> str:
    """A role name obeys the container name rules (see config._name_safe).

    Same alphabet as teams.py's ``validate_name``: non-empty, alphanumerics,
    ``-``, ``_`` or ``.`` -- the name becomes a file name here and half of a
    generated container name at spawn, so it must satisfy the strictest of
    the three.
    """
    if not name or not all(c.isalnum() or c in "-_." for c in name):
        raise ConfigError(
            f"role name {name!r} must be non-empty and use only "
            "alphanumerics, '-', '_' or '.'"
        )
    return name


def parse_role_data(raw: Any, fallback_name: str | None = None) -> dict:
    """Validate a parsed role mapping into the shape compose.ROLES expects.

    The returned spec carries every key compose() reads -- ``summary``,
    ``allow``, ``deny``, ``work``, ``network`` -- plus the custom additions
    ``shell`` and ``prompt``. ``fallback_name`` names the role when the
    mapping itself does not (the URL a PUT arrived on). Guarded isinstance
    checks throughout: TOML happily produces lists and tables where strings
    were expected, and membership tests on those raise TypeError.
    """
    if not isinstance(raw, dict):
        raise ConfigError("a role must be a TOML table")
    name_value = raw.get("name")
    if isinstance(name_value, str) and name_value.strip():
        name = validate_name(name_value.strip())
    else:
        name = validate_name(fallback_name or "")
    summary = raw.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise ConfigError(f"role {name!r} needs a non-empty summary")
    work = raw.get("work", "worktree")
    if not isinstance(work, str) or work not in WORK_MODES:
        raise ConfigError(
            f"role {name!r}: work {work!r} must be one of {', '.join(WORK_MODES)}"
        )
    network = raw.get("network", True)
    if not (network is True or network is False or network == "auto"):
        raise ConfigError(
            f"role {name!r}: network {network!r} must be true, false or 'auto'"
        )
    shell: str | None = raw.get("shell")
    if shell is not None:
        if not isinstance(shell, str) or shell not in SHELL_MODES:
            raise ConfigError(
                f"role {name!r}: shell {shell!r} must be one of "
                f"{', '.join(SHELL_MODES)}"
            )
    allow = raw.get("allow")
    if not isinstance(allow, list) or not all(isinstance(a, str) for a in allow):
        raise ConfigError(f"role {name!r}: allow must be a list of strings")
    deny = raw.get("deny", [])
    if not isinstance(deny, list) or not all(isinstance(d, str) for d in deny):
        raise ConfigError(f"role {name!r}: deny must be a list of strings")
    prompt = raw.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ConfigError(f"role {name!r} needs a non-empty prompt")

    spec: dict[str, Any] = {
        "name": name,
        "summary": summary,
        "work": work,
        "network": network,
        "allow": allow,
        "deny": deny,
        "prompt": prompt,
    }
    if shell is not None:
        spec["shell"] = shell
    return spec


def parse_role_toml(text: str, fallback_name: str | None = None) -> dict:
    """Parse a role's TOML text into a validated spec."""
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid role TOML: {exc}") from None
    return parse_role_data(raw, fallback_name)


def _toml_str(value: str) -> str:
    """A TOML basic string. json.dumps output is valid TOML for the short
    strings that end up here (names, summaries, globs) -- projects.py does
    the same."""
    return json.dumps(value)


def _toml_prompt(value: str) -> str:
    """The prompt as a TOML multi-line basic string.

    Multi-line, because a role prompt is markdown the operator reads and
    edits. Every backslash and double quote gets escaped, so no closing
    delimiter can appear inside the body whatever the prompt contains --
    the hand-rolled serializer bug this pre-empts is a trailing run of
    quotes ending the string early. TOML strips only the first newline
    after the opening delimiter, so the text round-trips exactly.
    """
    body = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"""\n{body}"""'


def role_toml(spec: Mapping[str, Any]) -> str:
    """Serialize a role spec back into the TOML the operator edits."""
    lines = [
        f"name = {_toml_str(spec['name'])}",
        f"summary = {_toml_str(spec['summary'])}",
        f"work = {_toml_str(spec['work'])}",
    ]
    network = spec.get("network", True)
    if network is not True:
        lines.append(f"network = {'false' if network is False else _toml_str(network)}")
    shell = spec.get("shell")
    if shell:
        lines.append(f"shell = {_toml_str(shell)}")
    lines.append(f"allow = [{', '.join(_toml_str(a) for a in spec['allow'])}]")
    lines.append(f"deny = [{', '.join(_toml_str(d) for d in spec['deny'])}]")
    lines.append("")
    lines.append(f"prompt = {_toml_prompt(spec['prompt'])}")
    return "\n".join(lines) + "\n"


def _parse_role_file(path: Path) -> dict:
    """Parse one role file, insisting the file's name matches the role's.

    The file stem is the identity the console addresses (``roles/<name>.toml``
    for ``<name>``); a file that defines a differently-named role would
    otherwise be listable under one name and unreadable under it.
    """
    try:
        spec = parse_role_toml(path.read_text())
    except ConfigError as exc:
        raise ConfigError(f"{path.name}: {exc}") from None
    if spec["name"] != path.stem:
        raise ConfigError(
            f"{path.name}: it defines role {spec['name']!r}; rename the file "
            f"to {spec['name']}.toml or the table to {path.stem!r}"
        )
    return spec


def load_roles() -> dict[str, dict]:
    """Every custom role on disk, keyed by name.

    A missing directory is simply no custom roles -- the daemon boots empty
    on a fresh install, not with an error. A file that does not parse is
    skipped rather than fatal, the same leniency projects and persisted
    teams get: one typoed file must not take the console's role list down.
    """
    out: dict[str, dict] = {}
    directory = roles_dir()
    if not directory.is_dir():
        return out
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.suffix != ".toml":
            continue
        try:
            spec = _parse_role_file(path)
        except ConfigError:
            continue
        out[spec["name"]] = spec
    return out


def load_role(name: str) -> dict:
    """One custom role's spec.

    ConfigError when the name is unusable, the file is missing, or it does
    not parse -- the caller decides between 400 and 404.
    """
    validate_name(name)
    path = roles_dir() / f"{name}.toml"
    try:
        return _parse_role_file(path)
    except FileNotFoundError:
        raise ConfigError(f"no such role: {name!r}") from None


def save_role(spec: Mapping[str, Any], builtin_names: Iterable[str] = ()) -> Path:
    """Write one custom role file, refusing a built-in name.

    ``builtin_names`` comes from the caller's compose module -- the name
    check here is the last line of defence behind the API layer's 409, so a
    built-in role can never be overwritten by a save, whatever path the
    spec took to get here.
    """
    name = validate_name(str(spec.get("name", "")))
    if name in set(builtin_names):
        raise ConfigError(
            f"{name!r} is a built-in role; built-ins are read-only, "
            "save the copy under a different name"
        )
    directory = roles_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.toml"
    path.write_text(role_toml(spec))
    return path


def delete_role(name: str, builtin_names: Iterable[str] = ()) -> bool:
    """Remove a custom role's file. False when there was nothing to remove.

    Built-ins are refused before anything is touched.
    """
    validate_name(name)
    if name in set(builtin_names):
        raise ConfigError(f"{name!r} is a built-in role; built-ins cannot be deleted")
    path = roles_dir() / f"{name}.toml"
    if not path.exists():
        return False
    path.unlink()
    return True


def install_custom_roles(compose_module: Any) -> dict[str, dict]:
    """Merge every readable custom role into this process's compose module.

    New names are added to ``ROLES``; entries that already exist are left
    untouched, so built-ins cannot be shadowed, overridden or deleted
    through a custom file (a file that names one is ignored here -- the
    save path refuses to create it in the first place). The specs are the
    loaded dicts themselves, and the module also remembers them in
    ``custom_roles``, so the web layer can tell built-in from custom
    without re-reading the directory.

    Idempotent for a *reused* module: a role deleted (or re-saved) since
    the last merge is dropped or refreshed rather than lingering, which is
    only observable when a caller keeps one module alive across calls
    (tests, embedded daemon loops).

    Returns the custom specs as loaded (shared dicts, not copies). Never
    touches ``built/`` or the role markdown.
    """
    custom = load_roles()
    roles_table = compose_module.ROLES
    previous = getattr(compose_module, "custom_roles", None)
    if previous:
        for name in previous:
            if name not in custom and roles_table.get(name) is previous[name]:
                del roles_table[name]
    for name, spec in custom.items():
        if name not in roles_table or roles_table[name] is (previous or {}).get(name):
            roles_table[name] = spec
    compose_module.custom_roles = custom
    return custom


def builtin_role_names(compose_module: Any) -> set[str]:
    """The compose module's ROLES keys that are NOT custom roles.

    The complement of what ``install_custom_roles`` merged in, re-derived
    from the module's own record: a state file naming a built-in role
    never makes the name custom, so saves and deletes keep refusing it.
    """
    merged = getattr(compose_module, "custom_roles", None)
    if merged is None:
        return set(compose_module.ROLES)
    return set(compose_module.ROLES) - set(merged)
