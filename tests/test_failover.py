"""Circuit breaker + LDAP replica config (REPLICATION_FAILOVER.md)."""
from fileengine_mcp.failover import CircuitBreaker


def test_circuit_breaker_transitions():
    t = {"v": 0.0}
    b = CircuitBreaker(cooldown_s=10, clock=lambda: t["v"])
    assert b.should_try_primary() and not b.is_degraded()
    b.trip()
    assert b.is_degraded() and not b.should_try_primary()
    t["v"] = 9.9
    assert b.is_degraded()
    t["v"] = 10.0
    assert b.should_try_primary() and not b.is_degraded()
    b.trip()
    b.reset()
    assert b.should_try_primary()


def test_config_ldap_replica_defaults_off(monkeypatch):
    from fileengine_mcp.config import Config

    for k in ("FILEENGINE_LDAP_ENDPOINT_REPLICA", "FILEENGINE_LDAP_REPLICA_ENABLED"):
        monkeypatch.delenv(k, raising=False)
    c = Config()
    assert c.ldap_replica_enabled is False
    assert c.failover_cooldown_s == 30


def test_config_ldap_replica_enabled_defaults_localhost(monkeypatch):
    from fileengine_mcp.config import Config

    monkeypatch.delenv("FILEENGINE_LDAP_ENDPOINT_REPLICA", raising=False)
    monkeypatch.setenv("FILEENGINE_LDAP_REPLICA_ENABLED", "true")
    c = Config()
    assert c.ldap_replica_enabled is True
    assert c.ldap_uri_replica == "ldap://localhost:1389"


def test_config_ldap_replica_explicit(monkeypatch):
    from fileengine_mcp.config import Config

    monkeypatch.setenv("FILEENGINE_LDAP_ENDPOINT_REPLICA", "ldap://10.0.0.9:1389")
    c = Config()
    assert c.ldap_replica_enabled and c.ldap_uri_replica == "ldap://10.0.0.9:1389"
