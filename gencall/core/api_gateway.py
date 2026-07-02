"""
GenCall - API Gateway & CI/CD Integration

Enables GenCall to be triggered from CI/CD pipelines, Slack bots,
monitoring systems, and other automation tools:
  - API key authentication
  - Webhook triggers (receive POST to start a test)
  - Test templates (pre-configured test configs)
  - Async test execution with callback URLs
  - Rate limiting
  - Audit log of all API actions
  - Integration endpoints for Jenkins, GitHub Actions, GitLab CI
"""

import hashlib
import hmac
import json
import time
import secrets
import logging
import threading
from collections import defaultdict, deque
from dataclasses import dataclass, field

logger = logging.getLogger("gencall.api_gateway")


# ─── API Key Management ──────────────────────────────────────────────────────

@dataclass
class APIKey:
    """An API key for authenticating external requests."""
    key_id: str
    key_hash: str        # SHA-256 hash of the actual key
    name: str            # Human-readable name
    created_at: float = 0.0
    last_used: float = 0.0
    enabled: bool = True
    permissions: list[str] = field(default_factory=lambda: ["read", "execute"])
    rate_limit: int = 60     # requests per minute
    request_count: int = 0
    # Console role for a session-backed principal ("admin"/"operator"/"viewer");
    # "machine" for an API key. Display/introspection only — enforcement is
    # driven by ``permissions`` ("execute" gates state-changing methods).
    role: str = "machine"

    def can_write(self) -> bool:
        """True if this principal may issue state-changing requests."""
        return "execute" in self.permissions

    def to_dict(self) -> dict:
        return {
            "key_id": self.key_id,
            "name": self.name,
            "created_at": self.created_at,
            "last_used": self.last_used,
            "enabled": self.enabled,
            "permissions": self.permissions,
            "rate_limit": self.rate_limit,
            "request_count": self.request_count,
            "role": self.role,
        }


def _row_to_apikey(row) -> "APIKey":
    """Convert a db.models.APIKey ORM row into the in-memory APIKey dataclass."""
    perms = (row.permissions or "").split(",") if row.permissions else []
    return APIKey(
        key_id=row.key_id,
        key_hash=row.key_hash,
        name=row.name,
        created_at=row.created_at or 0.0,
        last_used=row.last_used or 0.0,
        enabled=row.enabled,
        permissions=perms,
        rate_limit=row.rate_limit or 60,
        request_count=row.request_count or 0,
    )


