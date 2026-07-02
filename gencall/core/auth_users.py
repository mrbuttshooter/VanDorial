"""
Console login: user accounts + browser sessions.

This is the human-facing authentication layer that sits in front of the NOC
console. It is deliberately separate from the fleet API-key system
(gencall.core.api_gateway): API keys authenticate machines (worker↔controller,
CI), while these accounts authenticate people opening the console in a browser.

  * ``UserManager``   — CRUD over the ``users`` table; passwords are hashed with
    PBKDF2-HMAC-SHA256 (Python stdlib, so an offline installer needs no extra
    package). All accounts share one role today: any logged-in user has full
    console access. The ``role`` column is kept for a future RBAC split.
  * ``SessionManager`` — issues a random session token on login, stores only its
    SHA-256 hash with an expiry, and validates/revokes it. The raw token is
    handed to the browser and presented exactly like an API key, so the existing
    auth dependency validates it through one extra lookup.

Both require a ``Database``; they raise ``RuntimeError`` if constructed without
one (there is no in-memory fallback — login must be durable across restarts).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time

logger = logging.getLogger("gencall.auth_users")

# Console RBAC. A viewer may only issue safe (GET/HEAD/OPTIONS) requests;
# operator and admin get full read+write. Machine API keys are unaffected —
# they carry their own permissions and are meant for automation. The mapping is
# expressed as gateway permissions so the existing require_api_key path enforces
# it: "execute" gates every state-changing method.
VALID_ROLES = ("admin", "operator", "viewer")
DEFAULT_ROLE = "operator"


def permissions_for_role(role: str) -> list[str]:
    """Gateway permission list for a console role (unknown role -> read-only)."""
    if role in ("admin", "operator"):
        return ["read", "execute"]
    return ["read"]

# PBKDF2 cost. 200k SHA-256 iterations is a sane 2020s default for an internal
# tool and keeps login well under ~150 ms on the boxes this runs on.
_PBKDF2_ITERS = 200_000
_PBKDF2_ALGO = "pbkdf2_sha256"

# Session lifetime: a console login is good for 12 hours, then re-auth.
DEFAULT_SESSION_TTL_S = 12 * 3600

# Session tokens carry this prefix so the auth dependency can route them to the
# session store without a wasted API-key lookup (and so they're recognisable).
SESSION_TOKEN_PREFIX = "gcs_"


# ─── Password hashing (stdlib PBKDF2) ────────────────────────────────────────

def hash_password(password: str) -> str:
    """Return a ``pbkdf2_sha256$iters$salt_hex$hash_hex`` encoded hash."""
    if not password:
        raise ValueError("password must not be empty")
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERS)
    return f"{_PBKDF2_ALGO}${_PBKDF2_ITERS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    """Constant-time check of ``password`` against a stored encoded hash."""
    try:
        algo, iters_s, salt_hex, hash_hex = (encoded or "").split("$")
        if algo != _PBKDF2_ALGO:
            return False
        iters = int(iters_s)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (ValueError, AttributeError):
        return False
    dk = hashlib.pbkdf2_hmac("sha256", (password or "").encode("utf-8"), salt, iters)
    return hmac.compare_digest(dk, expected)


# A fixed, valid-format hash used only to spend the SAME PBKDF2 time on logins
# for unknown/disabled users as for real ones — otherwise the response latency
# reveals whether a username exists (an enumeration oracle).
_DUMMY_PASSWORD_HASH = hash_password("gencall-login-timing-equalizer")


# ─── Users ───────────────────────────────────────────────────────────────────

class UserManager:
    """CRUD over the ``users`` table (console login accounts)."""

    def __init__(self, db):
        if db is None:
            raise RuntimeError("UserManager requires a database")
        self.db = db

    def count_users(self) -> int:
        from gencall.db.models import User
        session = self.db.get_session()
        try:
            return session.query(User).count()
        finally:
            session.close()

    def create_user(self, username: str, password: str,
                    role: str = DEFAULT_ROLE) -> dict:
        """Create an account. Raises ValueError on a blank/duplicate username."""
        from gencall.db.models import User
        username = (username or "").strip()
        if not username:
            raise ValueError("username is required")
        if len(password or "") < 8:
            raise ValueError("password must be at least 8 characters")
        role = (role or DEFAULT_ROLE).strip().lower()
        if role not in VALID_ROLES:
            raise ValueError(
                f"role must be one of {', '.join(VALID_ROLES)}")
        session = self.db.get_session()
        try:
            if session.query(User).filter_by(username=username).first():
                raise ValueError(f"user {username!r} already exists")
            u = User(username=username, password_hash=hash_password(password),
                     role=role, enabled=True)
            session.add(u)
            session.commit()
            logger.info("console user created: %s", username)
            return u.to_dict()
        finally:
            session.close()

    def verify(self, username: str, password: str) -> dict | None:
        """Return the user dict if the credentials are valid and enabled."""
        from gencall.db.models import User
        session = self.db.get_session()
        try:
            u = session.query(User).filter_by(username=(username or "").strip()).first()
            if not u or not u.enabled:
                # Spend the same PBKDF2 time as a real check so login latency
                # can't be used to tell whether the username exists.
                verify_password(password, _DUMMY_PASSWORD_HASH)
                return None
            if not verify_password(password, u.password_hash):
                return None
            return u.to_dict()
        finally:
            session.close()

    def list_users(self) -> list[dict]:
        from gencall.db.models import User
        session = self.db.get_session()
        try:
            return [u.to_dict() for u in session.query(User).order_by(User.id).all()]
        finally:
            session.close()

    def set_password(self, user_id: int, password: str) -> bool:
        from gencall.db.models import User, LoginSession
        if len(password or "") < 8:
            raise ValueError("password must be at least 8 characters")
        session = self.db.get_session()
        try:
            u = session.query(User).filter_by(id=user_id).first()
            if not u:
                return False
            u.password_hash = hash_password(password)
            # A password change must invalidate every outstanding browser session
            # for this account — otherwise a compromised/stale session survives the
            # reset. SessionManager.validate() only checks token_hash+expiry, so
            # deleting the rows is the only way to revoke them.
            session.query(LoginSession).filter_by(user_id=user_id).delete()
            session.commit()
            return True
        finally:
            session.close()

    def set_role(self, user_id: int, role: str) -> bool:
        """Change an account's role. Raises ValueError on an unknown role."""
        from gencall.db.models import User
        role = (role or "").strip().lower()
        if role not in VALID_ROLES:
            raise ValueError(f"role must be one of {', '.join(VALID_ROLES)}")
        session = self.db.get_session()
        try:
            u = session.query(User).filter_by(id=user_id).first()
            if not u:
                return False
            u.role = role
            session.commit()
            return True
        finally:
            session.close()

    def delete_user(self, user_id: int) -> bool:
        from gencall.db.models import User, LoginSession
        session = self.db.get_session()
        try:
            u = session.query(User).filter_by(id=user_id).first()
            if not u:
                return False
            # Revoke the user's live browser sessions before removing the account.
            # validate() authenticates on token_hash+expiry alone and never
            # re-checks that the account still exists, so without this an already
            # -issued session token keeps working after the user is deleted.
            session.query(LoginSession).filter_by(user_id=user_id).delete()
            session.delete(u)
            session.commit()
            logger.info("console user deleted: %s", u.username)
            return True
        finally:
            session.close()


