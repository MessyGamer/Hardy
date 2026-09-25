"""TAK certificate enrollment (HTTPS, port 8446 by default).

This is the sign-in step iTAK performs before connecting: the phone sends a username,
password and a certificate signing request (CSR); if the login is valid we sign the CSR
with our CA and return it. The phone then connects to the secure streaming port using
that certificate.

Endpoints follow TAK Server's API:
    GET  /Marti/api/tls/config               -> which name fields to put in the CSR
    POST /Marti/api/tls/signClient/v2        -> signed certificate + CA (XML or JSON)
    GET  /Marti/api/device/profile/enrollment -> no extra profile (204)
"""

from __future__ import annotations

import asyncio
import base64
import hmac
import json
import logging
import ssl
from urllib.parse import parse_qs, urlsplit

from cryptography.hazmat.primitives import serialization

from .certs import CertAuthority, load_csr

log = logging.getLogger(__name__)

MAX_HEADER = 16 * 1024
MAX_BODY = 64 * 1024

TLS_CONFIG_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<ns2:certificateConfig xmlns="http://bbn.com/marti/xml/config" xmlns:ns2="com.bbn.marti.config">'
    '<nameEntries><nameEntry name="O" value="TAK"/><nameEntry name="OU" value="TAK"/></nameEntries>'
    "</ns2:certificateConfig>"
)

VERSION = "5.2-RELEASE-atak-bridge"


class HttpRequest:
    def __init__(self, method: str, target: str, headers: dict[str, str], body: bytes) -> None:
        self.method = method
        parts = urlsplit(target)
        self.path = parts.path
        self.query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        self.headers = headers
        self.body = body


async def read_request(reader: asyncio.StreamReader) -> HttpRequest | None:
    try:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=15)
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError, ConnectionError):
        return None
    if len(head) > MAX_HEADER:
        return None
    lines = head.decode("latin-1").split("\r\n")
    try:
        method, target, _ = lines[0].split(" ", 2)
    except ValueError:
        return None
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    body = b""
    if headers.get("transfer-encoding", "").lower() == "chunked":
        while True:
            size_line = await reader.readline()
            size = int(size_line.split(b";")[0].strip() or b"0", 16)
            if size == 0:
                await reader.readline()
                break
            body += await reader.readexactly(size)
            await reader.readline()
            if len(body) > MAX_BODY:
                return None
    else:
        length = int(headers.get("content-length", "0") or 0)
        if length > MAX_BODY:
            return None
        if length:
            body = await reader.readexactly(length)
    return HttpRequest(method.upper(), target, headers, body)


def response(status: str, body: bytes = b"", content_type: str = "text/plain", extra: str = "") -> bytes:
    return (
        f"HTTP/1.1 {status}\r\nContent-Type: {content_type}\r\nContent-Length: {len(body)}\r\n"
        f"{extra}Connection: close\r\n\r\n"
    ).encode() + body


class EnrollmentServer:
    def __init__(self, ca: CertAuthority, users: dict[str, str], port: int, bind: str = "0.0.0.0") -> None:
        self.ca = ca
        self.users = users
        self.port = port
        self.bind = bind
        self._server: asyncio.base_events.Server | None = None

    async def start(self) -> None:
        ctx = self.ca.server_context(require_client_cert=False)
        self._server = await asyncio.start_server(self._handle, self.bind, self.port, ssl=ctx)
        log.info("Certificate sign-in (enrollment) listening on port %d (HTTPS)", self.port)

    async def close(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    def _check_login(self, req: HttpRequest) -> str | None:
        auth = req.headers.get("authorization", "")
        if not auth.lower().startswith("basic "):
            return None
        try:
            user, _, password = base64.b64decode(auth[6:]).decode("utf-8").partition(":")
        except (ValueError, UnicodeDecodeError):
            return None
        expected = self.users.get(user)
        if expected is not None and hmac.compare_digest(expected.encode(), password.encode()):
            return user
        return None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = (writer.get_extra_info("peername") or ("?",))[0]
        try:
            req = await read_request(reader)
            if req is None:
                return
            out = await self._route(req, peer)
            writer.write(out)
            await writer.drain()
        except (ConnectionError, ssl.SSLError, asyncio.IncompleteReadError, ValueError) as exc:
            log.info("Sign-in connection from %s failed: %s", peer, exc)
        finally:
            writer.close()

    async def _route(self, req: HttpRequest, peer: str) -> bytes:
        log.info("Sign-in request from %s: %s %s", peer, req.method, req.path)

        # Unauthenticated informational endpoints.
        if req.path == "/Marti/api/version":
            return response("200 OK", VERSION.encode())
        if req.path == "/Marti/api/version/config":
            data = {"version": "3", "type": "ServerConfig", "data": {"version": VERSION, "api": "3", "hostname": "0.0.0.0"}, "nodeId": "atak-bridge"}
            return response("200 OK", json.dumps(data).encode(), "application/json")

        user = self._check_login(req)
        if user is None:
            await asyncio.sleep(1.0)  # slow down password guessing
            log.warning("Sign-in from %s rejected: wrong or missing username/password", peer)
            return response("401 Unauthorized", b"Unauthorized", extra='WWW-Authenticate: Basic realm="TAK"\r\n')

        if req.path == "/Marti/api/tls/config" and req.method == "GET":
            return response("200 OK", TLS_CONFIG_XML.encode(), "application/xml")

        if req.path in ("/Marti/api/tls/signClient/v2", "/Marti/api/tls/signClient") and req.method == "POST":
            try:
                csr = load_csr(req.body)
                cert = self.ca.sign_csr(csr)
            except ValueError as exc:
                log.warning("Sign-in from %s: bad certificate request (%s)", peer, exc)
                return response("400 Bad Request", b"Bad CSR")
            signed = base64.b64encode(cert.public_bytes(serialization.Encoding.DER)).decode()
            ca0 = base64.b64encode(self.ca.ca_der()).decode()
            log.info(
                "Signed in %s from %s (device %s) - certificate issued",
                user, peer, req.query.get("clientUid", "?"),
            )
            if "application/json" in req.headers.get("accept", ""):
                body = json.dumps({"signedCert": signed, "ca0": ca0}).encode()
                return response("200 OK", body, "application/json")
            body = f"<enrollment><signedCert>{signed}</signedCert><ca0>{ca0}</ca0></enrollment>".encode()
            return response("200 OK", body, "application/xml")

        if req.path == "/Marti/api/device/profile/enrollment":
            return response("204 No Content")

        log.info("Sign-in request from %s for %s is not supported (answered 404)", peer, req.path)
        return response("404 Not Found", b"Not found")
