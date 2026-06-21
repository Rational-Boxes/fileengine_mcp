# FileEngine MCP — Tool & Resource Reference

_Generated from the running server with `scripts/gen_tool_reference.py`._

Surface for this configuration: **19 tools**. The set depends on `MCP_READ_ONLY` / `MCP_ALLOW_DELETE`; version culling and hard delete are never present.

## Tools

| Tool | Params | Description |
|---|---|---|
| `check_permission` _(read-only)_ | `uid: string`, `permission: string`, `principal: string` *(opt)* | Check whether a principal has a permission on a resource. ``permission`` |
| `copy` | `uid: string`, `destination_parent_uid: string` | Copy a file or directory into a destination directory. |
| `create_directory` | `parent_uid: string`, `name: string` | Create a new directory under a parent and return its UID. |
| `create_file` | `parent_uid: string`, `name: string` | Create a new (empty) file under a parent and return its UID. |
| `delete_metadata` _(idempotent)_ | `uid: string`, `key: string` | Remove a metadata key. Metadata only — does not touch file content or |
| `exists` _(read-only)_ | `uid: string` | Return whether a file or directory exists. |
| `get_metadata` _(read-only)_ | `uid: string`, `key: string` *(opt)* | Get metadata for a file. With a key, returns ``{key: value}``; without, |
| `list_directory` _(read-only)_ | `uid: string` *(opt)*, `show_deleted: boolean` *(opt)* | List the contents of a directory by UID. |
| `list_versions` _(read-only)_ | `uid: string` | List the version timestamps of a file, newest first. |
| `move` _(idempotent)_ | `uid: string`, `destination_parent_uid: string` | Move a file or directory under a new parent directory. |
| `read_file` _(read-only)_ | `uid: string` | Read the current content of a file by UID, returned as UTF-8 text. |
| `read_version` _(read-only)_ | `uid: string`, `version: string` | Time-travel read: return a file's content at a specific version timestamp |
| `rename` _(idempotent)_ | `uid: string`, `new_name: string` | Rename a file or directory in place. |
| `restore_version` | `uid: string`, `version: string` | Restore a file to a prior version (from list_versions). This is |
| `set_metadata` _(idempotent)_ | `uid: string`, `key: string`, `value: string` | Set a metadata key/value on a file or directory. |
| `soft_delete` _(destructive, idempotent)_ | `uid: string` | Soft-delete (hide) a file or directory. Reversible with ``undelete``: |
| `stat` _(read-only)_ | `uid: string` | Get metadata for a file or directory: type, size, owner, parent, and the |
| `undelete` _(idempotent)_ | `uid: string` | Restore a soft-deleted file (pairs with ``soft_delete``). |
| `write_file` | `uid: string`, `content: string`, `as_: string` *(opt)* | Write file content. This is **append-only**: it adds a new version and |

## Resources

| URI template | Description |
|---|---|
| `fileengine://{tenant}/{uid}` | Current content of a file as a readable resource. |
| `fileengine://{tenant}/{uid}/versions` | The file's immutable version history (newest-first timestamps), as JSON. |
| `fileengine://{tenant}/{uid}/versions/{version}` | Content of a specific historical version of a file (time travel). |

## Never exposed (by design)

- **`purge_old_versions` / any version culling** — under no flag or role.
- **Hard delete** — the only delete is reversible `soft_delete` (gated).
- Role/ACL administration and `trigger_sync` — manage via the CLI / HTTP bridge.
