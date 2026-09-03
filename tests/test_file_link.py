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

"""`file_link`: the address a file has for a human, not for the API.

The link must be IDENTICAL to the one the SPA's "Copy link" button produces
(frontend utils/fileLocation.ts) — `?folder=<uid>` for a directory, `?file=<uid>`
for a file, `&tenant=` always, because UIDs are tenant-scoped. One shape, so a
link written by an agent and a link copied by a person are the same link."""
import types

import pytest

from fileengine_mcp.guards import GuardError
from fileengine_mcp.ldap_auth import Identity
from fileengine_mcp.session import Session


def _server():
    from fileengine_mcp import server
    return server


def _info(uid="f1", name="Report.docx", is_dir=False):
    return types.SimpleNamespace(uid=uid, name=name, is_dir=is_dir, parent_uid="",
                                 size=1, owner="jo", version="v1",
                                 created_at=None, modified_at=None)


class _MF:
    def __init__(self, info):
        self.info = info

    def stat(self, uid):
        return self.info


def _bind(monkeypatch, info, *, origin="https://acme.example.com",
          tenant="acme", override=""):
    server = _server()
    identity = Identity(user="jo@example.com", roles=[], tenant=tenant, authenticated=True)
    monkeypatch.setattr(server, "_mf", lambda: _MF(info))
    monkeypatch.setattr(server, "_active_identity", lambda: identity)
    monkeypatch.setattr(server, "get_session",
                        lambda: Session(identity, None, label="http", origin=origin))
    monkeypatch.setattr(server.config, "public_app_url", override, raising=False)
    return server


def test_a_file_link_matches_the_spa_copy_link_shape(monkeypatch):
    server = _bind(monkeypatch, _info())
    out = server.file_link("f1")
    assert out["url"] == "https://acme.example.com/files?file=f1&tenant=acme"
    assert out["kind"] == "file"
    assert out["name"] == "Report.docx"


def test_a_folder_link_names_the_query_key_by_kind(monkeypatch):
    """The SPA distinguishes them: a folder opens, a file opens its parent and
    selects itself. Same reveal, different key."""
    server = _bind(monkeypatch, _info(uid="d1", name="Drawings", is_dir=True))
    out = server.file_link("d1")
    assert out["url"] == "https://acme.example.com/files?folder=d1&tenant=acme"
    assert out["kind"] == "folder"


def test_the_tenant_always_travels_with_the_link(monkeypatch):
    """UIDs are tenant-scoped; a link without the tenant resolves nowhere for a
    reader whose session is on another one."""
    server = _bind(monkeypatch, _info(), tenant="other")
    assert server.file_link("f1")["url"].endswith("&tenant=other")


def test_markdown_is_ready_to_paste(monkeypatch):
    server = _bind(monkeypatch, _info())
    out = server.file_link("f1")
    assert out["markdown"] == f'[Report.docx]({out["url"]})'


def test_the_origin_comes_from_the_request_by_default(monkeypatch):
    """Each tenant reaches the platform on its own door, and /mcp/ is proxied on
    that same vhost — so the request's origin IS that tenant's app origin, with
    nothing to configure."""
    server = _bind(monkeypatch, _info(), origin="https://ergc.proximafilevault.com")
    assert server.file_link("f1")["url"].startswith("https://ergc.proximafilevault.com/files?")


def test_an_override_wins_and_substitutes_the_tenant(monkeypatch):
    """For the deployment whose public FQDN is not the Host MCP sees."""
    server = _bind(monkeypatch, _info(), origin="http://mcp-internal:8096",
                   tenant="acme", override="https://{tenant}.example.com/")
    assert server.file_link("f1")["url"] == "https://acme.example.com/files?file=f1&tenant=acme"


def test_no_origin_refuses_rather_than_returning_a_relative_link(monkeypatch):
    """stdio has no request. A relative link looks usable, gets pasted into a
    document, and then fails for the reader rather than for the agent."""
    server = _bind(monkeypatch, _info(), origin="")
    with pytest.raises(GuardError, match="MCP_PUBLIC_APP_URL"):
        server.file_link("f1")


def test_the_tool_is_registered_and_read_only():
    import asyncio
    tools = {t.name: t for t in asyncio.run(_server().server.list_tools())}
    assert "file_link" in tools
    assert tools["file_link"].annotations.readOnlyHint is True
