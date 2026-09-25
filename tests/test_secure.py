"""End-to-end: behave like iTAK - import package, enroll with username/password, connect over SSL."""

import asyncio
import base64
import io
import re
import socket
import ssl
import tempfile
import zipfile
from pathlib import Path

import pytest

pytest.importorskip("cryptography")

from cryptography import x509  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from cryptography.hazmat.primitives.serialization import pkcs12  # noqa: E402
from cryptography.x509.oid import NameOID  # noqa: E402

from atak_bridge.app import Bridge  # noqa: E402
from atak_bridge.config import Config  # noqa: E402
from atak_bridge.cot import CotStreamParser, parse_event  # noqa: E402
from atak_bridge.state import StateStore  # noqa: E402
from tests.test_server import sa_event  # noqa: E402


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def http(port, request: bytes, ssl_ctx=None) -> tuple[str, bytes]:
    r, w = await asyncio.open_connection("127.0.0.1", port, ssl=ssl_ctx)
    w.write(request)
    await w.drain()
    data = await asyncio.wait_for(r.read(), 5)
    w.close()
    head, _, body = data.partition(b"\r\n\r\n")
    return head.decode(), body


def basic(user, password):
    return base64.b64encode(f"{user}:{password}".encode()).decode()


def test_itak_style_enrollment_and_secure_connection():
    async def scenario(tmp: Path):
        cfg = Config()
        cfg.tak_server.bind = "127.0.0.1"
        cfg.tak_server.port = free_port()
        cfg.secure.enabled = True
        cfg.secure.cert_dir = str(tmp / "certs")
        cfg.secure.ssl_port = free_port()
        cfg.secure.enrollment_port = free_port()
        cfg.secure.users = {"hardy": "s3cret"}
        cfg.multicast.enabled = False

        bridge = Bridge(cfg, StateStore(), link=None)
        task = asyncio.create_task(bridge.run())
        await asyncio.sleep(1.5)  # key generation

        # 1. Phone downloads the iPhone package from the plain port.
        head, body = await http(cfg.tak_server.port, b"GET /hardy-tak-server-iphone.zip HTTP/1.1\r\n\r\n")
        assert "200 OK" in head
        zf = zipfile.ZipFile(io.BytesIO(body))
        assert sorted(zf.namelist()) == ["config.pref", "truststore-hardy.p12"]  # all at the root
        pref = zf.read("config.pref").decode()
        assert f"127.0.0.1:{cfg.secure.ssl_port}:ssl" in pref
        assert "enrollForCertificateWithTrust0" in pref
        assert "<entry key=\"caLocation0\" class=\"class java.lang.String\">cert/truststore-hardy.p12<" in pref
        ca_password = re.search(r'caPassword0" class="class java.lang.String">([^<]+)<', pref).group(1)

        # 2. Phone trusts the CA from the package's trust store.
        _, _, cas = pkcs12.load_key_and_certificates(zf.read("truststore-hardy.p12"), ca_password.encode())
        ca_pem = cas[0].public_bytes(serialization.Encoding.PEM).decode()
        trust = ssl.create_default_context(cadata=ca_pem)
        trust.check_hostname = False  # phone connects by IP; chain is what matters here

        port = cfg.secure.enrollment_port
        # 3a. Wrong password is refused.
        head, _ = await http(
            port,
            f"GET /Marti/api/tls/config HTTP/1.1\r\nAuthorization: Basic {basic('hardy', 'nope')}\r\n\r\n".encode(),
            trust,
        )
        assert "401" in head

        # 3b. Right password: fetch CSR config, send CSR, get a signed certificate.
        auth = f"Authorization: Basic {basic('hardy', 's3cret')}\r\n"
        head, body = await http(port, f"GET /Marti/api/tls/config HTTP/1.1\r\n{auth}\r\n".encode(), trust)
        assert "200 OK" in head and b'name="O"' in body

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        csr = (
            x509.CertificateSigningRequestBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "hardy")]))
            .sign(key, hashes.SHA256())
        )
        csr_b64 = base64.b64encode(csr.public_bytes(serialization.Encoding.DER))
        req = (
            f"POST /Marti/api/tls/signClient/v2?clientUid=IPHONE-1&version=2 HTTP/1.1\r\n{auth}"
            f"Content-Type: text/plain\r\nContent-Length: {len(csr_b64)}\r\n\r\n"
        ).encode() + csr_b64
        head, body = await http(port, req, trust)
        assert "200 OK" in head
        signed_b64 = re.search(rb"<signedCert>([^<]+)</signedCert>", body).group(1)
        signed = x509.load_der_x509_certificate(base64.b64decode(signed_b64))
        assert signed.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == "hardy"

        # JSON flavour works too.
        head, body = await http(port, req.replace(b"Content-Type", b"Accept: application/json\r\nContent-Type"), trust)
        assert b'"signedCert"' in body

        # 4. Without a certificate the secure port refuses the phone.
        with pytest.raises((ssl.SSLError, ConnectionError, asyncio.IncompleteReadError)):
            r, w = await asyncio.open_connection("127.0.0.1", cfg.secure.ssl_port, ssl=trust)
            w.write(sa_event("NOCERT", "X"))
            await w.drain()
            if not await asyncio.wait_for(r.read(100), 3):
                raise ConnectionError("closed")

        # 5. With the issued certificate it connects and receives the drone.
        cert_path, key_path = tmp / "client.pem", tmp / "client.key"
        cert_path.write_bytes(signed.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
            )
        )
        trust.load_cert_chain(cert_path, key_path)
        r, w = await asyncio.open_connection("127.0.0.1", cfg.secure.ssl_port, ssl=trust)
        w.write(sa_event("IPHONE-1", "HARDY"))
        await w.drain()
        await asyncio.sleep(0.2)
        bridge.publish_local("DRONE", sa_event("DRONE", "DEMO-DRONE"), 10)
        parser = CotStreamParser()
        events = []
        while not any(e.uid == "DRONE" for e in events):
            events += [parse_event(x) for x in parser.feed(await asyncio.wait_for(r.read(65536), 3))]
        w.close()

        task.cancel()
        await bridge.server.close()
        await bridge.enrollment.close()

    with tempfile.TemporaryDirectory() as d:
        asyncio.run(scenario(Path(d)))


def test_secure_mode_refuses_placeholder_password(caplog):
    async def scenario(tmp: Path):
        cfg = Config()
        cfg.tak_server.bind = "127.0.0.1"
        cfg.tak_server.port = free_port()
        cfg.secure.enabled = True
        cfg.secure.cert_dir = str(tmp / "certs")
        cfg.secure.users = {"hardy": "change-me"}
        bridge = Bridge(cfg, StateStore(), link=None)
        bridge.server = None
        from atak_bridge.net import TakServer

        bridge.server = TakServer(cfg.tak_server, bridge.on_client_event)
        await bridge.server.start()
        await bridge.start_secure()
        assert bridge.enrollment is None and bridge.server.secure is None
        await bridge.server.close()

    with tempfile.TemporaryDirectory() as d:
        asyncio.run(scenario(Path(d)))
    assert "Secure mode is OFF" in caplog.text
