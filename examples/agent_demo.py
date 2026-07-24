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

"""Example agent task: read -> write -> time-travel recovery.

Drives the FileEngine MCP server through its real tool dispatch (the same
``call_tool`` path an MCP host uses, so guardrails + audit run) to show the
headline guarantee: an agent clobbers a file, then fully recovers the prior
version with the tools it already has.

Run against a live LDAP + core:

    PYTHONPATH=src:../python_interface python examples/agent_demo.py
"""
import asyncio
import json
import os


async def _call(srv, tool, **args):
    """Invoke an MCP tool and return its result.

    FastMCP returns a ``(content, structured)`` tuple when a tool has a
    structured schema (scalar/list returns), or just the content blocks for a
    bare ``dict`` return — handle both, unwrapping the ``{"result": …}`` envelope."""
    res = await srv.server.call_tool(tool, args)
    content, structured = res if isinstance(res, tuple) else (res, None)
    if isinstance(structured, dict):
        keys = set(structured.keys())
        return structured["result"] if keys == {"result"} else structured
    text = "".join(getattr(c, "text", "") for c in content)
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return text


async def run(srv) -> dict:
    work = await _call(srv, "create_directory", parent_uid="root", name=f"agent_demo_{os.getpid()}")
    doc = await _call(srv, "create_file", parent_uid=work, name="report.md")
    try:
        good = "# Report\n\nFindings: all nominal.\n"
        await _call(srv, "write_file", uid=doc, content=good)
        snapshot = (await _call(srv, "list_versions", uid=doc))[0]   # remember this version

        # ... the agent then makes a mess ...
        await _call(srv, "write_file", uid=doc, content="(accidentally overwrote everything)")
        clobbered = await _call(srv, "read_file", uid=doc)

        # time-travel read of the good version, then restore it (append-only)
        time_travelled = await _call(srv, "read_version", uid=doc, version=snapshot)
        await _call(srv, "restore_version", uid=doc, version=snapshot)
        recovered = await _call(srv, "read_file", uid=doc)

        return {
            "snapshot": snapshot,
            "good": good,
            "clobbered": clobbered,
            "time_travelled": time_travelled,
            "recovered": recovered,
            "history_len": len(await _call(srv, "list_versions", uid=doc)),
            "recovered_ok": recovered == good and clobbered != good and time_travelled == good,
        }
    finally:
        srv.mf.remove(doc)
        srv.mf.remove(work)


def main() -> None:
    from fileengine_mcp import server
    result = asyncio.run(run(server))
    print("snapshot version :", result["snapshot"])
    print("after clobber    :", repr(result["clobbered"]))
    print("time-travel read :", repr(result["time_travelled"]))
    print("after restore    :", repr(result["recovered"]))
    print(f"history length   : {result['history_len']} versions (nothing culled)")
    print("RECOVERED OK     :", result["recovered_ok"])
    raise SystemExit(0 if result["recovered_ok"] else 1)


if __name__ == "__main__":
    main()