# ─── Sessions ──────────────────────────────────────────────────────────────────

class SessionManager:
    """Issues and validates browser login sessions (hashed tokens + expiry)."""

    def __init__(self, db, ttl_s: int = DEFAULT_SESSION_TTL_S):
        if db is None:
            raise RuntimeError("SessionManager requires a database")
        self.db = db
        self.ttl_s = ttl_s

    @staticmethod
    def _hash(raw_token: str) -> str:
        return hashlib.sha256((raw_token or "").encode("utf-8")).hexdigest()

    def create(self, user_id: int, username: str) -> tuple[str, float]:
        """Mint a session for a user. Returns (raw_token, expires_at_epoch)."""
        from gencall.db.models import LoginSession
        raw = SESSION_TOKEN_PREFIX + secrets.token_urlsafe(32)
        now = time.time()
        expires = now + self.ttl_s
        session = self.db.get_session()
        try:
            session.add(LoginSession(
                token_hash=self._hash(raw), user_id=user_id, username=username,
                created_at=now, expires_at=expires))
            session.commit()
            return raw, expires
        finally:
            session.close()

    def validate(self, raw_token: str) -> dict | None:
        """Return {user_id, username, role, expires_at} for a live token, else None.

        The role is read from the user row (not frozen at login) so promoting or
        demoting an account takes effect on its next request. A missing/disabled
        user invalidates the session.
        """
        if not raw_token:
            return None
        from gencall.db.models import LoginSession, User
        session = self.db.get_session()
        try:
            row = session.query(LoginSession).filter_by(
                token_hash=self._hash(raw_token)).first()
            if not row:
                return None
            if row.expires_at and row.expires_at < time.time():
                # Expired: drop it so the table self-cleans on use.
                session.delete(row)
                session.commit()
                return None
            user = session.query(User).filter_by(id=row.user_id).first()
            if user is None or not user.enabled:
                # The account was deleted or disabled after this token was issued.
                return None
            return {"user_id": row.user_id, "username": row.username,
                    "role": user.role or DEFAULT_ROLE,
                    "expires_at": row.expires_at}
        finally:
            session.close()

    def revoke(self, raw_token: str) -> bool:
        from gencall.db.models import LoginSession
        session = self.db.get_session()
        try:
            row = session.query(LoginSession).filter_by(
                token_hash=self._hash(raw_token)).first()
            if not row:
                return False
            session.delete(row)
            session.commit()
            return True
        finally:
            session.close()

    def purge_expired(self) -> int:
        from gencall.db.models import LoginSession
        session = self.db.get_session()
        try:
            n = session.query(LoginSession).filter(
                LoginSession.expires_at < time.time()).delete()
            session.commit()
            return n
        finally:
            session.close()
