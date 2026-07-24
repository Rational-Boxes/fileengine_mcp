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

"""LDAP master->replica failover (REPLICATION_FAILOVER.md). The per-server auth
routine is monkeypatched so no real directory is needed."""
import types

import pytest

from fileengine_mcp import ldap_auth
from fileengine_mcp.failover import CircuitBreaker
from fileengine_mcp.ldap_auth import Identity, _ServerUnreachable


def _cfg(replica=True):
    return types.SimpleNamespace(
        tenant="default",
        ldap_uri="ldap://masterhost",
        ldap_uri_replica="ldap://replicahost",
        ldap_replica_enabled=replica,
        failover_cooldown_s=30,
    )


@pytest.fixture
def clock():
    t = {"v": 0.0}
    ldap_auth._ldap_breaker = CircuitBreaker(cooldown_s=30, clock=lambda: t["v"])
    yield t
    ldap_auth._ldap_breaker = None


def _patch_targets(monkeypatch, reachable):
    seen = []

    def fake(uri, cfg, username, password):
        seen.append(uri)
        if uri in reachable:
            return Identity(user=username, roles=["users"], tenant=cfg.tenant, authenticated=True)
        raise _ServerUnreachable(uri)

    monkeypatch.setattr(ldap_auth, "_authenticate_against", fake)
    return seen


def test_master_up_uses_master(monkeypatch, clock):
    seen = _patch_targets(monkeypatch, reachable={"ldap://masterhost"})
    ident = ldap_auth.authenticate(_cfg(), "alice", "pw")
    assert ident.authenticated and seen == ["ldap://masterhost"]
    assert not ldap_auth._ldap_breaker.is_degraded()


def test_master_down_fails_over_to_replica(monkeypatch, clock):
    seen = _patch_targets(monkeypatch, reachable={"ldap://replicahost"})
    ident = ldap_auth.authenticate(_cfg(), "alice", "pw")
    assert ident.authenticated
    assert seen == ["ldap://masterhost", "ldap://replicahost"]
    assert ldap_auth._ldap_breaker.is_degraded()


def test_while_degraded_uses_replica_only(monkeypatch, clock):
    seen = _patch_targets(monkeypatch, reachable={"ldap://replicahost"})
    ldap_auth.authenticate(_cfg(), "alice", "pw")
    seen.clear()
    ldap_auth.authenticate(_cfg(), "bob", "pw")
    assert seen == ["ldap://replicahost"]


def test_no_replica_uses_master_only(monkeypatch, clock):
    seen = _patch_targets(monkeypatch, reachable=set())
    ident = ldap_auth.authenticate(_cfg(replica=False), "alice", "pw")
    assert not ident.authenticated and seen == ["ldap://masterhost"]


def test_recovery_after_cooldown(monkeypatch, clock):
    _patch_targets(monkeypatch, reachable={"ldap://replicahost"})
    ldap_auth.authenticate(_cfg(), "alice", "pw")  # master down -> replica, trips breaker
    assert ldap_auth._ldap_breaker.is_degraded()

    # master recovers; after the cooldown it is tried (and succeeds) first again
    seen = _patch_targets(monkeypatch, reachable={"ldap://masterhost"})
    clock["v"] = 30.0
    ident = ldap_auth.authenticate(_cfg(), "alice", "pw")
    assert ident.authenticated and seen == ["ldap://masterhost"]
    assert not ldap_auth._ldap_breaker.is_degraded()
