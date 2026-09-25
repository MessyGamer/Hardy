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
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

try:
    import pymavlink  # noqa: F401
except ImportError:
    sys.exit("pymavlink is missing. Install it first with:  pip install pymavlink")

from atak_bridge.app import Bridge  # noqa: E402
from atak_bridge.config import Config  # noqa: E402
from atak_bridge.mavlink_link import MavlinkLink  # noqa: E402
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


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lat", type=float, default=35.0, help="latitude the pretend drone circles")
    ap.add_argument("--lon", type=float, default=-117.0, help="longitude the pretend drone circles")
    ap.add_argument("--port", type=int, default=8087, help="TAK server port for ATAK to connect to")
    args = ap.parse_args()

    cfg = Config()
    cfg.log_level = "INFO"
    cfg.mavlink.connection = f"udpin:127.0.0.1:{DEMO_PORT_MAVLINK}"
    cfg.drone.callsign = "DEMO-DRONE"
    cfg.drone.uid = "DEMO-DRONE-1"
    cfg.tak_server.port = args.port
    # Safe to enable here: the only thing it can steer is the pretend drone.
    cfg.commands.enabled = True

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
    print(
        f"""
==================================================================
  DEMO RUNNING - pretend drone circling {args.lat:.5f}, {args.lon:.5f}

  In ATAK:  Settings > Network Preferences > TAK Servers > Add
            Address:  {ip}
            Port:     {args.port}      Protocol: TCP  (untick SSL/TLS)

  Look for "DEMO-DRONE" on the map. Drop a marker, rename it
  GOTO and the drone flies to it. Press Ctrl+C here to stop.
==================================================================
"""
    )

    store = StateStore()
    link = MavlinkLink(cfg.mavlink, store)
    link.start()
    try:
        asyncio.run(Bridge(cfg, store, link).run())
    except KeyboardInterrupt:
        pass
    finally:
        link.stop()
        fake.terminate()
        print("Demo stopped.")


if __name__ == "__main__":
    main()
