"""Capability-governed network access.

Two halves, tested separately: the kernel decides whether a container may reach
a destination, and the proxy is the only route by which it could. The first is
pure policy; the second is a server, and the tests here drive it over a real
socket rather than trusting its parsing to a mock.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from capwrap.config import load_config_data
from capwrap.errors import ConfigError
from capwrap.kernel.kernel import CapKernel
from capwrap.net import proxy as proxy_mod
from capwrap.net.proxy import NetProxy, ProxyError, target_of
from capwrap.paths import GUEST_PROXY_PORT, GUEST_PROXY_SOCKET


def config(name: str, rules: list[dict], caps: dict | None = None, **extra):
    return load_config_data(
        {"name": name, "caps": {**(caps or {}), "network": rules}, **extra},
        base_dir=Path("/tmp"),
    )


def slot_labelled(kernel: CapKernel, actor: str, label: str) -> int:
    for info in kernel.cap_list(actor):
        if info.label == label:
            return info.slot
    raise AssertionError(f"{actor} holds no capability labelled {label!r}")


# ==========================================================================
# what a rule means
# ==========================================================================


@pytest.fixture
def kernel():
    k = CapKernel()
    k.register_container(config("a", [
        {"name": "pypi", "pattern": r"(pypi\.org|files\.pythonhosted\.org):443"},
        {"name": "docs", "pattern": r"docs\.example\.com:443"},
    ]))
    return k


@pytest.mark.parametrize("host,port,allowed", [
    ("pypi.org", 443, True),
    ("files.pythonhosted.org", 443, True),
    ("docs.example.com", 443, True),
    # Same host, different port: 443 and 22 are not the same authority.
    ("pypi.org", 22, False),
    ("pypi.org", 8443, False),
    # A prefix or suffix must not be enough, or every rule is a wildcard.
    ("evil-pypi.org", 443, False),
    ("pypi.org.attacker.example", 443, False),
    ("notdocs.example.com", 443, False),
    ("github.com", 443, False),
])
def test_a_rule_matches_the_whole_authority_and_nothing_more(
    kernel, host, port, allowed
):
    """Patterns are anchored at both ends, always.

    Unanchored, `pypi\\.org:443` would also accept
    `pypi.org:443.attacker.example` -- the exact opposite of what the person who
    wrote the rule believed it said.
    """
    assert kernel.net_allows("a", host, port)["allowed"] is allowed


def test_a_container_with_no_rules_reaches_nothing(kernel):
    """No network capability is the same position as no capability at all."""
    kernel.register_container(load_config_data({"name": "b"}, base_dir=Path("/tmp")))
    assert kernel.net_allows("b", "pypi.org", 443)["allowed"] is False
    assert kernel.net_rules("b") == []


def test_both_decisions_are_audited(kernel):
    kernel.net_allows("a", "pypi.org", 443)
    kernel.net_allows("a", "github.com", 443)

    entries = [e for e in kernel.audit.tail(limit=50) if e["op"] == "net.connect"]
    by_target = {e["target"]: e for e in entries}
    assert by_target["pypi.org:443"]["allowed"] == 1
    assert by_target["github.com:443"]["allowed"] == 0


def test_a_rule_without_connect_cannot_be_used(kernel):
    """`inspect` on a rule says it exists; it does not open anything."""
    kernel.register_container(config("looker", [
        {"name": "pypi", "pattern": r"pypi\.org:443", "rights": ["inspect"]},
    ]))
    assert kernel.net_allows("looker", "pypi.org", 443)["allowed"] is False


def test_revoking_a_rule_closes_the_hole_at_once(kernel):
    assert kernel.net_allows("a", "pypi.org", 443)["allowed"] is True
    slot = next(s for s, rule in kernel.net_rules("a") if rule.rule == "pypi")
    kernel.cap_revoke("a", slot, include_self=True)
    assert kernel.net_allows("a", "pypi.org", 443)["allowed"] is False


def test_a_child_can_be_given_some_of_a_parents_reach_but_never_more(kernel):
    """Narrowing is delegating a subset of your rules, and it is checked.

    Rules are separate objects precisely so this works. Subsetting a list is
    decidable; asking whether one regex is contained in another is not, and a
    security model should not rest on a question nobody can answer.
    """
    kernel.register_container(load_config_data({"name": "child"}, base_dir=Path("/tmp")))
    kernel.register_container(config(
        "boss",
        [
            {"name": "pypi", "pattern": r"pypi\.org:443",
             "rights": ["connect", "delegate"]},
            {"name": "docs", "pattern": r"docs\.example\.com:443",
             "rights": ["connect", "delegate"]},
        ],
        caps={"peers": [{"container": "child", "rights": ["send"]}]},
    ))

    child_slot = slot_labelled(kernel, "boss", "peer:child")
    pypi_slot = next(s for s, rule in kernel.net_rules("boss") if rule.rule == "pypi")
    kernel.cap_delegate("boss", child_slot, pypi_slot, ["connect"])

    assert kernel.net_allows("child", "pypi.org", 443)["allowed"] is True
    # The rule it was not handed is not reachable, and never was: there is no
    # way for it to name a rule it holds no capability on.
    assert kernel.net_allows("child", "docs.example.com", 443)["allowed"] is False


def test_a_child_cannot_be_handed_a_rule_its_parent_may_not_pass_on(kernel):
    """`connect` without `delegate` is a rule you may use but not spread."""
    from capwrap.errors import InsufficientRights

    kernel.register_container(load_config_data({"name": "kid"}, base_dir=Path("/tmp")))
    kernel.register_container(config(
        "keeper",
        [{"name": "pypi", "pattern": r"pypi\.org:443", "rights": ["connect"]}],
        caps={"peers": [{"container": "kid", "rights": ["send"]}]},
    ))

    kid_slot = slot_labelled(kernel, "keeper", "peer:kid")
    pypi_slot = next(s for s, rule in kernel.net_rules("keeper") if rule.rule == "pypi")
    with pytest.raises(InsufficientRights):
        kernel.cap_delegate("keeper", kid_slot, pypi_slot, ["connect"])
    assert kernel.net_allows("kid", "pypi.org", 443)["allowed"] is False


# ==========================================================================
# configuration
# ==========================================================================


def test_rules_imply_the_proxy_and_leave_the_namespace_unshared():
    cfg = config("n", [{"name": "x", "pattern": "example.com:443"}])
    assert cfg.proxied_network is True
    assert cfg.sandbox.network is False


def test_unrestricted_network_and_rules_together_are_refused():
    """Silently ignoring the rules would be the worst of the options.

    `sandbox.network = true` hands over the host's namespace, so the agent could
    simply connect around the proxy -- the config would read as restricted and
    not be.
    """
    with pytest.raises(ConfigError, match="could not be enforced"):
        config("n", [{"name": "x", "pattern": "a:1"}], sandbox={"network": True})


def test_a_rule_that_is_not_a_regex_is_rejected_where_it_is_written():
    with pytest.raises(ConfigError, match="valid regex"):
        config("n", [{"name": "x", "pattern": "([unclosed"}])


def test_the_sandbox_gets_a_proxy_it_can_name():
    from capwrap.paths import ContainerPaths
    from capwrap.runtime import bwrap as bwrap_mod

    cfg = config("n", [{"name": "x", "pattern": "example.com:443"}])
    env = bwrap_mod.build_env(cfg)
    assert env["HTTPS_PROXY"] == f"http://127.0.0.1:{GUEST_PROXY_PORT}"
    assert env["https_proxy"] == env["HTTPS_PROXY"]
    # The relay itself must not be proxied through itself.
    assert "127.0.0.1" in env["NO_PROXY"]

    command = bwrap_mod._entry_command(cfg)
    assert "netrelay.py" in " ".join(command)
    assert command[-1] == cfg.runtime.command[-1]
    assert GUEST_PROXY_SOCKET in command


def test_a_container_without_rules_gets_no_proxy_at_all():
    from capwrap.runtime import bwrap as bwrap_mod

    cfg = load_config_data({"name": "n"}, base_dir=Path("/tmp"))
    env = bwrap_mod.build_env(cfg)
    assert "HTTPS_PROXY" not in env
    assert bwrap_mod._entry_command(cfg) == cfg.runtime.command


# ==========================================================================
# parsing what the client asked for
# ==========================================================================


@pytest.mark.parametrize("method,target,expected", [
    ("CONNECT", "pypi.org:443", ("pypi.org", 443)),
    ("CONNECT", "pypi.org", ("pypi.org", 443)),
    ("CONNECT", "[::1]:8080", ("::1", 8080)),
    ("GET", "http://example.com/a/b?c=d", ("example.com", 80)),
    ("GET", "http://example.com:8080/", ("example.com", 8080)),
    ("GET", "https://example.com/", ("example.com", 443)),
    # Userinfo is not the destination. `pypi.org@evil.example` reaches evil.
    ("GET", "http://pypi.org@evil.example/", ("evil.example", 80)),
])
def test_the_destination_is_read_the_way_the_client_meant_it(method, target, expected):
    assert target_of(method, target) == expected


def test_a_request_that_is_not_a_proxy_request_is_refused():
    with pytest.raises(ProxyError):
        target_of("GET", "/relative/path")


# ==========================================================================
# the proxy, over a real socket
# ==========================================================================


async def _ask_proxy(socket_path: Path, request: bytes) -> bytes:
    reader, writer = await asyncio.open_unix_connection(str(socket_path))
    try:
        writer.write(request)
        await writer.drain()
        return await asyncio.wait_for(reader.read(4096), timeout=5)
    finally:
        writer.close()


async def test_the_proxy_refuses_a_destination_with_no_capability(tmp_path):
    """And says so in HTTP, rather than dropping the connection.

    A dropped connection sends the agent hunting for a network fault; a 403
    naming the rules it does hold tells it to ask for the one it needs.
    """
    decisions = []

    def decide(container, host, port):
        decisions.append((container, host, port))
        return {"allowed": False, "held_rules": ["docs"]}

    proxy = NetProxy("netty", decide=decide)
    socket_path = tmp_path / "proxy.sock"
    await proxy.start(socket_path)
    try:
        reply = await _ask_proxy(
            socket_path, b"CONNECT pypi.org:443 HTTP/1.1\r\nHost: pypi.org\r\n\r\n"
        )
    finally:
        await proxy.stop()

    assert decisions == [("netty", "pypi.org", 443)]
    assert reply.startswith(b"HTTP/1.1 403")
    assert b"no network capability for pypi.org:443" in reply
    assert b"Rules held: docs" in reply
    assert b"capctl request net_rule" in reply


async def test_the_proxy_tunnels_a_destination_it_is_allowed(tmp_path):
    """CONNECT to something permitted gets a tunnel, and bytes cross it."""
    echo = await asyncio.start_server(
        lambda r, w: _echo(r, w), host="127.0.0.1", port=0
    )
    port = echo.sockets[0].getsockname()[1]

    proxy = NetProxy("netty", decide=lambda *_: {"allowed": True, "rule": "local"})
    socket_path = tmp_path / "proxy.sock"
    await proxy.start(socket_path)
    try:
        reader, writer = await asyncio.open_unix_connection(str(socket_path))
        writer.write(f"CONNECT 127.0.0.1:{port} HTTP/1.1\r\n\r\n".encode())
        await writer.drain()
        established = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
        assert established.startswith(b"HTTP/1.1 200")

        writer.write(b"through the tunnel")
        await writer.drain()
        assert await asyncio.wait_for(reader.read(64), timeout=5) == b"through the tunnel"
        writer.close()
    finally:
        await proxy.stop()
        echo.close()
        await echo.wait_closed()


async def test_a_malformed_request_gets_an_error_not_a_hang(tmp_path):
    proxy = NetProxy("netty", decide=lambda *_: {"allowed": True})
    socket_path = tmp_path / "proxy.sock"
    await proxy.start(socket_path)
    try:
        reply = await _ask_proxy(socket_path, b"GET /relative HTTP/1.1\r\n\r\n")
    finally:
        await proxy.stop()
    assert reply.startswith(b"HTTP/1.1 400")


async def test_the_decision_is_taken_per_request_not_per_connection(tmp_path):
    """Keep-alive must not let a second destination ride in on the first's check.

    The proxy forces `Connection: close` upstream for plain HTTP precisely so a
    client cannot send a second absolute-URI request for a different host down a
    connection that has already been decided.
    """
    assert "Connection: close" in proxy_mod._origin_form(
        "GET", "http://example.com/", "HTTP/1.1", ["Host: example.com"]
    ).decode()


def test_hop_by_hop_headers_do_not_reach_the_origin():
    out = proxy_mod._origin_form(
        "GET", "http://example.com/x", "HTTP/1.1",
        ["Host: example.com", "Proxy-Authorization: secret", "Accept: */*"],
    ).decode()
    assert "Proxy-Authorization" not in out
    assert "Accept: */*" in out
    assert out.startswith("GET /x HTTP/1.1")


async def _echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            chunk = await reader.read(4096)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
    except (ConnectionResetError, BrokenPipeError):
        pass
    finally:
        writer.close()
