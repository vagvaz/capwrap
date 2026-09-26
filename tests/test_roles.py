"""File-backed custom roles: save/load round-trips, built-in refusal, and
the compose / web / team integration through the merged module.

Like the spawn tests, `daemon.start` is mocked so no sandbox is needed; the
interesting assertions are about the TOML files the state dir ends up with
and the configs compose() generates from them. BUILT is pointed at a temp
directory and the state dir at `CAPWRAP_STATE` the way the rest of the
suite does; nothing is written into the repo's built/ or roles/.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from capwrap import roles
from capwrap.daemon import Daemon
from capwrap.errors import ConfigError
from capwrap.teams import load_compose, parse_team_data

WORK_ROLE_TOML = '''\
name = "parser-writer"
summary = "writes parsers and their tests"
work = "worktree"
shell = "work"
network = "auto"
allow = ["Read", "Write", "Edit"]
deny = ["Bash(sudo *)"]

prompt = """
# You are the parser writer
Make the parser and its tests. Do not redesign the console.
"""
'''

READONLY_ROLE_TOML = '''\
name = "gate-keeper"
summary = "watches and reports; changes nothing"
work = "worktree"
shell = "readonly"
network = false
allow = ["Read"]
deny = ["Write", "Edit"]

prompt = """
Watch and report. You cannot change anything.
"""
'''


def write_role(state_dir: Path, name: str, text: str) -> Path:
    """Drop a role file into the patched state dir, the way a save would."""
    directory = state_dir / "roles"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.toml"
    path.write_text(text)
    return path


@pytest.fixture
async def daemon(state_dir):
    d = Daemon(audit_path=Path(state_dir) / "audit.db")
    yield d
    await d.shutdown()


class _FakeSession:
    """The daemon boundary stand-in the other integration tests use."""

    running = True

    def status(self):
        return {"running": True}

    async def terminate(self, grace=5.0):
        self.running = False
        return 0


@pytest.fixture
def compose(tmp_path, state_dir):
    """The real compose module with BUILT pointed at a temp directory.

    load_compose() already merges whatever the (empty) state dir had; tests
    that write role files first call `roles.install_custom_roles(compose)`.
    """
    module = load_compose()
    module.BUILT = tmp_path / "built"
    module.BUILT.mkdir(parents=True, exist_ok=True)
    return module


@pytest.fixture
def compose_module(tmp_path, state_dir, monkeypatch):
    """The real compose module for the web app, BUILT redirected.

    The web endpoints build on whatever `_load_compose` returns, so the
    patch re-merges the state dir's custom roles on every call -- exactly
    what the real loader now does.
    """
    import capwrap.web.app as web_app

    module = load_compose()
    module.BUILT = tmp_path / "built"
    module.BUILT.mkdir(parents=True, exist_ok=True)

    def loader():
        roles.install_custom_roles(module)
        return module

    monkeypatch.setattr(web_app, "_load_compose", loader)
    return module


@pytest.fixture
def client(daemon, compose_module, monkeypatch):
    from capwrap.web.app import create_app
    from fastapi.testclient import TestClient

    async def fake_start(name):
        container = daemon.containers[name]
        container.session = _FakeSession()
        container.obj.state = "running"
        return container

    monkeypatch.setattr(daemon, "start", fake_start)
    return TestClient(create_app(daemon))


# ==========================================================================
# store: save / load / delete
# ==========================================================================


def test_save_then_load_round_trips(state_dir):
    spec = roles.parse_role_toml(WORK_ROLE_TOML)
    path = roles.save_role(spec)
    assert path == roles.roles_dir() / "parser-writer.toml"
    assert (Path(state_dir) / "roles" / "parser-writer.toml").is_file()
    assert roles.load_role("parser-writer") == spec
    assert roles.load_roles() == {"parser-writer": spec}
    # Serializing the loaded spec yields the same file again.  The prompt
    # -- markdown with newlines -- survives byte for byte.
    assert roles.parse_role_toml(roles.role_toml(spec)) == spec
    assert spec["prompt"].endswith("Do not redesign the console.\n")


def test_prompt_with_quotes_round_trips(state_dir):
    """Every quote going escaped keeps a delimiter-lookalike from ending
    the multi-line string early."""
    spec = roles.parse_role_data(
        {
            "name": "quoter",
            "summary": "s",
            "allow": [],
            "deny": [],
            "work": "none",
            "network": False,
            "shell": "ambient",
            "prompt": 'Has "quotes" and \\ backslashes',
        }
    )
    roles.save_role(spec)
    assert roles.load_role("quoter")["prompt"] == spec["prompt"]


def test_save_refuses_a_builtin_name(state_dir):
    spec = roles.parse_role_toml(WORK_ROLE_TOML.replace("parser-writer", "implementer"))
    with pytest.raises(ConfigError, match="built-in"):
        roles.save_role(spec, builtin_names=["implementer"])
    assert not (roles.roles_dir() / "implementer.toml").exists()


def test_delete_refuses_builtin_and_reports_missing(state_dir):
    write_role(state_dir, "gone", WORK_ROLE_TOML.replace("parser-writer", "gone"))
    with pytest.raises(ConfigError, match="built-in"):
        roles.delete_role("implementer", builtin_names=["implementer"])
    assert roles.delete_role("gone") is True
    assert roles.delete_role("gone") is False


def test_broken_and_misnamed_files_are_skipped(state_dir):
    (roles.roles_dir()).mkdir(parents=True, exist_ok=True)
    (roles.roles_dir() / "broken.toml").write_text("name = [not valid")
    write_role(state_dir, "other", WORK_ROLE_TOML.replace("parser-writer", "elsewhere"))
    assert roles.load_roles() == {}
    with pytest.raises(ConfigError):
        roles.load_role("broken")
    with pytest.raises(ConfigError, match="elsewhere"):
        roles.load_role("other")
    # A name that cannot be a file name is refused before any lookup.
    with pytest.raises(ConfigError, match="must be non-empty"):
        roles.load_role("bad name!")


def test_install_merges_without_shadowing_builtins(state_dir):
    write_role(state_dir, "parser-writer", WORK_ROLE_TOML)
    write_role(
        state_dir, "reviewer", READONLY_ROLE_TOML.replace("gate-keeper", "reviewer")
    )
    module = load_compose()
    merged = roles.install_custom_roles(module)
    assert "parser-writer" in module.ROLES
    assert module.ROLES["parser-writer"]["shell"] == "work"
    # Built-ins cannot be shadowed: reviewer stays the original spec.
    assert module.ROLES["reviewer"]["summary"] == (
        "reviews a diff; cannot write, by construction"
    )
    # Both files loaded, but only the non-colliding names merged in.
    assert set(merged) == {"parser-writer", "reviewer"}
    assert roles.builtin_role_names(module) == {
        name for name in module.ROLES if name not in merged
    }


# ==========================================================================
# compose: prompt replacement and shell posture
# ==========================================================================


def test_compose_uses_the_custom_prompt_and_work_shell(state_dir, compose):
    write_role(state_dir, "parser-writer", WORK_ROLE_TOML)
    roles.install_custom_roles(compose)
    # A custom role merged in must not trip compose()'s sys.exit.
    path = compose.compose("parser-writer", "pragmatist")
    text = path.read_text()
    prompt_md = path.with_suffix(".md").read_text()
    assert "You are the parser writer" in prompt_md
    assert "Make the parser and its tests" in prompt_md
    assert "You are the implementer" not in prompt_md
    assert "smallest change that does" not in prompt_md
    assert 'role_prompt = "parser-writer-pragmatist.md"' in text
    # The work shell grant reached claude's permission table.
    permissions = tomllib.loads(text)["runtime"]["permissions"]
    assert "Bash(pytest*)" in permissions["allow"]
    assert "Bash(uv*)" in permissions["allow"]
    assert "Bash(git commit*)" not in permissions["deny"]


def test_a_readonly_custom_role_denies_mutating_git(state_dir, compose):
    write_role(state_dir, "gate-keeper", READONLY_ROLE_TOML)
    roles.install_custom_roles(compose)
    path = compose.compose("gate-keeper", "pragmatist")
    text = path.read_text()
    permissions = tomllib.loads(text)["runtime"]["permissions"]
    # The mutating git verbs are denied, not asked (GIT_MUTATING encoding).
    assert "Bash(git commit*)" in permissions["deny"]
    assert "Bash(git push*)" in permissions["deny"]
    # No work-shell entry anywhere: not granted, not denied-into-grant.
    assert "Bash(pytest*)" not in permissions["allow"]
    assert "Bash(pytest*)" not in text
    # The ambient baseline persists, and the network stays closed.
    assert "Bash(git status*)" in permissions["allow"]
    assert tomllib.loads(text)["sandbox"]["network"] is False


def test_compose_without_a_merge_still_refuses_unknown_roles(state_dir, compose):
    write_role(state_dir, "parser-writer", WORK_ROLE_TOML)
    with pytest.raises(SystemExit):
        compose.compose("parser-writer", "pragmatist")


# ==========================================================================
# web: /api/roles, options, preview, spawn
# ==========================================================================


async def test_roles_endpoints_round_trip(client, compose_module, state_dir):
    put = client.put("/api/roles/parser-writer", json={"toml": WORK_ROLE_TOML})
    assert put.status_code == 200, put.text
    assert put.json()["name"] == "parser-writer"

    listing = client.get("/api/roles").json()
    by_name = {row["name"]: row for row in listing}
    assert set(by_name) >= {"architect", "reviewer", "parser-writer"}
    assert by_name["architect"]["builtin"] is True
    assert by_name["reviewer"]["builtin"] is True
    assert by_name["parser-writer"]["builtin"] is False
    assert by_name["parser-writer"]["shell"] == "work"
    assert by_name["parser-writer"]["work"] == "worktree"
    assert by_name["parser-writer"]["network"] == "auto"
    assert by_name["reviewer"]["shell"] is None

    detail = client.get("/api/roles/parser-writer").json()
    assert detail["builtin"] is False
    assert detail["summary"] == "writes parsers and their tests"
    assert "You are the parser writer" in detail["toml"]

    assert client.get("/api/roles/nonsense-mcgoat").status_code == 404


async def test_role_detail_of_a_builtin_is_readonly_with_empty_toml(client):
    detail = client.get("/api/roles/implementer").json()
    assert detail["builtin"] is True
    assert detail["toml"] == ""
    assert detail["work"] == "worktree"
    assert detail["network"] == "auto"


async def test_put_to_a_builtin_is_a_409(client, compose_module):
    body = {"toml": WORK_ROLE_TOML.replace("parser-writer", "implementer")}
    response = client.put("/api/roles/implementer", json=body)
    assert response.status_code == 409, response.text
    assert not (roles.roles_dir() / "implementer.toml").exists()


async def test_put_can_never_alias_a_builtin_via_the_body(client, compose_module):
    body = {"toml": WORK_ROLE_TOML.replace("parser-writer", "implementer")}
    response = client.put("/api/roles/not-a-copy", json=body)
    assert response.status_code == 409, response.text
    assert not (roles.roles_dir() / "implementer.toml").exists()
    assert not (roles.roles_dir() / "not-a-copy.toml").exists()


async def test_put_from_fields_saves(client, compose_module, state_dir):
    response = client.put(
        "/api/roles/wiki-tender",
        json={
            "name": "wiki-tender",
            "summary": "keeps the wiki",
            "work": "none",
            "network": False,
            "shell": "ambient",
            "allow": ["Read", "Write"],
            "deny": [],
            "prompt": "Tend the wiki.",
        },
    )
    assert response.status_code == 200, response.text
    assert roles.load_role("wiki-tender")["prompt"] == "Tend the wiki."


async def test_put_validation_errors_are_a_400(client, compose_module):
    bad_name = WORK_ROLE_TOML.replace("parser-writer", "bad name!")
    assert client.put("/api/roles/bad", json={"toml": bad_name}).status_code == 400
    bad_shell = WORK_ROLE_TOML.replace('shell = "work"', 'shell = "sudo"')
    assert (
        client.put("/api/roles/parser-writer", json={"toml": bad_shell}).status_code
        == 400
    )
    no_prompt = WORK_ROLE_TOML.split("prompt =")[0]
    assert (
        client.put("/api/roles/no-prompt", json={"toml": no_prompt}).status_code == 400
    )
    assert roles.load_roles() == {}


async def test_delete_rules(client, compose_module, state_dir):
    write_role(state_dir, "parser-writer", WORK_ROLE_TOML)
    assert client.delete("/api/roles/parser-writer").status_code == 200
    assert client.get("/api/roles/parser-writer").status_code == 404
    assert client.delete("/api/roles/parser-writer").status_code == 404
    response = client.delete("/api/roles/implementer")
    assert response.status_code == 409, response.text
    assert not (roles.roles_dir() / "implementer.toml").exists()


async def test_compose_options_include_custom_roles(client, compose_module, state_dir):
    write_role(state_dir, "parser-writer", WORK_ROLE_TOML)
    options = client.get("/api/compose/options").json()
    assert "parser-writer" in options["roles"]
    assert "architect" in options["roles"]


async def test_preview_of_a_custom_role_carries_its_prompt(
    client, compose_module, state_dir
):
    write_role(state_dir, "parser-writer", WORK_ROLE_TOML)
    response = client.get(
        "/api/compose/preview",
        params={"role": "parser-writer", "persona": "pragmatist"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert "You are the parser writer" in body["prompt"]
    assert "You are the implementer" not in body["prompt"]


async def test_spawn_the_form_shape_with_a_custom_role(
    client, daemon, compose_module, state_dir
):
    write_role(state_dir, "parser-writer", WORK_ROLE_TOML)
    response = client.post(
        "/api/spawn",
        json={"role": "parser-writer", "persona": "pragmatist"},
    )
    assert response.status_code == 200, response.text
    assert "parser-writer-pragmatist" in daemon.containers
    prompt = (compose_module.BUILT / "parser-writer-pragmatist.md").read_text()
    assert "Make the parser and its tests" in prompt


async def test_an_unknown_role_is_still_a_clean_400(client, compose_module):
    response = client.get(
        "/api/compose/preview",
        params={"role": "nonsense-mcgoat", "persona": "pragmatist"},
    )
    assert response.status_code == 400, response.text
    assert "unknown role" in response.json()["detail"]


async def test_the_role_read_endpoint_serves_a_custom_prompt(
    client, compose_module, state_dir
):
    write_role(state_dir, "parser-writer", WORK_ROLE_TOML)
    body = client.get("/api/compose/role/parser-writer").json()
    assert body["name"] == "parser-writer"
    assert "You are the parser writer" in body["markdown"]
    # Built-ins keep reading the repo markdown; nothing was written for them.
    builtin = client.get("/api/compose/role/implementer").json()
    assert "You are the implementer" in builtin["markdown"]


# ==========================================================================
# teams: the same merge reaches team validation
# ==========================================================================


def test_team_parse_accepts_the_custom_role_after_the_merge(state_dir):
    write_role(state_dir, "parser-writer", WORK_ROLE_TOML)
    module = load_compose()
    team = parse_team_data(
        {
            "name": "parsers",
            "goal": "ship the parser rewrite",
            "members": [{"role": "parser-writer", "persona": "pragmatist"}],
        },
        module,
    )
    assert team.members[0].name == "parser-writer-pragmatist"
    with pytest.raises(ConfigError, match="unknown role"):
        parse_team_data(
            {
                "name": "parsers",
                "goal": "ship the parser rewrite",
                "members": [{"role": "nonsense", "persona": "pragmatist"}],
            },
            module,
        )


async def test_team_spawn_endpoint_accepts_a_custom_role(
    daemon, state_dir, monkeypatch
):
    write_role(state_dir, "parser-writer", WORK_ROLE_TOML)

    async def fake_start(name):
        container = daemon.containers[name]
        container.session = _FakeSession()
        container.obj.state = "running"
        return container

    monkeypatch.setattr(daemon, "start", fake_start)

    from capwrap.web.app import create_app
    from fastapi.testclient import TestClient

    with TestClient(create_app(daemon)) as client:
        response = client.post(
            "/api/teams/spawn",
            json={
                "team": {
                    "name": "parsers",
                    "goal": "ship the parser rewrite",
                    "members": [{"role": "parser-writer", "persona": "pragmatist"}],
                }
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["members"] == ["parser-writer-pragmatist"]
        assert "parser-writer-pragmatist" in daemon.containers
