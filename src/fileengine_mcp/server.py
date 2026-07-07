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
import functools
import inspect
import sys

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from . import audit
from .config import Config, load_dotenv
from .guards import (GuardError, cap_read_bytes, cap_results, cap_write_bytes,
                     within_allowlist)
from .ldap_auth import authenticate
from .session import get_session, get_session_mf
from ._client import ManagedFiles, NotFoundError

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
    revs = _mf().revisions(_norm_uid(uid))
    versions = [r.version for r in revs]
    if version not in versions:
        raise ValueError(f"version '{version}' not found for '{uid}'")
    buf = _mf().get(_norm_uid(uid), back=versions.index(version))
    data = buf.getvalue()
    cap_read_bytes(data, config.max_read_bytes)
    return data


# --- build the server -------------------------------------------------------
# Identity is transport-specific:
#   * stdio — ONE process identity, authenticated once here at startup, used for
#     every call (there is no per-request auth over stdio).
#   * HTTP  — identity comes from EACH request (Authorization: Basic → LDAP bind,
#     or Bearer token) and is bound per session; the process needs no identity.
# So the bootstrap authentication below is BEST-EFFORT: if it fails or no agent is
# configured, we start anyway with no process identity/client. stdio's main()
# refuses to serve without one; the HTTP transport does not need one.
load_dotenv()
config = Config()
identity = authenticate(config, config.agent_user, config.agent_password)
if identity.authenticated:
    mf = ManagedFiles(
        user_name=identity.user,
        user_roles=identity.roles,
        server_address=config.grpc_address,
        tenant=identity.tenant,
    )
else:
    print(
        f"MCP bootstrap agent '{config.agent_user or '(unset)'}' did not authenticate; "
        "starting with per-request identity only. This is expected for the HTTP "
        "transport (each request authenticates itself); stdio will refuse to serve.",
        file=sys.stderr,
    )
    identity = None
    mf = None

# DNS-rebinding protection for the HTTP transport. Loopback is always trusted;
# MCP_ALLOWED_HOSTS/_ORIGINS add the public host(s) when running behind a reverse
# proxy or tunnel so external requests aren't rejected with 421 "Invalid Host
# header". stdio ignores this entirely.
_transport_security = None
if config.allowed_hosts or config.allowed_origins:
    from mcp.server.transport_security import TransportSecuritySettings

    _transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*", *config.allowed_hosts],
        allowed_origins=config.allowed_origins,
    )

server = FastMCP("fileengine", transport_security=_transport_security)


def _mf():
    """The active gRPC client: the per-request HTTP identity if one is bound to
    this request's session, otherwise the process identity (stdio)."""
    active = get_session_mf() or mf
    if active is None:
        # HTTP requests always carry a session identity (the middleware 401s
        # otherwise); this only guards a stdio server started with no bootstrap
        # agent, which main() already refuses.
        raise GuardError("no identity bound to this request")
    return active


audit.configure(config.audit_log_file)

# UID-bearing parameter names — the targets a guardrail/audit cares about.
_UID_PARAMS = ("uid", "parent_uid", "destination_parent_uid")


def _active_identity():
    sess = get_session()
    return sess.identity if sess else identity


def _active_label() -> str:
    sess = get_session()
    return sess.label if sess else "stdio"


def _parent_of(uid: str):
    """Parent UID of an entity (for subtree containment), or None at the root
    or when the entity does not exist."""
    try:
        info = _mf().stat(_norm_uid(uid))
    except NotFoundError:
        return None
    parent = info.parent_uid
    return None if parent in ROOT_ALIASES else parent


def _check_subtree(uids) -> None:
    """Enforce the optional subtree allow-list against every target UID."""
    if not config.subtree_allowlist:
        return
    for uid in uids:
        if not within_allowlist(_norm_uid(uid), config.subtree_allowlist,
                                parent_of=_parent_of, root_uid=""):
            raise GuardError(f"'{uid}' is outside the allowed subtree")


