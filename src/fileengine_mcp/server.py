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
    data = buf.getvalue()
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return "[base64] " + base64.b64encode(data).decode("ascii")


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
