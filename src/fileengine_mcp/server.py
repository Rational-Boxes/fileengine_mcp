"""FileEngine MCP server.

A stdio Model Context Protocol server that authenticates the agent against LDAP,
connects to the FileEngine gRPC core as that identity, and exposes the
filesystem to AI agents.

The surface is built around the recoverability guarantee (see DESIGN.md):
- Read/browse tools and version-aware resources are always available.
- Write tools are **append-only** (every write adds a version; restore adds a
  version) and are hidden when ``MCP_READ_ONLY`` is set.
- Soft delete / undelete are reversible and gated behind ``MCP_ALLOW_DELETE``.
- Version culling (PurgeOldVersions) and hard delete are **never** exposed,
  under any flag or role."""
import base64
import sys

from mcp.server.fastmcp import FastMCP

from .config import Config, load_dotenv
from .ldap_auth import authenticate
from ._client import ManagedFiles

ROOT_ALIASES = {"root", "", "00000000-0000-0000-0000-000000000000"}


def _norm_uid(uid: str) -> str:
    """Map the root aliases to the core's empty-string root UID."""
    return "" if uid in ROOT_ALIASES else uid


def _content_to_text(data: bytes) -> str:
    """Return file bytes as UTF-8 text, or base64 (``[base64]`` prefix) if binary."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return "[base64] " + base64.b64encode(data).decode("ascii")


def _content_from_text(content: str, as_: str) -> bytes:
    """Encode tool input into bytes for a write. ``as_`` is ``text`` or ``base64``."""
    if as_ == "base64":
        return base64.b64decode(content)
    if as_ == "text":
        return content.encode("utf-8")
    raise ValueError(f"unknown content encoding '{as_}' (use 'text' or 'base64')")


def _read_version_bytes(uid: str, version: str) -> bytes:
    """Read the content of a specific version (by timestamp) of a file."""
    revs = mf.revisions(_norm_uid(uid))
    versions = [r.version for r in revs]
    if version not in versions:
        raise ValueError(f"version '{version}' not found for '{uid}'")
    buf = mf.get(_norm_uid(uid), back=versions.index(version))
    if buf is False:
        raise ValueError(f"could not read version '{version}' of '{uid}'")
    return buf.getvalue()


# --- build the server (auth + gRPC connection happen once at startup) ---
load_dotenv()
config = Config()
identity = authenticate(config, config.agent_user, config.agent_password)
if not identity.authenticated:
    raise SystemExit(
        f"LDAP authentication failed for MCP agent '{config.agent_user or '(unset)'}'. "
        "Set FILEENGINE_MCP_USER / FILEENGINE_MCP_PASSWORD."
    )

mf = ManagedFiles(
    user_name=identity.user,
    user_roles=identity.roles,
    server_address=config.grpc_address,
    tenant=identity.tenant,
)

server = FastMCP("fileengine")


@server.tool()
def list_directory(uid: str = "root", show_deleted: bool = False) -> list[dict]:
    """List the contents of a directory by UID.

    Use ``root`` (or the all-zeros UUID) for the filesystem root. Set
    ``show_deleted`` to include soft-deleted entries (useful before ``undelete``).
    Returns each entry's uid, name, type (file|directory), size, and
    version_count."""
    entries = mf.dir(_norm_uid(uid), show_deleted=show_deleted)
    if entries is False:
        raise ValueError(f"could not list directory '{uid}'")
    return [
        {
            "uid": e.uid,
            "name": e.name,
            "type": "directory" if e.is_container else "file",
            "size": e.size,
            "version_count": e.version_count,
        }
        for e in entries
    ]


@server.tool()
def read_file(uid: str) -> str:
    """Read the current content of a file by UID, returned as UTF-8 text.

    Binary content that is not valid UTF-8 is returned base64-encoded with a
    ``[base64]`` prefix."""
    buf = mf.get(_norm_uid(uid))
    if buf is False:
        raise ValueError(f"could not read file '{uid}'")
    return _content_to_text(buf.getvalue())


@server.tool()
def stat(uid: str) -> dict:
    """Get metadata for a file or directory: type, size, owner, parent, and the
    current version timestamp."""
    info = mf.stat(_norm_uid(uid))
    if info is None:
        raise ValueError(f"could not stat '{uid}'")
    return {
        "uid": info.uid,
        "name": info.name,
        "parent_uid": info.parent_uid,
        "type": "directory" if info.is_dir else "file",
        "size": info.size,
        "owner": info.owner,
        "version": info.version,
    }


@server.tool()
def exists(uid: str) -> bool:
    """Return whether a file or directory exists."""
    return bool(mf.entity_exists(_norm_uid(uid)))


@server.tool()
def list_versions(uid: str) -> list[str]:
    """List the version timestamps of a file, newest first.

    This is the file's immutable history; every write appends a version and no
    version is ever removed through this server."""
    return [r.version for r in mf.revisions(_norm_uid(uid))]


@server.tool()
def read_version(uid: str, version: str) -> str:
    """Time-travel read: return a file's content at a specific version timestamp
    (from list_versions), as UTF-8 text (base64 fallback)."""
    return _content_to_text(_read_version_bytes(uid, version))


@server.tool()
def get_metadata(uid: str, key: str | None = None) -> dict:
    """Get metadata for a file. With a key, returns ``{key: value}``; without,
    returns all metadata as a map."""
    if key:
        value = mf.get_metadata_value(_norm_uid(uid), key)
        return {key: value}
    return mf.get_metadata_values(_norm_uid(uid))


@server.tool()
def check_permission(uid: str, permission: str, principal: str | None = None) -> bool:
    """Check whether a principal has a permission on a resource. ``permission``
    is a letter (r/w/x/d/...) or name (READ/WRITE/...); ``principal`` defaults to
    the calling agent."""
    return bool(mf.check_permission(_norm_uid(uid), permission, user=principal))


# --- resources: browsable files + their immutable version history ---
@server.resource("fileengine://{tenant}/{uid}")
def file_resource(tenant: str, uid: str) -> str:
    """Current content of a file as a readable resource."""
    buf = mf.get(_norm_uid(uid))
    if buf is False:
        raise ValueError(f"could not read file '{uid}'")
    return _content_to_text(buf.getvalue())


@server.resource("fileengine://{tenant}/{uid}/versions")
def versions_resource(tenant: str, uid: str) -> str:
    """The file's immutable version history (newest-first timestamps), as JSON."""
    import json
    return json.dumps([r.version for r in mf.revisions(_norm_uid(uid))])