class APIKeyManager:
    """Manages API keys for external authentication.

    When constructed with a `db` (a gencall.db.models.Database), keys are
    persisted to and validated against the `api_keys` table, so they survive
    restarts and are shared across worker processes. With `db=None` it keeps
    keys in memory only (used by unit tests and ephemeral contexts).
    """

    def __init__(self, db=None):
        self.db = db
        self._keys: dict[str, APIKey] = {}
        self._key_lookup: dict[str, str] = {}  # hash -> key_id
        self._lock = threading.Lock()

    def create_key(self, name: str, permissions: list[str] = None,
                   rate_limit: int = 60) -> tuple[str, APIKey]:
        """Create a new API key. Returns (raw_key, api_key_object)."""
        raw_key = f"gc_{secrets.token_urlsafe(32)}"
        key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
        key_id = f"key_{secrets.token_hex(8)}"
        perms = permissions or ["read", "execute"]

        api_key = APIKey(
            key_id=key_id,
            key_hash=key_hash,
            name=name,
            created_at=time.time(),
            permissions=perms,
            rate_limit=rate_limit,
        )

        if self.db is not None:
            from gencall.db.models import APIKey as APIKeyRow
            session = self.db.get_session()
            try:
                session.add(APIKeyRow(
                    key_id=key_id,
                    key_hash=key_hash,
                    name=name,
                    permissions=",".join(perms),
                    rate_limit=rate_limit,
                    request_count=0,
                    enabled=True,
                    created_at=api_key.created_at,
                    last_used=0.0,
                ))
                session.commit()
            finally:
                session.close()
        else:
            with self._lock:
                self._keys[key_id] = api_key
                self._key_lookup[key_hash] = key_id

        logger.info("API key created: %s (%s)", name, key_id)
        return raw_key, api_key

    def register_raw_key(self, raw_key: str, name: str = "console",
                         permissions: list[str] = None,
                         rate_limit: int = 240) -> APIKey:
        """Register a caller-supplied raw key so it validates like a minted one.

        Idempotent: if the key's hash already exists in the store, the existing
        record is returned unchanged. Used to pin a STABLE console key from
        ``GENCALL_CONSOLE_API_KEY`` so the auto-auth key survives restarts (a
        per-boot minted key would 401 every browser that cached the old one).
        """
        key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
        perms = permissions or ["read", "execute"]

        if self.db is not None:
            from gencall.db.models import APIKey as APIKeyRow
            session = self.db.get_session()
            try:
                existing = session.query(APIKeyRow).filter_by(
                    key_hash=key_hash).first()
                if existing:
                    return _row_to_apikey(existing)
                key_id = f"key_{secrets.token_hex(8)}"
                created = time.time()
                session.add(APIKeyRow(
                    key_id=key_id,
                    key_hash=key_hash,
                    name=name,
                    permissions=",".join(perms),
                    rate_limit=rate_limit,
                    request_count=0,
                    enabled=True,
                    created_at=created,
                    last_used=0.0,
                ))
                session.commit()
                logger.info("API key registered: %s (%s)", name, key_id)
                return APIKey(key_id=key_id, key_hash=key_hash, name=name,
                              created_at=created, permissions=perms,
                              rate_limit=rate_limit)
            finally:
                session.close()

        with self._lock:
            existing_id = self._key_lookup.get(key_hash)
            if existing_id:
                return self._keys[existing_id]
            key_id = f"key_{secrets.token_hex(8)}"
            api_key = APIKey(key_id=key_id, key_hash=key_hash, name=name,
                             created_at=time.time(), permissions=perms,
                             rate_limit=rate_limit)
            self._keys[key_id] = api_key
            self._key_lookup[key_hash] = key_id
            logger.info("API key registered: %s (%s)", name, key_id)
            return api_key

    def validate_key(self, raw_key: str) -> APIKey | None:
        """Validate a raw API key. Returns the APIKey if valid and enabled."""
        if not raw_key:
            return None
        key_hash = hashlib.sha256(raw_key.encode()).hexdigest()

        if self.db is not None:
            from gencall.db.models import APIKey as APIKeyRow
            session = self.db.get_session()
            try:
                row = session.query(APIKeyRow).filter_by(
                    key_hash=key_hash, enabled=True).first()
                if not row:
                    return None
                row.last_used = time.time()
                row.request_count = (row.request_count or 0) + 1
                result = _row_to_apikey(row)
                session.commit()
                return result
            finally:
                session.close()

        with self._lock:
            key_id = self._key_lookup.get(key_hash)
            if not key_id:
                return None

            api_key = self._keys.get(key_id)
            if not api_key or not api_key.enabled:
                return None

            api_key.last_used = time.time()
            api_key.request_count += 1
            return api_key

    def revoke_key(self, key_id: str) -> bool:
        if self.db is not None:
            from gencall.db.models import APIKey as APIKeyRow
            session = self.db.get_session()
            try:
                row = session.query(APIKeyRow).filter_by(key_id=key_id).first()
                if not row:
                    return False
                row.enabled = False
                session.commit()
                logger.info("API key revoked: %s", key_id)
                return True
            finally:
                session.close()

        with self._lock:
            api_key = self._keys.get(key_id)
            if not api_key:
                return False
            api_key.enabled = False
            # Remove from lookup
            self._key_lookup = {h: kid for h, kid in self._key_lookup.items()
                                 if kid != key_id}
            logger.info("API key revoked: %s", key_id)
            return True

    def list_keys(self) -> list[dict]:
        if self.db is not None:
            from gencall.db.models import APIKey as APIKeyRow
            session = self.db.get_session()
            try:
                return [_row_to_apikey(r).to_dict()
                        for r in session.query(APIKeyRow).all()]
            finally:
                session.close()

        with self._lock:
            return [k.to_dict() for k in self._keys.values()]

    def count_keys(self) -> int:
        """Number of keys in the store (used for first-run bootstrap)."""
        if self.db is not None:
            from gencall.db.models import APIKey as APIKeyRow
            session = self.db.get_session()
            try:
                return session.query(APIKeyRow).count()
            finally:
                session.close()
        with self._lock:
            return len(self._keys)


# ─── Rate Limiter ─────────────────────────────────────────────────────────────

