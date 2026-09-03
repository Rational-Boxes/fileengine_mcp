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

"""`read_text`: a document's extracted Markdown, fetched from convert_search_ai.

Unit tests — no core, no CSAI. What matters here is the seam: the caller's
identity is what gets asserted, and each of CSAI's answers becomes the right kind
of failure rather than a stray HTTPError surfacing to an agent."""
import io
import json
import urllib.error

import pytest

from fileengine_mcp.csai_client import (CsaiTextClient, ExtractionNotConfigured,
                                        TextForbidden, TextUnavailable)


class _Recorder:
    """Captures the request instead of sending it."""

    def __init__(self, payload=None, error=None):
        self.payload = payload if payload is not None else {"text": "# Doc", "truncated": False}
        self.error = error
        self.seen = None

    def __call__(self, req, timeout=None):
        self.seen = req
        if self.error:
            raise self.error
        body = json.dumps(self.payload).encode()

        class _Resp:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False

            def read(self_inner):
                return body

        return _Resp()


def _http_error(code, detail=""):
    return urllib.error.HTTPError(
        "http://csai/internal", code, "err", {},
        io.BytesIO(json.dumps({"detail": detail}).encode()))


def _client(url="http://fileengine-csai:8092", secret="s3cret"):
    return CsaiTextClient(url, secret)


def test_it_asserts_the_caller_and_authenticates_with_the_shared_secret(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr("urllib.request.urlopen", rec)

    text, truncated = _client().get_text(
        "f1", user="jo@example.com", roles=["administrators"], tenant="acme")

    assert (text, truncated) == ("# Doc", False)
    assert rec.seen.full_url.endswith("/internal/documents/f1/text")
    assert rec.seen.get_header("X-internal-auth") == "s3cret"
    # The principal travels in the body — this is the whole assertion.
    assert json.loads(rec.seen.data) == {
        "user": "jo@example.com", "roles": ["administrators"], "tenant": "acme"}


@pytest.mark.parametrize("url,secret", [("", "s"), ("http://csai", ""), ("", "")])
def test_an_unwired_deployment_says_so_rather_than_calling_nothing(url, secret):
    with pytest.raises(ExtractionNotConfigured):
        CsaiTextClient(url, secret).get_text("f1", user="jo", roles=[], tenant="acme")


def test_a_refused_read_is_a_denial(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen",
                        _Recorder(error=_http_error(403, "not permitted")))
    with pytest.raises(TextForbidden):
        _client().get_text("f1", user="jo", roles=[], tenant="acme")


def test_a_file_without_extracted_text_is_distinct_from_a_disabled_route(monkeypatch):
    """Both are 404. They need different things from whoever reads the message —
    index the file, or configure the service — so they are different exceptions."""
    monkeypatch.setattr("urllib.request.urlopen",
                        _Recorder(error=_http_error(404, "no extracted text for this file")))
    with pytest.raises(TextUnavailable):
        _client().get_text("f1", user="jo", roles=[], tenant="acme")

    monkeypatch.setattr("urllib.request.urlopen",
                        _Recorder(error=_http_error(404, "internal API not enabled")))
    with pytest.raises(ExtractionNotConfigured):
        _client().get_text("f1", user="jo", roles=[], tenant="acme")


def test_an_unreachable_service_is_reported_as_such(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen",
                        _Recorder(error=urllib.error.URLError("connection refused")))
    with pytest.raises(RuntimeError, match="unreachable"):
        _client().get_text("f1", user="jo", roles=[], tenant="acme")


# --- the tool itself --------------------------------------------------------

def _server():
    from fileengine_mcp import server
    return server


def test_the_tool_is_registered_and_read_only():
    import asyncio
    tools = {t.name: t for t in asyncio.run(_server().server.list_tools())}
    assert "read_text" in tools
    assert tools["read_text"].annotations.readOnlyHint is True


def test_the_tool_asserts_the_session_identity(monkeypatch):
    server = _server()
    from fileengine_mcp.ldap_auth import Identity

    calls = {}

    class _Fake:
        def get_text(self, uid, *, user, roles, tenant):
            calls.update(uid=uid, user=user, roles=list(roles), tenant=tenant)
            return "# Extracted", False

    monkeypatch.setattr(server, "text_client", lambda _cfg: _Fake())
    monkeypatch.setattr(server, "_active_identity",
                        lambda: Identity(user="jo@example.com", roles=["r"],
                                         tenant="acme", authenticated=True))

    assert server.read_text("f1") == "# Extracted"
    assert calls == {"uid": "f1", "user": "jo@example.com", "roles": ["r"], "tenant": "acme"}


def test_truncation_is_stated_in_the_text(monkeypatch):
    """An agent that cannot see the flag must still be told the document is cut."""
    server = _server()
    from fileengine_mcp.ldap_auth import Identity

    class _Fake:
        def get_text(self, uid, *, user, roles, tenant):
            return "# Long", True

    monkeypatch.setattr(server, "text_client", lambda _cfg: _Fake())
    monkeypatch.setattr(server, "_active_identity",
                        lambda: Identity(user="jo", roles=[], tenant="acme", authenticated=True))

    assert "truncated" in server.read_text("f1")


def test_a_denial_from_csai_is_a_guard_error(monkeypatch):
    """So it is audited as `denied`, in the same column as any other refusal."""
    server = _server()
    from fileengine_mcp.guards import GuardError
    from fileengine_mcp.ldap_auth import Identity

    class _Fake:
        def get_text(self, uid, *, user, roles, tenant):
            raise TextForbidden(uid)

    monkeypatch.setattr(server, "text_client", lambda _cfg: _Fake())
    monkeypatch.setattr(server, "_active_identity",
                        lambda: Identity(user="jo", roles=[], tenant="acme", authenticated=True))

    with pytest.raises(GuardError):
        server.read_text("f1")