def guarded(tool_name: str):
    """Wrap a tool with subtree enforcement + audit (ok|denied|error).

    ``functools.wraps`` keeps the original signature so FastMCP still derives the
    input schema from the wrapped function."""
    def deco(fn):
        sig = inspect.signature(fn)

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            bound = sig.bind(*args, **kwargs)
            bound.apply_defaults()
            targets = [str(bound.arguments[p]) for p in _UID_PARAMS if p in bound.arguments]
            uid = targets[0] if targets else ""
            ident = _active_identity()
            try:
                _check_subtree(targets)
                result = fn(*args, **kwargs)
            except GuardError as e:
                audit.record(tool=tool_name, uid=uid, result="denied", user=ident.user,
                             tenant=ident.tenant, session=_active_label(), reason=str(e))
                raise
            except Exception as e:
                audit.record(tool=tool_name, uid=uid, result="error", user=ident.user,
                             tenant=ident.tenant, session=_active_label(),
                             reason=type(e).__name__)
                raise
            audit.record(tool=tool_name, uid=uid, result="ok", user=ident.user,
                         tenant=ident.tenant, session=_active_label())
            return result

        return wrapper

    return deco


# Confirmation hints for MCP hosts. Reads are read-only; writes are mutating but
# (by design) recoverable, so none is truly "destructive" — only soft_delete is
# flagged so hosts prompt, even though it too is reversible via undelete.
_READ_HINT = ToolAnnotations(readOnlyHint=True)
_WRITE_HINTS = {
    "create_directory": ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False),
    "create_file": ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False),
    "write_file": ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False),
    "set_metadata": ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True),
    "delete_metadata": ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True),
    "rename": ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True),
    "move": ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True),
    "copy": ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False),
    "restore_version": ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False),
    "soft_delete": ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True),
    "undelete": ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True),
}


@server.tool(annotations=_READ_HINT)
@guarded("list_directory")
def list_directory(uid: str = "root", show_deleted: bool = False) -> list[dict]:
    """List the contents of a directory by UID.

    Use ``root`` (or the all-zeros UUID) for the filesystem root. Set
    ``show_deleted`` to include soft-deleted entries (useful before ``undelete``).
    Returns each entry's uid, name, type (file|directory), size, and
    version_count. Long listings are capped at ``MCP_MAX_RESULTS`` entries."""
    entries = _mf().dir(_norm_uid(uid), show_deleted=show_deleted)
    rows = [
        {
            "uid": e.uid,
            "name": e.name,
            "type": "directory" if e.is_container else "file",
            "size": e.size,
            "version_count": e.version_count,
        }
        for e in entries
    ]
    rows, truncated = cap_results(rows, config.max_results)
    if truncated:
        rows.append({"uid": "", "name": f"[truncated at MCP_MAX_RESULTS={config.max_results}]",
                     "type": "notice", "size": 0, "version_count": 0})
    return rows


@server.tool(annotations=_READ_HINT)
@guarded("read_file")
def read_file(uid: str) -> str:
    """Read the current content of a file by UID, returned as UTF-8 text.

    Binary content that is not valid UTF-8 is returned base64-encoded with a
    ``[base64]`` prefix. Content over ``MCP_MAX_READ_BYTES`` is rejected."""
    buf = _mf().get(_norm_uid(uid))
    data = buf.getvalue()
    cap_read_bytes(data, config.max_read_bytes)
    return _content_to_text(data)


@server.tool(annotations=_READ_HINT)
@guarded("stat")
def stat(uid: str) -> dict:
    """Get metadata for a file or directory: type, size, owner, parent, and the
    current version timestamp."""
    info = _mf().stat(_norm_uid(uid))
    return {
        "uid": info.uid,
        "name": info.name,
        "parent_uid": info.parent_uid,
        "type": "directory" if info.is_dir else "file",
        "size": info.size,
        "owner": info.owner,
        "version": info.version,
    }


@server.tool(annotations=_READ_HINT)
@guarded("exists")
def exists(uid: str) -> bool:
    """Return whether a file or directory exists."""
    return bool(_mf().entity_exists(_norm_uid(uid)))