class RateLimiter:
    """Token bucket rate limiter per API key."""

    def __init__(self):
        self._buckets: dict[str, deque] = defaultdict(lambda: deque(maxlen=1000))

    def check(self, key_id: str, limit: int) -> bool:
        """Check if a request is allowed. Returns True if within rate limit."""
        now = time.time()
        bucket = self._buckets[key_id]

        # Remove entries older than 60 seconds
        while bucket and bucket[0] < now - 60:
            bucket.popleft()

        if len(bucket) >= limit:
            return False

        bucket.append(now)
        return True

    def get_remaining(self, key_id: str, limit: int) -> int:
        now = time.time()
        bucket = self._buckets[key_id]
        while bucket and bucket[0] < now - 60:
            bucket.popleft()
        return max(0, limit - len(bucket))


# ─── Test Templates ───────────────────────────────────────────────────────────

@dataclass
class TestTemplate:
    """Pre-configured test that can be triggered by name."""
    template_id: str
    name: str
    description: str = ""
    scenario: str = "basic_call"
    remote_host: str = ""
    remote_port: int = 5060
    transport: str = "udp"
    call_rate: float = 1.0
    max_calls: int = 100
    call_limit: int = 10
    duration: int = 60
    success_threshold: float = 95.0   # min success rate to pass
    max_response_time_ms: float = 500  # max avg response time to pass
    tags: list[str] = field(default_factory=list)
    created_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "template_id": self.template_id,
            "name": self.name,
            "description": self.description,
            "scenario": self.scenario,
            "remote_host": self.remote_host,
            "remote_port": self.remote_port,
            "transport": self.transport,
            "call_rate": self.call_rate,
            "max_calls": self.max_calls,
            "call_limit": self.call_limit,
            "duration": self.duration,
            "success_threshold": self.success_threshold,
            "max_response_time_ms": self.max_response_time_ms,
            "tags": self.tags,
        }

    def to_start_request(self) -> dict:
        """Convert to API start test request format."""
        return {
            "name": f"template-{self.template_id}-{int(time.time())}",
            "scenario": self.scenario,
            "remote_host": self.remote_host,
            "remote_port": self.remote_port,
            "transport": self.transport,
            "call_rate": self.call_rate,
            "max_calls": self.max_calls,
            "call_limit": self.call_limit,
            "duration": self.duration,
        }


# ─── Audit Log ────────────────────────────────────────────────────────────────

@dataclass
class AuditEntry:
    timestamp: float
    key_id: str
    key_name: str
    action: str
    detail: str = ""
    ip_address: str = ""
    success: bool = True

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "key_id": self.key_id,
            "key_name": self.key_name,
            "action": self.action,
            "detail": self.detail,
            "ip_address": self.ip_address,
            "success": self.success,
        }


class AuditLog:
    def __init__(self, max_entries: int = 10000):
        self._entries: deque[AuditEntry] = deque(maxlen=max_entries)
        self._lock = threading.Lock()

    def log(self, key: APIKey, action: str, detail: str = "",
            ip: str = "", success: bool = True):
        entry = AuditEntry(
            timestamp=time.time(),
            key_id=key.key_id,
            key_name=key.name,
            action=action,
            detail=detail,
            ip_address=ip,
            success=success,
        )
        with self._lock:
            self._entries.append(entry)

    def get_entries(self, limit: int = 100, key_id: str = "",
                    action: str = "") -> list[dict]:
        with self._lock:
            entries = list(self._entries)
        if key_id:
            entries = [e for e in entries if e.key_id == key_id]
        if action:
            entries = [e for e in entries if e.action == action]
        return [e.to_dict() for e in entries[-limit:]]


# ─── Webhook Callback ────────────────────────────────────────────────────────

@dataclass
class WebhookCallback:
    """Configuration for calling back when a test completes."""
    url: str
    secret: str = ""          # For HMAC signature verification
    include_cdrs: bool = False
    include_stats: bool = True
    method: str = "POST"
    headers: dict[str, str] = field(default_factory=dict)

    def sign_payload(self, payload: str) -> str:
        """Generate HMAC-SHA256 signature for the payload."""
        if not self.secret:
            return ""
        return hmac.new(
            self.secret.encode(),
            payload.encode(),
            hashlib.sha256
        ).hexdigest()

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "include_cdrs": self.include_cdrs,
            "include_stats": self.include_stats,
        }


