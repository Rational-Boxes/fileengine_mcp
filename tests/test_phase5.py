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

"""Phase 5 — packaging acceptance: the example agent completes a
read -> write -> time-travel task end to end through the real MCP dispatch."""
import asyncio
import os
import sys

import pytest

os.environ.setdefault("FILEENGINE_MCP_TENANT", "default")

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "examples"))


# The live gate lives in conftest.py — one implementation, asking what the server
# actually needs (a verified `mcp` key:secret) rather than an LDAP password bind.
from conftest import live, needs_delete  # noqa: E402


@live
@needs_delete
def test_example_agent_recovers_via_time_travel():
    import agent_demo
    from fileengine_mcp import server
    result = asyncio.run(agent_demo.run(server))
    assert result["recovered_ok"] is True
    assert result["time_travelled"] == result["good"]      # old version readable
    assert result["clobbered"] != result["good"]           # it really was clobbered
    assert result["recovered"] == result["good"]           # restored via tools
    assert result["history_len"] >= 3                       # nothing culled
