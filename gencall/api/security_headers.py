"""
Security response headers (defense-in-depth for the console + API).

Adds a small, SPA-safe set of hardening headers to every HTTP response:
  * X-Content-Type-Options: nosniff   — stop MIME-type sniffing.
  * X-Frame-Options: DENY             — the NOC console is standalone; never
    let it be framed (clickjacking).
  * Referrer-Policy: no-referrer      — don't leak console URLs to third parties.
  * Content-Security-Policy           — only the low-risk directives
    (frame-ancestors/object-src/base-uri). We deliberately do NOT constrain
    script-src/default-src here so the built Vite bundle can't be broken by a
    too-strict policy; frame-ancestors 'none' still kills clickjacking.

HSTS is intentionally omitted: boxes can be flipped back to HTTP with
enable-https.sh --off, and the certs are self-signed, so pinning browsers to
HTTPS would be a foot-gun. Add it deliberately per-deployment if wanted.
"""

from starlette.middleware.base import BaseHTTPMiddleware

_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "frame-ancestors 'none'; object-src 'none'; base-uri 'self'",
}


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        resp = await call_next(request)
        for k, v in _HEADERS.items():
            resp.headers.setdefault(k, v)
        return resp


def install_security_headers(app) -> None:
    """Attach the security-headers middleware to a FastAPI/Starlette app."""
    app.add_middleware(SecurityHeadersMiddleware)
