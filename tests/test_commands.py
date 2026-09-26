import math
from datetime import datetime, timedelta, timezone

import pytest

from atak_bridge.commands import CommandHandler, distance_m
from atak_bridge.config import CommandConfig
from atak_bridge.cot import CotEvent
from atak_bridge.state import StateStore

NOW = datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc)
HOME = (35.0, -117.0)


def marker(callsign="GOTO", lat=35.005, lon=-117.005, uid="m1", sender="PILOT", age_s=1.0):
    return CotEvent(
        uid=uid,
        type="b-m-p-s-m",
        lat=lat,
        lon=lon,
        hae=0.0,
        callsign=callsign,
        sender_callsign=sender,
        time=NOW - timedelta(seconds=age_s),
        stale=NOW + timedelta(days=1),
        link_uid="",
        raw=b"",
    )


@pytest.fixture
def setup():
    store = StateStore()
    store.update(connected=True, armed=True, home_lat=HOME[0], home_lon=HOME[1])
    sent = []
    cfg = CommandConfig(enabled=True)

    def submit(cmd):
        sent.append(cmd)
        return True

    return CommandHandler(cfg, store, submit), cfg, store, sent


def test_distance():
    assert distance_m(0, 0, 0, 1) == pytest.approx(111_195, rel=1e-3)


def test_goto_default_alt(setup):
    handler, cfg, _, sent = setup
    result = handler.handle(marker(), NOW)
    assert result.startswith("sent")
    assert len(sent) == 1
    assert sent[0].alt_rel_m == cfg.default_alt_m
    assert sent[0].lat == pytest.approx(35.005)
    assert sent[0].loiter_radius_m == cfg.loiter_radius_m


def test_goto_altitude_suffix(setup):
    handler, _, _, sent = setup
    assert handler.handle(marker("goto 90m"), NOW).startswith("sent")
    assert sent[0].alt_rel_m == 90


def test_non_command_markers_ignored(setup):
    handler, _, _, sent = setup
    assert handler.handle(marker("Rally point"), NOW) is None
    assert not sent


def test_disabled(setup):
    handler, cfg, _, sent = setup
    cfg.enabled = False
    assert handler.handle(marker(), NOW) is None
    assert not sent


@pytest.mark.parametrize(
    "ev_kwargs, state_kwargs, cfg_kwargs, expect",
    [
        ({"callsign": "GOTO 500"}, {}, {}, "altitude"),
        ({"callsign": "GOTO high"}, {}, {}, "cannot parse"),
        ({"lat": 36.0}, {}, {}, "from home"),
        ({"age_s": 600}, {}, {}, "too old"),
        ({"sender": "RANDO"}, {}, {"allowed_senders": ["PILOT"]}, "allowed_senders"),
        ({}, {"connected": False}, {}, "no MAVLink"),
        ({}, {"armed": False}, {}, "not armed"),
        ({}, {"home_lat": math.nan}, {}, "home position unknown"),
        ({"lat": math.nan}, {}, {}, "invalid coordinates"),
    ],
)
def test_rejections(setup, ev_kwargs, state_kwargs, cfg_kwargs, expect):
    handler, cfg, store, sent = setup
    store.update(**state_kwargs)
    for k, v in cfg_kwargs.items():
        setattr(cfg, k, v)
    result = handler.handle(marker(**ev_kwargs), NOW)
    assert expect in result
    assert not sent


def test_duplicate_suppressed_but_move_accepted(setup):
    handler, _, _, sent = setup
    handler.handle(marker(), NOW)
    assert "duplicate" in handler.handle(marker(), NOW)
    assert handler.handle(marker(lat=35.006), NOW).startswith("sent")
    assert len(sent) == 2


def delete_of(uid):
    return CotEvent(
        uid="del-1", type="t-x-d-d", lat=0.0, lon=0.0, hae=0.0, callsign="", sender_callsign="",
        time=NOW, stale=NOW + timedelta(seconds=20), link_uid=uid, raw=b"",
    )


def test_deleting_active_goto_returns_home(setup):
    from atak_bridge.mavlink_link import ReturnHomeCommand

    handler, _, _, sent = setup
    handler.handle(marker(uid="pin-A"), NOW)
    assert handler.handle(delete_of("some-other-pin"), NOW) is None  # unrelated delete
    assert handler.handle(delete_of("pin-A"), NOW, sender="HARDY") == "sent: RETURN HOME (RTL)"
    assert isinstance(sent[-1], ReturnHomeCommand)
    assert handler.active_uid is None
    assert handler.handle(delete_of("pin-A"), NOW) is None  # already handled
    # The same pin can be used again afterwards.
    assert handler.handle(marker(uid="pin-A"), NOW).startswith("sent")


def test_delete_return_home_can_be_disabled_and_respects_senders(setup):
    handler, cfg, _, sent = setup
    handler.handle(marker(uid="pin-A"), NOW)
    cfg.delete_returns_home = False
    assert handler.handle(delete_of("pin-A"), NOW) is None
    cfg.delete_returns_home = True
    cfg.allowed_senders = ["PILOT"]
    assert "allowed_senders" in handler.handle(delete_of("pin-A"), NOW, sender="RANDO")
    assert handler.handle(delete_of("pin-A"), NOW, sender="PILOT") == "sent: RETURN HOME (RTL)"
    assert len(sent) == 2
