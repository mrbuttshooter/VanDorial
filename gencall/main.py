"""
GenCall - Main Application Entry Point.
Wires everything together and starts the web server.
"""

import argparse
import logging
import sys
import os
import uvicorn

from gencall.core.config import Config
from gencall.core.log import setup_logging
from gencall.core.sipp_engine import SIPpEngine
from gencall.core.stats import StatsEngine
from gencall.scenarios.manager import ScenarioManager
from gencall.db.models import Database
from gencall.api import routes
from gencall.web.dashboard import router as dashboard_router

logger = logging.getLogger("gencall")


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

    # Mount dashboard
    routes.app.include_router(dashboard_router)

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
    ║   Dashboard: http://{host}:{port:<5d}          ║
    ║   API:       http://{host}:{port:<5d}/api      ║
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
