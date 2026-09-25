"""Configuration loading (TOML, stdlib only)."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any


@dataclass
class MavlinkConfig:
    # pymavlink connection string. Examples:
    #   "udpin:0.0.0.0:14551"   - listen for MAVLink forwarded by mavlink-router/MAVProxy
    #   "/dev/ttyUSB0"          - telemetry radio directly (set baud)
    #   "tcp:127.0.0.1:5760"    - ArduPilot SITL
    connection: str = "udpin:0.0.0.0:14551"
    baud: int = 57600
    # Our own MAVLink identity. Deliberately NOT 255 so this bridge never
    # masks the autopilot's GCS failsafe (ArduPilot SYSID_MYGCS defaults to 255).
    source_system: int = 253
    source_component: int = 191
    # 0 = lock onto the first autopilot heard.
    target_system: int = 0
    stream_rate_hz: int = 4
    link_timeout_s: float = 5.0
    send_heartbeat: bool = True


@dataclass
class DroneConfig:
    uid: str = "HARDY-UAV-1"
    callsign: str = "HARDY-1"
    # MIL-STD-2525 CoT type. a-f-A-M-F-Q = friendly / air / military / fixed wing / UAV.
    cot_type: str = "a-f-A-M-F-Q"
    team: str = "Cyan"
    role: str = "Team Member"
    publish_rate_hz: float = 2.0
    stale_s: float = 10.0
    publish_home: bool = True
    # Optional video feed advertised to ATAK (e.g. "rtsp://192.168.1.50:8554/live").
    video_url: str = ""
    # Used for height-above-ellipsoid only if the autopilot does not report
    # GPS_RAW_INT.alt_ellipsoid. HAE = MSL + geoid_offset_m.
    geoid_offset_m: float = 0.0


@dataclass
class TakServerConfig:
    enabled: bool = True
    bind: str = "0.0.0.0"
    port: int = 8087
    # Relay CoT between connected ATAK clients (so the Pi acts as a small TAK server).
    relay: bool = True
    max_clients: int = 32
    tls: bool = False
    certfile: str = ""
    keyfile: str = ""
    # If set, clients must present a certificate signed by this CA.
    cafile: str = ""


@dataclass
class MulticastConfig:
    enabled: bool = True
    group: str = "239.2.3.1"
    port: int = 6969
    ttl: int = 1
    # IP address of the local interface to send on ("" = OS default).
    interface: str = ""


@dataclass
class UpstreamConfig:
    """Optional external TAK server (FreeTAKServer, TAK Server, ...) to forward to."""

    enabled: bool = False
    host: str = ""
    port: int = 8087
    tls: bool = False
    cafile: str = ""
    certfile: str = ""
    keyfile: str = ""


@dataclass
class CommandConfig:
    """Map-marker 'fly-to' commands from ATAK. Disabled by default."""

    enabled: bool = False
    # A marker whose callsign starts with this prefix is a fly-to request.
    # Optional altitude suffix in metres above home: "GOTO 150".
    goto_prefix: str = "GOTO"
    default_alt_m: float = 120.0
    min_alt_m: float = 60.0
    max_alt_m: float = 120.0
    loiter_radius_m: float = 80.0
    # Reject targets further than this from the home position.
    max_distance_from_home_m: float = 3000.0
    # Only accept commands from these ATAK callsigns (empty = anyone connected).
    allowed_senders: list[str] = field(default_factory=list)
    require_armed: bool = True
    # Ignore markers whose CoT timestamp is older than this (e.g. replayed by a TAK server).
    max_marker_age_s: float = 60.0


@dataclass
class Config:
    mavlink: MavlinkConfig = field(default_factory=MavlinkConfig)
    drone: DroneConfig = field(default_factory=DroneConfig)
    tak_server: TakServerConfig = field(default_factory=TakServerConfig)
    multicast: MulticastConfig = field(default_factory=MulticastConfig)
    upstream: UpstreamConfig = field(default_factory=UpstreamConfig)
    commands: CommandConfig = field(default_factory=CommandConfig)
    log_level: str = "INFO"


def _apply(obj: Any, data: dict[str, Any], path: str = "") -> None:
    known = {f.name: f for f in fields(obj)}
    for key, value in data.items():
        if key not in known:
            raise ValueError(f"Unknown config key: {path}{key}")
        current = getattr(obj, key)
        if is_dataclass(current):
            if not isinstance(value, dict):
                raise ValueError(f"Config section {path}{key} must be a table")
            _apply(current, value, f"{path}{key}.")
        else:
            if isinstance(current, float) and isinstance(value, int):
                value = float(value)
            if current is not None and not isinstance(value, type(current)):
                raise ValueError(
                    f"Config key {path}{key} expects {type(current).__name__}, "
                    f"got {type(value).__name__}"
                )
            setattr(obj, key, value)


def load_config(path: str | Path | None) -> Config:
    cfg = Config()
    if path:
        with open(path, "rb") as fh:
            _apply(cfg, tomllib.load(fh))
    return cfg
