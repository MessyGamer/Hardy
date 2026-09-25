#!/usr/bin/env python3
"""Bench-test helper: pretends to be an ArduPlane circling a point, speaking MAVLink over UDP.

    python tools/fake_uav.py --lat 35.0 --lon -117.0 --out udpout:127.0.0.1:14551

Run atak-bridge with the default connection ("udpin:0.0.0.0:14551") and the aircraft
appears in ATAK. DO_REPOSITION commands are acknowledged and the fake aircraft re-centres its orbit there.
"""

from __future__ import annotations

import argparse
import math
import os
import time

os.environ.setdefault("MAVLINK20", "1")
from pymavlink import mavutil  # noqa: E402

M_PER_DEG = 111_320.0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="udpout:127.0.0.1:14551")
    ap.add_argument("--lat", type=float, default=35.0)
    ap.add_argument("--lon", type=float, default=-117.0)
    ap.add_argument("--alt", type=float, default=700.0, help="home altitude MSL (m)")
    ap.add_argument("--radius", type=float, default=300.0)
    ap.add_argument("--speed", type=float, default=18.0)
    args = ap.parse_args()

    conn = mavutil.mavlink_connection(args.out, source_system=1, source_component=1)
    mav = conn.mav
    ml = mavutil.mavlink

    center_lat, center_lon = args.lat, args.lon
    radius = args.radius
    rel_alt = 100.0
    angle = 0.0
    custom_mode = 10  # ArduPlane AUTO
    battery = 100.0
    last_hb = 0.0
    dt = 0.1

    print(f"Fake ArduPlane sending to {args.out}; Ctrl+C to stop")
    while True:
        now = time.monotonic()
        omega = args.speed / radius
        angle = (angle + omega * dt) % (2 * math.pi)
        lat = center_lat + (radius * math.cos(angle)) / M_PER_DEG
        lon = center_lon + (radius * math.sin(angle)) / (M_PER_DEG * math.cos(math.radians(center_lat)))
        heading = (math.degrees(angle) + 90) % 360
        battery = max(0.0, battery - 0.002)

        if now - last_hb >= 1.0:
            mav.heartbeat_send(
                ml.MAV_TYPE_FIXED_WING,
                ml.MAV_AUTOPILOT_ARDUPILOTMEGA,
                ml.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED | ml.MAV_MODE_FLAG_SAFETY_ARMED,
                custom_mode,
                ml.MAV_STATE_ACTIVE,
            )
            mav.sys_status_send(0, 0, 0, 500, int(15.8 * 1000), -1, int(battery), 0, 0, 0, 0, 0, 0)
            mav.home_position_send(
                int(args.lat * 1e7), int(args.lon * 1e7), int(args.alt * 1000), 0, 0, 0, [1, 0, 0, 0], 0, 0, 0
            )
            last_hb = now

        t_boot = int(now * 1000) & 0xFFFFFFFF
        mav.global_position_int_send(
            t_boot,
            int(lat * 1e7),
            int(lon * 1e7),
            int((args.alt + rel_alt) * 1000),
            int(rel_alt * 1000),
            int(args.speed * math.cos(math.radians(heading)) * 100),
            int(args.speed * math.sin(math.radians(heading)) * 100),
            0,
            int(heading * 100),
        )
        mav.gps_raw_int_send(
            t_boot * 1000, 3, int(lat * 1e7), int(lon * 1e7), int((args.alt + rel_alt) * 1000),
            80, 120, int(args.speed * 100), int(heading * 100), 14,
            int((args.alt + rel_alt - 33.0) * 1000), 1500, 2500, 300, 0, 0,
        )
        mav.vfr_hud_send(args.speed + 0.5, args.speed, int(heading), 55, args.alt + rel_alt, 0.0)

        while (msg := conn.recv_match(blocking=False)) is not None:
            if msg.get_type() == "COMMAND_INT" and msg.command == ml.MAV_CMD_DO_REPOSITION:
                center_lat, center_lon, rel_alt = msg.x / 1e7, msg.y / 1e7, msg.z
                if msg.param3 >= 1:
                    radius = msg.param3
                custom_mode = 15  # GUIDED
                print(f"DO_REPOSITION -> {center_lat:.5f},{center_lon:.5f} @ {rel_alt:.0f} m")
                mav.command_ack_send(ml.MAV_CMD_DO_REPOSITION, ml.MAV_RESULT_ACCEPTED)

        time.sleep(dt)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