@server.tool(annotations=_READ_HINT)
@guarded("list_versions")
def list_versions(uid: str) -> list[str]:
    """List the version timestamps of a file, newest first.

    This is the file's immutable history; every write appends a version and no
    version is ever removed through this server."""
    return [r.version for r in _mf().revisions(_norm_uid(uid))]


@server.tool(annotations=_READ_HINT)
@guarded("read_version")
def read_version(uid: str, version: str) -> str:
    """Time-travel read: return a file's content at a specific version timestamp
    (from list_versions), as UTF-8 text (base64 fallback)."""
    return _content_to_text(_read_version_bytes(uid, version))


@server.tool(annotations=_READ_HINT)
@guarded("get_metadata")
def get_metadata(uid: str, key: str | None = None) -> dict:
    """Get metadata for a file. With a key, returns ``{key: value}``; without,
    returns all metadata as a map."""
    if key:
        value = _mf().get_metadata_value(_norm_uid(uid), key)
        return {key: value}
    return _mf().get_metadata_values(_norm_uid(uid))


@server.tool(annotations=_READ_HINT)
@guarded("check_permission")
def check_permission(uid: str, permission: str, principal: str | None = None) -> bool:
    """Check whether a principal has a permission on a resource. ``permission``
    is a letter (r/w/x/d/...) or name (READ/WRITE/...); ``principal`` defaults to
    the calling agent."""
    return bool(_mf().check_permission(_norm_uid(uid), permission, user=principal))


# --- resources: browsable files + their immutable version history ---
@server.resource("fileengine://{tenant}/{uid}")
def file_resource(tenant: str, uid: str) -> str:
    """Current content of a file as a readable resource."""
    buf = _mf().get(_norm_uid(uid))
    return _content_to_text(buf.getvalue())


@server.resource("fileengine://{tenant}/{uid}/versions")
def versions_resource(tenant: str, uid: str) -> str:
    """The file's immutable version history (newest-first timestamps), as JSON."""
    import json
    return json.dumps([r.version for r in _mf().revisions(_norm_uid(uid))])


@server.resource("fileengine://{tenant}/{uid}/versions/{version}")
def version_resource(tenant: str, uid: str, version: str) -> str:
    """Content of a specific historical version of a file (time travel)."""
    return _content_to_text(_read_version_bytes(uid, version))


# --- write tools: append-only. Defined unconditionally so they are importable
#     and unit-testable; only *registered* on the agent surface when writes are
#     enabled (hidden entirely in MCP_READ_ONLY mode). -----------------------
@guarded("create_directory")
def create_directory(parent_uid: str, name: str) -> str:
    """Create a new directory under a parent and return its UID."""
    return _mf().mkdir(_norm_uid(parent_uid), name)


@guarded("create_file")
def create_file(parent_uid: str, name: str) -> str:
    """Create a new (empty) file under a parent and return its UID.

    Write content with ``write_file``; that appends the first version."""
    return _mf().touch(_norm_uid(parent_uid), name)


@guarded("write_file")
def write_file(uid: str, content: str, as_: str = "text") -> dict:
    """Write file content. This is **append-only**: it adds a new version and
    never overwrites or erases prior versions (recoverable via list_versions /
    read_version / restore_version). ``as_`` is ``text`` or ``base64``. Payloads
    over ``MCP_MAX_WRITE_BYTES`` are rejected."""
    payload = _content_from_text(content, as_)
    cap_write_bytes(payload, config.max_write_bytes)
    before = len(_mf().revisions(_norm_uid(uid)))
    _mf().put(_norm_uid(uid), payload)
    versions = [r.version for r in _mf().revisions(_norm_uid(uid))]
    return {"uid": uid, "versions_before": before, "versions_after": len(versions),
            "current_version": versions[0] if versions else None}


@guarded("set_metadata")
def set_metadata(uid: str, key: str, value: str) -> bool:
    """Set a metadata key/value on a file or directory."""
    return bool(_mf().set_metadata_value(_norm_uid(uid), key, value))


