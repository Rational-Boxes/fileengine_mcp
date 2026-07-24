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

"""Per-request session identity for the Streamable HTTP transport.

Under stdio there is one identity for the whole process (``server.mf``). Under
HTTP, each request authenticates separately, so the resolved identity and its
gRPC client live in a context variable for the duration of that request; the
tools read it via ``get_session_mf()`` and fall back to the process identity."""
import contextvars
from dataclasses import dataclass
from typing import Optional

from .ldap_auth import Identity
from ._client import ManagedFiles


@dataclass
class Session:
    identity: Identity
    mf: ManagedFiles
    label: str = "http"   # audit session label (e.g. the MCP session id)


_current: "contextvars.ContextVar[Optional[Session]]" = contextvars.ContextVar(
    "fileengine_session", default=None
)


def mf_for(identity: Identity, config, source_addr: str = "") -> ManagedFiles:
    """Build a gRPC client scoped to a resolved identity + tenant. `source_addr` is
    the calling agent's client IP (HTTP transport), forwarded to the core for audit;
    empty for the stdio transport (local, no client connection)."""
    return ManagedFiles(
        user_name=identity.user,
        user_roles=identity.roles,
        server_address=config.grpc_address,
        tenant=identity.tenant,
        source_addr=source_addr,
    )


def set_session(session: Session) -> contextvars.Token:
    return _current.set(session)


def reset_session(token: contextvars.Token) -> None:
    _current.reset(token)


def get_session() -> Optional[Session]:
    return _current.get()


def get_session_mf() -> Optional[ManagedFiles]:
    sess = _current.get()
    return sess.mf if sess else None