def send_webhook(callback: WebhookCallback, test_result: dict) -> bool:
    """Send a webhook callback with test results."""
    import urllib.request
    import urllib.error

    payload = json.dumps({
        "event": "test_completed",
        "timestamp": time.time(),
        "result": test_result,
    })

    headers = dict(callback.headers)
    headers["Content-Type"] = "application/json"
    headers["User-Agent"] = "GenCall/2.0"

    if callback.secret:
        headers["X-GenCall-Signature"] = f"sha256={callback.sign_payload(payload)}"

    try:
        req = urllib.request.Request(
            callback.url,
            data=payload.encode(),
            headers=headers,
            method=callback.method,
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            logger.info("Webhook sent to %s: %d", callback.url, resp.status)
            return resp.status < 400
    except Exception as e:
        logger.error("Webhook failed: %s -> %s", callback.url, e)
        return False


# ─── CI/CD Integration Helpers ────────────────────────────────────────────────

def generate_github_actions_step(template: TestTemplate, api_url: str,
                                  api_key: str) -> str:
    """Generate a GitHub Actions step YAML for running a GenCall test."""
    return f"""    - name: Run SIP Test ({template.name})
      run: |
        RESULT=$(curl -s -X POST {api_url}/api/gateway/run-template \\
          -H "Authorization: Bearer {api_key}" \\
          -H "Content-Type: application/json" \\
          -d '{{"template_id": "{template.template_id}", "wait": true}}')
        echo "$RESULT" | python3 -c "
        import sys, json
        r = json.load(sys.stdin)
        print(f'Success Rate: {{r.get(\"success_rate\", 0)}}%')
        print(f'Total Calls: {{r.get(\"total_calls\", 0)}}')
        if r.get('passed', False):
            print('TEST PASSED')
        else:
            print('TEST FAILED')
            sys.exit(1)
        "
"""


def generate_jenkins_pipeline_stage(template: TestTemplate, api_url: str) -> str:
    """Generate a Jenkins pipeline stage for GenCall testing."""
    return f"""        stage('SIP Test: {template.name}') {{
            steps {{
                script {{
                    def response = httpRequest(
                        url: '{api_url}/api/gateway/run-template',
                        httpMode: 'POST',
                        customHeaders: [[name: 'Authorization', value: "Bearer ${{GENCALL_API_KEY}}"]],
                        requestBody: '{{"template_id": "{template.template_id}", "wait": true}}',
                        contentType: 'APPLICATION_JSON'
                    )
                    def result = readJSON text: response.content
                    if (!result.passed) {{
                        error "SIP test failed: ${{result.success_rate}}% success rate"
                    }}
                }}
            }}
        }}
"""


# ─── Gateway Controller ──────────────────────────────────────────────────────

class APIGateway:
    """
    Central gateway for external API access.
    Handles auth, rate limiting, templates, and audit logging.
    """

    def __init__(self):
        self.keys = APIKeyManager()
        self.rate_limiter = RateLimiter()
        self.audit = AuditLog()
        self.templates: dict[str, TestTemplate] = {}
        self._lock = threading.Lock()
        # Console login layer (set in main.py / controller app when a DB exists).
        # `users` authenticates people; `sessions` issues browser login tokens
        # that the auth dependency validates alongside machine API keys. They
        # stay None on boxes without a database (auth degrades to keys-only).
        self.users = None        # gencall.core.auth_users.UserManager
        self.sessions = None     # gencall.core.auth_users.SessionManager

    def add_template(self, template: TestTemplate):
        with self._lock:
            self.templates[template.template_id] = template

    def get_template(self, template_id: str) -> TestTemplate | None:
        return self.templates.get(template_id)

    def list_templates(self) -> list[dict]:
        return [t.to_dict() for t in self.templates.values()]

    def authenticate(self, raw_key: str) -> APIKey | None:
        """Authenticate and rate-check an API key."""
        api_key = self.keys.validate_key(raw_key)
        if not api_key:
            return None

        if not self.rate_limiter.check(api_key.key_id, api_key.rate_limit):
            logger.warning("Rate limit exceeded for key %s", api_key.key_id)
            return None

        return api_key

    def to_dict(self) -> dict:
        return {
            "api_keys": len(self.keys._keys),
            "templates": len(self.templates),
            "audit_entries": len(self.audit._entries),
        }
