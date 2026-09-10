"""Projects: named, predefined spawn configurations.

Covers the TOML validation, the file round-trip, compose() receiving the
project's parameters, the daemon's spawn flow (single and team), and the web
endpoints -- including that everything without a project behaves exactly as
before.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from capwrap.errors import ConfigError
from capwrap.projects import (
    delete_project,
    load_projects,
    parse_project,
    parse_project_data,
    save_project,
)


@pytest.fixture
def projects_dir(tmp_path) -> Path:
    return tmp_path / "projects"


@pytest.fixture
def project_data(git_repo, tmp_path) -> dict:
    """A valid project mapping, pointed at the fixture repo.

    The extra mount's source is a real file, so a spawn that uses the project
    passes config validation.
    """
    mount_src = tmp_path / "foo"
    mount_src.write_text("x\n")
    return {
        "name": "capwrap",
        "source": str(git_repo),
        "base": "main",
        "extra_mounts": [{"src": str(mount_src), "dest": "/foo", "mode": "ro"}],
        "env": ["MY_TOKEN"],
        "routing": "forward",
    }


# ==========================================================================
# validation
# ==========================================================================


def test_a_valid_project_parses(project_data):
    project = parse_project_data(project_data)
    assert project.name == "capwrap"
    assert project.base == "main"
    assert project.extra_mounts[0].mode == "ro"
    assert project.env == ["MY_TOKEN"]
    assert project.routing == "forward"


def test_a_bad_slug_name_is_refused(project_data):
    for bad in ("", "has space", "-leading", "weird$"):
        with pytest.raises(ConfigError):
            parse_project_data({**project_data, "name": bad})


def test_a_missing_source_is_refused(project_data, tmp_path):
    with pytest.raises(ConfigError, match="does not exist"):
        parse_project_data({**project_data, "source": str(tmp_path / "nope")})


def test_a_source_that_is_not_a_git_repo_is_refused(project_data, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(ConfigError, match="not a git repository"):
        parse_project_data({**project_data, "source": str(plain)})


def test_a_bad_mount_mode_is_refused(project_data):
    with pytest.raises(ConfigError, match="mode"):
        parse_project_data(
            {
                **project_data,
                "extra_mounts": [{"src": "~/.foo", "dest": "/foo", "mode": "rw!"}],
            }
        )


def test_a_mount_without_src_or_dest_is_refused(project_data):
    with pytest.raises(ConfigError, match="src and dest"):
        parse_project_data(
            {**project_data, "extra_mounts": [{"dest": "/foo", "mode": "ro"}]}
        )


def test_a_bad_routing_is_refused(project_data):
    with pytest.raises(ConfigError, match="routing"):
        parse_project_data({**project_data, "routing": "sideways"})


def test_a_bad_env_name_is_refused(project_data):
    with pytest.raises(ConfigError, match="variable name"):
        parse_project_data({**project_data, "env": ["MY TOKEN"]})


def test_an_empty_base_is_refused(project_data):
    with pytest.raises(ConfigError, match="base"):
        parse_project_data({**project_data, "base": ""})


def test_base_is_not_resolved_at_save(project_data, git_repo, monkeypatch):
    """Validation of `base` is lazy: saving must not run git."""
    import subprocess

    def no_git(*args, **kwargs):
        raise AssertionError("git ran during parse")

    monkeypatch.setattr(subprocess, "run", no_git)
    parse_project_data({**project_data, "base": "no-such-branch"})


def test_base_is_resolved_at_spawn(project_data, git_repo):
    project = parse_project_data({**project_data, "base": "main"})
    project.validate_base()  # resolves fine

    bad = parse_project_data({**project_data, "base": "no-such-branch"})
    with pytest.raises(ConfigError, match="does not resolve"):
        bad.validate_base()


# ==========================================================================
# file round-trip
# ==========================================================================


def test_save_load_round_trip(project_data, projects_dir):
    project = parse_project_data(project_data)
    path = save_project(project, projects_dir)
    assert path.name == "capwrap.toml"
    assert path.exists()

    loaded = load_projects(projects_dir)
    assert loaded["capwrap"].to_dict() == project.to_dict()


def test_a_missing_projects_dir_loads_as_empty(projects_dir):
    assert load_projects(projects_dir) == {}


def test_delete_removes_the_file(project_data, projects_dir):
    project = parse_project_data(project_data)
    save_project(project, projects_dir)
    assert delete_project(project.name, projects_dir) is True
    assert load_projects(projects_dir) == {}
    assert delete_project(project.name, projects_dir) is False


def test_an_unparseable_file_is_skipped_not_fatal(project_data, projects_dir):
    save_project(parse_project_data(project_data), projects_dir)
    (projects_dir / "broken.toml").write_text("name = [")
    loaded = load_projects(projects_dir)
    assert list(loaded) == ["capwrap"]


# ==========================================================================
# compose() with a project
# ==========================================================================


def _compose_module(monkeypatch, tmp_path):
    from capwrap.teams import load_compose

    module = load_compose()
    monkeypatch.setattr(module, "BUILT", tmp_path)
    return module


def test_compose_without_a_project_keeps_todays_defaults(monkeypatch, tmp_path):
    """Backward compat: no project arguments, today's exact worktree mount."""
    module = _compose_module(monkeypatch, tmp_path)
    path = module.compose("implementer", "pragmatist")
    text = path.read_text()
    assert 'src        = "~/capwrap-demo/repo"' in text
    assert 'base       = "main"' in text
    assert "MY_TOKEN" not in text


