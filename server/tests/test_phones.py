"""Several phones: registry + selection policy, and switching through the tunnel end to end."""
import asyncio
import contextlib
import json

import aiohttp

from conftest import TOKEN, adb_calls
from sim_device import AdbBridgeSim
from test_tunnel import start_fake_adbd, wait_for

from shuttle_server.adb.adb_cli import AdbResult
from shuttle_server.adb.phones import (IDENTIFY_CMD, Candidates, PhoneIdentity, PhoneRegistry, choose_host,
                                       next_host, parse_identity)
from shuttle_server.pipeline.phone_commands import RE_NEXT, RE_SELECT, _only_names


# ---------------------------------------------------------------------------- unit
def test_parse_identity():
    ident = parse_identity("R5CT123\n\nabc\nsamsung\nSM-S9180\nGalaxy S24\n")
    assert ident == PhoneIdentity("R5CT123", "samsung", "SM-S9180", "Galaxy S24")
    # no ro.serialno: fall back to android_id; "null" means unset
    ident = parse_identity("\n\n9f8e7d\nXiaomi\n23127PN0CC\nnull\n")
    assert ident.serial == "9f8e7d" and ident.device_name == ""
    assert parse_identity("\n\n\n") is None


def test_registry_persists_and_names(tmp_path):
    path = tmp_path / "phones.json"
    reg = PhoneRegistry(str(path), aliases={"S2": "工作机"})
    a = reg.seen(PhoneIdentity("S1", "Xiaomi", "Xiaomi 14"), "192.168.1.50", "home")
    b = reg.seen(PhoneIdentity("S2", "Google", "Pixel 8"), "192.168.1.51", "home")
    assert a.name == "Xiaomi 14" and b.name == "工作机"
    # DHCP gave .50 to the other phone: the IP moves with it
    reg.seen(PhoneIdentity("S2", "Google", "Pixel 8"), "192.168.1.50", "home")
    assert reg.by_host("192.168.1.50", "home").serial == "S2"
    assert reg.get("S1").last_host == ""
    reg.pin("S1")
    again = PhoneRegistry(str(path), aliases={"S2": "工作机"})
    assert set(again.phones) == {"S1", "S2"} and again.pinned == "S1"
    assert again.find_by_name("切换到工作机").serial == "S2"
    assert again.find_by_name("用pixel8").serial == "S2"
    assert again.find_by_name("打开设置") is None


def test_choose_host_policy():
    reg = PhoneRegistry()
    c = Candidates(ssid="home", gateway="192.168.1.1", hosts=["192.168.1.50", "192.168.1.51"])
    # nothing known: device's last choice, then first
    assert choose_host(reg, c)[0] == "192.168.1.50"
    assert choose_host(reg, Candidates(ssid="home", hosts=c.hosts, last="192.168.1.51"))[0] == "192.168.1.51"
    # hotspot: the phone that is the gateway
    hot = Candidates(ssid="hot", gateway="10.0.0.1", hosts=["10.0.0.7", "10.0.0.1"])
    assert choose_host(reg, hot) == ("10.0.0.1", "hotspot phone")
    # most recently used known phone
    reg.seen(PhoneIdentity("A"), "192.168.1.50", "home")
    reg.seen(PhoneIdentity("B"), "192.168.1.51", "home")
    assert choose_host(reg, c)[0] == "192.168.1.51"
    # pinned beats last used
    reg.pin("A")
    assert choose_host(reg, c)[0] == "192.168.1.50"
    # a never-seen phone that just appeared beats even the pin (unless turned off)
    c2 = Candidates(ssid="home", hosts=c.hosts + ["192.168.1.77"])
    assert choose_host(reg, c2, new_hosts=["192.168.1.77"]) == ("192.168.1.77", "new phone appeared")
    assert choose_host(reg, c2, new_hosts=["192.168.1.77"], auto_switch_new=False)[0] == "192.168.1.50"
    # an explicit request wins; unreachable hosts are avoided
    assert choose_host(reg, c2, requested="192.168.1.51")[0] == "192.168.1.51"
    assert choose_host(reg, c, avoid={"192.168.1.50"})[0] == "192.168.1.51"
    assert choose_host(reg, Candidates(hosts=[])) == (None, "no candidates")


