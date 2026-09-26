# Hardy — ATAK bridge for a fixed-wing UAV

> New to this? Start with the step-by-step [SETUP_CHECKLIST.md](SETUP_CHECKLIST.md).

`atak-bridge` runs on the ground control station (a Raspberry Pi 8 GB is plenty — it
uses ~40 MB RAM and a few % of one core). It connects to the aircraft over MAVLink
(ArduPlane or PX4) and puts the aircraft on every ATAK/WinTAK/iTAK map on your network.

```
 Fixed-wing UAV ──RF telemetry──► Raspberry Pi GCS ──Wi-Fi/Ethernet/mesh──► ATAK devices
 (ArduPlane/PX4)   (SiK/RFD900)    ├─ MAVLink link (pymavlink)
                                   ├─ built-in TAK server  TCP :8087  ◄──► ATAK clients
                                   ├─ SA multicast 239.2.3.1:6969     ───► ATAK on the LAN
                                   └─ optional upstream TAK server    ◄──► FreeTAKServer / TAK Server
```

## What it does

* **Aircraft track** — publishes the UAV as a CoT `a-f-A-M-F-Q` (friendly fixed-wing UAV)
  at 2 Hz with course, speed and height above the ellipsoid (HAE, which is what ATAK wants;
  taken from `GPS_RAW_INT.alt_ellipsoid` when the autopilot sends it). Remarks show flight
  mode, armed state, altitude, airspeed/groundspeed, climb, battery, GPS fix, and autopilot
  warnings. If the link drops, the icon goes stale in ATAK after `stale_s`.
* **Home point** — a `<callsign> HOME` marker at the launch point.
* **Video** — set `drone.video_url` (e.g. RTSP) and the feed is attached to the aircraft
  icon in ATAK.
* **Built-in TAK server** — ATAK clients connect to the Pi on TCP 8087. It relays position
  reports, markers, and drawings between connected users, answers ATAK pings, and sends
  new clients the current picture when they join. Optional TLS.
