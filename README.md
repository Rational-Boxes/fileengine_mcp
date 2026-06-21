# FileEngine MCP Server

A **Model Context Protocol (MCP)** server that exposes the FileEngine immutable,
versioned filesystem to AI agents — a storage service that can **always be
restored to any prior state regardless of agent mistakes**. Authentication and
authorization go through **LDAP** (the gRPC core enforces ACLs).

See **[`DESIGN.md`](./DESIGN.md)** for the full design and roadmap.

## Status — Phase 0 (scaffold)

Stdio MCP server with LDAP-resolved identity and the first read tools:

- `list_directory(uid="root")` — directory entries
- `read_file(uid)` — current file content (text; base64 fallback)

Reuses `python_interface`'s `ManagedFiles` client. Version-culling and hard
delete are **never** exposed (the immutability guarantee); write tools arrive in
Phase 2.

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
