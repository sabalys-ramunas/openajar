# ruff: noqa: F811 - `node` is test_environments_saves's fixture, and `proxied` test_environments_comeback's
"""environment_port with several ports and the VM's own numbers (openajar.environments.ports): a page at
localhost:5173 whose code calls its API at localhost:3000 reaches the VM's 3000 only when 3000 here is the
VM's too. The machine is test_environments_saves's, its SOCKS proxy test_environments_comeback's fake, which
sends every asked port to one dev server here."""

from __future__ import annotations

import socket
from types import SimpleNamespace

import httpx
import pytest
from test_environments_comeback import proxied  # noqa: F401 - the fixture
from test_environments_saves import node  # noqa: F401 - the fixture

from openajar.environments import ports
from openajar.environments.tunnels import free_port
from openajar.policy.wrap import ToolCallError


def listening_on(mgr, *numbers: int):
    """Views.ports as the machine would answer with these ports listening (each forwarded one with its link)."""
    async def fake(env):
        return ports.with_links(mgr, env.id, [{"port": n, "process": "node", "link": None} for n in numbers])
    return fake


async def get(url: str) -> str:
    async with httpx.AsyncClient() as http:
        return (await http.get(url)).text


async def test_several_ports_keep_their_numbers_here_so_the_pages_calls_reach_the_vm(node, proxied, monkeypatch):
    node["mgr"].on_laptop = True
    web, api = free_port(), free_port()
    monkeypatch.setattr(node["mgr"], "ports", listening_on(node["mgr"], web, api))
    got = await node["call"]("environment_port", ports=[web, api], same_numbers=True)
    assert [(f["port"], f["local_port"], f["same"], f["listening"]) for f in got["forwards"]] == [
        (web, web, True, True), (api, api, True, True)]
    assert got["forwards"][0]["url"] == f"http://localhost:{web}/"  # the origin the app knows from the VM
    assert "tunnel" not in got and "why" not in got["next"]
    assert await get(f"http://127.0.0.1:{api}/") == "hello"
    assert proxied["socks"].asked == [api]
    env = await node["call"]("environment")
    assert {p["port"]: p["link"] for p in env["ports"]} == {web: f"http://localhost:{web}/",
                                                            api: f"http://localhost:{api}/"}


async def test_a_port_taken_here_gets_another_and_says_what_breaks(node, proxied, monkeypatch):
    """The other program keeps its localhost (a wildcard listener, as dev servers open them); only that
    port moves, and the call does not fail."""
    node["mgr"].on_laptop = True
    web = free_port()
    theirs = socket.socket()
    theirs.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    theirs.bind(("0.0.0.0", 0))
    theirs.listen()
    api = theirs.getsockname()[1]
    monkeypatch.setattr(node["mgr"], "ports", listening_on(node["mgr"], web, api))
    try:
        got = await node["call"]("environment_port", ports=[web, api], same_numbers=True)
        first, second = got["forwards"]
        assert first["same"] is True and first["local_port"] == web
        assert second["same"] is False and second["local_port"] != api
        assert second["url"] == f"http://127.0.0.1:{second['local_port']}/"
        assert f"port {api} on this computer is taken by another program" in second["why"]
        assert f"localhost:{api} does not reach the VM" in second["why"] and "proxy" in second["why"]
        assert f"Port {api} could not keep its number here" in got["next"]
        assert "—" not in second["why"] + got["next"]
        assert await get(second["url"]) == "hello"
    finally:
        theirs.close()


async def test_a_free_port_link_moves_to_the_same_number_when_asked(node, proxied, monkeypatch):
    node["mgr"].on_laptop = True
    web = free_port()
    monkeypatch.setattr(node["mgr"], "ports", listening_on(node["mgr"], web))
    first = await node["call"]("environment_port", port=web)
    assert first["local_port"] != web and "same" not in first
    again = await node["call"]("environment_port", port=web, same_numbers=True)
    assert again["local_port"] == web and again["same"] is True and again["url"] == f"http://localhost:{web}/"
    with pytest.raises(httpx.ConnectError):  # the free port's link ended
        await get(first["url"])
    assert await get(again["url"]) == "hello"
    assert (await node["call"]("environment_port", port=web, same_numbers=True))["local_port"] == web


async def test_one_port_says_which_other_ports_the_vm_listens_on(node, proxied, monkeypatch):
    node["mgr"].on_laptop = True
    monkeypatch.setattr(node["mgr"], "ports", listening_on(node["mgr"], 3000, 5173))
    got = await node["call"]("environment_port", port=5173)
    assert got["also_listening"] == [3000] and got["listening"] is True
    assert "environment_port(ports=[5173, 3000], same_numbers=true)" in got["next"]
    monkeypatch.setattr(node["mgr"], "ports", listening_on(node["mgr"], 5173))
    alone = await node["call"]("environment_port", port=5173)
    assert "also_listening" not in alone and "ports=" not in alone["next"]


async def test_on_a_server_node_one_ssh_line_carries_every_port(node, proxied, monkeypatch):
    monkeypatch.setenv("OPENAJAR_SSH_HOST", "my-node")
    monkeypatch.setattr(node["mgr"], "ports", listening_on(node["mgr"]))
    web, api = free_port(), free_port()
    got = await node["call"]("environment_port", ports=[web, api], same_numbers=True)
    assert got["tunnel"] == f"ssh -N -L {web}:127.0.0.1:{web} -L {api}:127.0.0.1:{api} my-node"
    assert all("tunnel" not in f for f in got["forwards"])
    assert got["tunnel"] in got["next"] and "address is in use" in got["next"]
    assert f"Nothing listens on port {web}, {api} yet" in got["next"]
    single = await node["call"]("environment_port", port=web)  # the one-port shape, as before
    assert single["tunnel"] == f"ssh -N -L {web}:127.0.0.1:{web} my-node"


async def test_no_port_or_a_bad_one_is_refused(node, proxied):
    with pytest.raises(ToolCallError, match="ports="):
        await node["call"]("environment_port")
    with pytest.raises(ToolCallError):
        await node["call"]("environment_port", ports=[3000, 22])


def test_another_environments_link_counts_as_taken():
    fwd = SimpleNamespace(port=4321, serving=True)
    mgr = SimpleNamespace(_port_forwards={"e1": {8080: fwd}}, on_laptop=True)
    assert ports.taken_here(mgr, 4321) == "another environment's link"
    assert "on this computer" in ports.not_same_words(mgr, 4321, 5555, "another environment's link")
