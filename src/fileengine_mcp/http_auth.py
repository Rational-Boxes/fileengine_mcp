"""Per-request credential resolution for the Streamable HTTP transport.

Two credential paths, both ending at the same LDAP-derived identity (mirroring
the HTTP bridge):
  * ``Authorization: Basic <user:pass>``  → a live LDAP bind every request.
  * ``Authorization: Bearer <token>``     → a token from ``/auth/token`` (one
    bind, cached), resolved against the TokenStore.

The tenant is per-session: taken from the ``X-Tenant`` header or the request's
subdomain, falling back to the configured default — independent of the user's
LDAP entry, so one account can act across tenants."""
import base64
from dataclasses import replace

from .ldap_auth import Identity, authenticate
from .token_store import TokenStore


def decode_basic(header_value: str) -> tuple[str, str] | None:
    """Decode an ``Authorization: Basic`` header into ``(user, password)``."""
    if not header_value.startswith("Basic "):
        return None
    try:
        raw = base64.b64decode(header_value[len("Basic "):]).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    if ":" not in raw:
        return None
    user, password = raw.split(":", 1)
    return user, password


def extract_tenant(headers: dict, host: str, default: str) -> str:
    """Resolve the request tenant: explicit ``X-Tenant`` wins, else a subdomain
    label of the Host header, else the configured default.

    A bare host or one whose first label looks like a public/base name
    (``www``, ``api``, ``localhost``, ``mcp``) yields the default."""
    explicit = headers.get("x-tenant")
    if explicit:
        return explicit.strip()
    host = (host or "").split(":", 1)[0]
    labels = host.split(".")
    if len(labels) >= 3:  # sub.domain.tld
        first = labels[0].strip().lower()
        if first and first not in ("www", "api", "localhost", "mcp"):
            return first
    return default


def resolve_identity(auth_header: str, tenant: str, config, store: TokenStore) -> Identity | None:
    """Resolve an Authorization header to an authenticated Identity scoped to
    ``tenant``, or ``None`` if authentication fails / no credentials are given."""
    if not auth_header:
        return None
    if auth_header.startswith("Bearer "):
        identity = store.resolve(auth_header[len("Bearer "):].strip())
        if identity is None:
            return None
        return replace(identity, tenant=tenant)
    basic = decode_basic(auth_header)
    if basic is None:
        return None
    identity = authenticate(config, basic[0], basic[1])
    if not identity.authenticated:
        return None
    return replace(identity, tenant=tenant)
