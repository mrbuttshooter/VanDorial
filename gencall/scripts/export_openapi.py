"""
Export the OpenAPI schema WITHOUT booting the app (no DB, no threads, no SIPp).

The runtime apps disable the unauthenticated /openapi.json (the schema maps the
whole API surface to any network peer); authenticated callers get it from
GET /api/openapi.json. This script is the build-time path: it assembles a
schema-only FastAPI app (routers included, nothing wired) and prints the JSON,
so the frontend can generate TypeScript types from it and CI can diff the
checked-in copy against the code.

    python -m gencall.scripts.export_openapi --role worker  > docs/api/openapi.worker.json
    python -m gencall.scripts.export_openapi --role controller > docs/api/openapi.controller.json
    python -m gencall.scripts.export_openapi --role worker --check docs/api/openapi.worker.json
"""

import argparse
import json
import sys


def build_worker_schema() -> dict:
    # Importing routes creates the module-global app with the core REST surface;
    # the remaining routers are added exactly as main.create_app() does, but with
    # no engine/DB/thread wiring (handlers are never called for schema build).
    from gencall.api import routes
    from gencall.api import auth as auth_api
    from gencall.api import loops as loops_api
    from gencall.api import websocket

    app = routes.app
    app.include_router(auth_api.router)
    app.include_router(loops_api.router)
    app.include_router(websocket.router)
    return app.openapi()


def build_controller_schema() -> dict:
    from fastapi import FastAPI

    from gencall.api import auth as auth_api
    from gencall.controller import routes as controller_routes
    from gencall.controller import ws as controller_ws

    app = FastAPI(
        title="VanDorial Fleet Controller API",
        description="VanDorial fleet control-plane for GenCall workers",
        version="2.0.0",
    )
    app.include_router(controller_routes.router)
    app.include_router(controller_ws.router)
    app.include_router(auth_api.router)
    return app.openapi()


def render(role: str) -> str:
    schema = build_worker_schema() if role == "worker" else build_controller_schema()
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Export the GenCall OpenAPI schema")
    parser.add_argument("--role", choices=["worker", "controller"], default="worker")
    parser.add_argument("-o", "--output", default=None,
                        help="Write to this file instead of stdout")
    parser.add_argument("--check", metavar="FILE", default=None,
                        help="Exit 1 if FILE differs from the schema the code produces")
    args = parser.parse_args(argv)

    rendered = render(args.role)

    if args.check:
        try:
            with open(args.check, encoding="utf-8") as fh:
                on_disk = fh.read()
        except FileNotFoundError:
            print(f"MISSING: {args.check} — regenerate with:\n"
                  f"  python -m gencall.scripts.export_openapi --role {args.role} "
                  f"-o {args.check}", file=sys.stderr)
            return 1
        if on_disk != rendered:
            print(f"STALE: {args.check} does not match the API code — regenerate with:\n"
                  f"  python -m gencall.scripts.export_openapi --role {args.role} "
                  f"-o {args.check}", file=sys.stderr)
            return 1
        print(f"OK: {args.check} is current")
        return 0

    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(rendered)
        print(f"Wrote {args.output}")
    else:
        sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    sys.exit(main())
