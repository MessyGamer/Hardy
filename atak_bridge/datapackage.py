"""TAK "data package" that configures ATAK/iTAK to connect to this server over plain TCP.

iTAK's manual add-server screen tries certificate enrollment, which this server does not
offer; importing this package instead sets up a plain TCP connection with no login.
"""

from __future__ import annotations

import io
import uuid
import zipfile
from dataclasses import dataclass
from html import escape

PREF_TEMPLATE = """<?xml version='1.0' encoding='ASCII' standalone='yes'?>
<preferences>
  <preference version="1" name="cot_streams">
    <entry key="count" class="class java.lang.Integer">1</entry>
    <entry key="description0" class="class java.lang.String">{name}</entry>
    <entry key="enabled0" class="class java.lang.Boolean">true</entry>
    <entry key="connectString0" class="class java.lang.String">{host}:{port}:tcp</entry>
    <entry key="useAuth0" class="class java.lang.Boolean">false</entry>
  </preference>
</preferences>
"""

MANIFEST_TEMPLATE = """<MissionPackageManifest version="2">
  <Configuration>
    <Parameter name="uid" value="{uid}"/>
    <Parameter name="name" value="{name}"/>
    <Parameter name="onReceiveDelete" value="true"/>
  </Configuration>
  <Contents>
    <Content ignore="false" zipEntry="config.pref"/>
  </Contents>
</MissionPackageManifest>
"""


SECURE_PREF_TEMPLATE = """<?xml version='1.0' encoding='ASCII' standalone='yes'?>
<preferences>
  <preference version="1" name="cot_streams">
    <entry key="count" class="class java.lang.Integer">1</entry>
    <entry key="description0" class="class java.lang.String">{name}</entry>
    <entry key="enabled0" class="class java.lang.Boolean">true</entry>
    <entry key="connectString0" class="class java.lang.String">{host}:{port}:ssl</entry>
    <entry key="caLocation0" class="class java.lang.String">{ca_location}</entry>
    <entry key="caPassword0" class="class java.lang.String">{ca_password}</entry>
    <entry key="enrollForCertificateWithTrust0" class="class java.lang.Boolean">true</entry>
    <entry key="useAuth0" class="class java.lang.Boolean">true</entry>
    <entry key="cacheCreds0" class="class java.lang.String">Cache credentials</entry>
  </preference>
  <preference version="1" name="com.atakmap.app_preferences">
    <entry key="enrollForCertificateWithTrust0" class="class java.lang.Boolean">true</entry>
    <entry key="displayServerConnectionWidget" class="class java.lang.Boolean">true</entry>
  </preference>
</preferences>
"""

SECURE_MANIFEST_TEMPLATE = """<MissionPackageManifest version="2">
  <Configuration>
    <Parameter name="uid" value="{uid}"/>
    <Parameter name="name" value="{name}"/>
    <Parameter name="onReceiveDelete" value="true"/>
  </Configuration>
  <Contents>
    <Content ignore="false" zipEntry="config.pref"/>
    <Content ignore="false" zipEntry="{truststore}"/>
  </Contents>
</MissionPackageManifest>
"""


@dataclass
class SecureInfo:
    """What a secure (certificate) connection package needs to contain."""

    ssl_port: int
    truststore: bytes
    truststore_password: str


def truststore_filename(name: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in name).strip("-") or "tak"
    return f"truststore-{safe.lower()}.p12"


def build_secure_package(name: str, host: str, secure: SecureInfo, itak: bool) -> bytes:
    """Package that makes the app trust our CA, ask for a login, enroll, then connect over SSL.

    Layout follows TAK Server's own packages: iTAK gets config.pref and the trust store at
    the zip root with no manifest; ATAK gets a manifest. Both refer to the trust store as
    cert/<file>, which is where the apps file it on import.
    """
    ts_name = truststore_filename(name)
    pref = SECURE_PREF_TEMPLATE.format(
        name=escape(name),
        host=escape(host),
        port=secure.ssl_port,
        ca_location=f"cert/{ts_name}",
        ca_password=escape(secure.truststore_password),
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("config.pref", pref)
        zf.writestr(ts_name, secure.truststore)
        if not itak:
            zf.writestr(
                "MANIFEST/manifest.xml",
                SECURE_MANIFEST_TEMPLATE.format(
                    uid=uuid.uuid5(uuid.NAMESPACE_DNS, f"{host}:{secure.ssl_port}:ssl"),
                    name=escape(name),
                    truststore=ts_name,
                ),
            )
    return buf.getvalue()


def package_filename(name: str, itak: bool = False) -> str:
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in name).strip("-") or "tak"
    return f"{safe.lower()}-tak-server{'-iphone' if itak else ''}.zip"


