"""MAVLink connection to the aircraft (runs in its own thread; pymavlink is blocking)."""

from __future__ import annotations

import logging
import math
import os
import queue
import threading
import time
from dataclasses import dataclass

from .config import MavlinkConfig
from .state import StateStore

# Ask pymavlink to parse MAVLink 2 so extension fields (alt_ellipsoid, h_acc, ...) are available.
os.environ.setdefault("MAVLINK20", "1")

log = logging.getLogger(__name__)

UINT16_MAX = 65535


@dataclass
class RepositionCommand:
    lat: float
    lon: float
    alt_rel_m: float
    loiter_radius_m: float
    description: str


@dataclass
class ReturnHomeCommand:
    description: str = "RETURN HOME (RTL)"


Command = RepositionCommand | ReturnHomeCommand


class MavlinkLink(threading.Thread):
    HEARTBEAT_PERIOD_S = 1.0
    REQUEST_PERIOD_S = 10.0

    def __init__(self, cfg: MavlinkConfig, store: StateStore) -> None:
        super().__init__(name="mavlink", daemon=True)
        self.cfg = cfg
        self.store = store
        self._stop_evt = threading.Event()
        self._commands: queue.Queue[Command] = queue.Queue(maxsize=16)
        self._target_system = cfg.target_system
        self._target_component = 0
        self._mavutil = None
        # (MAV_CMD id, description) of the last command sent, for reporting its ACK.
        self._pending: tuple[int, str] | None = None

    # ----------------------------------------------------------------- public
    def stop(self) -> None:
        self._stop_evt.set()

    def submit_command(self, cmd: Command) -> bool:
        try:
            self._commands.put_nowait(cmd)
            return True
        except queue.Full:
            log.warning("Command queue full, dropping %s", cmd.description)
            return False

    # --------------------------------------------------------------- internal
    def run(self) -> None:
        from pymavlink import mavutil  # imported lazily so the rest of the package works without it

        self._mavutil = mavutil
        while not self._stop_evt.is_set():
            conn = None
            try:
                log.info("Opening MAVLink connection %s", self.cfg.connection)
                conn = mavutil.mavlink_connection(
                    self.cfg.connection,
                    baud=self.cfg.baud,
                    source_system=self.cfg.source_system,
                    source_component=self.cfg.source_component,
                )
                self._loop(conn)
            except Exception:  # noqa: BLE001 - keep the GCS alive whatever happens on the link
                log.exception("MAVLink connection error; retrying in 2 s")
                self.store.update(connected=False)
                self._stop_evt.wait(2.0)
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:  # noqa: BLE001
                        pass

    def _loop(self, conn) -> None:
        mav = self._mavutil.mavlink
        last_hb_sent = 0.0
        last_request = 0.0
        while not self._stop_evt.is_set():
            now = time.monotonic()

            if self.cfg.send_heartbeat and now - last_hb_sent >= self.HEARTBEAT_PERIOD_S:
                conn.mav.heartbeat_send(mav.MAV_TYPE_GCS, mav.MAV_AUTOPILOT_INVALID, 0, 0, mav.MAV_STATE_ACTIVE)
                last_hb_sent = now

            snap = self.store.snapshot()
            if snap.connected and now - snap.last_heartbeat > self.cfg.link_timeout_s:
                log.warning("Lost MAVLink heartbeat from vehicle %d", self._target_system)
                self.store.update(connected=False)

            if self._target_component and snap.connected and now - last_request >= self.REQUEST_PERIOD_S:
                stale_pos = now - snap.last_position > 3.0
                if stale_pos or not snap.has_home or last_request == 0.0:
                    self._request_streams(conn, want_home=not snap.has_home)
                last_request = now

            self._drain_commands(conn)

            msg = conn.recv_match(blocking=True, timeout=0.2)
            if msg is not None:
                self._handle(msg)

    def _request_streams(self, conn, want_home: bool) -> None:
        mav = self._mavutil.mavlink
        ts, tc = self._target_system, self._target_component
        rate = max(1, int(self.cfg.stream_rate_hz))
        log.info("Requesting telemetry streams at %d Hz from %d/%d", rate, ts, tc)
        # ArduPilot legacy stream request.
        conn.mav.request_data_stream_send(ts, tc, mav.MAV_DATA_STREAM_ALL, rate, 1)
        # Per-message intervals (PX4 and newer ArduPilot).
        interval_us = int(1e6 / rate)
        for msg_id in (
            mav.MAVLINK_MSG_ID_GLOBAL_POSITION_INT,
            mav.MAVLINK_MSG_ID_GPS_RAW_INT,
            mav.MAVLINK_MSG_ID_VFR_HUD,
            mav.MAVLINK_MSG_ID_SYS_STATUS,
        ):
            conn.mav.command_long_send(
                ts, tc, mav.MAV_CMD_SET_MESSAGE_INTERVAL, 0, msg_id, interval_us, 0, 0, 0, 0, 0
            )
        if want_home:
            conn.mav.command_long_send(
                ts, tc, mav.MAV_CMD_REQUEST_MESSAGE, 0, mav.MAVLINK_MSG_ID_HOME_POSITION, 0, 0, 0, 0, 0, 0
            )
            conn.mav.command_long_send(ts, tc, mav.MAV_CMD_GET_HOME_POSITION, 0, 0, 0, 0, 0, 0, 0, 0)

    def _drain_commands(self, conn) -> None:
        mav = self._mavutil.mavlink
        while True:
            try:
                cmd = self._commands.get_nowait()
            except queue.Empty:
                return
            if not self._target_component:
                log.warning("No vehicle connected; dropping %s", cmd.description)
                continue
            if isinstance(cmd, ReturnHomeCommand):
                log.info("Sending RETURN_TO_LAUNCH")
                conn.mav.command_long_send(
                    self._target_system, self._target_component,
                    mav.MAV_CMD_NAV_RETURN_TO_LAUNCH, 0, 0, 0, 0, 0, 0, 0, 0,
                )
                self._pending = (mav.MAV_CMD_NAV_RETURN_TO_LAUNCH, cmd.description)
                self.store.update(last_command=f"{cmd.description} (sent)")
                continue
            log.info("Sending DO_REPOSITION: %s", cmd.description)
            conn.mav.command_int_send(
                self._target_system,
                self._target_component,
                mav.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
                mav.MAV_CMD_DO_REPOSITION,
                0,  # current
                0,  # autocontinue
                -1,  # param1: ground speed (-1 = default)
                mav.MAV_DO_REPOSITION_FLAGS_CHANGE_MODE,  # param2: switch to GUIDED/HOLD
                cmd.loiter_radius_m,  # param3: loiter radius (ArduPlane)
                math.nan,  # param4: yaw (unused for fixed wing)
                int(round(cmd.lat * 1e7)),
                int(round(cmd.lon * 1e7)),
                float(cmd.alt_rel_m),
            )
            self._pending = (mav.MAV_CMD_DO_REPOSITION, cmd.description)
            self.store.update(last_command=f"{cmd.description} (sent)")

    def _handle(self, msg) -> None:
        mav = self._mavutil.mavlink
        mtype = msg.get_type()
        if mtype == "BAD_DATA":
            return
        src_sys, src_comp = msg.get_srcSystem(), msg.get_srcComponent()

        if mtype == "HEARTBEAT":
            if msg.type == mav.MAV_TYPE_GCS or msg.autopilot == mav.MAV_AUTOPILOT_INVALID:
                return
            if not self._target_component:
                if self._target_system and src_sys != self._target_system:
                    return
                self._target_system, self._target_component = src_sys, src_comp
                log.info("Locked onto vehicle system %d component %d", src_sys, src_comp)
            if (src_sys, src_comp) != (self._target_system, self._target_component):
                return
            snap = self.store.snapshot()
            if not snap.connected:
                log.info("MAVLink heartbeat from vehicle %d acquired", src_sys)
            self.store.update(
                connected=True,
                last_heartbeat=time.monotonic(),
                system_id=src_sys,
                mode=self._mavutil.mode_string_v10(msg),
                armed=bool(msg.base_mode & mav.MAV_MODE_FLAG_SAFETY_ARMED),
            )
            return

        if (src_sys, src_comp) != (self._target_system, self._target_component):
            return

        if mtype == "GLOBAL_POSITION_INT":
            if msg.lat == 0 and msg.lon == 0:
                return
            fields = dict(
                lat=msg.lat / 1e7,
                lon=msg.lon / 1e7,
                alt_msl=msg.alt / 1000.0,
                alt_rel=msg.relative_alt / 1000.0,
                last_position=time.monotonic(),
            )
            if msg.hdg != UINT16_MAX:
                fields["heading"] = msg.hdg / 100.0
            self.store.update(**fields)
        elif mtype == "GPS_RAW_INT":
            fields = dict(gps_fix=msg.fix_type, satellites=msg.satellites_visible)
            if msg.cog != UINT16_MAX:
                fields["course"] = msg.cog / 100.0
            h_acc = getattr(msg, "h_acc", 0)
            v_acc = getattr(msg, "v_acc", 0)
            fields["h_acc"] = h_acc / 1000.0 if h_acc else None
            fields["v_acc"] = v_acc / 1000.0 if v_acc else None
            alt_ellipsoid = getattr(msg, "alt_ellipsoid", 0)
            if alt_ellipsoid and msg.fix_type >= 3:
                fields["undulation"] = (alt_ellipsoid - msg.alt) / 1000.0
            self.store.update(**fields)
        elif mtype == "VFR_HUD":
            self.store.update(
                airspeed=float(msg.airspeed),
                groundspeed=float(msg.groundspeed),
                climb=float(msg.climb),
                throttle=int(msg.throttle),
            )
        elif mtype == "SYS_STATUS":
            self.store.update(
                battery_pct=msg.battery_remaining if msg.battery_remaining >= 0 else None,
                voltage=msg.voltage_battery / 1000.0 if msg.voltage_battery not in (0, UINT16_MAX) else None,
            )
        elif mtype == "HOME_POSITION":
            snap = self.store.snapshot()
            if not snap.has_home:
                log.info("Home position received")
            self.store.update(
                home_lat=msg.latitude / 1e7,
                home_lon=msg.longitude / 1e7,
                home_alt_msl=msg.altitude / 1000.0,
            )
        elif mtype == "STATUSTEXT":
            text = msg.text if isinstance(msg.text, str) else msg.text.decode(errors="replace")
            text = text.rstrip("\x00").strip()
            log.info("Vehicle: %s", text)
            if msg.severity <= mav.MAV_SEVERITY_WARNING:
                self.store.update(last_status_text=text)
        elif mtype == "COMMAND_ACK":
            result = mav.enums["MAV_RESULT"].get(msg.result)
            cmd = mav.enums["MAV_CMD"].get(msg.command)
            log.info(
                "COMMAND_ACK %s -> %s",
                cmd.name if cmd else msg.command,
                result.name if result else msg.result,
            )
            if self._pending and msg.command == self._pending[0]:
                status = "accepted" if msg.result == mav.MAV_RESULT_ACCEPTED else "REJECTED"
                self.store.update(last_command=f"{self._pending[1]} ({status})")
