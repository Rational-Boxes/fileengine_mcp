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

"""Streamable HTTP transport for the FileEngine MCP server.

Wraps the FastMCP Streamable-HTTP ASGI app with:
  * a pure-ASGI auth middleware that resolves each request's identity (Basic →
    LDAP bind, or Bearer → token) and tenant, and binds a per-request gRPC
    client into the session context for the tools to use;
  * ``POST /auth/token`` — exchange Basic credentials for a bearer token;
  * ``GET /whoami`` — the resolved {user, roles, tenant} for the caller.

The MCP endpoint (``/mcp``) and ``/whoami`` require authentication; ``/auth/token``
authenticates itself. Identity is always LDAP-derived and forwarded to the core,
which remains the ACL enforcement point."""
import json
import os
from dataclasses import replace

from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route

from . import metrics as _metrics
from .http_auth import decode_basic, extract_tenant, resolve_identity
from .ldap_auth import resolve_roles
from .service_cred_client import get_verifier
from .session import Session, mf_for, reset_session, set_session, get_session
from .token_store import TokenStore

_UNAUTH = JSONResponse(
    {"error": "authentication required",
     "detail": "use Authorization: Basic <user:pass> or Bearer <token> (POST /auth/token)"},
    status_code=401,
    headers={"WWW-Authenticate": 'Basic realm="fileengine-mcp"'},
)


# The unauthenticated monitoring surface, shared with every other service here.
# Prometheus cannot present a bearer token, so these must bypass AuthMiddleware —
# which is exactly why they are also IP-guarded below.
MONITORING_PATHS = {"/healthz", "/readyz", "/metrics"}

_FORBIDDEN = JSONResponse({"error": "forbidden"}, status_code=403)


def monitoring_allowlist() -> set[str]:
    """Client IPs permitted to reach the monitoring routes (empty = unrestricted).

    Same ``FILEENGINE_MONITORING_ALLOW_IPS`` knob the other services read. Read at
    call time rather than import time so a test (and an operator restarting under
    a new environment) sees the change.
    """
    raw = os.environ.get("FILEENGINE_MONITORING_ALLOW_IPS", "")
    return {ip.strip() for ip in raw.split(",") if ip.strip()}


class AuthMiddleware:
    """Pure-ASGI middleware: authenticate, set the session contextvar, delegate.

    Implemented at the ASGI layer (not Starlette ``BaseHTTPMiddleware``) so the
    contextvar set here reliably propagates to the downstream app/tools."""

    _OPEN_PATHS = {"/auth/token"} | MONITORING_PATHS

    def __init__(self, app, config, store: TokenStore):
        self.app = app
        self.config = config
        self.store = store

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "").rstrip("/")

        # Monitoring: no authentication, but not open to anyone either. Unlike the
        # C++ services this transport has a single listener that compose binds to
        # 0.0.0.0 and nginx publishes at /mcp/, so "it only listens on loopback"
        # is not true here and the allowlist is the control that replaces it.
        if scope["type"] == "http" and path in {p.rstrip("/") for p in MONITORING_PATHS}:
            allow = monitoring_allowlist()
            if allow:
                peer = scope.get("client")
                if (peer[0] if peer else "") not in allow:
                    return await _FORBIDDEN(scope, receive, send)
            return await self.app(scope, receive, send)

        if scope["type"] != "http" or path in {
            p.rstrip("/") for p in self._OPEN_PATHS
        }:
            return await self.app(scope, receive, send)

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        tenant = extract_tenant(headers, headers.get("host", ""), self.config.tenant)
        identity = resolve_identity(headers.get("authorization", ""), tenant, self.config, self.store)
        if identity is None or not identity.authenticated:
            return await _UNAUTH(scope, receive, send)

        # Caller IP forwarded to the core for audit — trusted-proxy aware (§3),
        # honoring FILEENGINE_TRUSTED_PROXIES like the C++ bridges.
        from .netutil import resolve_client_ip
        peer = scope.get("client")
        source_addr = resolve_client_ip(peer[0] if peer else "", headers.get("x-forwarded-for", ""))

        label = headers.get("mcp-session-id") or "http"
        token = set_session(Session(identity, mf_for(identity, self.config, source_addr), label=label))
        try:
            await self.app(scope, receive, send)
        finally:
            reset_session(token)


async def _token_endpoint(request: Request) -> JSONResponse:
    """Exchange a key:secret (Basic header or JSON body) for a bearer token (§16).

    The credential is a backend-generated service credential (scope ``mcp``), not an
    LDAP directory password; it is verified against ldap_manager and roles come from
    LDAP."""
    auth = request.headers.get("authorization", "")
    creds = decode_basic(auth)
    if creds is None:
        try:
            body = json.loads(await request.body() or b"{}")
        except json.JSONDecodeError:
            body = {}
        # Accept key_id/secret; fall back to the legacy field names as aliases.
        creds = (body.get("key_id", body.get("username", "")),
                 body.get("secret", body.get("password", "")))
    key_id, secret = creds
    if not key_id or not secret:
        return JSONResponse({"error": "missing credentials"}, status_code=400)

    store: TokenStore = request.app.state.token_store
    config = request.app.state.config
    tenant = extract_tenant(dict(request.headers), request.headers.get("host", ""), config.tenant)
    uid = get_verifier(config).verify(key_id, secret, tenant, "mcp")
    if uid is None:
        return JSONResponse({"error": "authentication failed"}, status_code=401)
    identity = resolve_roles(config, uid)
    if not identity.authenticated:
        return JSONResponse({"error": "authentication failed"}, status_code=401)
    identity = replace(identity, tenant=tenant)
    token = store.issue(identity)
    return JSONResponse({"access_token": token, "token_type": "bearer", "expires_in": store.ttl})


