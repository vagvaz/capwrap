"""File-backed team definitions: save/load round-trips in a temp state dir,
name-mismatch refusal, custom roles as members, and the /api/team-files
endpoints.

Like the role tests, `daemon.start` is mocked so nothing is sandboxed; the
interesting assertions are about the TOML files the state dir ends up with
and about what is *not* written: spawning does not happen from a save, and
the daemon's persisted teams.json is never touched by the definition store.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from capwrap import roles, teams
from capwrap.daemon import Daemon
from capwrap.errors import ConfigError

TEAM_TOML = """\
name = "feature-x"
goal = "ship the parser rewrite"
success_criteria = "all tests green"

[[members]]
role = "implementer"
persona = "pragmatist"

[[members]]
role = "reviewer"
persona = "devils-advocate"
agent = "claude"
"""

CUSTOM_ROLE_TOML = '''\
name = "parser-writer"
summary = "writes parsers and their tests"
work = "worktree"
allow = ["Read", "Write", "Edit"]
deny = ["Bash(sudo *)"]

prompt = """
# You are the parser writer
Make the parser and its tests.
"""
'''


def write_team_file(state_dir: Path, name: str, text: str) -> Path:
    """Drop a definition file into the patched state dir, the way a save would."""
    directory = state_dir / "team-files"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.toml"
    path.write_text(text)
    return path


def write_custom_role(state_dir: Path, text: str) -> None:
    """Drop a custom role file into the patched state dir's roles/."""
    directory = state_dir / "roles"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "parser-writer.toml").write_text(text)


@pytest.fixture
async def daemon(state_dir):
    d = Daemon(audit_path=Path(state_dir) / "audit.db")
    yield d
    await d.shutdown()


@pytest.fixture
def client(daemon, state_dir):
    from capwrap.web.app import create_app
    from fastapi.testclient import TestClient

    return TestClient(create_app(daemon))


# ==========================================================================
# store: save / load / list / delete
# ==========================================================================


def test_save_then_load_round_trips(state_dir):
    team = teams.parse_team_toml(TEAM_TOML)
    path = teams.save_team_file(team)
    assert path == teams.team_files_dir() / "feature-x.toml"
    assert (Path(state_dir) / "team-files" / "feature-x.toml").is_file()

    loaded = teams.load_team_file("feature-x")
    assert loaded.to_dict() == team.to_dict()
    assert [m.name for m in loaded.members] == [
        "implementer-pragmatist",
        "reviewer-devils-advocate",
    ]

    # Serializing the loaded team yields the same file again, byte for byte.
    assert teams.team_toml(loaded) == path.read_text()
    assert teams.save_team_file(teams.parse_team_toml(teams.team_toml(loaded))) == path

    assert teams.list_team_files() == [
        {
            "name": "feature-x",
            "goal": "ship the parser rewrite",
            "members": 2,
        }
    ]


def test_a_file_whose_name_does_not_match_is_refused(state_dir):
    write_team_file(state_dir, "other", TEAM_TOML)
    with pytest.raises(ConfigError, match="it defines team 'feature-x'"):
        teams.load_team_file("other")
    # And the lenient list skips it rather than dying.
    assert teams.list_team_files() == []


def test_a_member_role_nobody_knows_is_refused(state_dir):
    bad = TEAM_TOML.replace('name = "feature-x"', 'name = "bad"').replace(
        'role = "implementer"', 'role = "nonsense"'
    )
    with pytest.raises(ConfigError, match="unknown role"):
        teams.parse_team_toml(bad)
    write_team_file(state_dir, "bad", bad)
    with pytest.raises(ConfigError, match="unknown role"):
        teams.load_team_file("bad")
    # Nothing half-written: the broken file is the only trace.
    assert teams.list_team_files() == []


def test_a_custom_role_is_accepted_as_a_member(state_dir):
    write_custom_role(state_dir, CUSTOM_ROLE_TOML)
    toml = TEAM_TOML.replace(
        'name = "feature-x"',
        'name = "parsers"',
    ).replace(
        'role = "implementer"',
        'role = "parser-writer"',
    )
    team = teams.parse_team_toml(toml)
    assert team.members[0].name == "parser-writer-pragmatist"
    path = teams.save_team_file(team)
    assert teams.load_team_file("parsers").to_dict() == team.to_dict()
    assert path.read_text().count("[[members]]") == 2


