"""Network side: built-in TAK TCP server, SA multicast, and optional upstream TAK server."""

from __future__ import annotations

import asyncio
import logging
import socket
import ssl
import time
from datetime import datetime, timezone
from typing import Callable

from .config import MulticastConfig, TakServerConfig, UpstreamConfig
from .cot import CotEvent, CotStreamParser, build_pong, parse_event

log = logging.getLogger(__name__)

EventCallback = Callable[[CotEvent, "object"], None]


def _enable_keepalive(writer: asyncio.StreamWriter) -> None:
    sock = writer.get_extra_info("socket")
    if sock is None:
        return
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        if hasattr(socket, "TCP_KEEPIDLE"):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 30)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
    except OSError:
        pass


class ClientConnection:
    QUEUE_SIZE = 1000

    def __init__(self, writer: asyncio.StreamWriter, peer: str) -> None:
        self.writer = writer
        self.peer = peer
        self.callsign = ""
        self._queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=self.QUEUE_SIZE)
        self._dropped = 0

    def __repr__(self) -> str:
        return f"<client {self.callsign or '?'} {self.peer}>"

    def send(self, raw: bytes) -> None:
        try:
            self._queue.put_nowait(raw)
        except asyncio.QueueFull:
            self._dropped += 1
            if self._dropped % 100 == 1:
                log.warning("%r is not keeping up; dropped %d messages", self, self._dropped)

    async def run_writer(self) -> None:
        try:
            while True:
                raw = await self._queue.get()
                self.writer.write(raw)
                await self.writer.drain()
        except (ConnectionError, ssl.SSLError) as exc:
            log.info("%r write failed: %s", self, exc)
            self.writer.close()


class TakServer:
    """Minimal TAK streaming server (plain CoT XML over TCP, optional TLS).

    ATAK: Settings > Network Preferences > TAK Servers > Add, address = Pi IP,
    port = 8087, protocol TCP (or SSL on the configured port when tls = true).
    """

    def __init__(self, cfg: TakServerConfig, on_event: EventCallback) -> None:
        self.cfg = cfg
        self.on_event = on_event
        self.clients: set[ClientConnection] = set()
        # uid -> (raw, stale) of the latest event seen, replayed to newly connected clients.
        self._cache: dict[str, tuple[bytes, datetime | None]] = {}
        self._server: asyncio.base_events.Server | None = None

    def _ssl_context(self) -> ssl.SSLContext | None:
        if not self.cfg.tls:
            return None
        ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ctx.load_cert_chain(self.cfg.certfile, self.cfg.keyfile or None)
        if self.cfg.cafile:
            ctx.load_verify_locations(self.cfg.cafile)
            ctx.verify_mode = ssl.CERT_REQUIRED
        return ctx

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_client, self.cfg.bind, self.cfg.port, ssl=self._ssl_context()
        )
        log.info(
            "TAK server listening on %s:%d (%s)", self.cfg.bind, self.cfg.port, "TLS" if self.cfg.tls else "TCP"
        )

    @property
    def port(self) -> int:
        assert self._server is not None
        return self._server.sockets[0].getsockname()[1]

    async def close(self) -> None:
        if self._server:
            self._server.close()
            for client in list(self.clients):
                client.writer.close()
            await self._server.wait_closed()

    def remember(self, ev: CotEvent) -> None:
        if ev.is_delete:
            if ev.link_uid:
                self._cache.pop(ev.link_uid, None)
            return
        self._cache[ev.uid] = (ev.raw, ev.stale)

    def remember_raw(self, uid: str, raw: bytes, stale: datetime | None) -> None:
        self._cache[uid] = (raw, stale)

    def broadcast(self, raw: bytes, exclude: ClientConnection | None = None) -> None:
        for client in self.clients:
            if client is not exclude:
                client.send(raw)

    def _replay_cache(self, client: ClientConnection) -> None:
        now = datetime.now(timezone.utc)
        for uid, (raw, stale) in list(self._cache.items()):
            if stale is not None and stale < now:
                del self._cache[uid]
                continue
            client.send(raw)

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peername = writer.get_extra_info("peername")
        peer = f"{peername[0]}:{peername[1]}" if peername else "?"
        if len(self.clients) >= self.cfg.max_clients:
            log.warning("Rejecting %s: max_clients (%d) reached", peer, self.cfg.max_clients)
            writer.close()
            return
        _enable_keepalive(writer)
        client = ClientConnection(writer, peer)
        self.clients.add(client)
        log.info("ATAK client connected from %s (%d total)", peer, len(self.clients))
        self._replay_cache(client)
        writer_task = asyncio.create_task(client.run_writer())
        parser = CotStreamParser()
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                for raw in parser.feed(data):
                    ev = parse_event(raw)
                    if ev is None:
                        continue
                    if ev.is_ping:
                        client.send(build_pong())
                        continue
                    if not client.callsign and ev.type.startswith("a-") and ev.callsign:
                        client.callsign = ev.callsign
                        log.info("%s identified as %s", peer, ev.callsign)
                    self.on_event(ev, client)
        except (ConnectionError, ssl.SSLError, ValueError, asyncio.IncompleteReadError) as exc:
            log.info("%r connection error: %s", client, exc)
        finally:
            self.clients.discard(client)
            writer_task.cancel()
            writer.close()
            log.info("%r disconnected (%d remaining)", client, len(self.clients))


