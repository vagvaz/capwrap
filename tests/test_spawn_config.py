"""Spawning from an edited config: the spawn dialog's editable preview.

The console's spawn dialog shows the generated TOML in an editable textarea;
when the operator has edited it, POST /api/spawn receives the TOML text itself
(``config``), which is validated through the same loader compose's own output
goes through and spawned through the same operator path. These tests cover
that path, the preview's extra_prompt, and the role/persona library endpoints.

Like the team tests, `daemon.start` is mocked so no sandbox is needed; the
interesting assertions are about the config that results.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from capwrap.daemon import Daemon


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


@pytest.fixture
def compose_module(monkeypatch, tmp_path):
    """The real compose module, with BUILT pointed at a temp directory.

    The web app re-imports compose.py per request via `_load_compose`, so the
    patch replaces that loader with one returning this module instance --
    otherwise the patch on BUILT would not reach the endpoints.
    """
    import capwrap.web.app as web_app
    from capwrap.teams import load_compose

    module = load_compose()
    monkeypatch.setattr(module, "BUILT", tmp_path / "built")
    monkeypatch.setattr(web_app, "_load_compose", lambda: module)
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


def _preview(client, **params):
    defaults = {"role": "implementer", "persona": "pragmatist"}
    response = client.get("/api/compose/preview", params={**defaults, **params})
    assert response.status_code == 200, response.text
    return response.json()


# ==========================================================================
# spawn from an edited config
# ==========================================================================


async def test_spawn_from_an_edited_config_spawns_the_edit(
    client, daemon, compose_module
):
    """The edited TOML is what runs: the edit survives into the live config."""
    toml = _preview(client)["toml"]
    # The operator's edits: a model line, and an extra auto-allow pattern.
    edited = toml.replace(
        "tty       = true",
        'tty       = true\nmodel     = "opencode-go/glm-5.3-flash"',
    ).replace("auto_allow = [", 'auto_allow = ["Bash(make *)", ')
    assert edited != toml

    response = client.post("/api/spawn", json={"config": edited})
    assert response.status_code == 200, response.text

    config = daemon.containers["implementer-pragmatist"].config
    assert config.runtime.model == "opencode-go/glm-5.3-flash"
    assert "Bash(make *)" in config.runtime.auto_allow


async def test_relative_file_srcs_resolve_from_the_built_dir(
    client, daemon, compose_module
):
    """A submitted TOML's relative [[files]] srcs resolve against compose's
    BUILT directory -- the same place compose wrote them -- not the daemon's
    CWD. The body is written to a temp file there for `load_config` to read,
    and the temp file is removed again."""
    built = compose_module.BUILT
    built.mkdir(parents=True, exist_ok=True)
    (built / "pi-mcp.json").write_text("{}\n")

    toml = (
        'name = "mcp-user"\n'
        "\n"
        "[[files]]\n"
        'dest = "/work/pi-mcp.json"\n'
        'src  = "pi-mcp.json"\n'
    )
    response = client.post("/api/spawn", json={"config": toml})
    assert response.status_code == 200, response.text

    config = daemon.containers["mcp-user"].config
    assert config.files[0].src == (built / "pi-mcp.json").resolve()

    # The temp file the body was staged through did not linger.
    assert list(built.glob(".spawn-*")) == []


async def test_an_invalid_toml_is_a_400_with_the_parser_message(client):
    response = client.post("/api/spawn", json={"config": "name = [not valid"})
    assert response.status_code == 400, response.text
    assert "invalid TOML" in response.json()["detail"]


async def test_a_config_that_fails_validation_is_a_400(client, daemon):
    """Valid TOML, invalid config: the loader's message comes back."""
    response = client.post("/api/spawn", json={"config": 'name = "bad name!"'})
    assert response.status_code == 400, response.text
    assert "must be non-empty and use only" in response.json()["error"]
    assert daemon.containers == {}


async def test_an_empty_config_is_a_400(client):
    response = client.post("/api/spawn", json={"config": "   \n  "})
    assert response.status_code == 400, response.text


async def test_a_name_collision_is_a_409(client, daemon, compose_module, tmp_path):
    from capwrap.config import load_config_data

    daemon.register(
        load_config_data({"name": "implementer-pragmatist"}, base_dir=tmp_path)
    )
    toml = _preview(client)["toml"]
    response = client.post("/api/spawn", json={"config": toml})
    assert response.status_code == 409, response.text
    assert "already exists" in response.json()["detail"]


async def test_the_form_shape_still_spawns_and_carries_extra_prompt(
    client, daemon, compose_module
):
    """The form shape keeps working, and custom instructions reach the prompt."""
    response = client.post(
        "/api/spawn",
        json={
            "role": "implementer",
            "persona": "pragmatist",
            "extra_prompt": "Keep a decision log.",
        },
    )
    assert response.status_code == 200, response.text
    assert "implementer-pragmatist" in daemon.containers
    prompt = (compose_module.BUILT / "implementer-pragmatist.md").read_text()
    assert "Keep a decision log." in prompt


async def test_neither_shape_is_a_400(client):
    response = client.post("/api/spawn", json={})
    assert response.status_code == 400, response.text


# ==========================================================================
# preview with extra_prompt
# ==========================================================================


async def test_preview_honors_extra_prompt(client, compose_module):
    """extra_prompt reaches the generated prompt file, which the preview
    returns alongside the TOML so the operator sees what would run."""
    plain = _preview(client)
    extra = _preview(client, extra_prompt="Always write a plan first.")

    assert "Always write a plan first." not in plain["prompt"]
    assert "Always write a plan first." in extra["prompt"]
    # The TOML body itself is unchanged by instructions: they live in the
    # prompt file it points at.
    assert extra["toml"] == plain["toml"]


# ==========================================================================
# role/persona library
# ==========================================================================


async def test_role_and_persona_markdown_endpoints(client):
    role = client.get("/api/compose/role/reviewer")
    assert role.status_code == 200, role.text
    body = role.json()
    assert body["name"] == "reviewer"
    assert body["markdown"].strip()

    persona = client.get("/api/compose/persona/pragmatist")
    assert persona.status_code == 200, persona.text
    assert persona.json()["name"] == "pragmatist"
    assert persona.json()["markdown"].strip()


async def test_unknown_role_and_persona_are_a_404(client):
    assert client.get("/api/compose/role/nope").status_code == 404
    assert client.get("/api/compose/persona/nope").status_code == 404
