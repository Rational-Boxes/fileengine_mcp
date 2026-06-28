# FileEngine MCP Server

> ⚠️ **Active development — not production-ready.** This project is under active development and should **not** be considered safe for mission-critical use.

A **Model Context Protocol (MCP)** server that exposes the FileEngine immutable,
versioned filesystem to AI agents — a storage service that can **always be
restored to any prior state regardless of agent mistakes**. Authentication and
authorization go through **LDAP** (the gRPC core enforces ACLs).

See **[`DESIGN.md`](./DESIGN.md)** for the full design and roadmap.

## Status — Phase 5 (packaging + docs) · feature-complete

MCP server over **stdio** *and* **Streamable HTTP**, with LDAP-resolved identity,
the full read surface, browsable version-aware resources, **append-only** write
tools, a guardrail/audit hardening layer, a container image, a generated
[tool reference](./TOOLS.md), and a runnable [example agent](./examples/) — all
six design phases complete. The product guarantee — *a storage service that can
always be restored regardless of any AI mistake* — is enforced structurally and
proven end-to-end in the test suite.

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
the recoverability guarantee.

See the full [tool & resource reference](./TOOLS.md) (regenerate with
`python scripts/gen_tool_reference.py`) and [`examples/`](./examples/) for MCP
host configs and a runnable demo.

## Container image

The server reuses the sibling `python_interface` client, so build with the
**parent** directory as context:

```bash
docker build -f mcp/Dockerfile -t fileengine-mcp ..
docker run --rm -p 8089:8089 --env-file mcp/.env fileengine-mcp   # Streamable HTTP
```

## Hardening & guardrails

Layered *on top of* the LDAP/ACL decision in the core (they sandbox an agent;
they never grant access the core would deny):

- **Audit log** — every tool call emits one JSON line `{ts, user, session,
  tenant, tool, uid, result}` (`ok`/`denied`/`error`) to `MCP_AUDIT_LOG_FILE`
  or stderr. Content bytes, passwords, and tokens are never logged.
- **Size caps** — `MCP_MAX_READ_BYTES` / `MCP_MAX_WRITE_BYTES` reject oversized
  reads/writes (a rejected write never reaches the core, so no version is added).
- **Result cap** — `MCP_MAX_RESULTS` truncates long listings (with a notice row).
- **Subtree allow-list** — `MCP_SUBTREE_ALLOWLIST` (UIDs) sandboxes an agent to a
  subtree; operations outside it are denied and audited.
- **Confirmation hints** — tools carry `readOnlyHint` (reads) /
  `destructiveHint` / `idempotentHint` so MCP hosts can prompt before mutations.

Because every write is append-only and the soft-delete is reversible, **any
agent mistake is recoverable** with the tools the agent already has — a chaotic
write→rename→move→delete sequence rolls back to a pre-run snapshot
(`restore_version` + reverse `rename`/`move` + `undelete`), verified in the test
suite.

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
| `MCP_HTTP_HOST` / `_PORT` | Streamable HTTP bind address (default `127.0.0.1:8089`) |
| `MCP_TOKEN_TTL` | bearer-token lifetime in seconds (default `3600`) |
| `MCP_MAX_READ_BYTES` / `_WRITE_BYTES` | per-call size caps (default 10 MiB; `0` disables) |
| `MCP_MAX_RESULTS` | max directory entries per listing (default `1000`) |
| `MCP_SUBTREE_ALLOWLIST` | comma-separated UIDs to sandbox an agent (empty = unrestricted) |
| `MCP_AUDIT_LOG_FILE` | audit log file path (empty = stderr) |

## Test

Requires a live LDAP + FileEngine core (skips otherwise):

```bash
PYTHONPATH=src:../python_interface python -m pytest tests -v
```

## License

Copyright (C) 2026 James Hickman <james@rationalboxes.com>

This project is licensed under the **GNU General Public License, version 3 (or
later)** — see the [LICENSE](LICENSE) file for the full text.
