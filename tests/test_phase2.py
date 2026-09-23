# Copyright (C) 2026 James Hickman
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""Phase 2 integration tests — append-only write tools + mode gating.

Requires a live LDAP + FileEngine core (skips otherwise)."""
import asyncio
import os
import subprocess
import sys

import pytest

os.environ.setdefault("FILEENGINE_MCP_TENANT", "default")


# The live gate lives in conftest.py — one implementation, and it asks what the
# server actually needs (a verified `mcp` key:secret) rather than an LDAP bind.
from conftest import live, cleanup, fixtures_root  # noqa: E402

pytestmark = live


def _tool_names():
    from fileengine_mcp import server
    return {t.name for t in asyncio.run(server.server.list_tools())}


def test_write_tools_present_by_default():
    names = _tool_names()
    assert {"create_directory", "create_file", "write_file", "set_metadata",
            "delete_metadata", "rename", "move", "copy", "restore_version"} <= names
    # nothing that removes, in any configuration: the credential this door
    # presents holds no `delete`, so such a tool could only ever be refused
    assert "soft_delete" not in names and "undelete" not in names
    assert not any("purge" in n for n in names)


def test_create_write_is_append_only():
    """write_file appends a version; the prior version stays readable (recoverable)."""
    from fileengine_mcp import server
    d = server.create_directory(fixtures_root(), f"mcp_p2_{os.getpid()}")
    f = server.create_file(d, "doc.txt")
    r1 = server.write_file(f, "first")
    assert r1["versions_after"] == r1["versions_before"] + 1
    r2 = server.write_file(f, "second")
    assert r2["versions_after"] == r2["versions_before"] + 1  # strictly increases
    versions = server.list_versions(f)
    assert len(versions) >= 2
    assert server.read_file(f) == "second"
    assert server.read_version(f, versions[-1]) == "first"   # original preserved
    cleanup(f, d)


def test_restore_is_append_only():
    """restore_version adds a new version; it does not erase the one it overwrote."""
    from fileengine_mcp import server
    d = server.create_directory(fixtures_root(), f"mcp_p2r_{os.getpid()}")
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
    cleanup(f, d)


def test_metadata_write_and_clear():
    from fileengine_mcp import server
    d = server.create_directory(fixtures_root(), f"mcp_p2m_{os.getpid()}")
    f = server.create_file(d, "doc.txt")
    server.write_file(f, "x")
    assert server.set_metadata(f, "k", "v") is True
    assert server.get_metadata(f, "k") == {"k": "v"}
    assert server.delete_metadata(f, "k") is True
    assert server.get_metadata(f).get("k") is None
    cleanup(f, d)


def test_base64_roundtrip():
    import base64
    from fileengine_mcp import server
    d = server.create_directory(fixtures_root(), f"mcp_p2b_{os.getpid()}")
    f = server.create_file(d, "blob.bin")
    payload = bytes(range(256))
    server.write_file(f, base64.b64encode(payload).decode(), as_="base64")
    # non-UTF-8 content comes back base64-prefixed
    assert server.read_file(f) == "[base64] " + base64.b64encode(payload).decode()
    cleanup(f, d)


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
              "delete_metadata", "rename", "move", "copy", "restore_version"):
        assert w not in names, f"{w} leaked in read-only mode"
    assert "read_file" in names and "list_versions" in names  # reads remain


def test_no_environment_setting_can_add_a_removal_tool():
    """MCP_ALLOW_DELETE used to publish soft_delete/undelete. It is gone, and the
    name is asserted dead: setting it must not resurrect them.

    Kept as a test rather than deleted with the flag because the flag outlives
    the code — it is still written in old deployment configs and in the example
    host config that shipped with earlier versions, where it now means nothing.
    An operator who set it must get a door with no removal on it, not a surprise.
    """
    names = _surface_in_subprocess({"MCP_ALLOW_DELETE": "1"})
    assert "soft_delete" not in names and "undelete" not in names
    assert "write_file" in names           # writes still on
    assert "restore_version" in names      # the undo for a bad one
    assert not any("purge" in n for n in names)


@live
def test_append_only_is_enforced_by_the_core_not_by_mcp(tmp_path=None):
    """The MCP identity cannot delete, and the refusal comes from the CORE.

    "Append-only, recoverable" is the claim this door is built on, and it does not
    rest on the tool list being short: the service credential is issued
    `read write restore` and no `delete`, so the core refuses RemoveFile whatever
    the tools above it choose to offer. That is the property worth pinning — a
    future capability grant, or a tool that reaches past the guard, should fail
    HERE rather than in production.

    `restore` is in that set and is not an exception to it: RestoreToVersion
    inserts a new version row pointing at the older payload and removes nothing.

    Skipped (rather than inverted) where the identity DOES hold delete, because
    then the deployment has deliberately chosen the other trade-off.
    """
    import pytest as _pytest
    from conftest import HAS_DELETE, fixtures_root
    if HAS_DELETE:
        _pytest.skip("this deployment granted mcp the 'delete' capability")

    import os
    from fileengine import exceptions as fe_exc
    from fileengine_mcp import server

    d = server.mf.mkdir(fixtures_root(), f"mcp_appendonly_{os.getpid()}")
    f = server.mf.touch(d, "cannot_be_removed.txt")
    server.mf.put(f, b"written once")

    with _pytest.raises(Exception) as caught:
        server.mf.remove(f)
    message = str(caught.value)
    assert "capability" in message.lower(), (
        f"expected a capability refusal from the core, got: {message}")

    # The file is still there, and still readable: refused, not half-done.
    # mf.get returns a file-like object, not bytes.
    assert server.exists(f) is True
    assert server.mf.get(f).read() == b"written once"
