"""Cursor-on-Target (CoT) XML building and parsing."""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from . import __version__
from .config import DroneConfig
from .state import VehicleState

GPS_FIX_NAMES = {0: "No GPS", 1: "No fix", 2: "2D", 3: "3D", 4: "DGPS", 5: "RTK float", 6: "RTK fixed"}

# CoT uses 9999999.0 for "unknown" circular/linear error and HAE.
UNKNOWN = 9999999.0


def cot_time(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def parse_cot_time(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _num(value: float, fallback: float = 0.0, digits: int = 7) -> str:
    if value is None or math.isnan(value):
        value = fallback
    return f"{value:.{digits}f}"


def _event(uid: str, cot_type: str, how: str, stale_s: float, now: datetime | None = None) -> ET.Element:
    now = now or datetime.now(timezone.utc)
    return ET.Element(
        "event",
        {
            "version": "2.0",
            "uid": uid,
            "type": cot_type,
            "how": how,
            "time": cot_time(now),
            "start": cot_time(now),
            "stale": cot_time(now + timedelta(seconds=stale_s)),
        },
    )


def _to_bytes(event: ET.Element) -> bytes:
    return ET.tostring(event, encoding="utf-8", xml_declaration=False)


def hae_from_state(state: VehicleState, geoid_offset_m: float) -> float:
    if math.isnan(state.alt_msl):
        return UNKNOWN
    offset = state.undulation if state.undulation is not None else geoid_offset_m
    return state.alt_msl + offset


def remarks_from_state(state: VehicleState) -> str:
    parts = [f"Mode {state.mode}", "ARMED" if state.armed else "Disarmed"]
    if not math.isnan(state.alt_rel):
        parts.append(f"Alt {state.alt_rel:.0f} m AGL(home)")
    if not math.isnan(state.airspeed):
        parts.append(f"AS {state.airspeed:.1f} m/s")
    if not math.isnan(state.groundspeed):
        parts.append(f"GS {state.groundspeed:.1f} m/s")
    if not math.isnan(state.climb):
        parts.append(f"VS {state.climb:+.1f} m/s")
    if state.battery_pct is not None or state.voltage is not None:
        bat = "Bat"
        if state.battery_pct is not None:
            bat += f" {state.battery_pct}%"
        if state.voltage is not None:
            bat += f" {state.voltage:.1f}V"
        parts.append(bat)
    parts.append(f"GPS {GPS_FIX_NAMES.get(state.gps_fix, state.gps_fix)} {state.satellites} sats")
    if not state.connected:
        parts.append("LINK LOST")
    if state.last_command:
        parts.append(f"Last cmd: {state.last_command}")
    if state.last_status_text:
        parts.append(f"Msg: {state.last_status_text}")
    return " | ".join(parts)


def build_uav_event(state: VehicleState, drone: DroneConfig, now: datetime | None = None) -> bytes:
    """CoT position report for the aircraft itself."""
    ev = _event(drone.uid, drone.cot_type, "m-g", drone.stale_s, now)
    ET.SubElement(
        ev,
        "point",
        {
            "lat": _num(state.lat),
            "lon": _num(state.lon),
            "hae": _num(hae_from_state(state, drone.geoid_offset_m), UNKNOWN, 2),
            "ce": _num(state.h_acc if state.h_acc is not None else math.nan, UNKNOWN, 1),
            "le": _num(state.v_acc if state.v_acc is not None else math.nan, UNKNOWN, 1),
        },
    )
    detail = ET.SubElement(ev, "detail")
    ET.SubElement(detail, "contact", {"callsign": drone.callsign})
    ET.SubElement(detail, "uid", {"Droid": drone.callsign})
    ET.SubElement(detail, "__group", {"name": drone.team, "role": drone.role})
    course = state.course if not math.isnan(state.course) else state.heading
    ET.SubElement(
        detail,
        "track",
        {"course": _num(course, 0.0, 1), "speed": _num(state.groundspeed, 0.0, 2)},
    )
    ET.SubElement(detail, "precisionlocation", {"altsrc": "GPS", "geopointsrc": "GPS"})
    if state.battery_pct is not None:
        ET.SubElement(detail, "status", {"battery": str(max(0, min(100, state.battery_pct)))})
    ET.SubElement(
        detail,
        "takv",
        {"device": "Raspberry Pi GCS", "platform": "atak-bridge", "os": "Linux", "version": __version__},
    )
    if drone.video_url:
        ET.SubElement(detail, "__video", {"url": drone.video_url})
    ET.SubElement(detail, "remarks").text = remarks_from_state(state)
    return _to_bytes(ev)


def build_home_event(state: VehicleState, drone: DroneConfig, now: datetime | None = None) -> bytes:
    """Waypoint marker for the aircraft's home / launch point."""
    ev = _event(f"{drone.uid}-home", "b-m-p-w", "h-e", max(drone.stale_s, 60.0), now)
    hae = UNKNOWN
    if not math.isnan(state.home_alt_msl):
        offset = state.undulation if state.undulation is not None else drone.geoid_offset_m
        hae = state.home_alt_msl + offset
    ET.SubElement(
        ev,
        "point",
        {"lat": _num(state.home_lat), "lon": _num(state.home_lon), "hae": _num(hae, UNKNOWN, 2), "ce": "10.0", "le": "10.0"},
    )
    detail = ET.SubElement(ev, "detail")
    ET.SubElement(detail, "contact", {"callsign": f"{drone.callsign} HOME"})
    ET.SubElement(detail, "link", {"uid": drone.uid, "type": drone.cot_type, "relation": "p-p", "parent_callsign": drone.callsign})
    ET.SubElement(detail, "usericon", {"iconsetpath": "COT_MAPPING_SPOTMAP/b-m-p-s-m/-65536"})
    ET.SubElement(detail, "remarks").text = f"Home / launch point of {drone.callsign}"
    return _to_bytes(ev)


def build_pong(now: datetime | None = None) -> bytes:
    ev = _event("takPong", "t-x-c-t-r", "h-g-i-g-o", 20.0, now)
    ET.SubElement(ev, "point", {"lat": "0.0", "lon": "0.0", "hae": "0.0", "ce": str(UNKNOWN), "le": str(UNKNOWN)})
    ET.SubElement(ev, "detail")
    return _to_bytes(ev)


@dataclass
class CotEvent:
    uid: str
    type: str
    lat: float
    lon: float
    hae: float
    callsign: str
    sender_callsign: str
    time: datetime | None
    stale: datetime | None
    # For t-x-d-d (delete) events: the uid being deleted.
    link_uid: str
    raw: bytes

    @property
    def is_ping(self) -> bool:
        return self.type == "t-x-c-t"

    @property
    def is_delete(self) -> bool:
        return self.type.startswith("t-x-d-d")


def parse_event(raw: bytes) -> CotEvent | None:
    """Parse a single <event> document. Returns None if it is not valid CoT."""
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return None
    if root.tag != "event":
        return None
    uid = root.get("uid")
    cot_type = root.get("type")
    if not uid or not cot_type:
        return None
    point = root.find("point")
    try:
        lat = float(point.get("lat")) if point is not None else math.nan
        lon = float(point.get("lon")) if point is not None else math.nan
        hae = float(point.get("hae", UNKNOWN)) if point is not None else UNKNOWN
    except (TypeError, ValueError):
        lat = lon = math.nan
        hae = UNKNOWN
    callsign = ""
    sender = ""
    link_uid = ""
    detail = root.find("detail")
    if detail is not None:
        contact = detail.find("contact")
        if contact is not None:
            callsign = contact.get("callsign", "")
        for link in detail.findall("link"):
            if not link_uid and link.get("uid"):
                link_uid = link.get("uid", "")
            if not sender and link.get("relation") == "p-p" and link.get("parent_callsign"):
                sender = link.get("parent_callsign", "")
    return CotEvent(
        uid=uid,
        type=cot_type,
        lat=lat,
        lon=lon,
        hae=hae,
        callsign=callsign,
        sender_callsign=sender,
        time=parse_cot_time(root.get("time", "")),
        stale=parse_cot_time(root.get("stale", "")),
        link_uid=link_uid,
        raw=raw,
    )


class CotStreamParser:
    """Splits a TCP byte stream of concatenated CoT XML events into single events."""

    END = b"</event>"

    def __init__(self, max_buffer: int = 256 * 1024) -> None:
        self._buf = bytearray()
        self._max = max_buffer

    def feed(self, data: bytes) -> list[bytes]:
        self._buf += data
        events: list[bytes] = []
        while True:
            idx = self._buf.find(self.END)
            if idx < 0:
                break
            end = idx + len(self.END)
            chunk = bytes(self._buf[:end])
            del self._buf[:end]
            start = chunk.find(b"<event")
            if start >= 0:
                events.append(chunk[start:])
        if len(self._buf) > self._max:
            self._buf.clear()
            raise ValueError("CoT message exceeds maximum size")
        return events
