# FileEngine MCP Server

A **Model Context Protocol (MCP)** server that exposes the FileEngine immutable,
versioned filesystem to AI agents — a storage service that can **always be
restored to any prior state regardless of agent mistakes**. Authentication and
authorization go through **LDAP** (the gRPC core enforces ACLs).

See **[`DESIGN.md`](./DESIGN.md)** for the full design and roadmap.

## Status — Phase 1 (full read surface)

Stdio MCP server with LDAP-resolved identity and the complete read tool set plus
browsable, version-aware resources.

**Tools (8, all read):** `list_directory`, `read_file`, `stat`, `exists`,
`list_versions`, `read_version` (time-travel), `get_metadata`, `check_permission`.

**Resources:**
- `fileengine://{tenant}/{uid}` — current file content
- `fileengine://{tenant}/{uid}/versions` — the immutable version history
- `fileengine://{tenant}/{uid}/versions/{version}` — content at a past version

Reuses `python_interface`'s `ManagedFiles`. Version-culling and hard delete are
**never** exposed (the recoverability guarantee); append-only write tools arrive
in Phase 2.

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
| `MCP_READ_ONLY` | expose only read tools (default off in later phases) |

## Test

Requires a live LDAP + FileEngine core (skips otherwise):

```bash
PYTHONPATH=src:../python_interface python -m pytest tests -v
```
