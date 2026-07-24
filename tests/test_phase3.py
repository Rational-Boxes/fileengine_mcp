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

"""Phase 3 tests — Streamable HTTP transport, per-request auth + tenancy.

Unit tests run anywhere; the TestClient integration tests need a live LDAP +
core and skip otherwise."""
import os

import pytest

os.environ.setdefault("FILEENGINE_MCP_USER", "testuser")
os.environ.setdefault("FILEENGINE_MCP_PASSWORD", "password")
os.environ.setdefault("FILEENGINE_MCP_TENANT", "default")

# The configured agent identity — what the server authenticates as and what
# _services_up() checks. Tests assert against this rather than a hardcoded
# literal so they track whatever test LDAP user the environment provides.
_USER = os.environ["FILEENGINE_MCP_USER"]
_PASS = os.environ["FILEENGINE_MCP_PASSWORD"]


# --------------------------- unit: no services needed ---------------------------
def test_token_store_issue_resolve_and_expiry():
    from fileengine_mcp.ldap_auth import Identity
    from fileengine_mcp.token_store import TokenStore
    ident = Identity(user="alice", roles=["administrators"], tenant="default", authenticated=True)

    store = TokenStore(ttl_seconds=3600)
    tok = store.issue(ident)
    assert store.resolve(tok) is ident
    assert store.resolve("nope") is None
    store.revoke(tok)
    assert store.resolve(tok) is None

    expired = TokenStore(ttl_seconds=-1)
    assert expired.resolve(expired.issue(ident)) is None


def test_decode_basic():
    import base64
    from fileengine_mcp.http_auth import decode_basic
    hdr = "Basic " + base64.b64encode(b"bob:s3cr3t").decode()
    assert decode_basic(hdr) == ("bob", "s3cr3t")
    assert decode_basic("Bearer x") is None
    assert decode_basic("Basic !!notb64") is None


def test_extract_tenant():
    from fileengine_mcp.http_auth import extract_tenant
    assert extract_tenant({"x-tenant": "acme"}, "anything.example.com", "default") == "acme"
    assert extract_tenant({}, "acme.fileengine.com", "default") == "acme"
    assert extract_tenant({}, "www.fileengine.com", "default") == "default"
    assert extract_tenant({}, "fileengine.com", "default") == "default"
    assert extract_tenant({}, "localhost:8089", "default") == "default"


def test_resolve_identity_bearer_path_no_ldap():
    from dataclasses import replace
    from fileengine_mcp.http_auth import resolve_identity
    from fileengine_mcp.ldap_auth import Identity
    from fileengine_mcp.token_store import TokenStore
    store = TokenStore()
    ident = Identity(user="svc", roles=["readers"], tenant="default", authenticated=True)
    tok = store.issue(ident)

    out = resolve_identity(f"Bearer {tok}", "acme", config=None, store=store)
    assert out == replace(ident, tenant="acme")            # tenant is per-session
    assert resolve_identity("Bearer bad", "acme", None, store) is None
    assert resolve_identity("", "acme", None, store) is None


def test_session_contextvar_and_mf_fallback():
    from fileengine_mcp import session
    from fileengine_mcp.ldap_auth import Identity
    assert session.get_session_mf() is None                # no session bound
    sentinel = object()
    tok = session.set_session(session.Session(Identity(user="x"), sentinel))
    try:
        assert session.get_session_mf() is sentinel
    finally:
        session.reset_session(tok)
    assert session.get_session_mf() is None


# ----------------------- integration: live LDAP + core --------------------------
def _services_up() -> bool:
    try:
        from fileengine_mcp.config import Config
        from fileengine_mcp.ldap_auth import authenticate
        cfg = Config()
        return authenticate(cfg, cfg.agent_user, cfg.agent_password).authenticated
    except Exception:
        return False


live = pytest.mark.skipif(not _services_up(), reason="LDAP/core not reachable")


@pytest.fixture(scope="module")
def client():
    # FastMCP's session manager can be run only once per instance, so build the
    # app + TestClient once and share its single lifespan across the live tests.
    from starlette.testclient import TestClient
    from fileengine_mcp import server
    from fileengine_mcp.http_app import build_app
    with TestClient(build_app(server.server, server.config, 60)) as c:
        yield c


def _basic(user=_USER, pw=_PASS):
    import base64
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


@live
def test_token_endpoint_and_whoami(client):
    assert client.post("/auth/token", json={"username": _USER, "password": "wrong"}).status_code == 401
    r = client.post("/auth/token", json={"username": _USER, "password": _PASS})
    assert r.status_code == 200
    token = r.json()["access_token"]
    assert r.json()["token_type"] == "bearer"

    assert client.get("/whoami").status_code == 401          # unauthenticated
    who = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
    assert who.status_code == 200
    body = who.json()
    assert body["user"] == _USER
    assert "administrators" in body["roles"] and "system_admin" in body["roles"]
    assert body["tenant"] == "default"


@live
def test_basic_auth_and_per_session_tenancy(client):
    who = client.get("/whoami", headers=_basic())
    assert who.status_code == 200 and who.json()["tenant"] == "default"
    # X-Tenant scopes this session to a different tenant
    scoped = client.get("/whoami", headers={**_basic(), "X-Tenant": "acme"})
    assert scoped.status_code == 200 and scoped.json()["tenant"] == "acme"


@live
def test_mcp_endpoint_requires_auth(client):
    assert client.get("/mcp").status_code == 401             # gated by middleware
    # authenticated request passes the gate (bare GET isn't a valid MCP call,
    # so the MCP layer rejects it — but not with 401)
    assert client.get("/mcp", headers=_basic()).status_code != 401
