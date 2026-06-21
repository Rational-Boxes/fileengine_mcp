# FileEngine MCP Server — Design Document

**Status:** Draft · 2026-06-21
**Component:** `mcp/` — a Model Context Protocol (MCP) server exposing the
FileEngine **immutable, versioned filesystem** as tools and resources for AI
agents.

> Note: the protocol is the **Model Context Protocol** (MCP) — Anthropic's open
> standard for connecting LLM agents to tools and data. This server is an MCP
> *server*; agents (Claude Desktop, IDE assistants, custom agent runtimes) are
> the MCP *clients*.

---

## 1. Goal

Give AI agents safe, governed access to the FileEngine virtual filesystem —
browsing, reading (including any historical version), writing, and organizing
files — as a **storage service that can always be restored to any prior state,
regardless of any mistake an agent makes.** Recoverability is the product, not a
feature: no sequence of agent tool calls can destroy data or leave the
filesystem in an unrecoverable state.

### The recoverability guarantee (the core invariant)

FileEngine is append-only by design: every `write` creates a new version and old
versions are retained. This server is built so that **every mutating action an
agent can take is reversible or recoverable**, and the history is **immutable
from the agent's side**:

| If an agent does… | Recovery path | Why it's always recoverable |
|---|---|---|
| `write_file` (bad content) | `read_version` / `restore_version` to a prior timestamp | a new version is appended; the previous bytes remain |
| `restore_version` | `restore_version` to any other version | restore is itself append-only — it adds a version, never overwrites |
| `soft_delete` | `undelete` | delete only hides; the entity and its versions persist |
| `rename` | `rename` back | name change only; reversible |
| `move` | `move` back | parent change only; reversible |
| `set_metadata` / `delete_metadata` | re-set from a prior versioned value | metadata is versioned; content untouched |

This is enforced **structurally**, not by permission checks:

- **`PurgeOldVersions` (version culling) is never exposed** — no tool, resource,
  or parameter prunes/compacts versions, for *any* agent identity including
  `system_admin`. The version history cannot be shortened through this server.
- **No hard delete is exposed** — the only delete is the core's *soft* delete
  (`RemoveFile`/`RemoveDirectory`), reversible via `undelete`, and even that is
  config-gated off by default (§6).
- **Writes and restores are append-only** — they extend history; they never
  overwrite or erase.

Net effect: an agent can freely read and evolve the filesystem, but a human (or
the agent itself) can always roll any file — or the whole tree — back to any
previous point in time. Agent mistakes are undo-able by construction.

---

## 2. Why MCP

Agents increasingly need durable, governed file storage that is more than a
scratch directory: shared across sessions, ACL-controlled, multi-tenant, and
**auditable with full history**. MCP is the standard way to hand an agent a
toolset + browsable resources with typed schemas, working across Claude Desktop,
IDE assistants, and agent frameworks. Exposing FileEngine over MCP gives agents:

- a real versioned filesystem (not ephemeral context),
- "time travel" reads over the immutable history,
- server-enforced permissions and tenancy,
- a tamper-evident record of what the agent did.

---

## 3. Architecture

```
AI agent (MCP client) ──MCP (stdio | Streamable HTTP)──▶ FileEngine MCP server
                                                            │  (Python, official `mcp` SDK / FastMCP)
                                                            │  reuses python_interface ManagedFiles
                                                            ▼
                                                       FileEngine gRPC FileService (fileengine_rpc, :50051)
```

- **Language/SDK:** Python with the official `mcp` SDK (FastMCP). It **reuses
  `python_interface`'s `ManagedFiles`** client (already migrated to
  `fileengine_rpc`, Pydantic-typed, integration-tested), so the MCP layer is
  thin: tool/resource definitions + identity + immutability gating.
- **Source of truth:** the gRPC core. The MCP server holds no state beyond a
  `ManagedFiles` connection (and optional caches). ACLs, versions, and metadata
  are enforced/stored server-side.
- **Reuse over rebuild:** mirrors how the WebDAV and HTTP bridges were built on
  the shared gRPC wrapper. No new protocol.

---

## 4. Authentication & authorization — LDAP is the authority

**LDAP is the single source of truth for authentication, roles, and tenancy**,
exactly as in the WebDAV and HTTP bridges. The MCP server never invents
identities and makes no local authorization decisions — it authenticates the
agent against the directory, derives the agent's roles/tenant from LDAP, and
forwards that identity to the gRPC core, which enforces the ACLs. This keeps one
governance model across CLI, WebDAV, HTTP, and MCP.

### The flow
1. The agent presents **credentials** (username + password — its own service
   account, or an end-user it acts for).
2. The MCP server performs a **real LDAP bind** with those credentials
   (authentication). A failed bind ⇒ the agent is not connected / the tool call
   is rejected.
3. **Roles** come from LDAP **group membership** (the agent's groups in the
   tenant), and a tenant's `administrators` group maps to the core's
   `system_admin` role — same rule as the bridges.
