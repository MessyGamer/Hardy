"""Entry point: wires the MAVLink link, CoT publisher and network outputs together."""

from __future__ import annotations

import argparse
import asyncio
import hmac
import logging
import signal
from datetime import datetime, timedelta, timezone

from . import __version__
from .commands import CommandHandler
from .config import Config, load_config
from .cot import CotEvent, build_home_event, build_uav_event
from .mavlink_link import MavlinkLink
from .net import ClientConnection, MulticastListener, MulticastSender, TakServer, UpstreamClient
from .state import StateStore

log = logging.getLogger("atak_bridge")

HOME_PUBLISH_PERIOD_S = 10.0


class Bridge:
    def __init__(self, cfg: Config, store: StateStore, link: MavlinkLink | None) -> None:
        self.cfg = cfg
        self.store = store
        self.link = link
        self.server: TakServer | None = None
        self.multicast: MulticastSender | None = None
        self.mesh: MulticastListener | None = None
        self.enrollment = None
        self.upstream: UpstreamClient | None = None
        self.commands = CommandHandler(
            cfg.commands,
            store,
            link.submit_command if link else (lambda cmd: False),
            home_marker_uid=f"{cfg.drone.uid}-home",
        )

    # ------------------------------------------------------------ routing
    def publish_local(self, uid: str, raw: bytes, stale_s: float) -> None:
        """Our own CoT (aircraft, home) goes to every output."""
        if self.server:
            self.server.remember_raw(uid, raw, datetime.now(timezone.utc) + timedelta(seconds=stale_s))
            self.server.broadcast(raw)
        if self.multicast:
            self.multicast.send(raw)
        if self.upstream:
            self.upstream.send(raw)

    def on_client_event(self, ev: CotEvent, client: ClientConnection) -> None:
        # Position reports ("a-...") arrive every few seconds; log everything else
        # (markers, drawings, deletes) so it's easy to see what a phone actually sent.
        if not ev.type.startswith("a-"):
            what = "deleted an item" if ev.is_delete else f"sent '{ev.callsign or ev.uid}' ({ev.type})"
            log.info("%s %s", client.callsign or client.peer, what)
        if self.cfg.tak_server.relay and self.server:
            self.server.remember(ev)
            self.server.broadcast(ev.raw, exclude=client)
        if self.upstream:
            self.upstream.send(ev.raw)
        self.commands.handle(ev, sender=client.callsign, from_client=True)

    def on_upstream_event(self, ev: CotEvent) -> None:
        if self.server:
            self.server.remember(ev)
            self.server.broadcast(ev.raw)
        self.commands.handle(ev)

    # ------------------------------------------------------------ tasks
    async def publisher(self) -> None:
        period = 1.0 / max(0.1, self.cfg.drone.publish_rate_hz)
        max_age = max(self.cfg.mavlink.link_timeout_s, 2 * period)
        last_home = 0.0
        loop = asyncio.get_running_loop()
        while True:
            state = self.store.snapshot()
            if state.has_position and self.store.position_age() <= max_age:
                raw = build_uav_event(state, self.cfg.drone)
                self.publish_local(self.cfg.drone.uid, raw, self.cfg.drone.stale_s)
            if self.cfg.drone.publish_home and state.has_home and loop.time() - last_home >= HOME_PUBLISH_PERIOD_S:
                raw = build_home_event(state, self.cfg.drone)
                self.publish_local(f"{self.cfg.drone.uid}-home", raw, 60.0)
                last_home = loop.time()
            await asyncio.sleep(period)

    async def start_secure(self) -> None:
        """Certificate authority + sign-in (enrollment) + SSL streaming, as iTAK requires."""
        sec = self.cfg.secure
        usable = {u: p for u, p in sec.users.items() if p and p != "change-me"}
        if not usable:
            log.error(
                "Secure connections are enabled but no usable logins are set: add at least one "
                "username = \"password\" under [secure.users] (not 'change-me'). Secure mode is OFF."
            )
            return
        # Imported here so plain mode works without the 'cryptography' package installed.
        from .certs import TRUSTSTORE_PASSWORD, CertAuthority
        from .datapackage import SecureInfo
        from .enrollment import EnrollmentServer

        ca = CertAuthority(sec.cert_dir, self.cfg.tak_server.name)
        ca.ensure()
        def check_login(user: str, password: str) -> bool:
            expected = usable.get(user)
            return expected is not None and hmac.compare_digest(expected.encode(), password.encode())

        info = SecureInfo(
            sec.ssl_port, ca.truststore_p12(), TRUSTSTORE_PASSWORD,
            issue_client_p12=ca.issue_client_p12, check_login=check_login,
        )
        await self.server.start_secure(sec.ssl_port, ca.server_context(require_client_cert=True), info)
        self.enrollment = EnrollmentServer(ca, usable, sec.enrollment_port, self.cfg.tak_server.bind)
        await self.enrollment.start()

    async def run(self) -> None:
        tasks = [asyncio.create_task(self.publisher(), name="publisher")]
        if self.cfg.tak_server.enabled:
            self.server = TakServer(self.cfg.tak_server, self.on_client_event)
            await self.server.start()
            if self.cfg.secure.enabled:
                await self.start_secure()
        if self.cfg.multicast.enabled:
            self.multicast = MulticastSender(self.cfg.multicast)
            if self.cfg.multicast.listen:
                own = {self.cfg.drone.uid, f"{self.cfg.drone.uid}-home"}
                self.mesh = MulticastListener(self.cfg.multicast, self.on_upstream_event, own)
                try:
                    await self.mesh.start()
                except OSError as exc:
                    log.warning("Can't listen for multicast broadcasts: %s", exc)
                    self.mesh = None
        if self.cfg.upstream.enabled:
            self.upstream = UpstreamClient(self.cfg.upstream, self.on_upstream_event)
            tasks.append(asyncio.create_task(self.upstream.run(), name="upstream"))
        if self.cfg.commands.enabled:
            log.warning(
                "ATAK fly-to commands ENABLED: markers named '%s [alt]' will reposition the aircraft",
                self.cfg.commands.goto_prefix,
            )

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:
                pass
        await stop.wait()

        log.info("Shutting down")
        for task in tasks:
            task.cancel()
        if self.server:
            await self.server.close()
        if self.enrollment:
            await self.enrollment.close()
        if self.multicast:
            self.multicast.close()
        if self.mesh:
            self.mesh.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="ATAK <-> MAVLink bridge for a fixed-wing UAV")
    parser.add_argument("-c", "--config", help="path to config TOML (default: built-in defaults)")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    log.info("atak-bridge %s starting", __version__)

    store = StateStore()
    link = MavlinkLink(cfg.mavlink, store)
    link.start()
    try:
        asyncio.run(Bridge(cfg, store, link).run())
    finally:
        link.stop()
        link.join(timeout=3)


if __name__ == "__main__":
    main()