def test_compose_with_a_project_applies_its_parameters(
    monkeypatch, tmp_path, project_data, git_repo
):
    module = _compose_module(monkeypatch, tmp_path)
    project = parse_project_data(project_data)
    mount_src = project.extra_mounts[0].src
    path = module.compose(
        "implementer",
        "pragmatist",
        routing=project.routing or "forward",
        **project.compose_kwargs(),
    )
    text = path.read_text()
    # The worktree forks from the project's source and base.
    assert f'src        = "{git_repo}"' in text
    assert 'base       = "main"' in text
    # Extra mounts appended after the standard ones.
    assert f'[[mounts]]\nsrc  = "{mount_src}"\ndest = "/foo"\nmode = "ro"' in text
    # Extra env merged into env_from_host.
    assert "MY_TOKEN" in text
    assert 'question_routing = "forward"' in text


def test_compose_project_routing_is_the_default(monkeypatch, tmp_path, project_data):
    module = _compose_module(monkeypatch, tmp_path)
    project = parse_project_data({**project_data, "routing": "auto"})
    path = module.compose(
        "implementer", "pragmatist", routing=project.routing, **project.compose_kwargs()
    )
    assert 'question_routing = "auto"' in path.read_text()


def test_compose_project_env_dedupes(monkeypatch, tmp_path, project_data):
    module = _compose_module(monkeypatch, tmp_path)
    project = parse_project_data({**project_data, "env": ["OPENCODE_API_KEY"]})
    path = module.compose("implementer", "pragmatist", **project.compose_kwargs())
    text = path.read_text()
    env_line = next(
        line for line in text.splitlines() if line.startswith("env_from_host")
    )
    assert env_line.count("OPENCODE_API_KEY") == 1


def test_a_project_config_loads(monkeypatch, tmp_path, project_data, git_repo):
    """The generated TOML is a valid capwrap config with the project's mount."""
    from capwrap.config import load_config

    module = _compose_module(monkeypatch, tmp_path)
    project = parse_project_data(project_data)
    path = module.compose("implementer", "pragmatist", **project.compose_kwargs())
    config = load_config(path)
    worktree = next(m for m in config.mounts if m.mode == "worktree")
    assert str(worktree.src) == str(git_repo)
    assert worktree.base == "main"
    extra = next(m for m in config.mounts if m.dest == "/foo")
    assert extra.mode == "ro"


# ==========================================================================
# daemon: spawn flow
# ==========================================================================


