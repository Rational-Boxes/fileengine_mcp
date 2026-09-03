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

"""Configuration for the FileEngine MCP server, read from the environment.

A ``.env`` file in the working directory is loaded automatically (without
overriding values already set in the environment)."""
import os


def load_dotenv(path: str = ".env") -> None:
    if not os.path.isfile(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            os.environ.setdefault(key.strip(), val.strip())


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _bool(key: str, default: bool = False) -> bool:
    v = os.environ.get(key)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


class Config:
    def __init__(self) -> None:
        # gRPC core
        self.grpc_host = _env("FILEENGINE_GRPC_HOST", "localhost")
        self.grpc_port = _env("FILEENGINE_GRPC_PORT", "50051")
        self.grpc_address = f"{self.grpc_host}:{self.grpc_port}"

        # Tenant (one per stdio process) and mode
        self.tenant = _env("FILEENGINE_MCP_TENANT", "default")
        self.read_only = _env("MCP_READ_ONLY", "0").lower() in ("1", "true", "yes")
        # Soft delete / undelete are append-only-safe (reversible) but still gated.
        self.allow_delete = _env("MCP_ALLOW_DELETE", "0").lower() in ("1", "true", "yes")

        # Streamable HTTP transport (remote/multi-agent; run behind TLS).
        self.http_host = _env("MCP_HTTP_HOST", "127.0.0.1")
        self.http_port = int(_env("MCP_HTTP_PORT", "8089"))
        self.token_ttl = int(_env("MCP_TOKEN_TTL", "3600"))
        # Hosts/origins the Streamable HTTP transport will accept (DNS-rebinding
        # protection). Loopback is always allowed; add the public host when running
        # behind a reverse proxy or tunnel (e.g. MCP_ALLOWED_HOSTS=mcp.example.com),
        # otherwise external requests are rejected with 421 "Invalid Host header".
        self.allowed_hosts = [h.strip() for h in _env("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
        self.allowed_origins = [o.strip() for o in _env("MCP_ALLOWED_ORIGINS", "").split(",") if o.strip()]

        # Hardening / guardrails — layered on top of the LDAP/ACL decision, never
        # replacing it. Caps are per call; the allow-list sandboxes an agent to a
        # subtree (empty = unrestricted). Audit goes to a file or stderr.
        self.max_read_bytes = int(_env("MCP_MAX_READ_BYTES", str(10 * 1024 * 1024)))
        self.max_write_bytes = int(_env("MCP_MAX_WRITE_BYTES", str(10 * 1024 * 1024)))
        self.max_results = int(_env("MCP_MAX_RESULTS", "1000"))
        self.subtree_allowlist = [
            u.strip() for u in _env("MCP_SUBTREE_ALLOWLIST", "").split(",") if u.strip()
        ]
        self.audit_log_file = _env("MCP_AUDIT_LOG_FILE", "")

        # LDAP — the authentication and role authority (mirrors the bridges)
        self.ldap_uri = _env("FILEENGINE_LDAP_ENDPOINT", "ldap://localhost:1389")
        self.ldap_domain = _env("FILEENGINE_LDAP_DOMAIN", "dc=rationalboxes,dc=com")
        self.ldap_user_base = _env("FILEENGINE_LDAP_USER_BASE", "ou=users,dc=rationalboxes,dc=com")
        self.ldap_tenant_base = _env("FILEENGINE_LDAP_TENANT_BASE", "ou=tenants,dc=rationalboxes,dc=com")
        self.ldap_bind_dn = _env("FILEENGINE_LDAP_BIND_DN", "cn=admin,dc=rationalboxes,dc=com")
        self.ldap_bind_password = _env("FILEENGINE_LDAP_BIND_PASSWORD", "admin")
        # Read-only replica directory for disconnect fault tolerance
        # (REPLICATION_FAILOVER.md). When the master directory is unreachable, auth
        # fails over to this replica (auth is read-only). OFF unless configured; the
        # replica defaults to ldap://localhost:1389 when *_REPLICA_ENABLED is set.
        self.ldap_uri_replica = _env("FILEENGINE_LDAP_ENDPOINT_REPLICA", "")
        if not self.ldap_uri_replica and _bool("FILEENGINE_LDAP_REPLICA_ENABLED", False):
            self.ldap_uri_replica = "ldap://localhost:1389"
        self.ldap_replica_enabled = bool(self.ldap_uri_replica)
        self.failover_cooldown_s = int(_env("FILEENGINE_FAILOVER_COOLDOWN_S", "30"))

        # The LDAP identity the MCP server authenticates as (the agent's account)
        self.agent_user = _env("FILEENGINE_MCP_USER", _env("FILEENGINE_LDAP_USER", ""))
        self.agent_password = _env("FILEENGINE_MCP_PASSWORD", _env("FILEENGINE_LDAP_PASSWORD", ""))

        # Service-credential (key:secret) auth (PROPOSAL §16) — the ONLY credential
        # on both MCP transports (no legacy directory-password path). HTTP verifies a
        # Basic key:secret at /auth/token; stdio verifies FILEENGINE_MCP_KEY/SECRET
        # once at startup. Both call ldap_manager's internal verify endpoint; roles
        # come from LDAP (ldap_auth.resolve_roles).
        self.ldap_manager_url = _env("LDAP_MANAGER_URL", "")
        self.service_cred_internal_secret = _env(
            "SERVICE_CRED_INTERNAL_SECRET", _env("MFA_INTERNAL_SECRET", ""))
        self.verify_cache_ttl = int(_env("SERVICE_CRED_VERIFY_CACHE_TTL", "60"))
        self.mcp_key = _env("FILEENGINE_MCP_KEY", "")
        self.mcp_secret = _env("FILEENGINE_MCP_SECRET", "")

        # convert_search_ai, for `read_text`. The extracted Markdown of a document
        # lives in CSAI's database, not in the store, so it cannot be fetched over
        # the gRPC path every other tool uses. Reached in-cluster by container
        # name over its internal API, which authenticates with this shared secret
        # and takes the caller's identity as an assertion — CSAI still runs the
        # READ check against the core for that principal. Either value empty and
        # the tool reports that extraction is not wired rather than failing oddly.
        self.csai_url = _env("CSAI_URL", "")
        self.csai_internal_secret = _env(
            "CSAI_INTERNAL_SECRET", _env("SERVICE_CRED_INTERNAL_SECRET", ""))
        self.csai_timeout = float(_env("CSAI_TIMEOUT", "30"))

        # Base URL that `file_link` builds deep-links on, for the exceptional
        # deployment where the request's Host is NOT the public FQDN (MCP reached
        # directly, or behind a proxy that rewrites Host) — and for stdio, which
        # has no request at all. A ``{tenant}`` placeholder is substituted with
        # the caller's tenant, so one setting still gives each tenant its own
        # host. Empty means "use the origin the request arrived on", which is
        # right for every deployment behind the normal edge.
        self.public_app_url = _env("MCP_PUBLIC_APP_URL", "")
