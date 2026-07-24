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

"""Generate TOOLS.md from the live MCP server's registered tools + resources.

Run with the package importable and a reachable LDAP/core (the server resolves
its identity at import):

    PYTHONPATH=src:../python_interface python scripts/gen_tool_reference.py > TOOLS.md
"""
import asyncio


def _hints(ann) -> str:
    if ann is None:
        return ""
    flags = []
    if ann.readOnlyHint:
        flags.append("read-only")
    if ann.destructiveHint:
        flags.append("destructive")
    if ann.idempotentHint:
        flags.append("idempotent")
    return f" _({', '.join(flags)})_" if flags else ""


def _params(schema: dict) -> str:
    props = schema.get("properties", {})
    if not props:
        return "—"
    required = set(schema.get("required", []))
    parts = []
    for name, spec in props.items():
        typ = spec.get("type", spec.get("anyOf", [{}])[0].get("type", "any") if "anyOf" in spec else "any")
        parts.append(f"`{name}: {typ}`" + ("" if name in required else " *(opt)*"))
    return ", ".join(parts)


async def main() -> None:
    from fileengine_mcp import server

    tools = sorted(await server.server.list_tools(), key=lambda t: t.name)
    templates = await server.server.list_resource_templates()

    out = ["# FileEngine MCP — Tool & Resource Reference", ""]
    out.append("_Generated from the running server with `scripts/gen_tool_reference.py`._")
    out.append("")
    out.append(f"Surface for this configuration: **{len(tools)} tools**. The set "
               "depends on `MCP_READ_ONLY` / `MCP_ALLOW_DELETE`; "
               "version culling and hard delete are never present.")
    out.append("")
    out.append("## Tools")
    out.append("")
    out.append("| Tool | Params | Description |")
    out.append("|---|---|---|")
    for t in tools:
        desc = (t.description or "").strip().split("\n")[0]
        out.append(f"| `{t.name}`{_hints(t.annotations)} | {_params(t.inputSchema)} | {desc} |")
    out.append("")
    out.append("## Resources")
    out.append("")
    out.append("| URI template | Description |")
    out.append("|---|---|")
    for r in sorted(templates, key=lambda r: r.uriTemplate):
        out.append(f"| `{r.uriTemplate}` | {(r.description or '').strip().splitlines()[0]} |")
    out.append("")
    out.append("## Never exposed (by design)")
    out.append("")
    out.append("- **`purge_old_versions` / any version culling** — under no flag or role.")
    out.append("- **Hard delete** — the only delete is reversible `soft_delete` (gated).")
    out.append("- Role/ACL administration and `trigger_sync` — manage via the CLI / HTTP bridge.")
    print("\n".join(out))


if __name__ == "__main__":
    asyncio.run(main())
