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

"""The unauthenticated monitoring surface: /healthz, /readyz, /metrics.

These run anywhere — no LDAP, no core, no network — because the point of most of
them is the auth and allowlist behaviour, which must not depend on a dependency
being up. The live-infrastructure tests live in test_phase3.py.

The case that matters most here is the interaction between two requirements that
pull in opposite directions: a Prometheus scraper cannot present a credential, so
these routes must bypass authentication; and this transport is published through
nginx at /mcp/, so bypassing authentication cannot mean bypassing access control.
"""
import os
from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient


# --------------------------- token store stats ------------------------------

def test_token_store_stats_separates_active_from_expired():
    from fileengine_mcp.ldap_auth import Identity
    from fileengine_mcp.token_store import TokenStore

    store = TokenStore(ttl_seconds=3600)
    identity = Identity(user="a@example.com", roles=["users"], tenant="default",
                        authenticated=True)
    store.issue(identity)
    store.issue(identity)

    stats = store.stats()
    assert stats == {"active": 2, "expired": 0, "total": 2}


def test_token_store_stats_reports_expired_without_pruning_them():
    """Expired entries must stay visible rather than being quietly dropped.

    `resolve` only removes an expired token when that exact token is presented
    again, so one issued and never reused lingers for the life of the process.
    If stats() pruned, that leak would read as a healthy flat `active`.
    """
    from fileengine_mcp.ldap_auth import Identity
    from fileengine_mcp.token_store import TokenStore

    store = TokenStore(ttl_seconds=-1)  # already expired on issue
    store.issue(Identity(user="a@example.com", roles=[], tenant="default",
                         authenticated=True))

    assert store.stats() == {"active": 0, "expired": 1, "total": 1}
    # Still retained after being observed — stats() is read-only.
    assert store.stats()["total"] == 1


# ------------------------------ exposition ----------------------------------

def test_metrics_render_is_valid_prometheus_exposition():
    from fileengine_mcp import metrics

    body = metrics.render("mcp", [], {"version": "0.1.0"})

    assert 'fileengine_build_info{service="mcp",version="0.1.0"} 1' in body
    # Every family carries HELP and TYPE, which is what makes it machine-readable.
    for line in body.splitlines():
        if line.startswith("# TYPE "):
            assert line.split()[-1] in {"gauge", "counter", "histogram", "summary"}
    assert body.endswith("\n")
    assert metrics.CONTENT_TYPE.startswith("text/plain; version=0.0.4")


def test_metrics_render_survives_a_broken_collector():
    """A collector that raises must degrade the scrape, not blank it — metrics
    that vanish exactly when something breaks cannot be alerted on."""
    from fileengine_mcp import metrics

    def exploding(m):
        raise RuntimeError("boom")

    body = metrics.render("mcp", [exploding], {"version": "0.1.0"})
    assert "fileengine_collector_failed" in body
    assert "process_threads" in body or "fileengine_uptime_seconds" in body


# ------------------------- auth / allowlist behaviour -----------------------

def _app_with_middleware():
    """A minimal app wearing the real AuthMiddleware.

    Built by hand rather than via build_app() so these tests need neither the MCP
    server object nor a live directory; the middleware is the unit under test.
    """
    from fileengine_mcp.http_app import AuthMiddleware

    async def ok(request):
        return JSONResponse({"reached": request.url.path})

    app = Starlette(routes=[
        Route("/healthz", ok),
        Route("/readyz", ok),
        Route("/metrics", ok),
        Route("/whoami", ok),
    ])
    # A config stub carrying only what the auth path reads before it gives up on
    # an unauthenticated request. Nothing here reaches LDAP: with no
    # Authorization header, resolve_identity returns None without a bind.
    app.add_middleware(AuthMiddleware, config=SimpleNamespace(tenant="default"),
                       store=None)
    return app


def _client(app, peer="127.0.0.1"):
    """TestClient presenting a specific peer address.

    Its default peer is the literal "testclient", not an IP, which would make an
    allowlist test pass or fail for the wrong reason.
    """
    return TestClient(app, client=(peer, 12345))


@pytest.fixture(autouse=True)
def _clear_allowlist():
    saved = os.environ.pop("FILEENGINE_MONITORING_ALLOW_IPS", None)
    yield
    if saved is not None:
        os.environ["FILEENGINE_MONITORING_ALLOW_IPS"] = saved
    else:
        os.environ.pop("FILEENGINE_MONITORING_ALLOW_IPS", None)


@pytest.mark.parametrize("path", ["/healthz", "/readyz", "/metrics"])
def test_monitoring_paths_do_not_require_authentication(path):
    """A scraper has no bearer token, so 401 here would make the endpoint useless."""
    with TestClient(_app_with_middleware()) as c:
        assert c.get(path).status_code == 200


def test_a_non_monitoring_path_still_requires_authentication():
    """Guard against the bypass being written too broadly."""
    with TestClient(_app_with_middleware()) as c:
        assert c.get("/whoami").status_code == 401


@pytest.mark.parametrize("path", ["/healthz", "/readyz", "/metrics"])
def test_allowlist_refuses_a_client_that_is_not_listed(path):
    os.environ["FILEENGINE_MONITORING_ALLOW_IPS"] = "10.9.9.9"
    with _client(_app_with_middleware()) as c:
        assert c.get(path).status_code == 403


def test_allowlist_admits_a_listed_client():
    os.environ["FILEENGINE_MONITORING_ALLOW_IPS"] = "127.0.0.1,10.9.9.9"
    with _client(_app_with_middleware()) as c:
        assert c.get("/metrics").status_code == 200


def test_allowlist_is_read_per_request_not_at_import():
    """An operator restarting under a new environment must actually get it."""
    from fileengine_mcp.http_app import monitoring_allowlist

    assert monitoring_allowlist() == set()
    os.environ["FILEENGINE_MONITORING_ALLOW_IPS"] = " 1.2.3.4 , 5.6.7.8 "
    assert monitoring_allowlist() == {"1.2.3.4", "5.6.7.8"}


def test_trailing_slash_does_not_evade_the_allowlist():
    """/metrics/ must not slip past the guard that /metrics is subject to."""
    os.environ["FILEENGINE_MONITORING_ALLOW_IPS"] = "10.9.9.9"
    with _client(_app_with_middleware()) as c:
        assert c.get("/metrics/", follow_redirects=False).status_code == 403
