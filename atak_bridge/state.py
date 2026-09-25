"""Thread-safe snapshot of the vehicle's telemetry."""

from __future__ import annotations

import copy
import math
import threading
import time
from dataclasses import dataclass


@dataclass
class VehicleState:
    connected: bool = False
    last_heartbeat: float = 0.0
    last_position: float = 0.0

    system_id: int = 0
    mode: str = "UNKNOWN"
    armed: bool = False

    lat: float = math.nan
    lon: float = math.nan
    alt_msl: float = math.nan
    alt_rel: float = math.nan
    # Geoid undulation (HAE - MSL) derived from GPS_RAW_INT when available.
    undulation: float | None = None

    heading: float = math.nan
    course: float = math.nan
    groundspeed: float = math.nan
    airspeed: float = math.nan
    climb: float = math.nan
    throttle: int | None = None

    gps_fix: int = 0
    satellites: int = 0
    h_acc: float | None = None
    v_acc: float | None = None

    battery_pct: int | None = None
    voltage: float | None = None

    home_lat: float = math.nan
    home_lon: float = math.nan
    home_alt_msl: float = math.nan

    last_status_text: str = ""
    last_command: str = ""

    @property
    def has_position(self) -> bool:
        return not (math.isnan(self.lat) or math.isnan(self.lon))

    @property
    def has_home(self) -> bool:
        return not (math.isnan(self.home_lat) or math.isnan(self.home_lon))


class StateStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = VehicleState()

    def update(self, **kwargs) -> None:
        with self._lock:
            for key, value in kwargs.items():
                if not hasattr(self._state, key):
                    raise AttributeError(key)
                setattr(self._state, key, value)

    def snapshot(self) -> VehicleState:
        with self._lock:
            return copy.copy(self._state)

    def position_age(self) -> float:
        with self._lock:
            if not self._state.last_position:
                return math.inf
            return time.monotonic() - self._state.last_position