@server.resource("fileengine://{tenant}/{uid}/versions/{version}")
def version_resource(tenant: str, uid: str, version: str) -> str:
    """Content of a specific historical version of a file (time travel)."""
    return _content_to_text(_read_version_bytes(uid, version))


# --- write tools: append-only. Defined unconditionally so they are importable
#     and unit-testable; only *registered* on the agent surface when writes are
#     enabled (hidden entirely in MCP_READ_ONLY mode). -----------------------
def create_directory(parent_uid: str, name: str) -> str:
    """Create a new directory under a parent and return its UID."""
    uid = mf.mkdir(_norm_uid(parent_uid), name)
    if uid is False:
        raise ValueError(f"could not create directory '{name}' under '{parent_uid}'")
    return uid


def create_file(parent_uid: str, name: str) -> str:
    """Create a new (empty) file under a parent and return its UID.

    Write content with ``write_file``; that appends the first version."""
    uid = mf.touch(_norm_uid(parent_uid), name)
    if uid is False:
        raise ValueError(f"could not create file '{name}' under '{parent_uid}'")
    return uid


def write_file(uid: str, content: str, as_: str = "text") -> dict:
    """Write file content. This is **append-only**: it adds a new version and
    never overwrites or erases prior versions (recoverable via list_versions /
    read_version / restore_version). ``as_`` is ``text`` or ``base64``."""
    before = len(mf.revisions(_norm_uid(uid)))
    ok = mf.put(_norm_uid(uid), _content_from_text(content, as_))
    if ok is False:
        raise ValueError(f"could not write file '{uid}'")
    versions = [r.version for r in mf.revisions(_norm_uid(uid))]
    return {"uid": uid, "versions_before": before, "versions_after": len(versions),
            "current_version": versions[0] if versions else None}


def set_metadata(uid: str, key: str, value: str) -> bool:
    """Set a metadata key/value on a file or directory."""
    return bool(mf.set_metadata_value(_norm_uid(uid), key, value))


def delete_metadata(uid: str, key: str) -> bool:
    """Remove a metadata key. Metadata only — does not touch file content or
    its version history."""
    return bool(mf.delete_metadata_value(_norm_uid(uid), key))


def rename(uid: str, new_name: str) -> bool:
    """Rename a file or directory in place."""
    return bool(mf.rename(_norm_uid(uid), new_name))


def move(uid: str, destination_parent_uid: str) -> bool:
    """Move a file or directory under a new parent directory."""
    return bool(mf.move(_norm_uid(uid), _norm_uid(destination_parent_uid)))


def copy(uid: str, destination_parent_uid: str) -> bool:
    """Copy a file or directory into a destination directory."""
    return bool(mf.copy(_norm_uid(uid), _norm_uid(destination_parent_uid)))


def restore_version(uid: str, version: str) -> dict:
    """Restore a file to a prior version (from list_versions). This is
    **append-only**: it adds a new version equal to the chosen one; nothing is
    overwritten or lost, so it is always itself reversible."""
    restored = mf.restore_to_version(_norm_uid(uid), version)
    if restored is False:
        raise ValueError(f"could not restore '{uid}' to version '{version}'")
    return {"uid": uid, "restored_from": version, "new_version": restored}


# Soft delete / undelete — reversible, but gated behind MCP_ALLOW_DELETE.
def soft_delete(uid: str) -> bool:
    """Soft-delete (hide) a file or directory. Reversible with ``undelete``:
    the entity and its full version history persist. No hard delete and no
    version culling is ever performed."""
    return bool(mf.remove(_norm_uid(uid)))


def undelete(uid: str) -> bool:
    """Restore a soft-deleted file (pairs with ``soft_delete``)."""
    return bool(mf.undelete_file(_norm_uid(uid)))


_WRITE_TOOLS = (create_directory, create_file, write_file, set_metadata,
                delete_metadata, rename, move, copy, restore_version)
_DELETE_TOOLS = (soft_delete, undelete)

if not config.read_only:
    for _fn in _WRITE_TOOLS:
        server.add_tool(_fn)
    if config.allow_delete:
        for _fn in _DELETE_TOOLS:
            server.add_tool(_fn)


def main() -> None:
    """Console entry point — runs the MCP server over stdio."""
    print(
        f"FileEngine MCP server: agent='{identity.user}' tenant='{identity.tenant}' "
        f"roles={identity.roles} core={config.grpc_address} "
        f"read_only={config.read_only} allow_delete={config.allow_delete}",
        file=sys.stderr,
    )
    server.run()


if __name__ == "__main__":
    main()
