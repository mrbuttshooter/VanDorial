"""
GenCall - Main Application Entry Point.
Wires everything together and starts the web server.
"""

import argparse
import asyncio
import logging
import socket
import sys
import os
import tempfile
from contextlib import asynccontextmanager

import uvicorn

from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from gencall.core.config import Config
from gencall.core.log import setup_logging
from gencall.core.sipp_engine import SIPpEngine
from gencall.core.stats import StatsEngine
from gencall.scenarios.manager import ScenarioManager
from gencall.db.models import Database
from gencall.api import routes
from gencall.api import websocket

logger = logging.getLogger("gencall")

# Built NOC console (frontend/ → `npm run build` emits here).
CONSOLE_DIR = os.path.join(os.path.dirname(__file__), "web", "console")

# Served at / only when the console build is missing — points the operator at
# the build step and the live API. The React console in web/console/ is the UI.
CONSOLE_MISSING_HTML = (
    "<!doctype html><meta charset='utf-8'><title>GenCall</title>"
    "<body style='font:14px ui-monospace,monospace;background:#0a0e12;"
    "color:#e6edf3;padding:48px;line-height:1.6'>"
    "<h1 style='color:#00e6a7'>GenCall</h1>"
    "<p>The NOC console build was not found.</p>"
    "<p>Build it with <code>npm install &amp;&amp; npm run build</code> in "
    "<code>frontend/</code>, then restart.</p>"
    "<p>API is live at <a style='color:#00e6a7' href='/api/health'>/api/health</a> "
    "&middot; docs at <a style='color:#00e6a7' href='/docs'>/docs</a>.</p>"
    "</body>"
)


def _derive_node_address(config) -> str:
    """Best-effort base URL a worker advertises in its discovery beacon.

    Uses [web] host when it is a concrete IP; when it is 0.0.0.0/empty, probes
    the primary outbound IP (no traffic actually sent) so the controller gets a
    routable address rather than 0.0.0.0."""
    host = config.web_host
    if not host or host in ("0.0.0.0", "::"):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))  # no packets sent; just picks the route
            host = s.getsockname()[0]
            s.close()
        except OSError:
            host = socket.gethostbyname(socket.gethostname())
    scheme = "https" if config.web_ssl else "http"
    return f"{scheme}://{host}:{config.web_port}"