class _FakeSession:
    running = True

    def status(self):
        return {"running": True}

    async def terminate(self, grace=5.0):
        self.running = False
        return 0


@pytest.fixture
async def daemon(state_dir, projects_dir):
    from capwrap.daemon import Daemon

    d = Daemon(audit_path=Path(state_dir) / "audit.db", projects_dir=projects_dir)
    yield d
    await d.shutdown()


def _team_data():
    return {
        "name": "feature-x",
        "goal": "ship the parser rewrite",
        "members": [
            {"role": "implementer", "persona": "pragmatist"},
            {"role": "reviewer", "persona": "devils-advocate"},
        ],
    }


async def test_spawn_with_a_project_builds_its_config(
    daemon, monkeypatch, project_data, git_repo
):
    """A single spawn through the daemon resolves the project daemon-side."""
    from capwrap.web.app import create_app
    from fastapi.testclient import TestClient

    async def fake_start(name):
        container = daemon.containers[name]
        container.session = _FakeSession()
        return container

    monkeypatch.setattr(daemon, "start", fake_start)
    daemon.save_project(project_data)
    client = TestClient(create_app(daemon))

    response = client.post(
        "/api/spawn",
        json={
            "role": "implementer",
            "persona": "pragmatist",
            "project": "capwrap",
        },
    )
    assert response.status_code == 200, response.text

    config = daemon.containers["implementer-pragmatist"].config
    worktree = next(m for m in config.mounts if m.mode == "worktree")
    assert str(worktree.src) == str(git_repo)
    assert worktree.base == "main"
    assert any(m.dest == "/foo" and m.mode == "ro" for m in config.mounts)
    assert "MY_TOKEN" in config.runtime.env_from_host


async def test_spawn_with_an_unknown_project_is_a_clean_400(daemon, monkeypatch):
    from capwrap.web.app import create_app
    from fastapi.testclient import TestClient

    async def fake_start(name):
        return daemon.containers[name]

    monkeypatch.setattr(daemon, "start", fake_start)
    client = TestClient(create_app(daemon))
    response = client.post(
        "/api/spawn",
        json={"role": "implementer", "persona": "pragmatist", "project": "nope"},
    )
    assert response.status_code == 400, response.text
    body = response.json()
    assert "no such project" in (body.get("error") or body.get("detail") or "")


async def test_team_spawn_with_a_project_applies_to_members(
    daemon, monkeypatch, project_data, git_repo
):
    daemon.save_project(project_data)

    async def fake_start(name):
        container = daemon.containers[name]
        container.session = _FakeSession()
        return container

    monkeypatch.setattr(daemon, "start", fake_start)
    from capwrap.teams import load_compose, parse_team_data

    team = parse_team_data(_team_data(), load_compose())
    await daemon.spawn_team(team, project_name="capwrap")

    for name in ("implementer-pragmatist", "reviewer-devils-advocate"):
        config = daemon.containers[name].config
        worktree = next(m for m in config.mounts if m.mode == "worktree")
        assert str(worktree.src) == str(git_repo), name
        assert worktree.base == "main", name
        assert any(m.dest == "/foo" for m in config.mounts), name
        assert "MY_TOKEN" in config.runtime.env_from_host, name


async def test_team_spawn_with_an_unknown_project_refuses_everything(
    daemon, monkeypatch
):
    from capwrap.errors import CapwrapError
    from capwrap.teams import load_compose, parse_team_data

    team = parse_team_data(_team_data(), load_compose())
    with pytest.raises(CapwrapError, match="no such project"):
        await daemon.spawn_team(team, project_name="nope")
    assert daemon.containers == {}


