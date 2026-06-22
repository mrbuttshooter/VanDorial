"""
Console authentication API — login / logout / account management.

The console now requires a login: a user POSTs credentials to /api/auth/login
and receives a session token, which the browser then presents on every request
exactly like an API key (X-API-Key header / ws ``api_key`` param). The token is
validated by the shared ``require_api_key`` dependency (gencall.api.routes),
which falls back to the session store for non-key tokens.

All accounts share one role today (any logged-in user has full console access);
the account-management endpoints are themselves login-protected. Login is the
only unauthenticated endpoint here and is throttled per client IP to blunt
brute-force attempts.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict, deque

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from gencall.api import routes as _routes
from gencall.api.routes import require_api_key

logger = logging.getLogger("gencall.api.auth")

router = APIRouter(tags=["auth"])


# ─── Brute-force throttle (per client IP) ────────────────────────────────────

_MAX_FAILS = 5
_FAIL_WINDOW_S = 300
_fail_log: dict[str, deque] = defaultdict(deque)


def _too_many_failures(ip: str) -> bool:
    dq = _fail_log[ip]
    now = time.time()
    while dq and now - dq[0] > _FAIL_WINDOW_S:
        dq.popleft()
    return len(dq) >= _MAX_FAILS


def _record_failure(ip: str) -> None:
    _fail_log[ip].append(time.time())


def _clear_failures(ip: str) -> None:
    _fail_log.pop(ip, None)


# ─── Models ──────────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    username: str
    password: str


class CreateUserRequest(BaseModel):
    username: str
    password: str = Field(min_length=8)


class SetPasswordRequest(BaseModel):
    password: str = Field(min_length=8)


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _gateway():
    gw = getattr(_routes, "gateway", None)
    if gw is None or getattr(gw, "users", None) is None or getattr(gw, "sessions", None) is None:
        raise HTTPException(503, "Login is not configured on this box")
    return gw


# ─── Endpoints ───────────────────────────────────────────────────────────────

@router.post("/api/auth/login")
def login(req: LoginRequest, request: Request):
    """Authenticate a user and issue a session token."""
    gw = _gateway()
    ip = request.client.host if request.client else "?"
    if _too_many_failures(ip):
        raise HTTPException(429, "Too many failed logins — try again later")

    user = gw.users.verify(req.username, req.password)
    if not user:
        _record_failure(ip)
        logger.warning("auth: failed login for %r from %s", req.username, ip)
        raise HTTPException(401, "Invalid username or password")

    _clear_failures(ip)
    token, expires_at = gw.sessions.create(user["id"], user["username"])
    logger.info("auth: login %s from %s", user["username"], ip)
    return {"token": token, "username": user["username"], "expires_at": expires_at}


@router.post("/api/auth/logout")
def logout(request: Request, x_api_key: str = None):
    """Revoke the current session token (idempotent)."""
    gw = getattr(_routes, "gateway", None)
    token = request.headers.get("x-api-key")
    if gw is not None and getattr(gw, "sessions", None) is not None and token:
        gw.sessions.revoke(token)
    return {"status": "logged_out"}


@router.get("/api/auth/me", dependencies=[Depends(require_api_key)])
def me(principal=Depends(require_api_key)):
    """Return the current principal (logged-in username, or the API key name)."""
    return {"username": principal.name, "key_id": principal.key_id}


@router.get("/api/auth/users", dependencies=[Depends(require_api_key)])
def list_users():
    return {"users": _gateway().users.list_users()}


@router.post("/api/auth/users", dependencies=[Depends(require_api_key)])
def create_user(req: CreateUserRequest):
    try:
        return {"status": "created", "user": _gateway().users.create_user(
            req.username, req.password)}
    except ValueError as e:
        raise HTTPException(422, str(e))


@router.post("/api/auth/users/{user_id}/password", dependencies=[Depends(require_api_key)])
def set_password(user_id: int, req: SetPasswordRequest):
    try:
        ok = _gateway().users.set_password(user_id, req.password)
    except ValueError as e:
        raise HTTPException(422, str(e))
    if not ok:
        raise HTTPException(404, f"User {user_id} not found")
    return {"status": "updated", "id": user_id}


@router.delete("/api/auth/users/{user_id}", dependencies=[Depends(require_api_key)])
def delete_user(user_id: int):
    gw = _gateway()
    # Refuse to delete the last account — that would lock everyone out of the
    # console with no way back in short of the DB.
    if gw.users.count_users() <= 1:
        raise HTTPException(409, "Cannot delete the last remaining user")
    if not gw.users.delete_user(user_id):
        raise HTTPException(404, f"User {user_id} not found")
    return {"status": "deleted", "id": user_id}
