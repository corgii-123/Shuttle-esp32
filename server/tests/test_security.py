"""What the outside (through the Cloudflare Tunnel) can reach, and the bad-token lockout."""
import aiohttp

from conftest import TOKEN

TUNNEL = {"CF-Ray": "8c1e1f2a3b4c5d6e-HKG", "CF-Connecting-IP": "203.0.113.9"}
AUTH = {"Authorization": f"Bearer {TOKEN}"}


async def test_phone_api_and_status_are_local_only(server_factory):
    srv = await server_factory()
    base = srv.url.replace("ws://", "http://")
    async with aiohttp.ClientSession() as http:
        for path in ("/status", "/api/phone/screenshot", "/api/phones"):
            async with http.get(base + path, headers={**TUNNEL, **AUTH}) as r:
                assert r.status == 404, path  # even with the right token
        async with http.get(base + "/status", headers=AUTH) as r:
            assert r.status == 200  # a program on this Mac


async def test_remote_api_switch(server_factory):
    srv = await server_factory(server={"remote_api": True})
    base = srv.url.replace("ws://", "http://")
    async with aiohttp.ClientSession() as http:
        async with http.get(base + "/status", headers={**TUNNEL, **AUTH}) as r:
            assert r.status == 200
        async with http.get(base + "/status", headers=TUNNEL) as r:
            assert r.status == 401


async def test_health_tells_the_outside_nothing(server_factory):
    srv = await server_factory()
    base = srv.url.replace("ws://", "http://")
    async with aiohttp.ClientSession() as http:
        async with http.get(base + "/health", headers=TUNNEL) as r:
            assert r.status == 200 and await r.json() == {"status": "ok"}
        async with http.get(base + "/health") as r:
            assert "devices_ctrl" in await r.json()


async def test_device_channel_works_through_the_tunnel(server_factory):
    srv = await server_factory()
    async with aiohttp.ClientSession() as http:
        async with http.ws_connect(srv.url + "/ws/ctrl", headers={**TUNNEL, **AUTH, "Device-Id": "dev-t"}) as ws:
            await ws.send_json({"type": "hello", "version": 1, "transport": "websocket",
                                "audio_params": {"format": "opus", "sample_rate": 16000}})
            hello = await ws.receive_json(timeout=5)
            assert hello["type"] == "hello"


async def test_repeated_bad_tokens_block_the_address(server_factory):
    srv = await server_factory()
    guard = srv.shuttle.guard
    base = srv.url.replace("ws://", "http://")
    bad = {**TUNNEL, "Authorization": "Bearer wrong"}
    async with aiohttp.ClientSession() as http:
        for _ in range(guard.max_failures):
            async with http.get(base + "/ws/ctrl", headers=bad) as r:
                assert r.status == 401
        async with http.get(base + "/ws/ctrl", headers={**TUNNEL, **AUTH}) as r:
            assert r.status == 429  # blocked, even with the right token now
        other = {**AUTH, "CF-Ray": "x", "CF-Connecting-IP": "198.51.100.7"}
        async with http.ws_connect(srv.url + "/ws/ctrl", headers=other) as ws:
            assert not ws.closed  # another address is not affected
        for _ in range(guard.max_failures + 5):  # programs on this Mac are never blocked
            async with http.get(base + "/status", headers={"Authorization": "Bearer wrong"}) as r:
                assert r.status == 401
        async with http.get(base + "/status", headers=AUTH) as r:
            assert r.status == 200
    assert srv.shuttle.counters["blocked_clients"] == 1
