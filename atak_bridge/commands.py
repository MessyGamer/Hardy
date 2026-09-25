"""Turn ATAK map markers into guarded fly-to (DO_REPOSITION) commands."""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Callable

from .config import CommandConfig
from .cot import CotEvent
from .mavlink_link import RepositionCommand
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
        submit: Callable[[RepositionCommand], bool],
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.submit = submit
        self._last: dict[str, tuple[float, float, float]] = {}

    def is_command(self, ev: CotEvent) -> bool:
        prefix = self.cfg.goto_prefix.upper()
        return bool(prefix) and ev.callsign.upper().startswith(prefix) and not ev.is_delete

    def handle(self, ev: CotEvent, now: datetime | None = None) -> str | None:
        """Process an event. Returns a status string if it was a command, else None."""
        if not self.cfg.enabled or not self.is_command(ev):
            return None
        result = self._evaluate(ev, now or datetime.now(timezone.utc))
        log.info("GOTO marker %r (uid %s, from %s): %s", ev.callsign, ev.uid, ev.sender_callsign or "?", result)
        return result

    def _evaluate(self, ev: CotEvent, now: datetime) -> str:
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

        if self.cfg.allowed_senders and ev.sender_callsign not in self.cfg.allowed_senders:
            return f"rejected: sender {ev.sender_callsign or '(unknown)'} not in allowed_senders"

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
        return f"sent: {cmd.description} ({dist:.0f} m from home)"
