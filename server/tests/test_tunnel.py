"""ADB tunnel: device WebSocket <-> local TCP port, using the simulator's bridge and a fake adbd."""
import asyncio

import aiohttp
import pytest

from conftest import TOKEN, adb_calls
from sim_device import AdbBridgeSim


async def start_fake_adbd():
    """Echo server standing in for the phone's adbd: replies 'PHONE:' + data."""
    conns = []

    async def handle(reader, writer):
        conns.append(writer)
        while data := await reader.read(65536):
            writer.write(b"PHONE:" + data)
            await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1], conns


async def wait_for(pred, timeout=5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not pred():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.05)


async def test_bytes_flow_both_ways(server_factory, fake_adb_env):
    srv = await server_factory()
    adbd, phone_port, conns = await start_fake_adbd()
    async with aiohttp.ClientSession() as http:
        bridge = AdbBridgeSim(http, srv.url, TOKEN, "dev-1", f"127.0.0.1:{phone_port}")
        task = asyncio.create_task(bridge.run_forever())
        tunnels = srv.shuttle.tunnels
        await wait_for(lambda: "dev-1" in tunnels.tunnels and tunnels.tunnels["dev-1"].phone_link == "connected")
        tunnel = tunnels.tunnels["dev-1"]

        # the supervisor ran `adb connect 127.0.0.1:<port>`
        await wait_for(lambda: any(c[:1] == ["connect"] for c in adb_calls(fake_adb_env)))

        # act as the Mac's adb server
        reader, writer = await asyncio.open_connection("127.0.0.1", tunnel.port)
        writer.write(b"CNXN" + bytes(range(256)) * 400)  # ~100 KB
        await writer.drain()
        expected = b"PHONE:"
        got = b""
        while len(got) < 6 + 4 + 256 * 400:
            chunk = await asyncio.wait_for(reader.read(65536), 5)
            assert chunk
            got += chunk
        assert got.startswith(expected)
        assert b"CNXN" in got[:20]
        assert tunnel.paired

        # adb side closes -> session closes -> device reconnects with a fresh session
        sessions_before = tunnel.sessions_total
        writer.close()
        await wait_for(lambda: tunnel.sessions_total > sessions_before and tunnel.device_connected, 10)
        assert not tunnel.paired

        # a new adb connection pairs with the new session
        r2, w2 = await asyncio.open_connection("127.0.0.1", tunnel.port)
        w2.write(b"again")
        await w2.drain()
        assert (await asyncio.wait_for(r2.read(100), 5)).startswith(b"PHONE:again")

        # phone side closes -> adb connection is closed
        for c in conns:
            c.close()
        assert await asyncio.wait_for(r2.read(100), 5) == b""
        w2.close()
        task.cancel()
    adbd.close()


async def test_adb_connection_without_device_is_closed(server_factory):
    srv = await server_factory(adb={"pair_wait_s": 0.5})
    tunnel = srv.shuttle.tunnels.get_or_create("lonely")
    await tunnel.ensure_started()
    reader, writer = await asyncio.open_connection("127.0.0.1", tunnel.port)
    assert await asyncio.wait_for(reader.read(10), 3) == b""
    writer.close()
    await tunnel.stop()


async def test_phone_unreachable_reported(server_factory):
    srv = await server_factory()
    async with aiohttp.ClientSession() as http:
        bridge = AdbBridgeSim(http, srv.url, TOKEN, "dev-2", "127.0.0.1:1")  # nothing listens on port 1
        assert await bridge.run_once() is None
    tunnel = srv.shuttle.tunnels.tunnels["dev-2"]
    await wait_for(lambda: tunnel.phone_link == "unreachable")


async def test_tunnel_cleanup_after_grace(server_factory, fake_adb_env):
    srv = await server_factory(adb={"device_grace_s": 0.3})
    adbd, phone_port, _ = await start_fake_adbd()
    async with aiohttp.ClientSession() as http:
        bridge = AdbBridgeSim(http, srv.url, TOKEN, "dev-3", f"127.0.0.1:{phone_port}")
        task = asyncio.create_task(bridge.run_once())
        tunnels = srv.shuttle.tunnels
        await wait_for(lambda: "dev-3" in tunnels.tunnels and tunnels.tunnels["dev-3"].device_connected)
        tunnel = tunnels.tunnels["dev-3"]
        for s in [tunnel._session]:
            await s.ws.close()
        await task
    await wait_for(lambda: any(c[:1] == ["disconnect"] for c in adb_calls(fake_adb_env)), 5)
    assert not tunnel.active
    adbd.close()


@pytest.mark.parametrize("path", ["/ws/adb", "/ws/ctrl", "/status"])
async def test_auth_required(server_factory, path):
    srv = await server_factory()
    http_url = srv.url.replace("ws://", "http://") + path
    async with aiohttp.ClientSession() as http:
        async with http.get(http_url) as r:
            assert r.status == 401
        async with http.get(http_url, headers={"Authorization": "Bearer wrong"}) as r:
            assert r.status == 401
