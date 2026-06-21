# FileEngine MCP Server

A **Model Context Protocol (MCP)** server that exposes the FileEngine immutable,
versioned filesystem to AI agents — a storage service that can **always be
restored to any prior state regardless of agent mistakes**. Authentication and
authorization go through **LDAP** (the gRPC core enforces ACLs).

See **[`DESIGN.md`](./DESIGN.md)** for the full design and roadmap.

## Status — Phase 3 (Streamable HTTP transport)

MCP server over **stdio** *and* **Streamable HTTP**, with LDAP-resolved identity,
the full read surface, browsable version-aware resources, and **append-only**
write tools.

**Read tools (8, always on):** `list_directory`, `read_file`, `stat`, `exists`,
`list_versions`, `read_version` (time-travel), `get_metadata`, `check_permission`.

**Write tools (9, append-only; hidden when `MCP_READ_ONLY=1`):**
`create_directory`, `create_file`, `write_file`, `set_metadata`,
`delete_metadata`, `rename`, `move`, `copy`, `restore_version`. Every
`write_file`/`restore_version` *appends* a version — prior bytes always remain
readable via `read_version`.

**Soft delete (2, off unless `MCP_ALLOW_DELETE=1`):** `soft_delete`, `undelete`
— reversible hide; the entity and its full history persist.

**Resources:**
- `fileengine://{tenant}/{uid}` — current file content
- `fileengine://{tenant}/{uid}/versions` — the immutable version history
- `fileengine://{tenant}/{uid}/versions/{version}` — content at a past version

Reuses `python_interface`'s `ManagedFiles`. **Version culling
(`PurgeOldVersions`) and hard delete are never exposed under any flag or role** —
the recoverability guarantee. Next: Phase 4 hardening (audit log, size/result
caps, subtree allow-list, confirmation hints).

## Transports

| Transport | Entry point | Identity | Tenancy |
|---|---|---|---|
| **stdio** | `fileengine-mcp` | one LDAP identity per process (`FILEENGINE_MCP_USER/_PASSWORD`) | `FILEENGINE_MCP_TENANT` |
| **Streamable HTTP** | `fileengine-mcp-http` | **per request** — `Authorization: Basic <user:pass>` (LDAP bind) or `Bearer <token>` | **per session** — `X-Tenant` header or subdomain |

The HTTP transport exposes the MCP endpoint at `/mcp`, plus `POST /auth/token`
(exchange Basic credentials for a bearer token, one bind cached for
`MCP_TOKEN_TTL`s) and `GET /whoami` (the caller's resolved `{user, roles,
tenant}`). All endpoints except `/auth/token` require authentication; run it
behind TLS (a reverse proxy) for remote/multi-agent use. Identity is always
LDAP-derived and forwarded to the core, which remains the ACL enforcement point.

```bash
# remote: get a token, then connect an MCP client to http://host:8089/mcp
curl -sX POST http://host:8089/auth/token \
     -d '{"username":"agent","password":"…"}' -H 'content-type: application/json'
# -> {"access_token":"…","token_type":"bearer","expires_in":3600}
```

## Install & run

```bash
pip install -e .                      # or: pip install mcp ldap3
pip install ../python_interface       # the reused FileEngine client (or rely on the sibling-checkout bootstrap)

cp .env-default .env                  # set FILEENGINE_MCP_USER/_PASSWORD (LDAP) + core/LDAP endpoints
fileengine-mcp                        # stdio server  (or: python -m fileengine_mcp.server)
fileengine-mcp-http                   # Streamable HTTP server (remote/multi-agent; see Transports)
```

Configure an MCP host (e.g. Claude Desktop) to launch `fileengine-mcp` over
stdio with the LDAP credentials in its environment.

### Configuration (env / `.env`)

| Variable | Meaning |
|---|---|
| `FILEENGINE_GRPC_HOST` / `_PORT` | FileEngine core (default `localhost:50051`) |
| `FILEENGINE_MCP_USER` / `_PASSWORD` | the agent's LDAP credentials |
| `FILEENGINE_MCP_TENANT` | tenant for this process (default `default`) |
| `FILEENGINE_LDAP_*` | LDAP endpoint / domain / bind / bases |
| `MCP_READ_ONLY` | `1` hides all write tools (read/browse only); default `0` |
| `MCP_ALLOW_DELETE` | `1` enables reversible `soft_delete`/`undelete`; default `0` |

## Test

Requires a live LDAP + FileEngine core (skips otherwise):

```bash
PYTHONPATH=src:../python_interface python -m pytest tests -v
```
