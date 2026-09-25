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
    <Content ignore="false" zipEntry="config.pref"/>
  </Contents>
</MissionPackageManifest>
"""


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


def build_download_page(name: str, host: str, port: int) -> bytes:
    android = package_filename(name)
    iphone = package_filename(name, itak=True)
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(name)} TAK server</title>
<style>body{{font-family:-apple-system,system-ui,sans-serif;max-width:32rem;margin:2rem auto;padding:0 1rem;line-height:1.5}}
a.btn{{display:block;text-align:center;background:#2563eb;color:#fff;padding:1rem;border-radius:.6rem;text-decoration:none;font-weight:600;margin:.6rem 0}}
code{{background:#eee;padding:.1rem .3rem;border-radius:.3rem}}</style></head>
<body>
<h1>{escape(name)} TAK server</h1>
<p>Server address <code>{escape(host)}:{port}</code> (TCP, no login).</p>
<a class="btn" href="/{iphone}">iPhone (iTAK) connection package</a>
<a class="btn" href="/{android}">Android (ATAK) connection package</a>
<h2>iPhone (iTAK)</h2>
<ol>
<li>Tap the iPhone button above and download the file.</li>
<li>Open the <b>Files</b> app &rarr; <b>Downloads</b>, <b>press and hold</b> the file (tapping it would unzip it),
choose <b>Share</b>, then <b>iTAK</b>.</li>
<li>In iTAK: <b>Network &rarr; Servers &rarr; + &rarr; Upload server package</b> and pick <code>{iphone}</code>.</li>
</ol>
<h2>Android (ATAK)</h2>
<ol>
<li>Tap the Android button above and download the file.</li>
<li>In ATAK use <b>Import</b> &rarr; Local SD and pick the file from Downloads.</li>
</ol>
<p>The server <b>{escape(name)}</b> then appears in the app's server list and connects.</p>
</body></html>
""".encode()
