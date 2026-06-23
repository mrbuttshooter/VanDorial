"""
VanDorial Fleet Controller — application factory (design §2, §6).

create_controller_app(config) wires:
  - the controller DB (separate from any worker DB),
  - the FleetAggregator (stats ~1 Hz, health ~5 s) + its WS bridge listeners,
  - the controller REST routes + WebSocket hub,
  - browser→controller auth by pointing gencall.api.routes.gateway at a
    controller APIGateway whose keys = APIKeyManager(db=<controller db>),
  - the built console static mount at /console with a root redirect (reusing the
    worker's main.py pattern).

The browser only ever talks to this controller. Auth reuses the worker's exact
`require_api_key` dependency (imported by controller/routes.py).
"""

from __future__ import annotations

import asyncio
import logging
import os

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from gencall.core.config import Config
from gencall.controller.models import ControllerDatabase
from gencall.controller.aggregator import FleetAggregator
from gencall.controller import routes as controller_routes
from gencall.controller import ws as controller_ws

logger = logging.getLogger("gencall.controller")

# Built NOC/fleet console (same artifact the worker serves).
CONSOLE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "web", "console")


def _controller_db_url(config: Config) -> str:
    """Resolve the controller's OWN database URL (separate from a worker DB).

    Honors GENCALL_CONTROLLER_DATABASE_URL first, else falls back to the
    standard config.db_url (operators are expected to point the controller at a
    distinct sqlite_path / DATABASE_URL — see contract §G).
    """
    return os.environ.get("GENCALL_CONTROLLER_DATABASE_URL") or config.db_url


