"""Built-in certificate authority for secure TAK connections (needed by iTAK).

Layout of the certificate directory:
    ca.key / ca.pem          the authority that signs everything (created once, kept)
    server.key / server.pem  this server's certificate (re-issued at each start so it
                             always lists the machine's current IP addresses)
    truststore.p12           the CA certificate, packaged for phones to trust this server
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import logging
import os
import socket
import ssl
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

log = logging.getLogger(__name__)

# The password TAK clients conventionally use for trust stores. It protects nothing secret
# (the file only holds the public CA certificate).
TRUSTSTORE_PASSWORD = "atakatak"


def local_ipv4_addresses() -> list[str]:
    ips: set[str] = {"127.0.0.1"}
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # picks the LAN interface; sends nothing
        ips.add(s.getsockname()[0])
    except OSError:
        pass
    finally:
        s.close()
    return sorted(ips)


def _new_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _write_private(path: Path, data: bytes) -> None:
    path.write_bytes(data)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _key_pem(key) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()
    )


def _legacy_p12_encryption(password: str):
    # Old-style (3DES/SHA1) PKCS#12 encryption: the most widely readable by phones.
    return (
        serialization.PrivateFormat.PKCS12.encryption_builder()
        .kdf_rounds(50000)
        .key_cert_algorithm(pkcs12.PBES.PBESv1SHA1And3KeyTripleDESCBC)
        .hmac_hash(hashes.SHA1())
        .build(password.encode())
    )


class CertAuthority:
    def __init__(self, directory: str | Path, name: str = "Hardy") -> None:
        self.dir = Path(directory)
        self.name = name
        self.ca_key = None
        self.ca_cert: x509.Certificate | None = None

    @property
    def server_cert_path(self) -> Path:
        return self.dir / "server.pem"

    @property
    def server_key_path(self) -> Path:
        return self.dir / "server.key"

    @property
    def ca_cert_path(self) -> Path:
        return self.dir / "ca.pem"

    def ensure(self, hosts: list[str] | None = None) -> None:
        """Load or create the CA, then (re)issue the server certificate."""
        self.dir.mkdir(parents=True, exist_ok=True)
        key_path, cert_path = self.dir / "ca.key", self.ca_cert_path
        if key_path.exists() and cert_path.exists():
            self.ca_key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
            self.ca_cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        else:
            log.info("Creating certificate authority in %s", self.dir)
            self.ca_key = _new_key()
            subject = x509.Name(
                [
                    x509.NameAttribute(NameOID.COMMON_NAME, f"{self.name} TAK CA"),
                    x509.NameAttribute(NameOID.ORGANIZATION_NAME, "TAK"),
                ]
            )
            now = dt.datetime.now(dt.timezone.utc)
            self.ca_cert = (
                x509.CertificateBuilder()
                .subject_name(subject)
                .issuer_name(subject)
                .public_key(self.ca_key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - dt.timedelta(days=1))
                .not_valid_after(now + dt.timedelta(days=3650))
                .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
                .add_extension(
                    x509.KeyUsage(
                        digital_signature=True, key_cert_sign=True, crl_sign=True,
                        content_commitment=False, key_encipherment=False, data_encipherment=False,
                        key_agreement=False, encipher_only=False, decipher_only=False,
                    ),
                    critical=True,
                )
                .add_extension(x509.SubjectKeyIdentifier.from_public_key(self.ca_key.public_key()), critical=False)
                .sign(self.ca_key, hashes.SHA256())
            )
            _write_private(key_path, _key_pem(self.ca_key))
            cert_path.write_bytes(self.ca_cert.public_bytes(serialization.Encoding.PEM))
        self._issue_server_cert(hosts or local_ipv4_addresses())
        (self.dir / "truststore.p12").write_bytes(self.truststore_p12())

    def _issue_server_cert(self, hosts: list[str]) -> None:
        key = _new_key()
        sans: list[x509.GeneralName] = [x509.DNSName("localhost")]
        try:
            sans.append(x509.DNSName(socket.gethostname()))
        except OSError:
            pass
        for host in hosts:
            try:
                sans.append(x509.IPAddress(ipaddress.ip_address(host)))
            except ValueError:
                sans.append(x509.DNSName(host))
        now = dt.datetime.now(dt.timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hosts[-1] if hosts else "localhost")]))
            .issuer_name(self.ca_cert.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=825))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.SubjectAlternativeName(sans), critical=False)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(self.ca_key.public_key()), critical=False
            )
            .sign(self.ca_key, hashes.SHA256())
        )
        _write_private(self.server_key_path, _key_pem(key))
        # Chain file: server certificate followed by the CA.
        self.server_cert_path.write_bytes(
            cert.public_bytes(serialization.Encoding.PEM) + self.ca_cert.public_bytes(serialization.Encoding.PEM)
        )
        log.info("Server certificate issued for %s", ", ".join(hosts))

    def sign_csr(self, csr: x509.CertificateSigningRequest, days: int = 365) -> x509.Certificate:
        if not csr.is_signature_valid:
            raise ValueError("CSR signature is invalid")
        now = dt.datetime.now(dt.timezone.utc)
        return (
            x509.CertificateBuilder()
            .subject_name(csr.subject)
            .issuer_name(self.ca_cert.subject)
            .public_key(csr.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(now + dt.timedelta(days=days))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(self.ca_key.public_key()), critical=False
            )
            .sign(self.ca_key, hashes.SHA256())
        )

    def truststore_p12(self) -> bytes:
        return pkcs12.serialize_key_and_certificates(
            f"{self.name.lower()}-ca".encode(), None, None, [self.ca_cert],
            _legacy_p12_encryption(TRUSTSTORE_PASSWORD),
        )

    def ca_der(self) -> bytes:
        return self.ca_cert.public_bytes(serialization.Encoding.DER)

    # ---- TLS contexts
    def server_context(self, require_client_cert: bool) -> ssl.SSLContext:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(self.server_cert_path, self.server_key_path)
        if require_client_cert:
            ctx.load_verify_locations(self.ca_cert_path)
            ctx.verify_mode = ssl.CERT_REQUIRED
        return ctx


def load_csr(body: bytes) -> x509.CertificateSigningRequest:
    """Accepts a CSR as PEM (with or without the BEGIN/END lines) or base64 DER."""
    import base64

    text = body.strip()
    if b"BEGIN" in text:
        return x509.load_pem_x509_csr(text)
    der = base64.b64decode(b"".join(text.split()), validate=False)
    return x509.load_der_x509_csr(der)
