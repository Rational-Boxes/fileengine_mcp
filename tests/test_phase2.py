"""Phase 2 integration tests — append-only write tools + mode gating.

Requires a live LDAP + FileEngine core (skips otherwise)."""
import asyncio
import os
import subprocess
import sys

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


def _tool_names():
    from fileengine_mcp import server
    return {t.name for t in asyncio.run(server.server.list_tools())}


def test_write_tools_present_by_default():
    names = _tool_names()
    assert {"create_directory", "create_file", "write_file", "set_metadata",
            "delete_metadata", "rename", "move", "copy", "restore_version"} <= names
    # delete tools are gated OFF by default
    assert "soft_delete" not in names and "undelete" not in names
    # never, under any config
    assert not any("purge" in n for n in names)


def test_create_write_is_append_only():
    """write_file appends a version; the prior version stays readable (recoverable)."""
    from fileengine_mcp import server
    d = server.create_directory("root", f"mcp_p2_{os.getpid()}")
    f = server.create_file(d, "doc.txt")
    r1 = server.write_file(f, "first")
    assert r1["versions_after"] == r1["versions_before"] + 1
    r2 = server.write_file(f, "second")
    assert r2["versions_after"] == r2["versions_before"] + 1  # strictly increases
    versions = server.list_versions(f)
    assert len(versions) >= 2
    assert server.read_file(f) == "second"
    assert server.read_version(f, versions[-1]) == "first"   # original preserved
    server.mf.remove(f)
    server.mf.remove(d)


def test_restore_is_append_only():
    """restore_version adds a new version; it does not erase the one it overwrote."""
    from fileengine_mcp import server
    d = server.create_directory("root", f"mcp_p2r_{os.getpid()}")
    f = server.create_file(d, "doc.txt")
    server.write_file(f, "good")
    server.write_file(f, "bad")
    before = len(server.list_versions(f))
    oldest = server.list_versions(f)[-1]          # the "good" version
    server.restore_version(f, oldest)
    after = server.list_versions(f)
    assert len(after) == before + 1               # restore appended, nothing lost
    assert server.read_file(f) == "good"          # content recovered
    assert server.read_version(f, after[1]) == "bad"  # the mistake still in history
    server.mf.remove(f)
    server.mf.remove(d)


def test_metadata_write_and_clear():
    from fileengine_mcp import server
    d = server.create_directory("root", f"mcp_p2m_{os.getpid()}")
    f = server.create_file(d, "doc.txt")
    server.write_file(f, "x")
    assert server.set_metadata(f, "k", "v") is True
    assert server.get_metadata(f, "k") == {"k": "v"}
    assert server.delete_metadata(f, "k") is True
    assert server.get_metadata(f).get("k") is None
    server.mf.remove(f)
    server.mf.remove(d)


def test_base64_roundtrip():
    import base64
    from fileengine_mcp import server
    d = server.create_directory("root", f"mcp_p2b_{os.getpid()}")
    f = server.create_file(d, "blob.bin")
    payload = bytes(range(256))
    server.write_file(f, base64.b64encode(payload).decode(), as_="base64")
    # non-UTF-8 content comes back base64-prefixed
    assert server.read_file(f) == "[base64] " + base64.b64encode(payload).decode()
    server.mf.remove(f)
    server.mf.remove(d)


# --- env-gated surface, verified in a fresh process so registration re-runs ---
def _surface_in_subprocess(env_extra):
    code = (
        "import asyncio;from fileengine_mcp import server;"
        "print(','.join(sorted(t.name for t in asyncio.run(server.server.list_tools()))))"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = "src:../python_interface"
    env.update(env_extra)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         env=env, cwd=os.path.dirname(os.path.dirname(__file__)))
    assert out.returncode == 0, out.stderr
    return set(out.stdout.strip().split(","))


def test_read_only_mode_hides_writes():
    names = _surface_in_subprocess({"MCP_READ_ONLY": "1"})
    for w in ("create_directory", "create_file", "write_file", "set_metadata",
              "delete_metadata", "rename", "move", "copy", "restore_version",
              "soft_delete", "undelete"):
        assert w not in names, f"{w} leaked in read-only mode"
    assert "read_file" in names and "list_versions" in names  # reads remain


def test_allow_delete_gate_enables_soft_delete():
    names = _surface_in_subprocess({"MCP_ALLOW_DELETE": "1"})
    assert "soft_delete" in names and "undelete" in names
    assert "write_file" in names           # writes still on
    assert not any("purge" in n for n in names)   # culling never appears