@guarded("delete_metadata")
def delete_metadata(uid: str, key: str) -> bool:
    """Remove a metadata key. Metadata only — does not touch file content or
    its version history."""
    return bool(_mf().delete_metadata_value(_norm_uid(uid), key))


@guarded("rename")
def rename(uid: str, new_name: str) -> bool:
    """Rename a file or directory in place."""
    return bool(_mf().rename(_norm_uid(uid), new_name))


@guarded("move")
def move(uid: str, destination_parent_uid: str) -> bool:
    """Move a file or directory under a new parent directory."""
    return bool(_mf().move(_norm_uid(uid), _norm_uid(destination_parent_uid)))


@guarded("copy")
def copy(uid: str, destination_parent_uid: str) -> bool:
    """Copy a file or directory into a destination directory."""
    return bool(_mf().copy(_norm_uid(uid), _norm_uid(destination_parent_uid)))


@guarded("restore_version")
def restore_version(uid: str, version: str) -> dict:
    """Restore a file to a prior version (from list_versions). This is
    **append-only**: it adds a new version equal to the chosen one; nothing is
    overwritten or lost, so it is always itself reversible."""
    restored = _mf().restore_to_version(_norm_uid(uid), version)
    return {"uid": uid, "restored_from": version, "new_version": restored}


# Soft delete / undelete — reversible, but gated behind MCP_ALLOW_DELETE.
@guarded("soft_delete")
def soft_delete(uid: str) -> bool:
    """Soft-delete (hide) a file or directory. Reversible with ``undelete``:
    the entity and its full version history persist. No hard delete and no
    version culling is ever performed."""
    return bool(_mf().remove(_norm_uid(uid)))


@guarded("undelete")
def undelete(uid: str) -> bool:
    """Restore a soft-deleted file (pairs with ``soft_delete``)."""
    return bool(_mf().undelete_file(_norm_uid(uid)))


_WRITE_TOOLS = (create_directory, create_file, write_file, set_metadata,
                delete_metadata, rename, move, copy, restore_version)
_DELETE_TOOLS = (soft_delete, undelete)

if not config.read_only:
    for _fn in _WRITE_TOOLS:
        server.add_tool(_fn, annotations=_WRITE_HINTS.get(_fn.__name__))
    if config.allow_delete:
        for _fn in _DELETE_TOOLS:
            server.add_tool(_fn, annotations=_WRITE_HINTS.get(_fn.__name__))


def _banner(transport: str) -> None:
    who = (
        f"bootstrap-agent='{identity.user}' tenant='{identity.tenant}' roles={identity.roles}"
        if identity is not None
        else "identity=per-request (no bootstrap agent)"
    )
    print(
        f"FileEngine MCP server [{transport}]: {who} core={config.grpc_address} "
        f"read_only={config.read_only} allow_delete={config.allow_delete}",
        file=sys.stderr,
    )


def main() -> None:
    """Console entry point — runs the MCP server over stdio."""
    # stdio has no per-request auth, so it needs the process identity that the
    # bootstrap above tried to establish.
    if identity is None:
        raise SystemExit(
            f"LDAP authentication failed for MCP agent '{config.agent_user or '(unset)'}'. "
            "The stdio transport needs a process identity — set "
            "FILEENGINE_MCP_USER / FILEENGINE_MCP_PASSWORD."
        )
    _banner("stdio")
    server.run()


def main_http() -> None:
    """Console entry point — runs the Streamable HTTP transport.

    Each request authenticates independently (Basic → LDAP bind, or Bearer token
    from POST /auth/token) and is scoped to its tenant (X-Tenant / subdomain).
    Run behind TLS (a reverse proxy) for remote/multi-agent use."""
    import uvicorn

    from .http_app import build_app

    _banner("streamable-http")
    host = config.http_host
    port = config.http_port
    print(f"  listening on http://{host}:{port}  (MCP at /mcp, token at /auth/token)",
          file=sys.stderr)
    uvicorn.run(build_app(server, config, config.token_ttl), host=host, port=port)


if __name__ == "__main__":
    main()
