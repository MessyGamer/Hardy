#!/usr/bin/env python3
"""One-command demo: a pretend drone plus the ATAK server, all on this computer.

    pip install pymavlink
    python demo.py --lat 40.7128 --lon -74.0060

Then connect ATAK (phone on the same Wi-Fi) to the address this prints, port 8087, TCP.
Drop a map marker named "GOTO" and the pretend drone flies there and circles it.
Press Ctrl+C to stop.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import secrets
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

missing = []
for module, package in (("pymavlink", "pymavlink"), ("cryptography", "cryptography")):
    try:
        __import__(module)
    except ImportError:
        missing.append(package)
if missing:
    sys.exit(f"Missing pieces. Install them first with:  pip install {' '.join(missing)}")

from atak_bridge.app import Bridge  # noqa: E402
from atak_bridge.config import Config  # noqa: E402
from atak_bridge.datapackage import build_server_package, package_filename  # noqa: E402
from atak_bridge.mavlink_link import MavlinkLink  # noqa: E402
from atak_bridge.probe import start_probes  # noqa: E402
from atak_bridge.state import StateStore  # noqa: E402

DEMO_PORT_MAVLINK = 14561


def lan_ip() -> str:
    """Best guess at this computer's address on the local network (sends nothing)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


async def run(bridge: Bridge, args: argparse.Namespace) -> None:
    skip = {args.port, bridge.cfg.secure.ssl_port, bridge.cfg.secure.enrollment_port}
    probes = await start_probes(skip=skip) if args.probe else []
    try:
        await bridge.run()
    finally:
        for server in probes:
            server.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lat", type=float, default=35.0, help="latitude the pretend drone circles")
    ap.add_argument("--lon", type=float, default=-117.0, help="longitude the pretend drone circles")
    ap.add_argument("--port", type=int, default=8087, help="TAK server port for ATAK to connect to")
    ap.add_argument(
        "--probe", action="store_true", help="also log connection attempts on other common TAK ports (diagnostic)"
    )
    args = ap.parse_args()

    cfg = Config()
    cfg.log_level = "INFO"
    cfg.mavlink.connection = f"udpin:127.0.0.1:{DEMO_PORT_MAVLINK}"
    cfg.drone.callsign = "DEMO-DRONE"
    cfg.drone.uid = "DEMO-DRONE-1"
    cfg.tak_server.port = args.port
    # Safe to enable here: the only thing it can steer is the pretend drone.
    cfg.commands.enabled = True

    # Secure (certificate) connections, which iTAK requires. The demo keeps its certificates
    # and a generated password in ./tak-certs so phones stay signed in between runs.
    cert_dir = ROOT / "tak-certs"
    cert_dir.mkdir(exist_ok=True)
    login_file = cert_dir / "demo-login.txt"
    if login_file.exists():
        password = login_file.read_text().strip()
    else:
        alphabet = "abcdefghjkmnpqrstuvwxyz23456789"  # no look-alikes (0/o, 1/l/i)
        password = "".join(secrets.choice(alphabet) for _ in range(8))
        login_file.write_text(password + "\n")
    cfg.secure.enabled = True
    cfg.secure.cert_dir = str(cert_dir)
    cfg.secure.users = {"hardy": password}

    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")

    fake = subprocess.Popen(
        [
            sys.executable,
            str(ROOT / "tools" / "fake_uav.py"),
            "--lat", str(args.lat),
            "--lon", str(args.lon),
            "--out", f"udpout:127.0.0.1:{DEMO_PORT_MAVLINK}",
        ]
    )

    ip = lan_ip()
    # Backup route for phones: a ready-made connection package saved next to this script,
    # which can be emailed / AirDropped / iCloud-shared to the phone and opened in iTAK or ATAK.
    package_path = ROOT / package_filename(cfg.tak_server.name, itak=True)
    try:
        for itak in (False, True):
            (ROOT / package_filename(cfg.tak_server.name, itak)).write_bytes(
                build_server_package(cfg.tak_server.name, ip, args.port, itak=itak)
            )
    except OSError:
        package_path = None
    print(
        f"""
==================================================================
  DEMO RUNNING - pretend drone circling {args.lat:.5f}, {args.lon:.5f}

  iTAK / ATAK LOGIN:   username  hardy      password  {password}

  EASIEST (iPhone or Android): open this in the phone's web browser
      http://{ip}:{args.port}
  and tap the iPhone or Android button, then open the file in iTAK/ATAK.

  Backup: the files are also saved here (iPhone one shown) - email it to your phone
      {package_path or "(could not save)"}

  Or add it by hand in ATAK (Android):
      Settings > Network Preferences > TAK Servers > Add
      Address {ip}   Port {args.port}   TCP   (untick SSL/TLS)

  Look for "DEMO-DRONE" on the map. Drop a marker, rename it
  GOTO and the drone flies to it. Press Ctrl+C here to stop.
==================================================================
"""
    )

    store = StateStore()
    link = MavlinkLink(cfg.mavlink, store)
    link.start()
    try:
        asyncio.run(run(Bridge(cfg, store, link), args))
    except KeyboardInterrupt:
        pass
    finally:
        link.stop()
        fake.terminate()
        print("Demo stopped.")


if __name__ == "__main__":
    main()