def create_controller_app(config: Config = None):
    """Create and configure the VanDorial fleet controller FastAPI app.

    Returns (app, config).
    """
    config = config or Config()

    logger.info("=" * 60)
    logger.info("  VanDorial Fleet Controller (GenCall v2.0.0)")
    logger.info("=" * 60)

    # ── Controller database (its own, separate from workers) ────────────────
    db = None
    try:
        db_url = _controller_db_url(config)
        if db_url.startswith("sqlite:///"):
            db_path = db_url[len("sqlite:///"):]
            db_dir = os.path.dirname(db_path)
            if db_dir:
                os.makedirs(db_dir, exist_ok=True)
        db = ControllerDatabase(db_url)
        db.create_tables()
        logger.info("Controller database initialized")
    except Exception as exc:
        logger.warning(
            "Controller DB init failed (running without persistence/auth): %s",
            exc)
        db = None

    controller_routes.db = db
    controller_routes.verify_tls = False

    # ── Browser→Controller auth (reuse worker dependency) ───────────────────
    # require_api_key reads gencall.api.routes.gateway. Point it at a controller
    # gateway backed by the controller DB so the admin key lives in OUR db.
    from gencall.api import routes as worker_routes
    if db is not None:
        from gencall.core.api_gateway import APIGateway, APIKeyManager
        from gencall.core.auth_users import UserManager, SessionManager
        gateway = APIGateway()
        gateway.keys = APIKeyManager(db=db)
        gateway.users = UserManager(db=db)
        gateway.sessions = SessionManager(db=db)
        worker_routes.gateway = gateway

        if gateway.keys.count_keys() == 0:
            raw_key, _ = gateway.keys.create_key("controller-admin")
            logger.warning("=" * 60)
            logger.warning("No controller API keys found — minted 'controller-admin'.")
            logger.warning("SAVE THIS NOW (shown only once):")
            logger.warning("  X-API-Key: %s", raw_key)
            logger.warning("=" * 60)
        logger.info("Controller authentication enabled (%d key(s))",
                    gateway.keys.count_keys())
    else:
        worker_routes.gateway = None
        logger.warning(
            "Controller authentication DISABLED — no database. Endpoints are "
            "unprotected; configure persistence to enable auth.")

    # ── Aggregation engine ──────────────────────────────────────────────────
    def _enabled_node_provider():
        if db is None:
            return []
        from gencall.controller.models import Node
        session = db.get_session()
        try:
            rows = session.query(Node).filter_by(enabled=True).all()
            # Detach into plain dicts so the aggregator thread never touches a
            # live SQLAlchemy session.
            return [{
                "id": n.id, "address": n.address, "api_key": n.api_key,
                "group_id": n.group_id, "enabled": bool(n.enabled),
            } for n in rows]
        finally:
            session.close()

    def _fleet_trust_provider():
        """The EFFECTIVE fleet trust config re-pushed to a worker that rejoins
        (empty ips when enforcement is off = allow-all). None DB → allow-all."""
        if db is None:
            return {"ips": [], "drop_untrusted": False}
        return {"ips": db.effective_fleet_ips(),
                "drop_untrusted": db.get_fleet_trust()["drop_untrusted"]}

    aggregator = FleetAggregator(
        _enabled_node_provider,
        stats_interval=1.0,
        health_interval=5.0,
        history_size=config.stats_history_size,
        verify_tls=False,
        fleet_trust_provider=_fleet_trust_provider,
    )
    aggregator.add_stats_listener(controller_ws.on_fleet_stats)
    aggregator.add_status_listener(controller_ws.on_node_status)
    controller_routes.aggregator = aggregator

    # ── FastAPI app ─────────────────────────────────────────────────────────
    app = FastAPI(
        title="VanDorial Fleet Controller API",
        description="VanDorial fleet control-plane for GenCall workers",
        version="2.0.0",
        docs_url=None, redoc_url=None, openapi_url=None,
    )

    app.include_router(controller_routes.router)
    app.include_router(controller_ws.router)

    # Console login (account auth in front of the fleet console). Reuses the
    # worker auth router; it reads the same gateway we wired above.
    from gencall.api import auth as auth_api
    app.include_router(auth_api.router)

    # Defense-in-depth response headers on the controller console + API too.
    from gencall.api.security_headers import install_security_headers
    install_security_headers(app)

    # ── Fleet discovery listener (opt-in: [fleet] discovery = true) ──────────
    # Auto-register workers that broadcast a beacon on the VLAN. The shared fleet
    # token both filters foreign beacons and becomes the api_key the controller
    # uses to command the discovered worker.
    discovery_listener = None
    if config.fleet_discovery:
        from gencall.core.discovery import BeaconListener, upsert_discovered_node

        def _on_beacon(info):
            try:
                upsert_discovered_node(db, info, config.fleet_token)
            except Exception as e:  # pragma: no cover - defensive
                logger.warning("Could not register discovered node %s: %s",
                               info.get("address"), e)

        discovery_listener = BeaconListener(
            _on_beacon, port=config.fleet_beacon_port, token=config.fleet_token)

    @app.on_event("startup")
    async def _on_startup() -> None:
        controller_ws.set_event_loop(asyncio.get_running_loop())
        aggregator.start()
        if discovery_listener is not None:
            discovery_listener.start()

    @app.on_event("shutdown")
    async def _on_shutdown() -> None:
        aggregator.stop()
        if discovery_listener is not None:
            discovery_listener.stop()

    # ── Console static mount + root redirect (mirror worker main.py) ────────
    if os.path.isdir(CONSOLE_DIR):
        app.mount("/console", StaticFiles(directory=CONSOLE_DIR, html=True),
                  name="console")

        @app.get("/", include_in_schema=False)
        def _root_redirect():
            return RedirectResponse(url="/console/")

        logger.info("Fleet console mounted: /console")
    else:
        logger.warning(
            "Console build not found at %s — run `npm run build` in frontend/.",
            CONSOLE_DIR)

        @app.get("/", include_in_schema=False)
        def _root_redirect_missing():
            return RedirectResponse(url="/console/")

    logger.info("Controller ready (web %s:%d)", config.web_host, config.web_port)
    return app, config


def run():
    """Console-script entrypoint (`gencall-controller`).

    Parses a minimal argument set, builds the controller app, and serves it with
    uvicorn. Mirrors the worker's gencall.main.main() server bootstrap.
    """
    import argparse
    import uvicorn

    from gencall.core.log import setup_logging

    parser = argparse.ArgumentParser(
        prog="gencall-controller",
        description="VanDorial Fleet Controller (GenCall control-plane)",
    )
    parser.add_argument("-c", "--config", default=None, help="Path to gencall.cfg")
    parser.add_argument("-H", "--host", default=None, help="Web server bind address")
    parser.add_argument("-p", "--port", type=int, default=None, help="Web server port")
    parser.add_argument("--no-ssl", action="store_true", help="Disable SSL")
    args = parser.parse_args()

    config = Config(args.config)
    try:
        setup_logging(config)
    except Exception:
        logging.basicConfig(level=logging.INFO)

    app, config = create_controller_app(config)

    host = args.host or config.web_host
    port = args.port or config.web_port

    ssl_kwargs = {}
    if config.web_ssl and not args.no_ssl:
        ssl_kwargs["ssl_certfile"] = config.ssl_cert
        ssl_kwargs["ssl_keyfile"] = config.ssl_key

    uvicorn.run(app, host=host, port=port, log_level="info", **ssl_kwargs)
