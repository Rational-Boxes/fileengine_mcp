"""Verify a ``key:secret`` service credential against ldap_manager (PROPOSAL §16).

The MCP door — like the WebDAV bridge — no longer accepts an LDAP directory
password. A Basic ``key:secret`` (scope ``mcp``) is verified here via ldap_manager's
internal endpoint; the resolved uid's roles then come from LDAP
(``ldap_auth.resolve_roles``). Successful verifications are cached briefly: the HTTP
transport verifies once at ``/auth/token`` and reuses the bearer, but the
direct-Basic path would otherwise re-verify per request.
"""
import json
import threading
import time
import urllib.error
import urllib.request
from typing import Optional


class ServiceCredVerifier:
    def __init__(self, base_url: str, internal_secret: str,
                 cache_ttl: int = 60, timeout: float = 3.0) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.secret = internal_secret or ""
        self.ttl = cache_ttl
        self.timeout = timeout
        self._lock = threading.Lock()
        self._cache: dict[tuple, tuple[str, float]] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.base_url) and bool(self.secret)

    def verify(self, key_id: str, secret: str, tenant: str, scope: str,
               source_ip: Optional[str] = None) -> Optional[str]:
        """Return the uid iff the key:secret is valid for ``scope`` in ``tenant``."""
        if not self.enabled or not key_id or not secret:
            return None
        ck = (key_id, secret, tenant, scope)
        now = time.time()
        with self._lock:
            hit = self._cache.get(ck)
            if hit and hit[1] > now:
                return hit[0]
        payload = {"key_id": key_id, "secret": secret, "tenant": tenant, "scope": scope}
        if source_ip:
            payload["source_ip"] = source_ip
        req = urllib.request.Request(
            self.base_url + "/internal/service-cred/verify",
            data=json.dumps(payload).encode("utf-8"), method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("X-Internal-Auth", self.secret)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                if resp.status != 200:
                    return None
                body = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError):
            return None  # unreachable / 401 / bad body → deny
        uid = body.get("uid")
        if not uid:
            return None
        with self._lock:
            self._cache[ck] = (uid, now + self.ttl)
        return uid


_verifier: Optional[ServiceCredVerifier] = None
_verifier_lock = threading.Lock()


def get_verifier(config) -> ServiceCredVerifier:
    """A process-wide verifier built from config (config is a singleton)."""
    global _verifier
    if _verifier is None:
        with _verifier_lock:
            if _verifier is None:
                _verifier = ServiceCredVerifier(
                    config.ldap_manager_url, config.service_cred_internal_secret,
                    config.verify_cache_ttl)
    return _verifier