async def _whoami(request: Request) -> JSONResponse:
    """Report the caller's resolved identity (set by AuthMiddleware)."""
    sess = get_session()
    if sess is None:  # pragma: no cover - middleware guarantees a session here
        return _UNAUTH
    return JSONResponse({
        "user": sess.identity.user,
        "roles": sess.identity.roles,
        "tenant": sess.identity.tenant,
    })


# ---------------------------- monitoring -----------------------------------

def _check_core(config) -> bool:
    """gRPC core reachable. A channel-ready probe rather than an RPC, so readiness
    does not depend on any principal's ACLs."""
    try:
        import grpc
        channel = grpc.insecure_channel(config.grpc_address)
        try:
            grpc.channel_ready_future(channel).result(timeout=2)
            return True
        finally:
            channel.close()
    except Exception:  # noqa: BLE001 - any failure means "not reachable"
        return False


def _check_ldap(config) -> bool:
    """LDAP reachable. Authentication is the whole point of this server — every
    request resolves an identity against the directory — so an unreachable
    directory means genuinely not ready, not merely degraded.

    A bare connect, not a bind: readiness must not depend on the agent account
    being configured, since the HTTP transport authenticates each caller.
    """
    try:
        from urllib.parse import urlparse
        import socket
        parsed = urlparse(config.ldap_uri)
        port = parsed.port or (636 if parsed.scheme == "ldaps" else 389)
        with socket.create_connection((parsed.hostname or "localhost", port), timeout=2):
            return True
    except Exception:  # noqa: BLE001
        return False


async def _healthz(request: Request) -> JSONResponse:
    """Liveness: the process is up and serving. No external calls."""
    from . import __version__ as _v
    return JSONResponse({"status": "ok", "service": "mcp", "version": _v})


async def _readyz(request: Request) -> JSONResponse:
    """Readiness: the dependencies this server cannot serve a request without."""
    from starlette.concurrency import run_in_threadpool
    config = request.app.state.config
    checks = {
        "core": await run_in_threadpool(_check_core, config),
        "ldap": await run_in_threadpool(_check_ldap, config),
    }
    ok = all(checks.values())
    return JSONResponse({"status": "ok" if ok else "degraded", "checks": checks},
                        status_code=200 if ok else 503)


async def _metrics_endpoint(request: Request) -> PlainTextResponse:
    """Prometheus exposition. Same format and namespace as every other service."""
    from . import __version__ as _v
    app = request.app

    def _service_metrics(m: "_metrics.Metrics") -> None:
        store: TokenStore | None = getattr(app.state, "token_store", None)
        if store is not None:
            st = store.stats()
            m.gauge("fileengine_mcp_tokens_active",
                    "Bearer tokens issued to agents that have not yet expired",
                    st["active"])
            # Expired-but-retained entries are only dropped when that exact token
            # is presented again, so this climbing is the signal that agents are
            # taking tokens and never reusing them.
            m.gauge("fileengine_mcp_tokens_expired_retained",
                    "Expired tokens still held in memory; grows if issued tokens are never reused",
                    st["expired"])

        config = getattr(app.state, "config", None)
        if config is not None:
            m.gauge("fileengine_mcp_read_only",
                    "1 when the server refuses every mutating tool",
                    1 if getattr(config, "read_only", False) else 0)
            m.gauge("fileengine_mcp_allow_delete",
                    "1 when the (reversible) delete/undelete tools are enabled",
                    1 if getattr(config, "allow_delete", False) else 0)

        sessions = getattr(app.state, "mcp_session_manager", None)
        if sessions is not None and hasattr(sessions, "__len__"):
            try:
                m.gauge("fileengine_mcp_sessions",
                        "Live Streamable-HTTP MCP sessions", len(sessions))
            except Exception:  # noqa: BLE001 - session bookkeeping is best-effort
                pass

    return PlainTextResponse(
        _metrics.render("mcp", [_service_metrics], {"version": _v}),
        media_type=_metrics.CONTENT_TYPE)


def build_app(server, config, ttl_seconds: int = 3600):
    """Build the Streamable-HTTP ASGI app for an MCP ``server`` + ``config``."""
    app = server.streamable_http_app()
    store = TokenStore(ttl_seconds)
    app.state.token_store = store
    app.state.config = config
    app.router.routes.append(Route("/auth/token", _token_endpoint, methods=["POST"]))
    app.router.routes.append(Route("/whoami", _whoami, methods=["GET"]))
    # Monitoring. Unauthenticated (a scraper has no credential) and IP-guarded in
    # AuthMiddleware; see MONITORING_PATHS.
    app.router.routes.append(Route("/healthz", _healthz, methods=["GET"]))
    app.router.routes.append(Route("/readyz", _readyz, methods=["GET"]))
    app.router.routes.append(Route("/metrics", _metrics_endpoint, methods=["GET"]))
    app.add_middleware(AuthMiddleware, config=config, store=store)
    return app
