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

"""In-memory TTL bearer-token store for the HTTP transport.

Mirrors the HTTP bridge's token cache: one LDAP bind issues a token, which is
then accepted (until it expires) without re-binding on every request. Tokens map
to a resolved Identity; nothing sensitive (no password) is stored, and tokens
themselves are never logged."""
import secrets
import threading
import time

from .ldap_auth import Identity


class TokenStore:
    def __init__(self, ttl_seconds: int = 3600) -> None:
        self.ttl = ttl_seconds
        self._lock = threading.Lock()
        self._tokens: dict[str, tuple[Identity, float]] = {}

    def issue(self, identity: Identity) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._tokens[token] = (identity, time.time() + self.ttl)
        return token

    def resolve(self, token: str) -> Identity | None:
        with self._lock:
            entry = self._tokens.get(token)
            if entry is None:
                return None
            identity, expiry = entry
            if time.time() > expiry:
                del self._tokens[token]
                return None
            return identity

    def revoke(self, token: str) -> None:
        with self._lock:
            self._tokens.pop(token, None)

    def stats(self) -> dict[str, int]:
        """Token counts for monitoring. Read-only — deliberately does not prune.

        Expired entries are only dropped when someone presents that exact token
        again (see ``resolve``), so a token issued and never reused stays in the
        dict for the life of the process. Reporting ``expired`` separately makes
        that visible as a number that climbs; pruning here would hide it behind a
        healthy-looking ``active``.
        """
        now = time.time()
        with self._lock:
            expiries = [expiry for _identity, expiry in self._tokens.values()]
        return {
            "active": sum(1 for e in expiries if e >= now),
            "expired": sum(1 for e in expiries if e < now),
            "total": len(expiries),
        }