def build_server_package(name: str, host: str, port: int, itak: bool = False) -> bytes:
    """ATAK wants a MANIFEST/manifest.xml; iTAK wants just config.pref at the zip root."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("config.pref", PREF_TEMPLATE.format(name=escape(name), host=escape(host), port=port))
        if not itak:
            zf.writestr(
                "MANIFEST/manifest.xml",
                MANIFEST_TEMPLATE.format(uid=uuid.uuid5(uuid.NAMESPACE_DNS, f"{host}:{port}"), name=escape(name)),
            )
    return buf.getvalue()


def secure_android_filename(name: str) -> str:
    return package_filename(name).replace(".zip", "-android-secure.zip")


def build_download_page(name: str, host: str, port: int, secure: SecureInfo | None = None) -> bytes:
    android = package_filename(name)
    iphone = package_filename(name, itak=True)
    if secure:
        server_line = (
            f"Secure server <code>{escape(host)}:{secure.ssl_port}</code> (sign in with your username and "
            f"password). Plain server <code>{escape(host)}:{port}</code> (TCP, Android only)."
        )
        iphone_steps = f"""<li>Tap the iPhone button above and download the file.</li>
<li>Open the <b>Files</b> app &rarr; <b>Downloads</b>, <b>press and hold</b> the file (tapping it would unzip it),
choose <b>Share</b>, then <b>iTAK</b>.</li>
<li>In iTAK: <b>Network &rarr; Servers &rarr; + &rarr; Upload server package</b> and pick <code>{iphone}</code>.</li>
<li>When iTAK asks, enter the <b>username and password</b> you were given.</li>"""
        android_buttons = (
            f'<a class="btn" href="/{secure_android_filename(name)}">Android (ATAK) secure package</a>'
            f'<a class="btn alt" href="/{android}">Android (ATAK) plain package, no login</a>'
        )
    else:
        server_line = f"Server address <code>{escape(host)}:{port}</code> (TCP, no login)."
        iphone_steps = f"""<li>Tap the iPhone button above and download the file.</li>
<li>Open the <b>Files</b> app &rarr; <b>Downloads</b>, <b>press and hold</b> the file (tapping it would unzip it),
choose <b>Share</b>, then <b>iTAK</b>.</li>
<li>In iTAK: <b>Network &rarr; Servers &rarr; + &rarr; Upload server package</b> and pick <code>{iphone}</code>.</li>"""
        android_buttons = f'<a class="btn" href="/{android}">Android (ATAK) connection package</a>'
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(name)} TAK server</title>
<style>body{{font-family:-apple-system,system-ui,sans-serif;max-width:32rem;margin:2rem auto;padding:0 1rem;line-height:1.5}}
a.btn{{display:block;text-align:center;background:#2563eb;color:#fff;padding:1rem;border-radius:.6rem;text-decoration:none;font-weight:600;margin:.6rem 0}}
a.alt{{background:#64748b}}
code{{background:#eee;padding:.1rem .3rem;border-radius:.3rem}}</style></head>
<body>
<h1>{escape(name)} TAK server</h1>
<p>{server_line}</p>
<a class="btn" href="/{iphone}">iPhone (iTAK) connection package</a>
{android_buttons}
<h2>iPhone (iTAK)</h2>
<ol>
{iphone_steps}
</ol>
<h2>Android (ATAK)</h2>
<ol>
<li>Tap an Android button above and download the file.</li>
<li>In ATAK use <b>Import</b> &rarr; Local SD and pick the file from Downloads.</li>
</ol>
<p>The server <b>{escape(name)}</b> then appears in the app's server list and connects.</p>
</body></html>
""".encode()
