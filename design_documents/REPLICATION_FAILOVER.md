# fileengine-mcp — LDAP read-only failover

Status: **Implemented** on `feature/replica-failover`.

Part of the workspace-wide replica-failover feature (see the matching branches in
`convert_search_ai`, `file_engine_core`, `http_bridge`, `webdav_bridge`). The MCP
server only touches **LDAP** (it authenticates users and resolves roles, then
forwards the identity to the gRPC core); it has no database of its own.

## Topology

```
        auth (read-only bind + group search)        replication (syncrepl)
clients ───────────────▶  MASTER directory (cloud)  ──────────────▶  REPLICA (on-prem, localhost)
                                                                      read-only standby
```

When the master directory is unreachable, authentication fails over to the on-prem
replica so logins keep working in read-only fallback. LDAP auth is inherently
read-only (bind + search), so there is nothing to gate — only the endpoint fails over.

## Behavior

- **Failover engages only when a replica is configured.** With one directory,
  behavior is unchanged (fully backward compatible).
- **Lazy circuit-breaker** (no background threads): a connection-level failure on
  the master (service bind unreachable) trips the breaker for a cooldown; during the
  cooldown the replica is used; after it the master is re-probed and resumes on
  success. A credential rejection is *not* a failover trigger (the replica would
  reject too).

## Configuration (`config.py`)

| Env var | Default | Meaning |
|---------|---------|---------|
| `FILEENGINE_LDAP_ENDPOINT_REPLICA` | _(unset)_ | Replica directory URI. **Setting it enables failover.** |
| `FILEENGINE_LDAP_REPLICA_ENABLED` | `false` | Alt enable switch; when true and no URI given, defaults to `ldap://localhost:1389`. |
| `FILEENGINE_FAILOVER_COOLDOWN_S` | `30` | Circuit-breaker cooldown before re-probing the master. |

## Mechanism

- `failover.py` — `CircuitBreaker` (clock-injectable).
- `ldap_auth.authenticate()` — tries the master directory first (breaker-gated); on
  a connection-level `LDAPException` (`_ServerUnreachable`) it trips the breaker and
  retries the replica. `_authenticate_against(uri, …)` runs the bind + role lookup
  against one directory.

## Testing

`CircuitBreaker` transitions; config defaults; `authenticate` uses master when up,
fails over to the replica when the master is unreachable, uses replica-only while
degraded, master-only when no replica, and recovers after the cooldown.
