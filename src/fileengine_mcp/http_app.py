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
from dataclasses import replace

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

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


class AuthMiddleware:
    """Pure-ASGI middleware: authenticate, set the session contextvar, delegate.

    Implemented at the ASGI layer (not Starlette ``BaseHTTPMiddleware``) so the
    contextvar set here reliably propagates to the downstream app/tools."""

    _OPEN_PATHS = {"/auth/token"}

    def __init__(self, app, config, store: TokenStore):
        self.app = app
        self.config = config
        self.store = store

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path", "").rstrip("/") in {
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


def build_app(server, config, ttl_seconds: int = 3600):
    """Build the Streamable-HTTP ASGI app for an MCP ``server`` + ``config``."""
    app = server.streamable_http_app()
    store = TokenStore(ttl_seconds)
    app.state.token_store = store
    app.state.config = config
    app.router.routes.append(Route("/auth/token", _token_endpoint, methods=["POST"]))
    app.router.routes.append(Route("/whoami", _whoami, methods=["GET"]))
    app.add_middleware(AuthMiddleware, config=config, store=store)
    return app