4. **Tenant** is derived from LDAP / the connection context (one tenant per
   session; see below).
5. The resulting `{user, roles, tenant}` is placed in the gRPC
   `AuthenticationContext`; the **FileEngine core is the permission enforcement
   point**, evaluating ACLs against the LDAP-derived roles.

> Implementation: reuse the `ldap_authenticator` logic from the bridges (bind +
> group→role extraction + `administrators→system_admin`) as a small Python
> module (`ldap3`/`python-ldap`), so the directory rules are identical.

### How credentials reach the server per transport
| Transport | Credential source → LDAP |
|---|---|
| **stdio** (local) | LDAP username/password supplied to the server process via env/secret (`FILEENGINE_LDAP_USER` / `_PASSWORD`) or a one-time MCP login tool; the server binds and caches the resolved identity for the session. One LDAP identity / tenant per process. |
| **Streamable HTTP** (remote) | HTTP **Basic → LDAP bind** per the HTTP bridge, or a **bearer token** issued by an LDAP-backed `/auth/token` (one bind, cached). Per-session identity; multi-tenant via the subdomain/`X-Tenant` convention. |

LDAP connection config mirrors the other services
(`FILEENGINE_LDAP_ENDPOINT/_DOMAIN/_BIND_DN/_BIND_PASSWORD/_TENANT_BASE/_USER_BASE`).

### Security posture (on top of LDAP)
- **Least privilege by default:** a **read-only mode** (`MCP_READ_ONLY=1`)
  exposes only read tools/resources; the agent's LDAP roles still gate
  everything server-side.
- **No version culling, ever** (§1) — independent of the agent's LDAP roles,
  even `system_admin`.
- **Audit log:** every tool call logged with `{ldap-user, session, tool, uid,
  tenant, result}`. Credentials/tokens are never logged.
- **Guardrails for agents:** per-call read/write size caps, a max-results cap on
  listings, and optional subtree (UID) allow-listing to sandbox an agent —
  layered *on top of* the LDAP/ACL decision, never replacing it.
- **Confirmation hints:** mutating tools (`write_file`, `move`, `soft_delete`)
  carry `destructiveHint`/`idempotentHint`; reads carry `readOnlyHint`, so MCP
  hosts can prompt the user.

---

## 5. Transport & deployment

- **stdio**: launched by the agent host (e.g. Claude Desktop config). Best for
  a single user/agent on one tenant.
- **Streamable HTTP** (with SSE): a long-running server for remote/multi-agent
  use, behind TLS (reverse proxy), with OAuth. Mirrors the HTTP bridge's
  deployment story.
- Packaged as a console entry point (`fileengine-mcp`) and a container image.

---

## 6. Tools

UID-native (like the HTTP bridge — the core has no path→UID RPC). A `find`/
`list` flow lets the agent discover UIDs; the root is `root` / the all-zeros
UUID. Each tool has a JSON-Schema input and structured output, with annotations.

### Read / browse (always available)
| Tool | Maps to | Notes |
|---|---|---|
| `list_directory(uid, show_deleted=false)` | `dir` | entries (uid, name, type, size, version_count) |
| `stat(uid)` | `stat` | type/size/owner/parent/current version |
| `exists(uid)` | `entity_exists` | |
| `read_file(uid, as="text"\|"base64")` | `get` | current content; text or base64 for binary |
| `list_versions(uid)` | `revisions` | the **immutable history** (timestamps) |
| `read_version(uid, version)` | `get(back=…)` / `get_version` | time-travel read |
| `get_metadata(uid, key=null)` | `get_metadata_value(s)` | one key or all |
| `check_permission(uid, permission, principal=null)` | `check_permission` | |

### Write — append-only (opt-in; hidden in read-only mode)
| Tool | Maps to | Immutability note |
|---|---|---|
| `create_directory(parent_uid, name)` | `mkdir` | |
| `create_file(parent_uid, name)` | `touch` | |
| `write_file(uid, content, as="text"\|"base64")` | `put` | **creates a new version**; prior versions retained |
| `set_metadata(uid, key, value)` | `set_metadata_value` | |
| `rename(uid, new_name)` | `rename` | |
| `move(uid, destination_parent_uid)` | `move` | |
| `copy(uid, destination_parent_uid)` | `copy` | |
| `restore_version(uid, version)` | `restore_to_version` | **append-only restore** — adds a new version |
| `delete_metadata(uid, key)` | `delete_metadata_value` | metadata only; not a version op |

### Optional, config-gated (default OFF)
| Tool | Maps to | Why gated |
|---|---|---|
| `soft_delete(uid)` | `remove` | reversible hide; off unless `MCP_ALLOW_DELETE=1` |
| `undelete(uid)` | `undelete_file` | pairs with soft_delete |

### Deliberately excluded
- **`purge_old_versions` / any version-culling** — not exposed under any flag or
  role. (§1)