* **SA multicast** — ATAK devices on the same LAN see the aircraft with no setup.
* **Upstream** — can also forward to a larger TAK server.
* **Fly-to from the map (optional, off by default)** — drop a marker called `GOTO` or
  `GOTO 100` (metres above home) and the aircraft receives `MAV_CMD_DO_REPOSITION`, which
  switches it to GUIDED and loiters at that point. To send it home
  (`MAV_CMD_NAV_RETURN_TO_LAUNCH`), send the aircraft's HOME marker or a marker named
  `HOME` / `RTL`; on ATAK, deleting the active GOTO marker also works (iTAK doesn't send
  deletes). See [Safety](#safety-fly-to-commands).

## Hardware / OS

* Raspberry Pi 4 or 5, 8 GB, Raspberry Pi OS **Bookworm 64-bit** (Python 3.11).
* Telemetry radio on USB (appears as `/dev/ttyUSB0` or `/dev/ttyACM0`).
* A network to the ATAK devices: Pi Wi-Fi hotspot, a travel router, or a MANET/mesh radio.

## Install on the Pi

```bash
git clone <this repo> hardy && cd hardy
sudo ./scripts/install.sh
sudo nano /etc/atak-bridge/config.toml     # set connection, callsign, etc.
sudo systemctl restart atak-bridge
journalctl -u atak-bridge -f
```

This installs into `/opt/atak-bridge/venv`, creates an `atak` service user in the
`dialout` group, and enables the `atak-bridge` systemd service, which starts at boot.

To run it by hand instead:

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[test]'
atak-bridge --config config.example.toml
```

## Connecting to the aircraft

**Option A: share the radio with Mission Planner/QGC (recommended).** Only one program
can open a serial port, so use [mavlink-router](https://github.com/mavlink-router/mavlink-router)
on the Pi to split it:

```ini
# /etc/mavlink-router/main.conf
[UartEndpoint radio]
Device = /dev/ttyUSB0
Baud = 57600

[UdpEndpoint atak_bridge]
Mode = Normal
Address = 127.0.0.1
Port = 14551

[UdpEndpoint gcs_laptop]        # your Mission Planner / QGC machine
Mode = Normal
Address = 192.168.4.20
Port = 14550
```

Leave `mavlink.connection = "udpin:0.0.0.0:14551"` (the default).

**Option B: direct serial.** `connection = "/dev/ttyUSB0"` and `baud = 57600` (or whatever
your radio is set to).

The bridge uses MAVLink system id **253** by default, not 255. That way its heartbeat never
stands in for your main GCS's, and ArduPilot's GCS failsafe (`SYSID_MYGCS`, default 255)
still works as intended.

## ATAK setup

* **Multicast (zero config):** on the same LAN, the aircraft just appears.
* **TAK server:** *Settings → Network Preferences → TAK Servers → Add*. Address = the
  Pi's IP, port `8087`, protocol **TCP** (uncheck "Use default SSL/TLS certificates"). Every
  device connected this way shares positions and markers with the others.
* Tap the aircraft icon to see telemetry in the remarks, and the video feed if configured.

## Configuration

Everything is in [`config.example.toml`](config.example.toml) with comments. Key items:

| Section | Key | Purpose |
|---|---|---|
| `mavlink` | `connection` | pymavlink connection string (UDP, serial, TCP) |
| `drone` | `uid`, `callsign`, `cot_type` | How the aircraft appears in ATAK |
| `drone` | `video_url` | RTSP/UDP video URL attached to the icon |
| `tak_server` | `port`, `relay`, `tls`, `cafile` | Built-in server |
| `multicast` | `interface` | IP of the NIC facing the ATAK network |
| `upstream` | `host`, `port`, `tls` | Forward to another TAK server |
| `commands` | `enabled`, limits | Fly-to from ATAK markers |

## iPhones (iTAK) and secure connections

iTAK only connects over certificate-secured connections, so the bridge has a built-in
certificate authority and a secure listener (`[secure]` in the config):

* On first start, it creates its certificate authority in `cert_dir`.
* Add a login per person under `[secure.users]`. Secure mode stays off while any password is
  still `change-me`.
* **iPhone setup:** open `http://<pi-ip>:8087` in Safari and tap **iPhone (iTAK)**. Safari
  asks for the username and password, then downloads a package holding that person's own
  client certificate and the server's trust store, in the same layout OpenTAKServer uses.
  Import it in iTAK (*Network → Servers → + → Upload server package*) and it connects on
  **8089**. (iTAK's package import doesn't support the sign-in/enrollment style of package.)
* **Android ATAK** can use the secure package, which signs in on port **8446**
  (TAK-style certificate enrollment), or the plain TCP connection on 8087.
* The download page is plain HTTP, so the login travels unencrypted on the local network.
  Use it only on a network you trust.

## Safety: fly-to commands

`[commands]` is **disabled by default**. Before you enable it:

* The plain TCP server has **no authentication**. Anyone who can reach port 8087 can drop
  a marker. Keep it on a closed network or VPN, set `allowed_senders` to the callsigns
  allowed to command, and preferably turn on `tls` with a `cafile` so clients need a
  certificate.
* Every command is checked. It must parse, its altitude must be within
  `min_alt_m`–`max_alt_m` above home, it must be within `max_distance_from_home_m` of home,
  the link must be up, home must be known, and the aircraft must be armed (`require_armed`).
  Markers older than `max_marker_age_s` or already acted on are ignored. That stops replays
  after a reconnect or restart.
* The command is `MAV_CMD_DO_REPOSITION` with the change-mode flag. On ArduPlane (4.1+) and
  PX4, the aircraft switches to GUIDED/Hold and loiters at the point. Return to AUTO/RTL
  from your GCS or RC transmitter. **The pilot on the RC link or main GCS always has
  authority. This is not a replacement for either.**
* Check the altitude limits against your local airspace rules, and test in SITL first.

## Quick demo on any computer (no drone needed)

```bash
pip install pymavlink
python demo.py --lat <your latitude> --lon <your longitude>
```

This starts a pretend drone and the TAK server together, and prints the address to type
into ATAK (port 8087, TCP). In the demo, fly-to is enabled, so a marker named `GOTO` makes
the pretend drone fly there. Press Ctrl+C to stop.

## Testing without an aircraft

```bash
# Terminal 1: a fake ArduPlane circling a point (listens on TCP 5760, like SITL)
python tools/fake_uav.py --lat 35.0 --lon -117.0
# Terminal 2: with mavlink.connection = "tcp:127.0.0.1:5760" in your config
atak-bridge --config my-test-config.toml
```

(`python demo.py` does both of these in one go.)

Or use real ArduPilot SITL:
`sim_vehicle.py -v ArduPlane --out udp:<pi-ip>:14551`.

Unit and integration tests:

```bash
pip install -e '.[test]' && pytest
```

## Layout

```
atak_bridge/
  app.py           entry point, routing between MAVLink and TAK outputs
  mavlink_link.py  MAVLink thread: telemetry parsing, stream requests, commands
  cot.py           CoT XML builders/parsers, TCP stream framing
  net.py           TAK TCP/TLS server, multicast sender, upstream client
  commands.py      ATAK marker -> guarded DO_REPOSITION
  config.py        TOML config
  state.py         thread-safe vehicle state
tools/fake_uav.py  MAVLink simulator for bench tests
systemd/, scripts/ Pi service + installer
```
