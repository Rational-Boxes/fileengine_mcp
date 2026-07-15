"""Offline unit tests for the MCP key:secret auth wiring (§16) — no LDAP/HTTP.

Covers Basic decoding as key:secret, the verifier's disabled/short-circuit paths,
and that resolve_identity/token use the verifier (mocked) rather than an LDAP
password bind.
"""
import base64
from types import SimpleNamespace

from fileengine_mcp import http_auth, service_cred_client
from fileengine_mcp.ldap_auth import Identity


def _basic(key_id, secret):
    return "Basic " + base64.b64encode(f"{key_id}:{secret}".encode()).decode()


def test_decode_basic_is_key_secret():
    assert http_auth.decode_basic(_basic("fesk_abc", "fesks_xyz")) == ("fesk_abc", "fesks_xyz")
    assert http_auth.decode_basic("Bearer x") is None
    assert http_auth.decode_basic("Basic not-base64!!") is None


def test_verifier_disabled_without_url_or_secret():
    assert not service_cred_client.ServiceCredVerifier("", "sekret").enabled
    assert not service_cred_client.ServiceCredVerifier("http://x", "").enabled
    v = service_cred_client.ServiceCredVerifier("", "")
    assert v.verify("k", "s", "default", "mcp") is None  # no network attempted


def test_resolve_identity_uses_verifier_not_password(monkeypatch):
    cfg = SimpleNamespace(tenant="default", ldap_manager_url="http://x",
                          service_cred_internal_secret="sek", verify_cache_ttl=60)

    calls = {}

    class FakeVerifier:
        def verify(self, key_id, secret, tenant, scope, source_ip=None):
            calls["verify"] = (key_id, secret, tenant, scope)
            return "alice" if secret == "good" else None

    monkeypatch.setattr(http_auth, "get_verifier", lambda c: FakeVerifier())
    monkeypatch.setattr(http_auth, "resolve_roles",
                        lambda c, uid: Identity(user=uid, tenant=c.tenant,
                                                roles=["users"], authenticated=True))

    ok = http_auth.resolve_identity(_basic("fesk_1", "good"), "acme", cfg, store=None)
    assert ok is not None and ok.user == "alice" and ok.tenant == "acme"
    assert calls["verify"] == ("fesk_1", "good", "acme", "mcp")  # scope mcp, tenant threaded

    bad = http_auth.resolve_identity(_basic("fesk_1", "nope"), "acme", cfg, store=None)
    assert bad is None  # a wrong secret (or a directory password) is rejected