async def test_team_edit_with_a_project_applies_to_new_members(
    daemon, monkeypatch, project_data, git_repo
):
    """A project on an edit reaches the members that edit (re)spawns."""
    daemon.save_project(project_data)

    async def fake_start(name):
        container = daemon.containers[name]
        container.session = _FakeSession()
        return container

    monkeypatch.setattr(daemon, "start", fake_start)
    from capwrap.teams import load_compose, parse_team_data

    team = parse_team_data(_team_data(), load_compose())
    await daemon.spawn_team(team)

    # Replace the implementer with a different persona: it is regenerated,
    # and the project must reach its config.
    edited = {
        **_team_data(),
        "members": [
            {"role": "implementer", "persona": "idealist"},
            {"role": "reviewer", "persona": "devils-advocate"},
        ],
    }
    result = await daemon.edit_team("feature-x", edited, project_name="capwrap")
    assert result["ok"], result
    config = daemon.containers["implementer-idealist"].config
    worktree = next(m for m in config.mounts if m.mode == "worktree")
    assert str(worktree.src) == str(git_repo)
    assert worktree.base == "main"


async def test_team_spawn_without_a_project_keeps_todays_behavior(daemon, monkeypatch):
    """Backward compat: no project named, the demo defaults stand."""

    async def fake_start(name):
        container = daemon.containers[name]
        container.session = _FakeSession()
        return container

    monkeypatch.setattr(daemon, "start", fake_start)
    from capwrap.teams import load_compose, parse_team_data

    team = parse_team_data(_team_data(), load_compose())
    await daemon.spawn_team(team)
    config = daemon.containers["implementer-pragmatist"].config
    worktree = next(m for m in config.mounts if m.mode == "worktree")
    assert str(worktree.src) == str(Path("~/capwrap-demo/repo").expanduser())
    assert worktree.base == "main"
    assert not any(m.dest == "/foo" for m in config.mounts)
    assert "MY_TOKEN" not in config.runtime.env_from_host


# ==========================================================================
# web: project CRUD
# ==========================================================================


async def test_project_crud_endpoints(daemon, git_repo):
    from capwrap.web.app import create_app
    from fastapi.testclient import TestClient

    data = {"name": "capwrap", "source": str(git_repo), "base": "main"}
    client = TestClient(create_app(daemon))

    saved = client.post("/api/projects", json={"project": data})
    assert saved.status_code == 200, saved.text
    assert (daemon.projects_dir / "capwrap.toml").exists()

    listed = client.get("/api/projects").json()
    assert [p["name"] for p in listed] == ["capwrap"]

    gone = client.delete("/api/projects/capwrap")
    assert gone.status_code == 200, gone.text
    assert client.get("/api/projects").json() == []
    assert not (daemon.projects_dir / "capwrap.toml").exists()

    missing = client.delete("/api/projects/capwrap")
    assert missing.status_code == 400, missing.text


async def test_project_crud_refuses_an_invalid_project(daemon, project_data):
    from capwrap.web.app import create_app
    from fastapi.testclient import TestClient

    client = TestClient(create_app(daemon))
    bad = client.post(
        "/api/projects",
        json={"project": {**project_data, "routing": "sideways"}},
    )
    assert bad.status_code == 400, bad.text
    assert daemon.projects == {}


async def test_projects_list_endpoint(daemon, project_data):
    from capwrap.web.app import create_app
    from fastapi.testclient import TestClient

    daemon.save_project(project_data)
    client = TestClient(create_app(daemon))
    listed = client.get("/api/projects").json()
    assert listed[0]["name"] == "capwrap"
    assert listed[0]["extra_mounts"][0]["dest"] == "/foo"


# ==========================================================================
# CLI
# ==========================================================================


def test_cli_projects_parser_accepts_the_subcommands():
    from capwrap.cli import build_parser

    for argv in (
        ["projects", "list"],
        ["projects", "add", "p.toml"],
        ["projects", "remove", "capwrap"],
    ):
        args = build_parser().parse_args(argv)
        assert args.projects_command == argv[1]


def test_project_toml_round_trips_through_the_cli_parser(
    project_data, tmp_path, monkeypatch
):
    """`capwrap projects add` parses the file locally before POSTing it."""
    project = parse_project_data(project_data)
    path = save_project(project, tmp_path)
    reloaded = parse_project(path)
    assert reloaded.to_dict() == project.to_dict()
