# Copyright (C) 2026 James Hickman
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""Per-request credential resolution for the Streamable HTTP transport.

Two credential paths, both ending at the same LDAP-derived identity:
  * ``Authorization: Basic <key_id:secret>`` → a backend-generated service
    credential (scope ``mcp``, PROPOSAL §16), verified against ldap_manager; roles
    come from LDAP. There is NO directory-password path (no legacy).
  * ``Authorization: Bearer <token>``       → a token from ``/auth/token`` (one
    verify, cached), resolved against the TokenStore.

The tenant is per-session: taken from the ``X-Tenant`` header or the request's
subdomain, falling back to the configured default — independent of the user's
LDAP entry, so one account can act across tenants."""
import base64
import logging
import os

from .ldap_auth import Identity, resolve_roles
from .tenant_state import TenantStateGate

log = logging.getLogger("fileengine_mcp.http_auth")
from .service_cred_client import get_verifier
from .token_store import TokenStore


def decode_basic(header_value: str) -> tuple[str, str] | None:
    """Decode an ``Authorization: Basic`` header into ``(key_id, secret)``."""
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


def _reserved_labels() -> frozenset:
    """Host labels that are never a tenant, matching the bridge's
    ``isReservedTenantLabel``; ``login`` (or ``LOGIN_SUBDOMAIN``) is the shared
    sign-in origin. Read per call so a test or a restart picks up the setting."""
    login = (os.environ.get("LOGIN_SUBDOMAIN") or "login").strip().lower()
    return frozenset({"www", "api", "localhost", "mcp", login})


def extract_tenant(headers: dict, host: str, default: str) -> str:
    """Resolve the request tenant: explicit ``X-Tenant`` wins, else a subdomain
    label of the Host header, else the configured default.

    A bare host or one whose first label looks like a public/base name
    (``www``, ``api``, ``localhost``, ``mcp``) yields the default."""
    reserved = _reserved_labels()
    explicit = (headers.get("x-tenant") or "").strip()
    # A reserved name in X-Tenant is ignored, not obeyed, as the bridge does.
    if explicit and explicit.lower() not in reserved:
        return explicit
    host = (host or "").split(":", 1)[0]
    labels = host.split(".")
    if len(labels) >= 3:  # sub.domain.tld
        # Tenant ids contain no hyphen; <tenant>-<interface> resolves to the tenant.
        first = labels[0].strip().lower().split("-", 1)[0]
        if first and first not in reserved:
            return first
    return default


#: The tenant-state gate for this door. Module level so its cache is shared
#: across requests; the source is injected at startup (and by the tests).
TENANT_GATE = TenantStateGate()


def resolve_identity(auth_header: str, tenant: str, config, store: TokenStore) -> Identity | None:
    """Resolve an Authorization header to an authenticated Identity scoped to
    ``tenant``, or ``None`` if authentication fails / no credentials are given.

    §3.4c: the tenant's lifecycle state is checked here, at the ONE point both
    credential paths pass through. Only `live` admits; everything else refuses,
    and so does a lookup that could not be completed. Putting it here rather than
    in each branch is deliberate — a Bearer session must stop working when its
    tenant is suspended, not merely a fresh Basic login.
    """
    if not auth_header:
        return None
    if auth_header.startswith("Bearer "):
        identity = store.resolve(auth_header[len("Bearer "):].strip())
        if identity is None:
            return None
        # A token is bound to the tenant it was issued for, with the roles held
        # THERE. It used to be re-stamped with whatever tenant this request
        # named, so a token minted in A, sent with `X-Tenant: B`, acted in B
        # carrying A's roles. Refuse instead, as the other doors do.
        if identity.tenant != tenant:
            log.warning("MCP door refused %s: token issued for tenant %r used for %r",
                        identity.user or "<unknown>", identity.tenant, tenant)
            return None
        return _gated(identity, tenant)
    basic = decode_basic(auth_header)
    if basic is None:
        return None
    key_id, secret = basic
    # §16: verify the key:secret (scope "mcp") against ldap_manager, then resolve
    # roles from LDAP for the returned uid. A directory password is never accepted.
    uid = get_verifier(config).verify(key_id, secret, tenant, "mcp")
    if uid is None:
        return None
    # Roles held in the tenant this request is for, not the configured one.
    identity = resolve_roles(config, uid, tenant)
    if not identity.authenticated:
        return None
    return _gated(identity, tenant)


def _gated(identity: Identity | None, tenant: str) -> Identity | None:
    """Apply the tenant-state gate to an otherwise-authenticated identity.

    Returns None when the tenant does not admit, so the caller's existing
    "authentication failed" path handles it. The REASON is logged rather than
    returned: this door speaks MCP, not HTTP semantics, and the distinct-status
    treatment §3.4c property 4 asks for belongs where there is a status to set.
    That is a real gap for a person debugging an MCP client, and it is recorded
    as one rather than papered over.
    """
    if identity is None:
        return None
    ok, reason = TENANT_GATE.admits(tenant)
    if not ok:
        log.warning("MCP door refused %s: %s", identity.user or "<unknown>", reason)
        return None
    return identity