- Role/ACL administration (`create_role`, `grant_permission`, …) and admin ops
  (`trigger_sync`) — out of scope for an agent-facing surface; manage those via
  the CLI / HTTP bridge.

---

## 7. Resources

Expose the filesystem as browsable MCP **resources** so agents can read without
tool round-trips, and so hosts can show a file tree:

- `fileengine://{tenant}/{uid}` → current file content (mime from metadata).
- `fileengine://{tenant}/{uid}/versions` → the version list (immutable history).
- `fileengine://{tenant}/{uid}/versions/{ts}` → content at a specific version.
- **Resource templates** for directory listing so the agent can expand the tree.
- Subscriptions (optional): notify on change so an agent can react to new
  versions.

Resources are **read-only by contract** — all mutation goes through tools, which
keeps the immutability/audit story crisp.

---

## 8. Prompts (optional, later)

A small set of MCP prompts to steer common tasks, e.g. "summarize the changes
between two versions of `{uid}`" (uses `read_version` ×2) or "review this
directory." Low priority; ship after tools/resources.

---

## 9. Implementation plan

| Phase | Deliverable | Validate |
|---|---|---|
| 0 | Scaffold `mcp/` (Python pkg, `mcp` SDK dep, reuse `python_interface`); stdio server; identity from env; `list_directory` + `read_file` | MCP Inspector lists tools; reads a file from the default tenant |
| 1 | Full **read** tool set + resources (`fileengine://…` incl. versions) | time-travel read of an old version via Inspector |
| 2 | **Append-only write** tools (`create_*`, `write_file`, `restore_version`, metadata, rename/move/copy); read-only-mode flag | write creates a new version; old version still readable; **assert no purge tool exists** |
| 3 | Streamable HTTP transport + OAuth → FileEngine identity (reuse LDAP/token); per-session tenancy | remote agent connects, scoped to its tenant |
| 4 | Hardening: audit log, size/result caps, subtree allow-list, confirmation hints; optional soft_delete gating | guardrail tests |
| 5 | Packaging (entry point + container), docs (README + tool reference), example agent config | example agent completes a read→write→time-travel task |

---

## 10. Testing

- **MCP Inspector** for interactive tool/resource validation.
- **Automated**: a pytest suite driving the server in-process (FastMCP test
  client) against a live core — mirrors the client/CLI E2E coverage:
  browse, read, append-version, time-travel read, metadata, ACL-denied → error.
- **Recoverability assertions** (must-pass — the product guarantee):
  - the tool catalog contains **no** version-culling tool and **no** hard-delete;
  - `write_file` strictly increases the `list_versions` count, and
    `read_version` of an old timestamp returns the original bytes after
    subsequent writes *and* a `restore`;
  - **"undo any mistake" scenarios** each recover the prior state via tools the
    agent has: clobbering a file → `restore_version`; `soft_delete` → `undelete`;
    `rename`/`move` → reverse; a chaotic multi-step "agent gone wrong" sequence →
    every file rolled back to a pre-run snapshot timestamp.
- **Agent eval (optional):** a scripted Claude agent performing a multi-step
  task (create → edit across versions → recover a prior version) to validate
  ergonomics, tool descriptions, and that recovery is discoverable to the agent.

---

## 11. Risks & open questions

1. **Identity for stdio agents.** Single configured principal per server is
   simplest but coarse. Is per-tool identity needed locally, or is one
   principal/tenant per server instance acceptable for v1? (Proposed: yes.)
2. **Path ergonomics.** UID-native is faithful to the core but awkward for
   agents that "think in paths." Do we add a best-effort path resolver
   (tree-walk from root) or lean on resources + `list_directory`? (Proposed:
   resources + list first; add path helper if evals show friction.)
3. **Binary vs text.** Default `read_file` to text with a base64 fallback;
   enforce a read size cap to protect the agent's context window.
4. **Write confirmation.** Should writes/moves require host-side user
   confirmation by default (via annotations), given agents act autonomously?
5. **Multi-tenant over stdio.** One tenant per process, or allow a tenant
   argument per call (with authorization)? (Proposed: one per process for
   stdio; per-session for HTTP.)
6. **Reuse vs independence.** Build on `python_interface` (fastest, shared
   maintenance) vs a standalone client. (Proposed: reuse.)

---

## 12. Summary

A thin Python MCP server over the existing `ManagedFiles` client that gives
agents a governed, versioned filesystem with first-class **time-travel reads**
and an **append-only, never-cullable** history, authenticated and authorized
**through LDAP** (bind + group→role, `administrators→system_admin`), with the
gRPC core as the ACL enforcement point.

The product is **recoverability**: the storage service can always be restored to
any prior state regardless of agent mistakes. This is guaranteed *structurally*
— by never exposing version culling or hard delete, and by making every mutating
tool append-only or reversible — so the guarantee holds independent of any
agent's permissions, including `system_admin`.
