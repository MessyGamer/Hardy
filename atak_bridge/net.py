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
from .datapackage import (
    SecureInfo,
    build_download_page,
    build_itak_package,
    build_secure_package,
    build_server_package,
    package_filename,
    secure_android_filename,
)

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


def _basic_auth_user(request: bytes, check_login) -> str | None:
    import base64

    for line in request.split(b"\r\n")[1:]:
        if line.lower().startswith(b"authorization:"):
            value = line.split(b":", 1)[1].strip()
            if value[:6].lower() == b"basic ":
                try:
                    user, _, password = base64.b64decode(value[6:]).decode("utf-8").partition(":")
                except (ValueError, UnicodeDecodeError):
                    return None
                return user if check_login and check_login(user, password) else None
    return None


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
        self._secure_server: asyncio.base_events.Server | None = None
        # Set when certificate-based connections are available (see start_secure).
        self.secure: SecureInfo | None = None

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

    async def start_secure(self, port: int, ctx: ssl.SSLContext, info: SecureInfo) -> None:
        """Extra listener for certificate-authenticated (SSL) clients such as iTAK."""
        self._secure_server = await asyncio.start_server(self._handle_client, self.cfg.bind, port, ssl=ctx)
        self.secure = info
        log.info("Secure TAK server (certificates required) listening on %s:%d", self.cfg.bind, port)

    @property
    def port(self) -> int:
        assert self._server is not None
        return self._server.sockets[0].getsockname()[1]

    async def close(self) -> None:
        for client in list(self.clients):
            client.writer.close()
        for server in (self._server, self._secure_server):
            if server:
                server.close()
                await server.wait_closed()

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

    async def _serve_http(self, request: bytes, writer: asyncio.StreamWriter, peer: str) -> None:
        """Tiny web page so a phone browser can download a ready-made connection package."""
        try:
            if not self.cfg.connection_package:
                body, ctype, status, extra = b"Not found", "text/plain", "404 Not Found", ""
            else:
                path = request.split(b" ", 2)[1].decode("latin-1") if b" " in request else "/"
                # Advertise whichever of our addresses the phone actually reached.
                host = writer.get_extra_info("sockname")[0]
                path = path.lstrip("/")
                name = self.cfg.name
                # filename -> builder; the iPhone package is the secure one when available,
                # because iTAK always signs in with a certificate.
                iphone = package_filename(name, itak=True)
                packages = {
                    package_filename(name): lambda: build_server_package(name, host, self.cfg.port),
                    iphone: lambda: build_server_package(name, host, self.cfg.port, itak=True),
                }
                user = None
                if self.secure and path == iphone:
                    # The iPhone package contains a personal certificate, so it needs a login.
                    user = _basic_auth_user(request, self.secure.check_login)
                    if user is None:
                        log.info("Asked %s to log in before downloading the iPhone package", peer)
                        head = (
                            "HTTP/1.1 401 Unauthorized\r\nContent-Type: text/plain\r\nContent-Length: 12\r\n"
                            f'WWW-Authenticate: Basic realm="{name} TAK server"\r\nConnection: close\r\n\r\n'
                        ).encode()
                        writer.write(head + b"Unauthorized")
                        await writer.drain()
                        return
                    packages[iphone] = lambda: build_itak_package(name, host, self.secure, user)
                if self.secure:
                    packages[secure_android_filename(name)] = lambda: build_secure_package(
                        name, host, self.secure, itak=False
                    )
                if path in packages:
                    filename = path
                    body = packages[path]()
                    ctype, status = "application/zip", "200 OK"
                    extra = f'Content-Disposition: attachment; filename="{filename}"\r\n'
                    log.info(
                        "Sent connection package %s (server %s) to %s%s",
                        filename, host, peer, f" for user '{user}'" if user else "",
                    )
                else:
                    body = build_download_page(name, host, self.cfg.port, self.secure)
                    ctype, status, extra = "text/html; charset=utf-8", "200 OK", ""
                    log.info("Browser from %s opened the connection page", peer)
            head = (
                f"HTTP/1.1 {status}\r\nContent-Type: {ctype}\r\nContent-Length: {len(body)}\r\n"
                f"{extra}Cache-Control: no-store\r\nConnection: close\r\n\r\n"
            ).encode()
            writer.write(head if request.startswith(b"HEAD ") else head + body)
            await writer.drain()
        except (ConnectionError, ssl.SSLError, IndexError):
            pass
        finally:
            writer.close()

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peername = writer.get_extra_info("peername")
        peer = f"{peername[0]}:{peername[1]}" if peername else "?"
        if len(self.clients) >= self.cfg.max_clients:
            log.warning("Rejecting %s: max_clients (%d) reached", peer, self.cfg.max_clients)
            writer.close()
            return
        # TAK clients speak first (their own position report). If it's a web browser instead,
        # serve the connection-package download page on the same port.
        first = b""
        try:
            first = await asyncio.wait_for(reader.read(65536), timeout=1.0)
        except asyncio.TimeoutError:
            pass
        except (ConnectionError, ssl.SSLError):
            writer.close()
            return
        if first.startswith((b"GET ", b"HEAD ")):
            await self._serve_http(first, writer, peer)
            return
        if first[:1] == b"\x16" and first[1:2] == b"\x03" and not self.cfg.tls:
            # TLS ClientHello on our plain port: usually a phone browser auto-upgrading
            # http:// to https://. Refuse with a TLS alert so it falls back to plain http.
            log.info("%s tried a secure (TLS/HTTPS) connection; this port is plain TCP, refusing", peer)
            try:
                writer.write(b"\x15\x03\x01\x00\x02\x02\x28")  # alert: fatal handshake_failure
                await writer.drain()
            except (ConnectionError, OSError):
                pass
            writer.close()
            return

        _enable_keepalive(writer)
        client = ClientConnection(writer, peer)
        self.clients.add(client)
        cert = writer.get_extra_info("peercert")
        if cert:
            cn = next((v for rdn in cert.get("subject", ()) for k, v in rdn if k == "commonName"), "?")
            log.info("Secure client '%s' connected from %s (%d total)", cn, peer, len(self.clients))
        else:
            log.info("ATAK client connected from %s (%d total)", peer, len(self.clients))
        self._replay_cache(client)
        writer_task = asyncio.create_task(client.run_writer())
        parser = CotStreamParser()
        data = first
        try:
            while True:
                if not data:
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
                data = b""
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


