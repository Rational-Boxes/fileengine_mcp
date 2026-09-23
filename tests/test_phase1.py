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

"""Phase 1 integration tests — read tool set + version-aware resources.

Requires a live LDAP + FileEngine core (skips otherwise)."""
import asyncio
import os
import time

import pytest

os.environ.setdefault("FILEENGINE_MCP_TENANT", "default")


# The live gate lives in conftest.py — one implementation, and it asks what the
# server actually needs (a verified `mcp` key:secret) rather than an LDAP bind.
from conftest import live, cleanup, fixtures_root  # noqa: E402

pytestmark = live


def _mkfile(mf, name):
    d = mf.mkdir(fixtures_root(), f"mcp_p1_{os.getpid()}_{name}")
    f = mf.touch(d, name)
    return d, f


def test_stat_and_exists():
    from fileengine_mcp import server
    d, f = _mkfile(server.mf, "s.txt")
    server.mf.put(f, b"x")
    info = server.stat(f)
    assert info["type"] == "file" and info["name"] == "s.txt"
    assert server.stat(d)["type"] == "directory"
    assert server.exists(f) is True
    assert server.exists("deadbeef-0000-0000-0000-000000000000") is False
    cleanup(f, d)


def test_versions_and_time_travel():
    """The immutable-history guarantee: an old version is still readable after a
    newer write (time travel)."""
    from fileengine_mcp import server
    d, f = _mkfile(server.mf, "v.txt")
    server.mf.put(f, b"v1")
    time.sleep(1)
    server.mf.put(f, b"v2")
    versions = server.list_versions(f)         # newest first
    assert len(versions) >= 2
    oldest = versions[-1]
    assert server.read_version(f, oldest) == "v1"   # original preserved
    assert server.read_file(f) == "v2"              # current
    cleanup(f, d)


def test_metadata_and_permission():
    from fileengine_mcp import server
    d, f = _mkfile(server.mf, "m.txt")
    server.mf.put(f, b"x")
    server.mf.set_metadata_value(f, "color", "blue")
    assert server.get_metadata(f, "color") == {"color": "blue"}
    assert server.get_metadata(f).get("color") == "blue"
    # check_permission reads the decision for the caller's own identity.
    assert server.check_permission(f, "r") is True
    # It does not become an ACL editor: this door's credential holds no `acl`,
    # so GrantPermission is refused in the core's interceptor — the same shape as
    # the removal refusal in test_phase2, and for the same reason. Granting
    # rights is an administrative act, not something an agent does mid-task.
    # (This used to grant "dave" READ and assert the grant took, which needed a
    # capability MCP has never held; it was invisible because the test was gated
    # behind a `delete` probe it did not otherwise need.)
    import pytest as _pytest
    with _pytest.raises(Exception) as caught:
        server.mf.grant_permission(f, "dave", "r")
    assert "capability" in str(caught.value).lower()
    cleanup(f, d)


def test_version_resource_time_travel():
    """Read a historical version through the MCP resource URI."""
    from fileengine_mcp import server
    d, f = _mkfile(server.mf, "r.txt")
    server.mf.put(f, b"r1")
    time.sleep(1)
    server.mf.put(f, b"r2")
    oldest = server.list_versions(f)[-1]

    async def run():
        out = await server.server.read_resource(f"fileengine://default/{f}/versions/{oldest}")
        return list(out)

    contents = asyncio.run(run())
    text = getattr(contents[0], "content", contents[0])
    assert text == "r1"
    cleanup(f, d)


def test_read_tools_present_and_no_culling():
    """The read surface is always available, and no version-culling or hard-delete
    tool exists under the default config (the recoverability invariant)."""
    from fileengine_mcp import server
    tools = asyncio.run(server.server.list_tools())
    names = {t.name for t in tools}
    read_tools = {"list_directory", "read_file", "stat", "exists",
                  "list_versions", "read_version", "get_metadata", "check_permission"}
    assert read_tools <= names
    assert not any("purge" in n or "cull" in n or "hard" in n for n in names)
