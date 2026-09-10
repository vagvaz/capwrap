"""The authority view: one assembled answer to "what may this container do?".

The capability graph renders kernel objects only, which is nearly empty for
operator-spawned containers -- their real authority lives in the config's
permission lists, the grant table, the network rules, the peer caps and the
team boards. These tests pin the assembly: the grouping of the allow rules,
the merged deny list, the network posture, and the team membership that shows
up as peers and boards.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from capwrap.config import load_config, load_config_data
from capwrap.daemon import Daemon
from capwrap.web.app import authority_view


@pytest.fixture
def compose(tmp_path, monkeypatch):
    from capwrap.teams import load_compose

    module = load_compose()
    # compose() writes into BUILT; point it at a temp dir so the tests never
    # touch the checked-in examples/roles-and-personas/built/ directory.
    built = tmp_path / "built"
    built.mkdir()
    monkeypatch.setattr(module, "BUILT", built)
    return module


@pytest.fixture
async def daemon(state_dir):
    d = Daemon(audit_path=Path(state_dir) / "audit.db")
    yield d
    await d.shutdown()


class _FakeSession:
    running = True

    def status(self):
        return {"running": True}

    async def terminate(self, grace=5.0):
        self.running = False
        return 0


async def _spawn_team(daemon, compose, monkeypatch, **overrides):
    """Spawn a team with `daemon.start` mocked, so no sandbox is needed."""
    from capwrap.teams import parse_team_data

    async def fake_start(name):
        container = daemon.containers[name]
        container.session = _FakeSession()
        return container

    monkeypatch.setattr(daemon, "start", fake_start)
    data = {
        "name": "feature-x",
        "goal": "ship the parser rewrite",
        "success_criteria": "all tests green",
        "members": [
            {"role": "implementer", "persona": "pragmatist", "agent": "claude"},
            {"role": "reviewer", "persona": "devils-advocate", "agent": "claude"},
        ],
    }
    data.update(overrides)
    await daemon.spawn_team(parse_team_data(data, compose))


# ==========================================================================
# allow grouping
# ==========================================================================


async def test_implementer_allow_groups_are_populated_and_disjoint(
    daemon, compose, tmp_path
):
    """A generated implementer config splits three ways: the ambient baseline,
    the work shell, and the role's own tool grants -- and no pattern lands in
    two buckets."""
    path = compose.compose("implementer", "pragmatist")
    container = daemon.register(load_config(path))
    view = authority_view(daemon, container, compose)

    allow = view["allow"]
    assert allow["ambient"], "ambient baseline missing"
    assert allow["work_shell"], "work shell missing for a work role"
    assert allow["role"], "role grants missing"

    # The buckets are disjoint: every pattern appears exactly once overall.
    all_patterns = [*allow["ambient"], *allow["work_shell"], *allow["role"]]
    assert len(all_patterns) == len(set(all_patterns))

    # Spot-check the classification against compose's own tables.
    assert "Bash(ls*)" in allow["ambient"]
    assert "Bash(make*)" in allow["work_shell"]
    assert "Read" in allow["role"]
    # The role's own Bash patterns are role authority, not ambient shell.
    assert not any(p.startswith("Bash(") for p in allow["role"]) or all(
        p not in {"Bash(ls*)", "Bash(make*)"} for p in allow["role"]
    )


async def test_deny_is_merged_from_both_sources(daemon, compose, tmp_path):
    """auto_deny and permissions.deny fold into one deduped list."""
    path = compose.compose("implementer", "pragmatist")
    container = daemon.register(load_config(path))
    deny = authority_view(daemon, container, compose)["deny"]

    # "Bash(sudo *)" is in both sources; the shell denylist only in the
    # permissions block. Both must be present, and each pattern once.
    assert "Bash(sudo *)" in deny
    assert "Bash(curl *)" in deny
    assert len(deny) == len(set(deny))


# ==========================================================================
# grants, network, routing
# ==========================================================================


async def test_grants_are_included(daemon, compose, tmp_path):
    path = compose.compose("implementer", "pragmatist")
    container = daemon.register(load_config(path))
    container.grants.add("Bash(git push*)", tool="Bash")

    view = authority_view(daemon, container, compose)
    assert any(g["pattern"] == "Bash(git push*)" for g in view["grants"])


async def test_auto_network_config_lists_its_rules(
    daemon, compose, tmp_path, monkeypatch
):
    """network = "auto" derives [[caps.network]] rules; the view shows them,
    and the posture is proxied rather than open."""
    monkeypatch.setattr(
        compose,
        "extract_base_urls",
        lambda _path: ["https://api.example.com/v1"],
    )
    path = compose.compose("implementer", "pragmatist")
    container = daemon.register(load_config(path))

    network = authority_view(daemon, container, compose)["network"]
    assert network["open"] is False
    assert network["rules"], "auto-network config produced no rules"
    assert all("name" in r and "pattern" in r for r in network["rules"])


async def test_host_network_is_reported_open_and_unproxied(daemon, tmp_path):
    """sandbox.network = true hands over the host's network wholesale."""
    from capwrap.teams import load_compose

    config = load_config_data(
        {"name": "open-net", "sandbox": {"network": True}}, base_dir=tmp_path
    )
    container = daemon.register(config)
    network = authority_view(daemon, container, load_compose())["network"]
    assert network["open"] is True
    assert network["rules"] == []


async def test_routing_reflects_the_live_override(daemon, compose, tmp_path):
    path = compose.compose("implementer", "pragmatist", routing="forward")
    container = daemon.register(load_config(path))
    view = authority_view(daemon, container, compose)
    assert view["routing"]["effective"] == "forward"
    assert view["routing"]["override"] is None

    container.question_routing = "auto"
    view = authority_view(daemon, container, compose)
    assert view["routing"]["effective"] == "auto"
    assert view["routing"]["config"] == "forward"


# ==========================================================================
# peers, boards, and the endpoint itself
# ==========================================================================


async def test_team_membership_shows_up_as_peers_and_boards(
    daemon, compose, monkeypatch
):
    await _spawn_team(daemon, compose, monkeypatch)
    container = daemon.containers["implementer-pragmatist"]
    view = authority_view(daemon, container, compose)

    peers = {p["container"]: p["rights"] for p in view["peers"]}
    assert "reviewer-devils-advocate" in peers
    assert "send" in peers["reviewer-devils-advocate"]

    topics = {b["topic"] for b in view["boards"]}
    assert "team/feature-x" in topics


async def test_unknown_container_is_a_404(state_dir, tmp_path):
    from fastapi.testclient import TestClient

    from capwrap.web.app import create_app

    d = Daemon(audit_path=Path(state_dir) / "audit.db")
    try:
        client = TestClient(create_app(d))
        response = client.get("/api/containers/nobody/authority")
        assert response.status_code == 404
    finally:
        await d.shutdown()
