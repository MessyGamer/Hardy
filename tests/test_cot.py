import math
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import pytest

from atak_bridge.config import DroneConfig
from atak_bridge.cot import (
    CotStreamParser,
    build_home_event,
    build_pong,
    build_uav_event,
    parse_event,
)
from atak_bridge.state import VehicleState

NOW = datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc)


def flying_state(**overrides):
    s = VehicleState(
        connected=True,
        mode="AUTO",
        armed=True,
        lat=35.1234567,
        lon=-117.7654321,
        alt_msl=800.0,
        alt_rel=100.0,
        undulation=-33.0,
        heading=90.0,
        course=92.5,
        groundspeed=18.2,
        airspeed=19.0,
        climb=0.4,
        gps_fix=3,
        satellites=14,
        h_acc=1.5,
        v_acc=2.5,
        battery_pct=76,
        voltage=15.8,
        home_lat=35.12,
        home_lon=-117.76,
        home_alt_msl=700.0,
    )
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


def test_uav_event_fields():
    drone = DroneConfig(video_url="rtsp://10.0.0.5:8554/live")
    root = ET.fromstring(build_uav_event(flying_state(), drone, NOW))
    assert root.get("uid") == drone.uid
    assert root.get("type") == "a-f-A-M-F-Q"
    assert root.get("time") == "2026-09-25T12:00:00.000Z"
    assert root.get("stale") == "2026-09-25T12:00:10.000Z"
    pt = root.find("point")
    assert float(pt.get("lat")) == pytest.approx(35.1234567)
    assert float(pt.get("lon")) == pytest.approx(-117.7654321)
    assert float(pt.get("hae")) == pytest.approx(767.0)  # MSL + undulation
    assert float(pt.get("ce")) == pytest.approx(1.5)
    d = root.find("detail")
    assert d.find("contact").get("callsign") == drone.callsign
    assert float(d.find("track").get("course")) == pytest.approx(92.5)
    assert float(d.find("track").get("speed")) == pytest.approx(18.2)
    assert d.find("status").get("battery") == "76"
    assert d.find("__video").get("url") == "rtsp://10.0.0.5:8554/live"
    remarks = d.find("remarks").text
    assert "Mode AUTO" in remarks and "ARMED" in remarks and "76%" in remarks


def test_uav_event_uses_geoid_fallback_and_heading():
    drone = DroneConfig(geoid_offset_m=-30.0)
    state = flying_state(undulation=None, course=math.nan, h_acc=None)
    root = ET.fromstring(build_uav_event(state, drone, NOW))
    assert float(root.find("point").get("hae")) == pytest.approx(770.0)
    assert float(root.find("point").get("ce")) == pytest.approx(9999999.0)
    assert float(root.find("detail/track").get("course")) == pytest.approx(90.0)


def test_home_event_links_to_uav():
    drone = DroneConfig()
    root = ET.fromstring(build_home_event(flying_state(), drone, NOW))
    assert root.get("uid") == f"{drone.uid}-home"
    link = root.find("detail/link")
    assert link.get("uid") == drone.uid


def test_parse_roundtrip_and_ping():
    drone = DroneConfig()
    ev = parse_event(build_uav_event(flying_state(), drone, NOW))
    assert ev.uid == drone.uid and ev.callsign == drone.callsign
    assert ev.time == NOW
    pong = parse_event(build_pong(NOW))
    assert pong.type == "t-x-c-t-r"


def test_parse_rejects_garbage():
    assert parse_event(b"<event") is None
    assert parse_event(b"<foo uid='a' type='b'/>") is None
    assert parse_event(b"<event uid='a'/>") is None


def test_parse_marker_sender():
    raw = (
        b'<event version="2.0" uid="m1" type="b-m-p-s-m" how="h-g-i-g-o" time="2026-09-25T12:00:00Z" '
        b'start="2026-09-25T12:00:00Z" stale="2027-09-25T12:00:00Z">'
        b'<point lat="35.2" lon="-117.8" hae="0" ce="9999999" le="9999999"/>'
        b'<detail><contact callsign="GOTO 100"/>'
        b'<link uid="ANDROID-1" type="a-f-G-U-C" parent_callsign="PILOT" relation="p-p"/></detail></event>'
    )
    ev = parse_event(raw)
    assert ev.callsign == "GOTO 100"
    assert ev.sender_callsign == "PILOT"
    assert ev.lat == pytest.approx(35.2)


def test_stream_parser_splits_and_buffers():
    a = build_pong(NOW)
    b = build_uav_event(flying_state(), DroneConfig(), NOW)
    p = CotStreamParser()
    data = b'<?xml version="1.0"?>' + a + b"\n" + b
    assert p.feed(data[:25]) == []
    out = p.feed(data[25:-5])
    assert out == [a]
    assert p.feed(data[-5:]) == [b]


def test_stream_parser_limits_size():
    p = CotStreamParser(max_buffer=100)
    with pytest.raises(ValueError):
        p.feed(b"<event" + b"x" * 200)
