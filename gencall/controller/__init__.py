"""
VanDorial Fleet Controller package.

The controller is the second run-mode of the GenCall image (design §2). It owns
the node inventory + groups, fans out commands to worker nodes, aggregates their
telemetry, health-checks them, and serves the fleet-aware console. The browser
only ever talks to the controller; the controller proxies node-scoped requests
and merges live streams into a single API surface.

Public entrypoint: :func:`gencall.controller.app.create_controller_app`.
"""

__all__ = ["create_controller_app"]


def __getattr__(name):  # pragma: no cover - thin lazy import shim
    # Lazy-import so `import gencall.controller` is cheap and does not pull in
    # FastAPI/httpx unless the app factory is actually requested.
    if name == "create_controller_app":
        from gencall.controller.app import create_controller_app
        return create_controller_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