def test_next_host_round_robin():
    c = Candidates(hosts=["a", "b", "c"])
    assert next_host(c, "a") == "b" and next_host(c, "c") == "a" and next_host(c, "") == "a"
    assert next_host(Candidates(hosts=["a"]), "a") is None


def test_voice_patterns():
    reg = PhoneRegistry()
    rec = reg.seen(PhoneIdentity("S1", "Xiaomi", "Xiaomi 14"), "h", "")
    reg.aliases["S1"] = "小米"
    rec.alias = "小米"
    for t in ("换手机", "换一台手机", "切换手机", "下一台手机", "帮我换个手机吧"):
        assert RE_NEXT.search(t), t
    for t in ("换个壁纸", "切换到飞行模式", "打开设置"):
        assert not RE_NEXT.search(t), t
    m = RE_SELECT.match("切换到小米手机")
    assert m and _only_names(m.group(3), rec)
    m = RE_SELECT.match("用小米打开微信")
    assert m and not _only_names(m.group(3), rec)


# ---------------------------------------------------------------------------- end to end
class FakeAdbServer:
    """Stands in for the Mac's adb server: keeps one TCP connection to the tunnel port open."""

    def __init__(self, port: int):
        self.port = port
        self.task = asyncio.create_task(self._run())

    async def _run(self):
        while True:
            with contextlib.suppress(OSError):
                reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
                writer.write(b"CNXN")
                await writer.drain()
                while await reader.read(65536):
                    pass
                writer.close()
            await asyncio.sleep(0.1)

    def stop(self):
        self.task.cancel()


def identify_by_host(shuttle, device_id, identities):
    """Patch `adb shell <identify>` to answer with the identity of the phone behind the tunnel."""
    orig = shuttle.adb.shell

    async def shell(serial, command, timeout_s=None):
        if command == IDENTIFY_CMD:
            t = shuttle.tunnels.tunnels[device_id]
            serial_no, model = identities[t.current_host]
            return AdbResult(0, f"{serial_no}\n\n\nTestCo\n{model}\n\n".encode(), b"")
        return await orig(serial, command, timeout_s)

    shuttle.adb.shell = shell


async def test_switching_between_phones(server_factory, fake_adb_env):
    srv = await server_factory()
    shuttle = srv.shuttle
    adbd_a, port_a, _ = await start_fake_adbd()
    adbd_b, port_b, _ = await start_fake_adbd()
    a, b = f"127.0.0.1:{port_a}", f"127.0.0.1:{port_b}"
    identify_by_host(shuttle, "dev-m", {a: ("SER-A", "Phone A"), b: ("SER-B", "Phone B")})
    async with aiohttp.ClientSession() as http:
        bridge = AdbBridgeSim(http, srv.url, TOKEN, "dev-m", [a], scan_interval_s=3600)
        task = asyncio.create_task(bridge.run_forever())
        await wait_for(lambda: "dev-m" in shuttle.tunnels.tunnels)
        tunnel = shuttle.tunnels.tunnels["dev-m"]
        adb_server = FakeAdbServer(tunnel.port)
        try:
            # only phone A around: it is used, identified, and the device learns its name
            await wait_for(lambda: tunnel.phone is not None and tunnel.phone.serial == "SER-A", 10)
            await wait_for(lambda: bridge.phone_name == "TestCo Phone A")
            assert tunnel.current_host == a

            # someone runs `adb tcpip 5555` on phone B: the next scan finds it -> switch to it
            bridge.phones.append(b)
            await bridge.rescan()
            await wait_for(lambda: tunnel.phone is not None and tunnel.phone.serial == "SER-B", 10)
            assert bridge.current == b
            assert any(c[:1] == ["disconnect"] for c in adb_calls(fake_adb_env))

            # API: list and select by serial
            api = srv.url.replace("ws://", "http://")
            auth = {"Authorization": f"Bearer {TOKEN}"}
            async with http.get(f"{api}/api/phones?device=dev-m", headers=auth) as r:
                body = await r.json()
            assert [c["host"] for c in body["candidates"]] == [a, b]
            assert body["current"]["phone"]["serial"] == "SER-B"
            async with http.post(f"{api}/api/phones/select?device=dev-m", headers=auth,
                                 data=json.dumps({"serial": "SER-A"})) as r:
                assert r.status == 200, await r.text()
            await wait_for(lambda: tunnel.phone is not None and tunnel.phone.serial == "SER-A", 10)
            assert shuttle.registry.pinned == "SER-A"

            # pinned: reconnects stay on A even though B was used more recently before
            await tunnel._close_session(tunnel._session, 1000, "test drop")
            await wait_for(lambda: tunnel.phone_link == "connected" and tunnel.current_host == a, 10)

            # voice: which / next / by name
            say = shuttle.phone_commands.handle_voice
            assert "Phone A" in await say("现在连的是哪台手机", "dev-m")
            assert (await say("换手机", "dev-m")).startswith("好的")
            await wait_for(lambda: tunnel.phone is not None and tunnel.phone.serial == "SER-B", 10)
            assert "Phone A" in await say("切换到 Phone A", "dev-m")
            await wait_for(lambda: tunnel.phone is not None and tunnel.phone.serial == "SER-A", 10)
            assert await say("打开设置", "dev-m") is None
            assert await say("切换到飞行模式", "dev-m") is None

            # turn off wireless debugging on A: `adb usb`, then B is used
            msg = await say("关闭这台手机的无线调试", "dev-m")
            assert "无线调试" in msg
            assert any(c[-1:] == ["usb"] for c in adb_calls(fake_adb_env))
            await wait_for(lambda: tunnel.phone is not None and tunnel.phone.serial == "SER-B", 10)
        finally:
            adb_server.stop()
            task.cancel()
    adbd_a.close()
    adbd_b.close()


