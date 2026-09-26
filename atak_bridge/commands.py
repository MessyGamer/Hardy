"""Turn ATAK map markers into guarded fly-to (DO_REPOSITION) commands."""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Callable

from .config import CommandConfig
from .cot import CotEvent
from .mavlink_link import Command, RepositionCommand, ReturnHomeCommand
from .state import StateStore

log = logging.getLogger(__name__)

EARTH_RADIUS_M = 6371008.8


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle (haversine) distance in metres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


class CommandHandler:
    def __init__(
        self,
        cfg: CommandConfig,
        store: StateStore,
        submit: Callable[[Command], bool],
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.submit = submit
        self._last: dict[str, tuple[float, float, float]] = {}
        # uid of the GOTO marker the aircraft was last sent to (None = no active GOTO).
        self.active_uid: str | None = None

    def is_command(self, ev: CotEvent) -> bool:
        prefix = self.cfg.goto_prefix.upper()
        return bool(prefix) and ev.callsign.upper().startswith(prefix) and not ev.is_delete

    def handle(self, ev: CotEvent, now: datetime | None = None, sender: str = "") -> str | None:
        """Process an event. Returns a status string if it was a command, else None.

        `sender` is the callsign of the connection it arrived on, when known; it is used when
        the event itself doesn't say who sent it (deletes don't).
        """
        if not self.cfg.enabled:
            return None
        if ev.is_delete:
            if not (self.cfg.delete_returns_home and self.active_uid and ev.link_uid == self.active_uid):
                return None
            result = self._return_home(ev, sender)
            log.info("Active GOTO marker deleted (by %s): %s", sender or "?", result)
            return result
        if not self.is_command(ev):
            return None
        result = self._evaluate(ev, now or datetime.now(timezone.utc), sender)
        log.info("GOTO marker %r (uid %s, from %s): %s", ev.callsign, ev.uid, ev.sender_callsign or "?", result)
        return result

    def _return_home(self, ev: CotEvent, sender: str) -> str:
        if self.cfg.allowed_senders and sender not in self.cfg.allowed_senders:
            return f"rejected: sender {sender or '(unknown)'} not in allowed_senders"
        state = self.store.snapshot()
        if not state.connected:
            return "rejected: no MAVLink link to vehicle"
        if self.cfg.require_armed and not state.armed:
            return "rejected: vehicle is not armed"
        if not self.submit(ReturnHomeCommand()):
            return "rejected: command queue full"
        self._last.pop(self.active_uid, None)
        self.active_uid = None
        return "sent: RETURN HOME (RTL)"

    def _evaluate(self, ev: CotEvent, now: datetime, sender: str = "") -> str:
        suffix = ev.callsign[len(self.cfg.goto_prefix):].strip()
        if suffix:
            try:
                alt = float(suffix.rstrip("mM"))
            except ValueError:
                return f"rejected: cannot parse altitude from {suffix!r}"
        else:
            alt = self.cfg.default_alt_m
        if not (self.cfg.min_alt_m <= alt <= self.cfg.max_alt_m):
            return f"rejected: altitude {alt:.0f} m outside {self.cfg.min_alt_m:.0f}-{self.cfg.max_alt_m:.0f} m"

        if math.isnan(ev.lat) or math.isnan(ev.lon) or not (-90 <= ev.lat <= 90 and -180 <= ev.lon <= 180):
            return "rejected: invalid coordinates"

        # Ignore old markers replayed by a TAK server or re-sent after a restart.
        if ev.time is not None and (now - ev.time).total_seconds() > self.cfg.max_marker_age_s:
            return "ignored: marker is too old"
        if ev.stale is not None and ev.stale < now:
            return "ignored: marker is stale"

        who = ev.sender_callsign or sender
        if self.cfg.allowed_senders and who not in self.cfg.allowed_senders:
            return f"rejected: sender {who or '(unknown)'} not in allowed_senders"

        key = (round(ev.lat, 6), round(ev.lon, 6), alt)
        if self._last.get(ev.uid) == key:
            return "ignored: duplicate"

        state = self.store.snapshot()
        if not state.connected:
            return "rejected: no MAVLink link to vehicle"
        if self.cfg.require_armed and not state.armed:
            return "rejected: vehicle is not armed"
        if not state.has_home:
            return "rejected: home position unknown"
        dist = distance_m(state.home_lat, state.home_lon, ev.lat, ev.lon)
        if dist > self.cfg.max_distance_from_home_m:
            return f"rejected: {dist:.0f} m from home exceeds {self.cfg.max_distance_from_home_m:.0f} m"

        cmd = RepositionCommand(
            lat=ev.lat,
            lon=ev.lon,
            alt_rel_m=alt,
            loiter_radius_m=self.cfg.loiter_radius_m,
            description=f"GOTO {ev.lat:.5f},{ev.lon:.5f} @ {alt:.0f} m",
        )
        if not self.submit(cmd):
            return "rejected: command queue full"
        self._last[ev.uid] = key
        self.active_uid = ev.uid
        return f"sent: {cmd.description} ({dist:.0f} m from home)"
