"""A user abort (touch/button on the device -> {"type":"abort"}) stops a running agent task."""
import asyncio
import json

import aiohttp

from conftest import TOKEN
from shuttle_server.audio import pcm as pcmlib
from shuttle_server.audio.opus import OpusEncoder
from shuttle_server.pipeline.agent import Agent
from sim_device import FRAME_MS, UP_RATE, CtrlClient

STARTED = asyncio.Event()
CANCELLED = asyncio.Event()


class SlowAgent(Agent):
    name = "slow"

    async def run(self, instruction, ctx):
        STARTED.set()
        await ctx.report("acting", "1. 慢慢做")
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            CANCELLED.set()
            raise
        return "不该走到这里"


async def test_abort_cancels_running_agent(server_factory):
    STARTED.clear()
    CANCELLED.clear()
    srv = await server_factory(agent={"provider": "python", "python": {"target": "test_abort:SlowAgent"}})
    async with aiohttp.ClientSession() as http:
        ctrl = CtrlClient(http, srv.url, TOKEN, "dev-abort")
        await ctrl.connect()
        try:
            await ctrl.send({"type": "listen", "state": "start", "mode": "manual"})
            enc = OpusEncoder(UP_RATE, application=2048)
            frame = UP_RATE * FRAME_MS // 1000
            for f in pcmlib.frames(pcmlib.tone(300, 900, UP_RATE), frame):
                await ctrl.ws.send_bytes(enc.encode(f, frame))
            await ctrl.send({"type": "listen", "state": "stop"})
            await asyncio.wait_for(STARTED.wait(), 10)
            await ctrl.send({"type": "abort"})
            statuses = []
            while not statuses or statuses[-1] != "cancelled":
                kind, data = await asyncio.wait_for(ctrl.queue.get(), 10)
                if kind == "json" and data.get("event") == "status":
                    statuses.append(data["state"])
            await asyncio.wait_for(CANCELLED.wait(), 5)
            assert "acting" in statuses and statuses[-1] == "cancelled"
        finally:
            await ctrl.close()
