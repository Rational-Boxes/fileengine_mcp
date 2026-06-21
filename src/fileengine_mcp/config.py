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

        # LDAP — the authentication and role authority (mirrors the bridges)
        self.ldap_uri = _env("FILEENGINE_LDAP_ENDPOINT", "ldap://localhost:1389")
        self.ldap_domain = _env("FILEENGINE_LDAP_DOMAIN", "dc=rationalboxes,dc=com")
        self.ldap_user_base = _env("FILEENGINE_LDAP_USER_BASE", "ou=users,dc=rationalboxes,dc=com")
        self.ldap_tenant_base = _env("FILEENGINE_LDAP_TENANT_BASE", "ou=tenants,dc=rationalboxes,dc=com")
        self.ldap_bind_dn = _env("FILEENGINE_LDAP_BIND_DN", "cn=admin,dc=rationalboxes,dc=com")
        self.ldap_bind_password = _env("FILEENGINE_LDAP_BIND_PASSWORD", "admin")

        # The LDAP identity the MCP server authenticates as (the agent's account)
        self.agent_user = _env("FILEENGINE_MCP_USER", _env("FILEENGINE_LDAP_USER", ""))
        self.agent_password = _env("FILEENGINE_MCP_PASSWORD", _env("FILEENGINE_LDAP_PASSWORD", ""))
