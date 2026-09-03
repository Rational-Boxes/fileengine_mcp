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

"""Fetch a document's extracted Markdown from convert_search_ai.

``read_file`` returns what is IN the store: a .docx comes back as base64, because
that is what a .docx is. The readable text of one is produced during ingestion and
kept in CSAI's database — so an agent that wants to read a document, rather than
download it, has to ask CSAI.

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


class CsaiTextClient:
    def __init__(self, base_url: str, internal_secret: str, timeout: float = 30.0) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.secret = internal_secret or ""
        self.timeout = timeout

    @property
    def enabled(self) -> bool:
        return bool(self.base_url) and bool(self.secret)

    def get_text(self, file_uid: str, *, user: str, roles, tenant: str) -> Tuple[str, bool]:
        """``(markdown, truncated)`` for ``file_uid`` as ``user``."""
        if not self.enabled:
            raise ExtractionNotConfigured(
                "text extraction is not wired on this deployment "
                "(CSAI_URL / CSAI_INTERNAL_SECRET)")
        url = f"{self.base_url}/internal/documents/{file_uid}/text"
        payload = json.dumps({"user": user, "roles": list(roles or []),
                              "tenant": tenant}).encode()
        req = urllib.request.Request(url, data=payload, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("X-Internal-Auth", self.secret)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            # 404 is two different answers on one status: the route is off for the
            # whole deployment, or this one file has no extracted text. Told apart
            # by the detail, because they need different things from the operator.
            if e.code == 403:
                raise TextForbidden(file_uid) from None
            if e.code == 404:
                detail = _detail(e)
                if "not enabled" in detail:
                    raise ExtractionNotConfigured(
                        "convert_search_ai has no internal secret configured") from None
                raise TextUnavailable(file_uid) from None
            raise RuntimeError(f"convert_search_ai returned {e.code}: {_detail(e)}") from None
        except urllib.error.URLError as e:
            raise RuntimeError(f"convert_search_ai unreachable: {e.reason}") from None
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
