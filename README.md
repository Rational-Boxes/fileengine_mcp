# FileEngine MCP Server

A **Model Context Protocol (MCP)** server that exposes the FileEngine immutable,
versioned filesystem to AI agents — a storage service that can **always be
restored to any prior state regardless of agent mistakes**. Authentication and
authorization go through **LDAP** (the gRPC core enforces ACLs).

See **[`DESIGN.md`](./DESIGN.md)** for the full design and roadmap.

## Status — Phase 2 (append-only writes + mode gating)

Stdio MCP server with LDAP-resolved identity, the full read surface, browsable
version-aware resources, and **append-only** write tools.

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
the recoverability guarantee. Streamable-HTTP transport + OAuth/LDAP arrive in
Phase 3.

## Install & run

```bash
pip install -e .                      # or: pip install mcp ldap3
pip install ../python_interface       # the reused FileEngine client (or rely on the sibling-checkout bootstrap)

cp .env-default .env                  # set FILEENGINE_MCP_USER/_PASSWORD (LDAP) + core/LDAP endpoints
fileengine-mcp                        # stdio server  (or: python -m fileengine_mcp.server)
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
