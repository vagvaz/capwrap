"""Teams: TOML validation, spawning, membership, boards and persistence.

The spawn tests mock the daemon boundary (`daemon.start`) the way the rest of
the suite does, so a team can be registered and linked without a sandbox. The
interesting assertions are about the *capability* shape that results: members
can message each other without a card, a non-member stays gated, and the shared
board is scoped to members only.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from capwrap.daemon import Daemon
from capwrap.errors import ConfigError
from capwrap.teams import parse_team_data, team_preamble


@pytest.fixture
def compose():
    from capwrap.teams import load_compose

    return load_compose()


def team_data(**overrides):
    data = {
        "name": "feature-x",
        "goal": "ship the parser rewrite",
        "success_criteria": "all tests green, ADR merged",
        "members": [
            {"role": "implementer", "persona": "pragmatist", "agent": "claude"},
            {"role": "reviewer", "persona": "devils-advocate", "agent": "claude"},
        ],
    }
    data.update(overrides)
    return data


# ==========================================================================
# TOML validation
# ==========================================================================


def test_a_valid_team_parses(compose):
    team = parse_team_data(team_data(), compose)
    assert team.name == "feature-x"
    assert team.goal == "ship the parser rewrite"
    assert team.success_criteria == "all tests green, ADR merged"
    assert [m.name for m in team.members] == [
        "implementer-pragmatist",
        "reviewer-devils-advocate",
    ]
    assert team.board_topic == "team/feature-x"


def test_unknown_role_is_refused(compose):
    with pytest.raises(ConfigError, match="unknown role"):
        parse_team_data(
            team_data(members=[{"role": "nope", "persona": "pragmatist"}]), compose
        )


def test_unknown_persona_is_refused(compose):
    with pytest.raises(ConfigError, match="unknown persona"):
        parse_team_data(
            team_data(members=[{"role": "implementer", "persona": "nope"}]), compose
        )


def test_unknown_agent_is_refused(compose):
    with pytest.raises(ConfigError, match="unknown agent"):
        parse_team_data(
            team_data(
                members=[
                    {"role": "implementer", "persona": "pragmatist", "agent": "nope"}
                ]
            ),
            compose,
        )


def test_duplicate_member_names_are_refused(compose):
    # Same role+persona+agent twice -> the same generated container name.
    with pytest.raises(ConfigError, match="both generate the container name"):
        parse_team_data(
            team_data(
                members=[
                    {"role": "implementer", "persona": "pragmatist"},
                    {"role": "implementer", "persona": "pragmatist"},
                ]
            ),
            compose,
        )


def test_a_team_needs_a_goal(compose):
    with pytest.raises(ConfigError, match="needs a goal"):
        parse_team_data(team_data(goal=""), compose)


def test_a_team_needs_members(compose):
    with pytest.raises(ConfigError, match="at least one"):
        parse_team_data(team_data(members=[]), compose)


def test_agent_defaults_to_claude(compose):
    team = parse_team_data(
        team_data(members=[{"role": "implementer", "persona": "pragmatist"}]), compose
    )
    assert team.members[0].agent == "claude"
    assert team.members[0].name == "implementer-pragmatist"


def test_non_claude_agents_are_prefixed_in_the_name(compose):
    team = parse_team_data(
        team_data(
            members=[{"role": "implementer", "persona": "pragmatist", "agent": "pi"}]
        ),
        compose,
    )
    assert team.members[0].name == "pi-implementer-pragmatist"


def test_team_preamble_carries_goal_and_board(compose):
    team = parse_team_data(team_data(), compose)
    preamble = team_preamble(team)
    assert "ship the parser rewrite" in preamble
    assert "all tests green, ADR merged" in preamble
    assert "team/feature-x" in preamble


def test_compose_emits_the_chosen_question_routing(compose, tmp_path, monkeypatch):
    """Generated configs carry the routing position, defaulting to forward."""
    monkeypatch.setattr(compose, "BUILT", tmp_path)

    path = compose.compose("implementer", "pragmatist", routing="block")
    assert 'question_routing = "block"' in path.read_text()

    path = compose.compose("reviewer", "devils-advocate", routing="auto")
    assert 'question_routing = "auto"' in path.read_text()

    path = compose.compose("architect", "idealist")
    assert 'question_routing = "forward"' in path.read_text()


# ==========================================================================
# spawning, membership and boards
# ==========================================================================


class _FakeSession:
    running = True

    def status(self):
        return {"running": True}

    async def terminate(self, grace=5.0):
        self.running = False
        return 0


@pytest.fixture
async def daemon(state_dir):
    d = Daemon(audit_path=Path(state_dir) / "audit.db")
    yield d
    await d.shutdown()


async def _spawn(daemon, data, monkeypatch):
    """Spawn a team with `daemon.start` mocked so no sandbox is needed."""
    from capwrap.teams import parse_team_data, load_compose

    async def fake_start(name):
        container = daemon.containers[name]
        container.session = _FakeSession()
        container.obj.state = "running"
        return container

    monkeypatch.setattr(daemon, "start", fake_start)
    team = parse_team_data(data, load_compose())
    return await daemon.spawn_team(team)


async def test_spawn_creates_linked_members_who_message_each_other_without_a_card(
    daemon, tmp_path, monkeypatch
):
    result = await _spawn(daemon, team_data(), monkeypatch)
    assert set(result["members"]) == {
        "implementer-pragmatist",
        "reviewer-devils-advocate",
    }

    # Each member holds a peer capability on the other, with send.
    impl = {c.label: c for c in daemon.kernel.cap_list("implementer-pragmatist")}
    assert "peer:reviewer-devils-advocate" in impl
    assert "send" in impl["peer:reviewer-devils-advocate"].rights

    rev = {c.label: c for c in daemon.kernel.cap_list("reviewer-devils-advocate")}
    assert "peer:implementer-pragmatist" in rev
    assert "send" in rev["peer:implementer-pragmatist"].rights

    # A non-member holds no capability on either member.
    daemon.register(
        __import__("capwrap.config", fromlist=["load_config_data"]).load_config_data(
            {"name": "outsider"}, base_dir=tmp_path
        )
    )
    outsider = {c.label for c in daemon.kernel.cap_list("outsider")}
    assert "peer:implementer-pragmatist" not in outsider
    assert "peer:reviewer-devils-advocate" not in outsider


async def test_team_board_is_scoped_to_members(daemon, tmp_path, monkeypatch):
    await _spawn(daemon, team_data(), monkeypatch)

    boards = [b.topic for b in daemon.kernel.boards()]
    assert "team/feature-x" in boards

    # Every member can read and write the board.
    for member in ("implementer-pragmatist", "reviewer-devils-advocate"):
        board_caps = [
            c
            for c in daemon.kernel.cap_list(member)
            if c.kind == "board" and c.detail.get("topic") == "team/feature-x"
        ]
        assert len(board_caps) == 1
        assert set(board_caps[0].rights) == {"send", "read"}

    # A non-member holds no capability on the board.
    daemon.register(
        __import__("capwrap.config", fromlist=["load_config_data"]).load_config_data(
            {"name": "outsider"}, base_dir=tmp_path
        )
    )
    outsider = [
        c
        for c in daemon.kernel.cap_list("outsider")
        if c.kind == "board" and c.detail.get("topic") == "team/feature-x"
    ]
    assert outsider == []


async def test_collision_refuses_the_whole_team_and_spawns_nothing(
    daemon, tmp_path, monkeypatch
):
    # Pre-register one of the members the team would generate.
    from capwrap.config import load_config_data

    daemon.register(
        load_config_data({"name": "implementer-pragmatist"}, base_dir=tmp_path)
    )

    with pytest.raises(Exception, match="already exists"):
        await _spawn(daemon, team_data(), monkeypatch)

    # Nothing else was spawned, and no team was recorded.
    assert set(daemon.containers) == {"implementer-pragmatist"}
    assert daemon.teams == {}
    assert not (daemon.state / "teams.json").exists()


async def test_teams_persist_across_a_reload(daemon, tmp_path, monkeypatch):
    await _spawn(daemon, team_data(), monkeypatch)
    assert (daemon.state / "teams.json").exists()

    # A fresh daemon on the same state dir reloads the team.
    d2 = Daemon(audit_path=Path(tmp_path) / "audit2.db")
    try:
        assert "feature-x" in d2.teams
        view = d2.teams_view()
        assert view[0]["name"] == "feature-x"
        assert view[0]["goal"] == "ship the parser rewrite"
        # Members are not registered in the fresh daemon, so not running.
        assert all(m["running"] is False for m in view[0]["members"])
    finally:
        await d2.shutdown()


async def test_link_team_membership_regrants_the_board_after_reload(
    daemon, tmp_path, monkeypatch
):
    await _spawn(daemon, team_data(), monkeypatch)

    # Simulate a restart: fresh daemon, members re-registered from configs.
    d2 = Daemon(audit_path=Path(tmp_path) / "audit2.db")
    try:
        from capwrap.config import load_config_data

        for member in ("implementer-pragmatist", "reviewer-devils-advocate"):
            d2.register(load_config_data({"name": member}, base_dir=tmp_path))
        d2.link_team_membership()

        for member in ("implementer-pragmatist", "reviewer-devils-advocate"):
            board_caps = [
                c
                for c in d2.kernel.cap_list(member)
                if c.kind == "board" and c.detail.get("topic") == "team/feature-x"
            ]
            assert len(board_caps) == 1
    finally:
        await d2.shutdown()


async def test_stop_team_stops_every_member(daemon, tmp_path, monkeypatch):
    await _spawn(daemon, team_data(), monkeypatch)
    stopped = []

    async def fake_stop(name, grace=5.0):
        stopped.append(name)
        return 0

    monkeypatch.setattr(daemon, "stop", fake_stop)
    result = await daemon.stop_team("feature-x")
    assert {m["name"] for m in result["members"]} == {
        "implementer-pragmatist",
        "reviewer-devils-advocate",
    }
    assert set(stopped) == {"implementer-pragmatist", "reviewer-devils-advocate"}


# ==========================================================================
# web API
# ==========================================================================


async def test_teams_api_spawn_and_list(daemon, tmp_path, monkeypatch):
    from capwrap.web.app import create_app
    from fastapi.testclient import TestClient

    async def fake_start(name):
        container = daemon.containers[name]
        container.session = _FakeSession()
        container.obj.state = "running"
        return container

    monkeypatch.setattr(daemon, "start", fake_start)
    client = TestClient(create_app(daemon))

    response = client.post("/api/teams/spawn", json={"team": team_data()})
    assert response.status_code == 200, response.text
    assert set(response.json()["members"]) == {
        "implementer-pragmatist",
        "reviewer-devils-advocate",
    }

    listed = client.get("/api/teams").json()
    assert listed[0]["name"] == "feature-x"
    assert listed[0]["goal"] == "ship the parser rewrite"
    assert all(m["running"] for m in listed[0]["members"])


async def test_teams_api_rejects_an_invalid_team(daemon, tmp_path, monkeypatch):
    from capwrap.web.app import create_app
    from fastapi.testclient import TestClient

    client = TestClient(create_app(daemon))
    bad = team_data(members=[{"role": "nope", "persona": "pragmatist"}])
    response = client.post("/api/teams/spawn", json={"team": bad})
    assert response.status_code == 400
    assert "unknown role" in response.json()["error"]
    assert daemon.teams == {}


async def test_teams_api_stop(daemon, tmp_path, monkeypatch):
    from capwrap.web.app import create_app
    from fastapi.testclient import TestClient

    await _spawn(daemon, team_data(), monkeypatch)
    client = TestClient(create_app(daemon))

    response = client.post("/api/teams/feature-x/stop")
    assert response.status_code == 200, response.text
    assert {m["name"] for m in response.json()["members"]} == {
        "implementer-pragmatist",
        "reviewer-devils-advocate",
    }
