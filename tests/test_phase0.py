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

"""Phase 0 integration test — requires a live LDAP + FileEngine core.

Skips itself if the agent cannot authenticate against LDAP. Run with the
package and the fileengine client importable, e.g.:

    PYTHONPATH=src:../python_interface FILEENGINE_MCP_USER=testuser \\
        FILEENGINE_MCP_PASSWORD=password python -m pytest tests -v
"""
import os

import pytest

os.environ.setdefault("FILEENGINE_MCP_TENANT", "default")


# The live gate lives in conftest.py — one implementation, and it asks what the
# server actually needs (a verified `mcp` key:secret) rather than an LDAP bind.
from conftest import live, cleanup, fixtures_root  # noqa: E402

pytestmark = live


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
    d = mf.mkdir(fixtures_root(), f"mcp_phase0_{os.getpid()}")
    assert d
    f = mf.touch(d, "hello.txt")
    mf.put(f, b"hello from mcp")
    assert server.read_file(f) == "hello from mcp"
    cleanup(f, d)


def test_nothing_that_removes_is_on_the_surface():
    """Recoverability invariant: NO tool that removes anything is exposed, in any
    configuration — not version culling, not hard delete, and since the surface
    was cut to the door's capabilities, not the reversible soft delete either.

    The matching half of this is asserted in test_phase2: the core refuses the
    removal RPCs to this identity, so the guarantee does not rest on the list
    below staying short."""
    import asyncio
    from fileengine_mcp import server
    names = {t.name for t in asyncio.run(server.server.list_tools())}
    assert not any("purge" in n or "cull" in n or "hard_delete" in n for n in names)
    assert "purge_old_versions" not in names
    assert "soft_delete" not in names and "undelete" not in names
    # and the with-deleted listing, which the core classes with `delete` too
    schema = {t.name: t.inputSchema for t in asyncio.run(server.server.list_tools())}
    assert "show_deleted" not in (schema.get("list_directory") or {}).get("properties", {})