def test_delete_removes_only_the_file(state_dir):
    team = teams.parse_team_toml(TEAM_TOML)
    path = teams.save_team_file(team)
    assert teams.delete_team_file("feature-x") is True
    assert not path.exists()
    assert teams.delete_team_file("feature-x") is False
    with pytest.raises(ConfigError, match="no such team file"):
        teams.load_team_file("feature-x")


# ==========================================================================
# web: /api/team-files
# ==========================================================================


async def test_team_files_endpoints_round_trip(client, state_dir):
    put = client.put("/api/team-files/feature-x", json={"toml": TEAM_TOML})
    assert put.status_code == 200, put.text
    assert put.json()["name"] == "feature-x"

    listing = client.get("/api/team-files").json()
    assert listing == [
        {"name": "feature-x", "goal": "ship the parser rewrite", "members": 2}
    ]

    detail = client.get("/api/team-files/feature-x").json()
    assert detail["name"] == "feature-x"
    assert detail["goal"] == "ship the parser rewrite"
    assert [m["role"] for m in detail["members"]] == ["implementer", "reviewer"]
    # The TOML is exactly the file on disk, so an untouched edit round-trips.
    assert (
        detail["toml"]
        == (Path(state_dir) / "team-files" / "feature-x.toml").read_text()
    )

    assert client.get("/api/team-files/never-was").status_code == 404

    assert client.delete("/api/team-files/feature-x").status_code == 200
    assert client.get("/api/team-files/feature-x").status_code == 404
    assert client.delete("/api/team-files/feature-x").status_code == 404


async def test_put_with_a_mismatched_name_is_a_400(client, state_dir):
    response = client.put("/api/team-files/other", json={"toml": TEAM_TOML})
    assert response.status_code == 400, response.text
    # Neither name was written: the refusal lands before any file changes hands.
    assert not (Path(state_dir) / "team-files" / "other.toml").exists()
    assert not (Path(state_dir) / "team-files" / "feature-x.toml").exists()


async def test_put_from_fields_saves(client, state_dir):
    response = client.put(
        "/api/team-files/feature-x",
        json={
            "goal": "ship the parser rewrite",
            "success_criteria": "all tests green",
            "members": [
                {"role": "implementer", "persona": "pragmatist"},
                {"role": "reviewer", "persona": "devils-advocate"},
            ],
        },
    )
    assert response.status_code == 200, response.text
    team = teams.load_team_file("feature-x")
    assert team.goal == "ship the parser rewrite"
    assert team.members[1].agent == "claude"


async def test_put_validation_errors_are_a_400(client, state_dir):
    bad_role = TEAM_TOML.replace('role = "implementer"', 'role = "nonsense"')
    assert client.put("/api/team-files/bad", json={"toml": bad_role}).status_code == 400
    no_goal = "\n".join(
        line for line in TEAM_TOML.splitlines() if not line.startswith("goal =")
    )
    assert (
        client.put("/api/team-files/no-goal", json={"toml": no_goal}).status_code == 400
    )
    assert (
        client.put(
            "/api/team-files/broken", json={"toml": "name = [not valid"}
        ).status_code
        == 400
    )
    assert teams.list_team_files() == []


async def test_put_needs_no_running_team_and_never_touches_teams_json(
    client, daemon, state_dir
):
    response = client.put("/api/team-files/feature-x", json={"toml": TEAM_TOML})
    assert response.status_code == 200, response.text
    # No spawn happened and nothing was persisted: the file is the only
    # thing that changed.
    assert daemon.teams == {}
    assert daemon.containers == {}
    assert not (Path(state_dir) / "teams.json").exists()


async def test_a_custom_role_reaches_the_save_endpoint(client, state_dir):
    write_custom_role(state_dir, CUSTOM_ROLE_TOML)
    toml = TEAM_TOML.replace('name = "feature-x"', 'name = "parsers"').replace(
        'role = "implementer"', 'role = "parser-writer"'
    )
    response = client.put("/api/team-files/parsers", json={"toml": toml})
    assert response.status_code == 200, response.text
    assert teams.load_team_file("parsers").members[0].role == "parser-writer"


def test_custom_roles_and_team_files_do_not_collide(state_dir):
    """The definition store and the role store are siblings, not one bucket."""
    write_custom_role(state_dir, CUSTOM_ROLE_TOML)
    teams.save_team_file(teams.parse_team_toml(TEAM_TOML))
    assert roles.load_roles() == {"parser-writer": roles.load_role("parser-writer")}
    assert [row["name"] for row in teams.list_team_files()] == ["feature-x"]