class MulticastSender:
    """Sends CoT to the ATAK SA multicast group so LAN devices see the UAV with no server config."""

    def __init__(self, cfg: MulticastConfig) -> None:
        self.cfg = cfg
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        self._sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, cfg.ttl)
        if cfg.interface:
            self._sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(cfg.interface))
        self._sock.setblocking(False)
        self._last_error = 0.0
        log.info("Multicast SA output to %s:%d", cfg.group, cfg.port)

    def send(self, raw: bytes) -> None:
        try:
            self._sock.sendto(raw, (self.cfg.group, self.cfg.port))
        except OSError as exc:
            now = time.monotonic()
            if now - self._last_error > 30:
                log.warning("Multicast send failed: %s", exc)
                self._last_error = now

    def close(self) -> None:
        self._sock.close()


class UpstreamClient:
    """Keeps a TCP/TLS connection to an external TAK server and forwards CoT both ways."""

    def __init__(self, cfg: UpstreamConfig, on_event: Callable[[CotEvent], None]) -> None:
        self.cfg = cfg
        self.on_event = on_event
        self._queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=1000)
        self._connected = False

    def send(self, raw: bytes) -> None:
        if not self._connected:
            return
        try:
            self._queue.put_nowait(raw)
        except asyncio.QueueFull:
            pass

    def _ssl_context(self) -> ssl.SSLContext | None:
        if not self.cfg.tls:
            return None
        ctx = ssl.create_default_context(cafile=self.cfg.cafile or None)
        if self.cfg.certfile:
            ctx.load_cert_chain(self.cfg.certfile, self.cfg.keyfile or None)
        return ctx

    async def run(self) -> None:
        backoff = 1.0
        while True:
            try:
                reader, writer = await asyncio.open_connection(
                    self.cfg.host, self.cfg.port, ssl=self._ssl_context()
                )
            except (OSError, ssl.SSLError) as exc:
                log.warning("Upstream TAK server %s:%d unavailable: %s", self.cfg.host, self.cfg.port, exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
                continue
            backoff = 1.0
            _enable_keepalive(writer)
            log.info("Connected to upstream TAK server %s:%d", self.cfg.host, self.cfg.port)
            self._connected = True
            writer_task = asyncio.create_task(self._pump(writer))
            parser = CotStreamParser()
            try:
                while data := await reader.read(65536):
                    for raw in parser.feed(data):
                        ev = parse_event(raw)
                        if ev is not None and not ev.is_ping and ev.type != "t-x-c-t-r":
                            self.on_event(ev)
            except (ConnectionError, ssl.SSLError, ValueError) as exc:
                log.warning("Upstream connection error: %s", exc)
            finally:
                self._connected = False
                writer_task.cancel()
                writer.close()
                while not self._queue.empty():
                    self._queue.get_nowait()
            log.info("Upstream TAK server disconnected; reconnecting")
            await asyncio.sleep(backoff)

    async def _pump(self, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                raw = await self._queue.get()
                writer.write(raw)
                await writer.drain()
        except (ConnectionError, ssl.SSLError):
            pass
