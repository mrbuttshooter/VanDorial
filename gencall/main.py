"""
GenCall - Main Application Entry Point.
Wires everything together and starts the web server.
"""

import argparse
import asyncio
import logging
import sys
import os
import uvicorn

from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from gencall.core.config import Config
from gencall.core.log import setup_logging
from gencall.core.sipp_engine import SIPpEngine
from gencall.core.stats import StatsEngine
from gencall.scenarios.manager import ScenarioManager
from gencall.db.models import Database
from gencall.api import routes
from gencall.api import websocket
from gencall.web.dashboard import router as dashboard_router

logger = logging.getLogger("gencall")

# Built NOC console (frontend/ → `npm run build` emits here).
CONSOLE_DIR = os.path.join(os.path.dirname(__file__), "web", "console")


def create_app(config_path: str = None):
    """Create and configure the GenCall FastAPI application."""
    config = Config(config_path)
    setup_logging(config)

    logger.info("=" * 60)
    logger.info("  GenCall v2.0.0 - SIP Traffic Generator")
    logger.info("=" * 60)

    # Initialize components
    sipp_engine = SIPpEngine(config)
    stats_engine = StatsEngine(config)
    stats_engine.set_engine(sipp_engine)

    scenario_mgr = ScenarioManager(
        custom_dir=os.path.join(os.path.dirname(config.media_path), "scenarios", "custom")
    )

    # Database
    db = None
    try:
        db_dir = os.path.dirname(config.get("database", "sqlite_path", "/tmp/gencall.db"))
        os.makedirs(db_dir, exist_ok=True)
        db = Database(config.db_url)
        db.create_tables()
        logger.info("Database initialized: %s", config.db_engine)
    except Exception as e:
        logger.warning("Database init failed (running without persistence): %s", e)

    # Wire up the API
    routes.engine = sipp_engine
    routes.stats = stats_engine
    routes.scenarios = scenario_mgr
    routes.db = db

    # ── API authentication ────────────────────────────────────────────────────
    # Enforce X-API-Key on every endpoint except /api/health. Keys are persisted
    # in the database; without a database we cannot store keys, so auth stays off
    # (same degraded mode the app already uses for connectors/history).
    if db is not None:
        from gencall.core.api_gateway import APIGateway, APIKeyManager
        gateway = APIGateway()
        gateway.keys = APIKeyManager(db=db)
        routes.gateway = gateway

        if gateway.keys.count_keys() == 0:
            raw_key, key = gateway.keys.create_key("admin")
            logger.warning("=" * 60)
            logger.warning("No API keys found — minted an initial 'admin' key.")
            logger.warning("SAVE THIS NOW (shown only once):")
            logger.warning("  X-API-Key: %s", raw_key)
            logger.warning("Manage keys with: gencall keys [create|list|revoke]")
            logger.warning("=" * 60)
        logger.info("API authentication enabled (%d key(s))",
                    gateway.keys.count_keys())
    else:
        routes.gateway = None
        logger.warning(
            "API authentication DISABLED — no database available to store keys. "
            "Endpoints are unprotected; fix persistence to enable auth."
        )

    app = routes.app

    # ── Live streams ────────────────────────────────────────────────────────
    # Mount the WebSocket hub (/ws, /ws/stats, …) and feed it stats snapshots.
    app.include_router(websocket.router)
    stats_engine.add_listener(websocket.on_stats_update)

    @app.on_event("startup")
    async def _bind_ws_loop() -> None:
        # The sync→async broadcast bridge needs the running loop.
        websocket.set_event_loop(asyncio.get_running_loop())

    # ── Web UI ──────────────────────────────────────────────────────────────
    # New NOC console (SPA) at /console; legacy dashboard kept at /legacy.
    app.include_router(dashboard_router, prefix="/legacy")

    if os.path.isdir(CONSOLE_DIR):
        app.mount("/console", StaticFiles(directory=CONSOLE_DIR, html=True), name="console")

        @app.get("/", include_in_schema=False)
        def _root_redirect():
            return RedirectResponse(url="/console/")

        logger.info("NOC console mounted: /console")
    else:
        logger.warning(
            "Console build not found at %s — run `npm run build` in frontend/. "
            "Falling back to legacy dashboard at /.",
            CONSOLE_DIR,
        )
        app.include_router(dashboard_router)

    # Start stats collection
    stats_engine.start()

    logger.info("Loaded %d built-in scenarios", len(scenario_mgr.list_scenarios()))
    logger.info("SIPp binary: %s", config.sipp_command)
    logger.info("Web server: http://%s:%d", config.web_host, config.web_port)

    return routes.app, config


def main():
    parser = argparse.ArgumentParser(
        prog="gencall",
        description="GenCall - SIP Traffic Generator v2.0",
    )
    parser.add_argument("-c", "--config", default=None, help="Path to gencall.cfg")
    parser.add_argument("-H", "--host", default=None, help="Web server bind address")
    parser.add_argument("-p", "--port", type=int, default=None, help="Web server port")
    parser.add_argument("--no-ssl", action="store_true", help="Disable SSL")
    parser.add_argument("--workers", type=int, default=1, help="Number of worker processes")

    args = parser.parse_args()

    app, config = create_app(args.config)

    host = args.host or config.web_host
    port = args.port or config.web_port

    ssl_kwargs = {}
    if config.web_ssl and not args.no_ssl:
        ssl_kwargs["ssl_certfile"] = config.ssl_cert
        ssl_kwargs["ssl_keyfile"] = config.ssl_key

    print(f"""
    ╔═══════════════════════════════════════════╗
    ║   GenCall v2.0 - SIP Traffic Generator    ║
    ║                                           ║
    ║   Console:   http://{host}:{port:<5d}/console  ║
    ║   API:       http://{host}:{port:<5d}/api      ║
    ║   Streams:   ws://{host}:{port:<5d}/ws         ║
    ║   Health:    http://{host}:{port:<5d}/api/health║
    ╚═══════════════════════════════════════════╝
    """)

    uvicorn.run(
        app,
        host=host,
        port=port,
        workers=args.workers,
        log_level="info",
        **ssl_kwargs,
    )


if __name__ == "__main__":
    main()
