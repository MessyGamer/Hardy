import asyncio
from datetime import datetime, timedelta, timezone

from atak_bridge.app import Bridge
from atak_bridge.config import Config
from atak_bridge.cot import CotStreamParser, cot_time, parse_event
from atak_bridge.net import TakServer
from atak_bridge.state import StateStore


def sa_event(uid, callsign):
    now = datetime.now(timezone.utc)
    return (
        f'<event version="2.0" uid="{uid}" type="a-f-G-U-C" how="h-e" time="{cot_time(now)}" '
        f'start="{cot_time(now)}" stale="{cot_time(now + timedelta(minutes=5))}">'
        f'<point lat="35.0" lon="-117.0" hae="0" ce="10" le="10"/>'
        f'<detail><contact callsign="{callsign}"/></detail></event>'
    ).encode()


async def read_events(reader, n, timeout=2.0):
    parser = CotStreamParser()
    out = []
    while len(out) < n:
        data = await asyncio.wait_for(reader.read(65536), timeout)
        assert data, "connection closed"
        out.extend(parse_event(raw) for raw in parser.feed(data))
    return out


def test_relay_ping_and_replay():
    async def scenario():
        cfg = Config()
        cfg.tak_server.port = 0
        cfg.tak_server.bind = "127.0.0.1"
        bridge = Bridge(cfg, StateStore(), link=None)
        bridge.server = TakServer(cfg.tak_server, bridge.on_client_event)
        await bridge.server.start()
        port = bridge.server.port

        r1, w1 = await asyncio.open_connection("127.0.0.1", port)
        r2, w2 = await asyncio.open_connection("127.0.0.1", port)
        await asyncio.sleep(0.05)

        # Ping gets a pong.
        now = cot_time(datetime.now(timezone.utc))
        w1.write(
            f'<event version="2.0" uid="x-ping" type="t-x-c-t" how="h-g-i-g-o" time="{now}" '
            f'start="{now}" stale="{now}"><point lat="0" lon="0" hae="0" ce="0" le="0"/></event>'.encode()
        )
        await w1.drain()
        (pong,) = await read_events(r1, 1)
        assert pong.type == "t-x-c-t-r"

        # Client 1's SA is relayed to client 2 (not echoed back).
        w1.write(sa_event("ANDROID-1", "ALPHA"))
        await w1.drain()
        (ev,) = await read_events(r2, 1)
        assert ev.uid == "ANDROID-1"

        # Aircraft CoT goes to everyone.
        bridge.publish_local("UAV", sa_event("UAV", "HARDY-1"), 10)
        (a,) = await read_events(r1, 1)
        (b,) = await read_events(r2, 1)
        assert a.uid == b.uid == "UAV"

        # A late joiner gets the cached picture immediately.
        r3, w3 = await asyncio.open_connection("127.0.0.1", port)
        uids = {e.uid for e in await read_events(r3, 2)}
        assert uids == {"ANDROID-1", "UAV"}

        for w in (w1, w2, w3):
            w.close()
        await bridge.server.close()

    asyncio.run(scenario())


def test_browser_gets_connection_package():
    import io
    import zipfile

    async def fetch(port, path):
        r, w = await asyncio.open_connection("127.0.0.1", port)
        w.write(f"GET {path} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
        await w.drain()
        data = await asyncio.wait_for(r.read(), 2)
        w.close()
        head, _, body = data.partition(b"\r\n\r\n")
        return head.decode(), body

    async def scenario():
        cfg = Config()
        cfg.tak_server.port = 0
        cfg.tak_server.bind = "127.0.0.1"
        server = TakServer(cfg.tak_server, lambda ev, c: None)
        await server.start()
        cfg.tak_server.port = server.port

        head, body = await fetch(server.port, "/")
        assert "200 OK" in head and b"hardy-tak-server.zip" in body

        head, body = await fetch(server.port, "/hardy-tak-server.zip")
        assert "application/zip" in head
        zf = zipfile.ZipFile(io.BytesIO(body))
        pref = zf.read("server.pref").decode()
        assert f"127.0.0.1:{server.port}:tcp" in pref
        assert "MANIFEST/manifest.xml" in zf.namelist()
        assert not server.clients
        await server.close()

    asyncio.run(scenario())


def test_tls_attempt_on_plain_port_is_refused_quickly():
    async def scenario():
        cfg = Config()
        cfg.tak_server.port = 0
        cfg.tak_server.bind = "127.0.0.1"
        server = TakServer(cfg.tak_server, lambda ev, c: None)
        await server.start()
        r, w = await asyncio.open_connection("127.0.0.1", server.port)
        w.write(b"\x16\x03\x01\x00\x10" + b"\x00" * 16)  # looks like a TLS ClientHello
        await w.drain()
        data = await asyncio.wait_for(r.read(), 2)
        assert data.startswith(b"\x15\x03")  # TLS alert, then closed
        assert not server.clients
        w.close()
        await server.close()

    asyncio.run(scenario())
