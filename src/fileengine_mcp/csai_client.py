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

"""Ask convert_search_ai what MCP cannot answer from the store alone.

Two things live in CSAI's database rather than in FileEngine: a document's
extracted Markdown, and the index that makes the corpus searchable. Neither is
reachable over the gRPC path the other tools use.

``read_file`` returns what is IN the store: a .docx comes back as base64, because
that is what a .docx is. The readable text of one is produced during ingestion and
kept in CSAI's database — so an agent that wants to read a document, rather than
download it, has to ask CSAI. Search is the same story one level up: an agent
that has to walk the tree and read likely files to answer a question is doing by
hand what the index already did at ingestion.

MCP cannot call CSAI's user-facing route: that accepts CSAI's own tokens, an
http_bridge token, or an LDAP password, and MCP holds none of the three for the
caller it is acting for. It calls the internal route instead, naming the
principal over a shared-secret channel. CSAI applies its usual READ check against
the core to that name, so this asserts WHO is asking and never what they may see.
"""
import json
import urllib.error
import urllib.request
from typing import Optional, Tuple


class TextUnavailable(Exception):
    """No extracted text for this file (never indexed, or nothing to extract)."""


class TextForbidden(Exception):
    """CSAI refused the read for this principal."""


class ExtractionNotConfigured(Exception):
    """This deployment has not wired MCP to CSAI."""


class BadRequest(Exception):
    """CSAI rejected the request itself — an empty or over-long query."""


class CsaiTextClient:
    def __init__(self, base_url: str, internal_secret: str, timeout: float = 30.0) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.secret = internal_secret or ""
        self.timeout = timeout

    @property
    def enabled(self) -> bool:
        return bool(self.base_url) and bool(self.secret)

    def _post(self, path: str, payload: dict) -> dict:
        """POST to the internal API with the caller asserted. Shared by both calls
        so the assertion, the header and the error mapping cannot drift apart."""
        if not self.enabled:
            raise ExtractionNotConfigured(
                "convert_search_ai is not wired on this deployment "
                "(CSAI_URL / CSAI_INTERNAL_SECRET)")
        req = urllib.request.Request(f"{self.base_url}{path}",
                                     data=json.dumps(payload).encode(), method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("X-Internal-Auth", self.secret)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            detail = _detail(e)
            if e.code == 403:
                raise TextForbidden(detail or path) from None
            if e.code == 400:
                raise BadRequest(detail or "rejected by convert_search_ai") from None
            # 404 is two different answers on one status: the route is off for the
            # whole deployment, or this one file has no extracted text. Told apart
            # by the detail, because they need different things from the operator.
            if e.code == 404:
                if "not enabled" in detail:
                    raise ExtractionNotConfigured(
                        "convert_search_ai has no internal secret configured") from None
                raise TextUnavailable(detail or path) from None
            raise RuntimeError(f"convert_search_ai returned {e.code}: {detail}") from None
        except urllib.error.URLError as e:
            raise RuntimeError(f"convert_search_ai unreachable: {e.reason}") from None

    def search(self, query: str, *, user: str, roles, tenant: str,
               limit: int = 20, fuzzy: bool = True) -> list:
        """Permission-filtered hits for ``query`` as ``user``.

        The filtering happens in CSAI, against the core, for this principal — a
        hit list is a disclosure in itself, so it is not something to assemble
        here and trim afterwards."""
        body = self._post("/internal/search", {
            "user": user, "roles": list(roles or []), "tenant": tenant,
            "query": query, "limit": int(limit), "fuzzy": bool(fuzzy)})
        return list(body.get("hits") or [])

    def get_text(self, file_uid: str, *, user: str, roles, tenant: str) -> Tuple[str, bool]:
        """``(markdown, truncated)`` for ``file_uid`` as ``user``."""
        body = self._post(f"/internal/documents/{file_uid}/text",
                          {"user": user, "roles": list(roles or []), "tenant": tenant})
        return body.get("text", ""), bool(body.get("truncated"))


def _detail(e: urllib.error.HTTPError) -> str:
    try:
        return str(json.loads(e.read().decode() or "{}").get("detail", ""))
    except Exception:                                   # noqa: BLE001
        return ""


_client: Optional[CsaiTextClient] = None


def text_client(config) -> CsaiTextClient:
    """Process-wide client. One object, so the URL and secret are read once."""
    global _client
    if _client is None:
        _client = CsaiTextClient(config.csai_url, config.csai_internal_secret,
                                 getattr(config, "csai_timeout", 30.0))
    return _client