class _MulticastProtocol(asyncio.DatagramProtocol):
    def __init__(self, listener: "MulticastListener") -> None:
        self.listener = listener

    def datagram_received(self, data: bytes, addr) -> None:
        self.listener.handle(data, addr)


class MulticastListener:
    """Receives CoT that ATAK/iTAK devices broadcast on the SA multicast group."""

    def __init__(self, cfg: MulticastConfig, on_event: Callable[[CotEvent], None], ignore_uids: set[str]) -> None:
        self.cfg = cfg
        self.on_event = on_event
        self.ignore_uids = ignore_uids
        self._transport: asyncio.DatagramTransport | None = None
        self._seen_peers: set[str] = set()
        self._binary_peers: set[str] = set()

    async def start(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        sock.bind(("", self.cfg.port))
        iface = socket.inet_aton(self.cfg.interface) if self.cfg.interface else socket.inet_aton("0.0.0.0")
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, socket.inet_aton(self.cfg.group) + iface)
        sock.setblocking(False)
        loop = asyncio.get_running_loop()
        self._transport, _ = await loop.create_datagram_endpoint(lambda: _MulticastProtocol(self), sock=sock)
        log.info("Listening for ATAK/iTAK broadcasts on %s:%d", self.cfg.group, self.cfg.port)

    def handle(self, data: bytes, addr) -> None:
        peer = addr[0] if addr else "?"
        if data[:1] == b"\xbf":
            # TAK Protocol v1 (protobuf) - not decoded yet; say so once per device.
            if peer not in self._binary_peers:
                self._binary_peers.add(peer)
                log.info("Device %s broadcasts in TAK binary format, which this version can't read yet", peer)
            return
        start = data.find(b"<event")
        if start < 0:
            return
        ev = parse_event(data[start:])
        if ev is None or ev.uid in self.ignore_uids or ev.is_ping:
            return
        if peer not in self._seen_peers:
            self._seen_peers.add(peer)
            log.info("Hearing broadcasts from device %s (%s)", peer, ev.callsign or ev.uid)
        if not ev.type.startswith("a-"):
            log.info("Broadcast from %s: %s %r", peer, ev.type, ev.callsign)
        self.on_event(ev)

    def close(self) -> None:
        if self._transport:
            self._transport.close()


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
