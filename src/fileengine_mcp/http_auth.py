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
from dataclasses import replace

from .ldap_auth import Identity, resolve_roles
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
    key_id, secret = basic
    # §16: verify the key:secret (scope "mcp") against ldap_manager, then resolve
    # roles from LDAP for the returned uid. A directory password is never accepted.
    uid = get_verifier(config).verify(key_id, secret, tenant, "mcp")
    if uid is None:
        return None
    identity = resolve_roles(config, uid)
    if not identity.authenticated:
        return None
    return replace(identity, tenant=tenant)
