"""Diagnostic: listen on the ports TAK apps commonly use and log who knocks and how.

Used by `demo.py --probe` to find out what a client (e.g. iTAK) tries before it gives up.
"""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger("probe")

# Port -> what a TAK client would normally want there.
TAK_PORTS = {
    80: "plain web",
    443: "secure web",
    8080: "TAK web API (plain)",
    8088: "TAK streaming, plain TCP (OpenTAKServer style)",
    8089: "TAK streaming, secure (SSL)",
    8443: "TAK web API, secure (logins / certificates)",
    8446: "TAK certificate sign-in (enrollment)",
}

HTTP_METHODS = (b"GET ", b"POST ", b"PUT ", b"HEAD ", b"OPTIONS ", b"DELETE ")


def describe(first: bytes) -> str:
    if not first:
        return "connected but sent nothing"
    if first[:1] == b"\x16" and first[1:2] == b"\x03":
        kind = "SECURE (TLS) connection"
        if b"http/1.1" in first or b"h2" in first:
            kind += ", looks like a secure web (HTTPS) request"
        return kind
    if first.startswith(HTTP_METHODS):
        return "plain web request: " + first.split(b"\r\n", 1)[0].decode("latin-1", "replace")
    if b"<event" in first:
        return "plain TAK map data (CoT XML)"
    if first[:1] == b"\xbf":
        return "TAK binary (protobuf) data"
    preview = first[:40].decode("latin-1", "replace")
    return f"unknown data: {preview!r}"


async def start_probes(skip: set[int]) -> list[asyncio.base_events.Server]:
    servers = []

    for port, meaning in TAK_PORTS.items():
        if port in skip:
            continue

        async def handler(reader, writer, port=port, meaning=meaning):
            peer = writer.get_extra_info("peername")
            who = peer[0] if peer else "?"
            try:
                first = await asyncio.wait_for(reader.read(4096), timeout=3.0)
            except (asyncio.TimeoutError, ConnectionError):
                first = b""
            log.info("PROBE  %s knocked on port %d (%s): %s", who, port, meaning, describe(first))
            if first.startswith(HTTP_METHODS):
                try:
                    writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                    await writer.drain()
                except ConnectionError:
                    pass
            writer.close()

        try:
            servers.append(await asyncio.start_server(handler, "0.0.0.0", port))
        except OSError as exc:
            log.info("PROBE  can't watch port %d (%s)", port, exc.strerror or exc)
    watched = sorted(s.sockets[0].getsockname()[1] for s in servers)
    log.info("PROBE  watching ports %s for connection attempts", ", ".join(map(str, watched)))
    return servers