def create_app(config_path: str = None):
    """Create and configure the GenCall FastAPI application."""
    config = Config(config_path)
    setup_logging(config)

    logger.info("=" * 60)
    logger.info("  GenCall v2.1.2 - SIP Traffic Generator")
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
        db_dir = os.path.dirname(
            config.get("database", "sqlite_path", os.path.join(tempfile.gettempdir(), "gencall.db"))
        )
        os.makedirs(db_dir, exist_ok=True)
        db = Database(config.db_url)
        db.create_tables()
        # Plain ordered SQL migrations (no Alembic) for tables outside the ORM,
        # e.g. managed_processes (design §4.5). Idempotent; safe every boot.
        try:
            from gencall.db.migrations import apply_migrations

            applied = apply_migrations(db.engine)
            if applied:
                logger.info("Applied %d DB migration(s): %s", len(applied), ", ".join(applied))
        except Exception as e:
            logger.warning("DB migrations failed (reliability registry degraded): %s", e)
        logger.info("Database initialized: %s", config.db_engine)
    except Exception as e:
        logger.warning("Database init failed (running without persistence): %s", e)

    # ── Reliability: managed-process registry (design §4.5) ──────────────────
    # Records every spawned SIPp PID so we can reconcile crash-orphans on boot
    # and stop everything on shutdown. Uses the DB (managed_processes table) with
    # a JSON-file fallback when the DB is down. Wire it into the engine so every
    # start/stop is tracked.
    from gencall.core.process_registry import ProcessRegistry

    registry = ProcessRegistry(db=db)
    sipp_engine.registry = registry

    # Startup reconciliation: kill any still-alive SIPp from a previous run whose
    # cmdline still matches (PID-reuse guarded) and mark its campaign interrupted.
    try:
        summary = registry.reconcile()
        if summary["killed"]:
            logger.warning(
                "Startup reconciliation killed %d stray process(es): %s",
                len(summary["killed"]), summary["killed"],
            )
        else:
            logger.info("Startup reconciliation: no stray SIPp processes found")
    except Exception as e:
        logger.warning("Startup reconciliation failed: %s", e)

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

        # Console auto-auth: when this box serves the NOC console, give the
        # browser a key at load (/api/console/bootstrap) so opening /console
        # just works — no per-browser key paste. A stable key from
        # GENCALL_CONSOLE_API_KEY survives restarts; otherwise we mint one per
        # boot (the console re-bootstraps on every load, so that is fine).
        if config.serve_console:
            env_console_key = os.environ.get("GENCALL_CONSOLE_API_KEY")
            if env_console_key:
                gateway.keys.register_raw_key(env_console_key, name="console")
                routes.console_api_key = env_console_key
            else:
                raw_console, _ = gateway.keys.create_key("console")
                routes.console_api_key = raw_console
            logger.info("Console auto-auth enabled (GET /api/console/bootstrap)")
    else:
        routes.gateway = None
        logger.warning(
            "API authentication DISABLED — no database available to store keys. "
            "Endpoints are unprotected; fix persistence to enable auth."
        )

    app = routes.app

    # ── Loop subsystem (design §4.1 / §4.4) ──────────────────────────────────
    # The LoopEngine owns one persistent UAS (answer side, started on boot) and
    # N per-campaign UAC processes, built on the shared SIPpEngine so every PID
    # is tracked by the registry above. Mount its API router and wire it in.
    from gencall.core.loop_engine import LoopEngine
    from gencall.core.loop_matcher import LoopMatcher
    from gencall.api import loops as loops_api

    loop_engine = LoopEngine(sipp_engine, db=db, config=config)
    loops_api.loop_engine = loop_engine
    app.include_router(loops_api.router)

    # ── Loop accounting (design §4.3) ────────────────────────────────────────
    # The LoopMatcher joins out/in call_records into per-campaign loop_stats on a
    # throttled (>= 10 s) schedule and feeds each snapshot to the WS 'loops'
    # topic. The engine tracks/untracks campaigns on start/stop so the matcher
    # only works running campaigns.
    loop_matcher = LoopMatcher(db=db, on_stats=websocket.on_loop_stats)
    loops_api.loop_matcher = loop_matcher
    loop_engine.matcher = loop_matcher
    loop_matcher.start()

    # ── Per-call record ingest (design §4.2) ─────────────────────────────────
    # The CallRecordParser tail-parses each running SIPp instance's per-call
    # <log> file into call_records — the table EVERY minutes/completion stat is
    # computed from. Without this wiring call_records stays empty and all loop
    # accounting reads 0. The LoopEngine registers each UAC/UAS instance's log
    # path with this parser on start (and removes it on stop). The §4.1 trust
    # filter (config [trust] whitelist / drop_untrusted) lives here too, so it is
    # actually applied instead of being dead code.
    from gencall.core.call_records import CallRecordParser

    call_parser = CallRecordParser(
        db=db,
        trust_whitelist=config.trust_whitelist,
        drop_untrusted=config.trust_drop_untrusted,
    )
    loop_engine.parser = call_parser
    loops_api.call_parser = call_parser
    call_parser.start()

    # ── Retention (design §5, §7 stage 10) ───────────────────────────────────
    # call_records is the growth table; the retention job prunes it INTERVAL-
    # GATED (default once/24 h, rows older than 30 days), never per-iteration, so
    # we never rebuild sigma's DELETE storm. The gate timestamp is persisted, so
    # a restart loop cannot prune on every boot. No-op without a DB.
    from gencall.core.retention import build_from_config

    retention_job = build_from_config(config, db)
    retention_job.start()

    # Start the answering side now so returning MADA calls are always answered.
    # Best-effort: a missing real SIPp must not stop the API from coming up.
    try:
        loop_engine.start_answer()
    except Exception as e:
        logger.warning("Could not start loop answer side (UAS): %s", e)

    # ── Live streams ────────────────────────────────────────────────────────
    # Mount the WebSocket hub (/ws, /ws/stats, …) and feed it stats snapshots.
    # Headless fleet workers skip this — the controller pulls /api/stats over the
    # VLAN on a timer (aggregator), so no per-worker broadcast loop is needed.
    if config.serve_console:
        app.include_router(websocket.router)
        stats_engine.add_listener(websocket.on_stats_update)

    @asynccontextmanager
    async def _lifespan(_app):
        # The sync→async broadcast bridge needs the running loop.
        websocket.set_event_loop(asyncio.get_running_loop())
        try:
            yield
        finally:
            # Graceful shutdown (design §4.5): stop every running SIPp so killing
            # GenCall never leaves orphaned dialers. stop_all() runs the existing
            # SIGUSR1→SIGKILL group logic per instance and clears the registry.
            try:
                # Stop the answer-side monitor first so it doesn't restart the
                # UAS while we're tearing everything down.
                loop_engine.stop_monitor()
            except Exception as e:
                logger.warning("Error stopping loop monitor: %s", e)
            try:
                # Stop the call-record parser before the matcher so the matcher
                # is not racing a final parse pass during teardown.
                call_parser.stop()
            except Exception as e:
                logger.warning("Error stopping call-record parser: %s", e)
            try:
                loop_matcher.stop()
            except Exception as e:
                logger.warning("Error stopping loop matcher: %s", e)
            try:
                retention_job.stop()
            except Exception as e:
                logger.warning("Error stopping retention job: %s", e)
            try:
                logger.info("Shutdown: stopping all SIPp instances...")
                sipp_engine.stop_all()
            except Exception as e:
                logger.warning("Error during shutdown stop_all: %s", e)
            b = getattr(_app.state, "fleet_broadcaster", None)
            if b is not None:
                try:
                    b.stop()
                except Exception as e:
                    logger.warning("Error stopping fleet beacon: %s", e)

    app.router.lifespan_context = _lifespan

    # ── Fleet discovery beacon (opt-in: [fleet] announce = true) ─────────────
    # A worker broadcasts its address on the VLAN so a controller running with
    # [fleet] discovery = true auto-registers it. Trust = the shared fleet token.
    if config.fleet_announce:
        from gencall.core.discovery import BeaconBroadcaster

        node_addr = config.fleet_node_address or _derive_node_address(config)
        broadcaster = BeaconBroadcaster(
            config.fleet_token, node_addr,
            port=config.fleet_beacon_port,
            interval=config.fleet_beacon_interval,
            hostname=socket.gethostname(),
            version="2.0",
        )
        broadcaster.start()
        app.state.fleet_broadcaster = broadcaster

    # ── Web UI ──────────────────────────────────────────────────────────────
    # The NOC console SPA (frontend/, built into web/console/). On a FLEET WORKER
    # ([web] serve_console = false) we skip the console + live-stats WebSocket
    # entirely so the box runs headless (REST API + loop engine only) — the
    # single controller GUI is the one pane of glass. See config.serve_console.
    if not config.serve_console:
        logger.info("Headless mode: console + WS disabled (fleet worker)")

        @app.get("/", response_class=HTMLResponse, include_in_schema=False)
        def _headless_root():
            return (
                "<!doctype html><meta charset='utf-8'><title>GenCall worker</title>"
                "<body style='font:14px system-ui;padding:2rem'>"
                "<h3>GenCall fleet worker (headless)</h3>"
                "<p>This box runs the REST API + loop engine only. Manage it from "
                "the controller console.</p></body>"
            )
    elif os.path.isdir(CONSOLE_DIR):
        app.mount("/console", StaticFiles(directory=CONSOLE_DIR, html=True), name="console")

        @app.get("/", include_in_schema=False)
        def _root_redirect():
            return RedirectResponse(url="/console/")

        logger.info("NOC console mounted: /console")
    else:
        logger.warning(
            "Console build not found at %s — run `npm run build` in frontend/.",
            CONSOLE_DIR,
        )

        @app.get("/", response_class=HTMLResponse, include_in_schema=False)
        def _console_missing():
            return CONSOLE_MISSING_HTML

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
    parser.add_argument(
        "--mode", choices=["worker", "controller"], default="worker",
        help="Run-mode: 'worker' (default, single-node GenCall server) or "
             "'controller' (VanDorial fleet control-plane).",
    )
    parser.add_argument(
        "--headless", action="store_true",
        help="Fleet worker: run REST API + loop engine only (no web console / "
             "live-stats WebSocket) to save CPU. Same as [web] serve_console=false.",
    )

    args = parser.parse_args()
    if args.headless:
        os.environ["GENCALL_HEADLESS"] = "1"

    # Single-process guard (design §4.5): GenCall keeps all running-test and
    # managed-PID state in module-global / in-process structures, which are NOT
    # shared across uvicorn worker processes. Running >1 worker would give each
    # its own engine and registry — orphan tracking, shutdown stop_all, and
    # startup reconciliation would all silently cover only one of N processes.
    # Refuse it loudly rather than break reliability invariants.
    if args.workers and args.workers > 1:
        parser.error(
            "--workers > 1 is not supported: GenCall keeps per-process state "
            "(running tests, managed PIDs) that is not shared across workers, so "
            "multiple workers would break orphan tracking and shutdown. Run a "
            "single worker (scale out with the fleet controller instead)."
        )

    if args.mode == "controller":
        from gencall.controller.app import create_controller_app
        app, config = create_controller_app(Config(args.config))
    else:
        app, config = create_app(args.config)

    host = args.host or config.web_host
    port = args.port or config.web_port

    ssl_kwargs = {}
    if config.web_ssl and not args.no_ssl:
        ssl_kwargs["ssl_certfile"] = config.ssl_cert
        ssl_kwargs["ssl_keyfile"] = config.ssl_key

    banner = f"""
    ╔═══════════════════════════════════════════╗
    ║   GenCall v2.0 - SIP Traffic Generator    ║
    ║                                           ║
    ║   Console:   http://{host}:{port:<5d}/console  ║
    ║   API:       http://{host}:{port:<5d}/api      ║
    ║   Streams:   ws://{host}:{port:<5d}/ws         ║
    ║   Health:    http://{host}:{port:<5d}/api/health║
    ╚═══════════════════════════════════════════╝
    """
    try:
        print(banner)
    except UnicodeEncodeError:
        # Console encoding can't render box-drawing glyphs (e.g. Windows cp1252).
        # Fall back to plain ASCII rather than crashing on startup.
        print(
            f"\n  GenCall v2.0 - SIP Traffic Generator\n"
            f"    Console: http://{host}:{port}/console\n"
            f"    API:     http://{host}:{port}/api\n"
            f"    Streams: ws://{host}:{port}/ws\n"
            f"    Health:  http://{host}:{port}/api/health\n"
        )

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