async def test_known_phone_coming_back_does_not_steal(server_factory):
    """A phone that was used before (re)appearing must not interrupt the current one."""
    srv = await server_factory()
    shuttle = srv.shuttle
    adbd_a, port_a, _ = await start_fake_adbd()
    adbd_b, port_b, _ = await start_fake_adbd()
    a, b = f"127.0.0.1:{port_a}", f"127.0.0.1:{port_b}"
    shuttle.registry.seen(PhoneIdentity("SER-B", model="Phone B"), b, "sim-wifi", used=False)
    identify_by_host(shuttle, "dev-k", {a: ("SER-A", "Phone A"), b: ("SER-B", "Phone B")})
    async with aiohttp.ClientSession() as http:
        bridge = AdbBridgeSim(http, srv.url, TOKEN, "dev-k", [a], scan_interval_s=3600)
        task = asyncio.create_task(bridge.run_forever())
        await wait_for(lambda: "dev-k" in shuttle.tunnels.tunnels)
        tunnel = shuttle.tunnels.tunnels["dev-k"]
        adb_server = FakeAdbServer(tunnel.port)
        try:
            await wait_for(lambda: tunnel.phone is not None and tunnel.phone.serial == "SER-A", 10)
            bridge.phones.append(b)
            await bridge.rescan()
            await wait_for(lambda: len(tunnel.cands.hosts) == 2)
            await asyncio.sleep(0.5)
            assert tunnel.current_host == a and bridge.current == a
        finally:
            adb_server.stop()
            task.cancel()
    adbd_a.close()
    adbd_b.close()


async def test_old_firmware_without_hello_still_works(server_factory):
    srv = await server_factory()
    adbd, port, _ = await start_fake_adbd()
    async with aiohttp.ClientSession() as http:
        bridge = AdbBridgeSim(http, srv.url, TOKEN, "dev-old", f"127.0.0.1:{port}", hello=False)
        task = asyncio.create_task(bridge.run_forever())
        await wait_for(lambda: "dev-old" in srv.shuttle.tunnels.tunnels
                       and srv.shuttle.tunnels.tunnels["dev-old"].phone_link == "connected")
        t = srv.shuttle.tunnels.tunnels["dev-old"]
        assert t.current_host == "127.0.0.1" and not t.multi_phone
        res = await srv.shuttle.phone_commands.next(t)
        assert not res["ok"] and "固件" in res["message"]
        task.cancel()
    adbd.close()
