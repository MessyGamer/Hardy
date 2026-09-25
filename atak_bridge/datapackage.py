"""TAK "data package" that configures ATAK/iTAK to connect to this server over plain TCP.

iTAK's manual add-server screen tries certificate enrollment, which this server does not
offer; importing this package instead sets up a plain TCP connection with no login.
"""

from __future__ import annotations

import io
import uuid
import zipfile
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
    <Content ignore="false" zipEntry="server.pref"/>
  </Contents>
</MissionPackageManifest>
"""


def package_filename(name: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in name).strip("-") or "tak"
    return f"{safe.lower()}-tak-server.zip"


def build_server_package(name: str, host: str, port: int) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("server.pref", PREF_TEMPLATE.format(name=escape(name), host=escape(host), port=port))
        zf.writestr(
            "MANIFEST/manifest.xml",
            MANIFEST_TEMPLATE.format(uid=uuid.uuid5(uuid.NAMESPACE_DNS, f"{host}:{port}"), name=escape(name)),
        )
    return buf.getvalue()


def build_download_page(name: str, host: str, port: int) -> bytes:
    filename = package_filename(name)
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(name)} TAK server</title>
<style>body{{font-family:-apple-system,system-ui,sans-serif;max-width:32rem;margin:2rem auto;padding:0 1rem;line-height:1.5}}
a.btn{{display:block;text-align:center;background:#2563eb;color:#fff;padding:1rem;border-radius:.6rem;text-decoration:none;font-weight:600}}
code{{background:#eee;padding:.1rem .3rem;border-radius:.3rem}}</style></head>
<body>
<h1>{escape(name)} TAK server</h1>
<p>Server address <code>{escape(host)}:{port}</code> (TCP, no login).</p>
<p><a class="btn" href="/{filename}">Download connection package</a></p>
<ol>
<li>Tap the button above and download the file.</li>
<li><b>iPhone (iTAK):</b> open the <b>Files</b> app, go to <b>Downloads</b>, tap the file, then use the
<b>Share</b> button and choose <b>iTAK</b>. Or, in iTAK, use its import option and pick the file from Downloads.</li>
<li><b>Android (ATAK):</b> in ATAK use <b>Import</b> &rarr; Local SD and pick the file from Downloads.</li>
<li>The server <b>{escape(name)}</b> appears in the app's server list and connects.</li>
</ol>
</body></html>
""".encode()
