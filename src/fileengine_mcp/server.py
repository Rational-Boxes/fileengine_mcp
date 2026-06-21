"""FileEngine MCP server (Phase 0).

A stdio Model Context Protocol server that authenticates the agent against LDAP,
connects to the FileEngine gRPC core as that identity, and exposes read tools.
Mutating/version-culling tools are intentionally absent in Phase 0; the
immutability/recoverability guarantees are designed in DESIGN.md."""
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
def list_directory(uid: str = "root") -> list[dict]:
    """List the contents of a directory by UID.

    Use ``root`` (or the all-zeros UUID) for the filesystem root. Returns each
    entry's uid, name, type (file|directory), size, and version_count."""
    entries = mf.dir(_norm_uid(uid))
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


def main() -> None:
    """Console entry point — runs the MCP server over stdio."""
    print(
        f"FileEngine MCP server: agent='{identity.user}' tenant='{identity.tenant}' "
        f"roles={identity.roles} core={config.grpc_address} read_only={config.read_only}",
        file=sys.stderr,
    )
    server.run()


if __name__ == "__main__":
    main()
