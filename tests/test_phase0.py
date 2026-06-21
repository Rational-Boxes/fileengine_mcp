"""Phase 0 integration test — requires a live LDAP + FileEngine core.

Skips itself if the agent cannot authenticate against LDAP. Run with the
package and the fileengine client importable, e.g.:

    PYTHONPATH=src:../python_interface FILEENGINE_MCP_USER=testuser \\
        FILEENGINE_MCP_PASSWORD=password python -m pytest tests -v
"""
import os

import pytest

os.environ.setdefault("FILEENGINE_MCP_USER", "testuser")
os.environ.setdefault("FILEENGINE_MCP_PASSWORD", "password")
os.environ.setdefault("FILEENGINE_MCP_TENANT", "default")


def _services_up() -> bool:
    try:
        from fileengine_mcp.config import Config
        from fileengine_mcp.ldap_auth import authenticate
        cfg = Config()
        return authenticate(cfg, cfg.agent_user, cfg.agent_password).authenticated
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _services_up(), reason="LDAP/core not reachable")


def test_ldap_identity_resolved():
    from fileengine_mcp import server
    assert server.identity.authenticated
    # testuser is a member of the default tenant's administrators group.
    assert "administrators" in server.identity.roles
    assert "system_admin" in server.identity.roles  # mapped from administrators
    assert server.identity.tenant == "default"


def test_list_directory_root():
    from fileengine_mcp import server
    entries = server.list_directory("root")
    assert isinstance(entries, list)
    for e in entries:
        assert {"uid", "name", "type", "size", "version_count"} <= set(e)
        assert e["type"] in ("file", "directory")


def test_read_file_roundtrip():
    """Create a file, write content, and read it back through the tools' client."""
    from fileengine_mcp import server
    mf = server.mf
    d = mf.mkdir("", f"mcp_phase0_{os.getpid()}")
    assert d
    f = mf.touch(d, "hello.txt")
    mf.put(f, b"hello from mcp")
    assert server.read_file(f) == "hello from mcp"
    mf.remove(f)
    mf.remove(d)


def test_phase0_exposes_only_read_tools():
    """Immutability guard: no mutating or version-culling tool in Phase 0."""
    from fileengine_mcp import server
    forbidden = {"purge", "purge_old_versions", "write_file", "delete_file",
                 "remove", "create_file", "create_directory", "restore_version"}
    exposed = {n for n in dir(server) if callable(getattr(server, n)) and not n.startswith("_")}
    assert not (exposed & forbidden), f"unexpected mutating tool(s): {exposed & forbidden}"
