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
"""An MCP identity belongs to ONE tenant, with the roles held there.

The cross-tenant bug: a bearer token issued in tenant A was re-stamped with
whatever tenant the next request named, keeping A's roles, and both credential
paths resolved roles in the CONFIGURED tenant before stamping the request's
tenant on the result. An administrator of the configured tenant was therefore
an administrator in every tenant, and a member of only the requested tenant
could not sign in at all.
"""
import base64
from types import SimpleNamespace

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from fileengine_mcp import http_app, http_auth, ldap_auth
from fileengine_mcp.ldap_auth import Identity
from fileengine_mcp.token_store import TokenStore


def _basic(user, secret):
    return "Basic " + base64.b64encode(f"{user}:{secret}".encode()).decode()


# --- role resolution runs in the requested tenant ----------------------------

class _Entry:
    def __init__(self, dn):
        self.entry_dn = dn


class _Conn:
    """A directory where alice is `administrators` in alpha and `users` in beta."""
    GROUPS = {"alpha": ["administrators"], "beta": ["users"]}

    def __init__(self, *a, **k):
        self.entries = []

    def search(self, base, flt, **k):
        self.entries = [_Entry("uid=alice,ou=people,dc=x")] if "uid=alice" in flt else []

    def unbind(self):
        pass


def _cfg():
    return SimpleNamespace(tenant="alpha", ldap_bind_dn="cn=svc", ldap_bind_password="p",
                           ldap_user_base="ou=people,dc=x", ldap_tenant_base="ou=tenants,dc=x",
                           ldap_uri="ldap://x", ldap_replica_uri="", service_principals="")


def _roles_by_tenant(monkeypatch):
    monkeypatch.setattr(ldap_auth, "Server", lambda *a, **k: None)
    monkeypatch.setattr(ldap_auth, "Connection", _Conn)
    monkeypatch.setattr(ldap_auth, "roles_in_tenant",
                        lambda svc, cfg, dn, tenant: list(_Conn.GROUPS.get(tenant, [])))
    monkeypatch.setattr(ldap_auth, "_ldap_targets", lambda cfg: [("ldap://x", True)])
    monkeypatch.setattr(ldap_auth, "_breaker", lambda cfg: SimpleNamespace(reset=lambda: None,
                                                                           trip=lambda: None))


def test_roles_are_the_ones_held_in_the_requested_tenant(monkeypatch):
    _roles_by_tenant(monkeypatch)
    beta = ldap_auth.resolve_roles(_cfg(), "alice", "beta")
    assert beta.tenant == "beta" and beta.roles == ["users"]      # not alpha's admin
    alpha = ldap_auth.resolve_roles(_cfg(), "alice")              # stdio: configured tenant
    assert alpha.tenant == "alpha" and alpha.roles == ["administrators"]


def test_a_member_of_only_the_requested_tenant_can_sign_in(monkeypatch):
    _roles_by_tenant(monkeypatch)
    monkeypatch.setattr(_Conn, "GROUPS", {"beta": ["users"]})
    assert ldap_auth.resolve_roles(_cfg(), "alice", "beta").authenticated
    assert not ldap_auth.resolve_roles(_cfg(), "alice", "alpha").authenticated


# --- a token is bound to the tenant it was issued for ------------------------

class _Verifier:
    def verify(self, key_id, secret, tenant, scope, source_ip=None):
        return "alice" if secret == "good" else None


def _token_app(monkeypatch):
    monkeypatch.setattr(http_app, "get_verifier", lambda c: _Verifier())
    monkeypatch.setattr(http_app, "resolve_roles",
                        lambda c, uid, tenant=None: Identity(
                            user=uid, tenant=tenant, authenticated=True,
                            roles={"alpha": ["administrators"], "beta": ["users"]}[tenant]))
    store = TokenStore()
    app = Starlette(routes=[Route("/auth/token", http_app._token_endpoint, methods=["POST"])])
    app.state.token_store = store
    app.state.config = SimpleNamespace(tenant="alpha")
    return TestClient(app), store


def test_the_token_endpoint_issues_with_the_tenants_own_roles(monkeypatch):
    client, store = _token_app(monkeypatch)
    r = client.post("/auth/token", headers={"Authorization": _basic("k", "good"),
                                            "X-Tenant": "beta"})
    assert r.status_code == 200, r.text
    ident = store.resolve(r.json()["access_token"])
    assert ident.tenant == "beta" and ident.roles == ["users"]


def test_a_token_from_one_tenant_is_refused_in_another(monkeypatch):
    client, store = _token_app(monkeypatch)
    tok = client.post("/auth/token", headers={"Authorization": _basic("k", "good"),
                                              "X-Tenant": "alpha"}).json()["access_token"]
    bearer = f"Bearer {tok}"
    mine = http_auth.resolve_identity(bearer, "alpha", None, store)
    assert mine is not None and mine.roles == ["administrators"]
    # The exploit: alpha's administrator token, pointed at beta.
    assert http_auth.resolve_identity(bearer, "beta", None, store) is None


def test_the_login_label_is_not_a_tenant(monkeypatch):
    monkeypatch.delenv("LOGIN_SUBDOMAIN", raising=False)
    assert http_auth.extract_tenant({}, "login.example.com", "default") == "default"
    assert http_auth.extract_tenant({"x-tenant": "login"}, "acme.example.com", "d") == "acme"
    assert http_auth.extract_tenant({"x-tenant": "beta"}, "login.example.com", "d") == "beta"
