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


def mf_for(identity: Identity, config) -> ManagedFiles:
    """Build a gRPC client scoped to a resolved identity + tenant."""
    return ManagedFiles(
        user_name=identity.user,
        user_roles=identity.roles,
        server_address=config.grpc_address,
        tenant=identity.tenant,
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
