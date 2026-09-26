#!/usr/bin/env python3
"""Bench-test helper: pretends to be an ArduPlane circling a point, speaking MAVLink over UDP.

    python tools/fake_uav.py --lat 35.0 --lon -117.0

By default it listens like ArduPilot SITL on TCP 127.0.0.1:5760, so run atak-bridge with
connection = "tcp:127.0.0.1:5760" and the aircraft appears in ATAK. (UDP also works on
Linux, e.g. --out udpout:127.0.0.1:14551, but not on Windows, where pymavlink's UDP sender
binds the destination port itself and replies never reach it.) DO_REPOSITION commands are acknowledged and the fake aircraft
flies there and circles it.
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
    ap.add_argument("--out", default="tcpin:127.0.0.1:5760")
    ap.add_argument("--lat", type=float, default=35.0)
    ap.add_argument("--lon", type=float, default=-117.0)
    ap.add_argument("--alt", type=float, default=700.0, help="home altitude MSL (m)")
    ap.add_argument("--radius", type=float, default=300.0)
    ap.add_argument("--speed", type=float, default=18.0)
    args = ap.parse_args()

    conn = mavutil.mavlink_connection(args.out, source_system=1, source_component=1)
    mav = conn.mav
    ml = mavutil.mavlink

    cos_lat = math.cos(math.radians(args.lat))

    def to_latlon(x: float, y: float) -> tuple[float, float]:
        """Local east/north metres from home -> lat/lon."""
        return args.lat + y / M_PER_DEG, args.lon + x / (M_PER_DEG * cos_lat)

    # Orbit centre, aircraft position (east/north metres from home) and altitudes.
    cx, cy = 0.0, 0.0
    x, y = args.radius, 0.0
    radius = args.radius
    rel_alt, target_alt = 100.0, 100.0
    climb = 0.0
    custom_mode = 10  # ArduPlane AUTO
    battery = 100.0
    last_hb = 0.0
    dt = 0.1

    print(f"Fake ArduPlane sending to {args.out}; Ctrl+C to stop")
    while True:
        now = time.monotonic()

        # Fly straight at the orbit centre until near the circle, then orbit it anticlockwise.
        dx, dy = x - cx, y - cy
        d = math.hypot(dx, dy) or 1e-6
        rx, ry = dx / d, dy / d
        if d > radius * 1.5:
            vx, vy = -rx, -ry
        else:
            correction = max(-1.0, min(1.0, (d - radius) / 50.0))
            vx, vy = -ry - correction * rx, rx - correction * ry
        norm = math.hypot(vx, vy)
        vx, vy = vx / norm, vy / norm
        x += vx * args.speed * dt
        y += vy * args.speed * dt
        heading = math.degrees(math.atan2(vx, vy)) % 360
        lat, lon = to_latlon(x, y)

        climb = max(-3.0, min(3.0, target_alt - rel_alt))
        rel_alt += climb * dt
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
            int(-climb * 100),
            int(heading * 100),
        )
        mav.gps_raw_int_send(
            t_boot * 1000, 3, int(lat * 1e7), int(lon * 1e7), int((args.alt + rel_alt) * 1000),
            80, 120, int(args.speed * 100), int(heading * 100), 14,
            int((args.alt + rel_alt - 33.0) * 1000), 1500, 2500, 300, 0, 0,
        )
        mav.vfr_hud_send(args.speed + 0.5, args.speed, int(heading), 55, args.alt + rel_alt, climb)

        while (msg := conn.recv_match(blocking=False)) is not None:
            if msg.get_type() == "COMMAND_LONG" and msg.command == ml.MAV_CMD_NAV_RETURN_TO_LAUNCH:
                cx, cy, target_alt, radius = 0.0, 0.0, 100.0, args.radius
                custom_mode = 11  # ArduPlane RTL
                print("RETURN_TO_LAUNCH -> flying home")
                mav.command_ack_send(ml.MAV_CMD_NAV_RETURN_TO_LAUNCH, ml.MAV_RESULT_ACCEPTED)
            if msg.get_type() == "COMMAND_INT" and msg.command == ml.MAV_CMD_DO_REPOSITION:
                tlat, tlon, target_alt = msg.x / 1e7, msg.y / 1e7, msg.z
                cx = (tlon - args.lon) * M_PER_DEG * cos_lat
                cy = (tlat - args.lat) * M_PER_DEG
                if msg.param3 >= 1:
                    radius = msg.param3
                custom_mode = 15  # GUIDED
                print(f"DO_REPOSITION -> {tlat:.5f},{tlon:.5f} @ {target_alt:.0f} m")
                mav.command_ack_send(ml.MAV_CMD_DO_REPOSITION, ml.MAV_RESULT_ACCEPTED)

        time.sleep(dt)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
