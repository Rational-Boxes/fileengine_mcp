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

"""The tenant-membership invariant for this server.

Membership in a tenant IS holding >=1 LDAP group beneath that tenant's ou.
This server is pinned to one tenant by config, so the header cannot move it —
but the role search used to run against the whole tenant base, which made a
user who is `administrators` in ANY tenant arrive here holding system_admin in
the pinned one. Roles must come from the pinned tenant's ou alone.
"""
import types

import pytest

from fileengine_mcp import ldap_auth, tenant_access


class _Entry:
    def __init__(self, cn):
        self.cn = cn
        self.entry_dn = f"cn={cn}"


class _FakeConn:
    def __init__(self, groups_by_base, user_dn="uid=alice,ou=users,dc=x"):
        self.groups_by_base = groups_by_base
        self.user_dn = user_dn
        self.entries = []

    def search(self, base, filt, **kw):
        if base.startswith("ou=users"):
            e = _Entry("alice")
            e.entry_dn = self.user_dn
            self.entries = [e]
            return True
        self.entries = [_Entry(cn) for cn in self.groups_by_base.get(base, [])]
        return True

    def unbind(self):
        pass


def _cfg(**kw):
    c = types.SimpleNamespace(
        ldap_tenant_base="ou=tenants,dc=x",
        ldap_user_base="ou=users,dc=x",
        ldap_bind_dn="cn=svc",
        ldap_bind_password="pw",
        tenant="alpha",
        agent_user="",
        service_principals="",
    )
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def test_roles_come_only_from_the_pinned_tenants_ou():
    cfg = _cfg()
    conn = _FakeConn({"ou=beta,ou=tenants,dc=x": ["administrators"]})
    # `administrators` in beta must not become system_admin in alpha.
    assert tenant_access.roles_in_tenant(conn, cfg, "uid=alice", "alpha") == []
    assert "system_admin" in tenant_access.roles_in_tenant(conn, cfg, "uid=alice", "beta")


def test_bind_without_a_group_in_the_pinned_tenant_is_not_authenticated(monkeypatch):
    cfg = _cfg()
    conn = _FakeConn({"ou=beta,ou=tenants,dc=x": ["administrators"]})
    monkeypatch.setattr(ldap_auth, "Server", lambda *a, **k: object())
    monkeypatch.setattr(ldap_auth, "Connection", lambda *a, **k: conn)
    ident = ldap_auth._authenticate_against("ldap://x", cfg, "alice", "pw")
    assert not ident.authenticated
    assert "system_admin" not in ident.roles


def test_a_member_of_the_pinned_tenant_authenticates(monkeypatch):
    cfg = _cfg()
    conn = _FakeConn({"ou=alpha,ou=tenants,dc=x": ["users"]})
    monkeypatch.setattr(ldap_auth, "Server", lambda *a, **k: object())
    monkeypatch.setattr(ldap_auth, "Connection", lambda *a, **k: conn)
    ident = ldap_auth._authenticate_against("ldap://x", cfg, "alice", "pw")
    assert ident.authenticated and ident.roles == ["users"]


def test_service_principal_holds_no_tenant_roles(monkeypatch):
    cfg = _cfg(agent_user="svc@platform.test")
    conn = _FakeConn({})
    monkeypatch.setattr(ldap_auth, "Server", lambda *a, **k: object())
    monkeypatch.setattr(ldap_auth, "Connection", lambda *a, **k: conn)
    ident = ldap_auth._authenticate_against("ldap://x", cfg, "SVC@Platform.test", "pw")
    assert ident.authenticated and ident.roles == []


def test_role_lookup_failure_fails_closed():
    class _Boom(_FakeConn):
        def search(self, base, filt, **kw):
            raise tenant_access.LDAPException("unreachable")

    assert tenant_access.roles_in_tenant(_Boom({}), _cfg(), "uid=alice", "alpha") == []
