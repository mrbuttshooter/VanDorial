"""
GenCall Authentication Utilities.
Simple password hashing for the web interface.
"""

import hashlib
import secrets


def hash_password(password: str, salt: str = None) -> tuple[str, str]:
    """Hash a password with SHA-256 + salt. Returns (hash, salt)."""
    if salt is None:
        salt = secrets.token_hex(16)
    hashed = hashlib.sha256((salt + password).encode()).hexdigest()
    return hashed, salt


def verify_password(password: str, hashed: str, salt: str) -> bool:
    """Verify a password against a hash."""
    check, _ = hash_password(password, salt)
    return check == hashed


def generate_api_key() -> str:
    """Generate a random API key."""
    return secrets.token_urlsafe(32)
