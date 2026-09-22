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

"""Make the src-layout package importable for unit tests without an install
(live phase tests still rely on PYTHONPATH including ../python_interface)."""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "src"))


# ── the live gate ───────────────────────────────────────────────────────────
#
# Each phase test carried its own copy of this, and every copy tested the
# PRE-§16 credential model: an LDAP password bind for FILEENGINE_MCP_USER /
# _PASSWORD, defaulted to "testuser"/"password". Two things followed.
#
# With no credentials configured the bind failed and 27 tests skipped with
# "LDAP/core not reachable" — which points at infrastructure, when in fact the
# directory and core were both up and the suite simply had nothing to present.
#
# With real LDAP credentials supplied the gate OPENED and 21 tests failed with
# `AttributeError: 'NoneType' object has no attribute 'mkdir'`, because the gate
# and the server no longer agree on what an agent is: server.py builds its
# ManagedFiles client at import only when a `mcp`-scoped key:secret service
# credential (FILEENGINE_MCP_KEY / _SECRET) verifies against ldap_manager. An
# LDAP password is no longer a credential for this door at all.
#
# So the gate now asks the only question that matters to these tests — did the
# server get a client — and names what to set when it did not.
def _live_reason() -> str:
    try:
        from fileengine_mcp import server
    except Exception as e:  # noqa: BLE001 — a gate must not raise
        return f"fileengine_mcp could not be imported ({type(e).__name__}: {e})"
    if getattr(server, "mf", None) is not None:
        return ""
    from fileengine_mcp.config import Config
    cfg = Config()
    if not (cfg.mcp_key and cfg.mcp_secret):
        return ("no mcp service credential set — these tests drive the process "
                "identity, which needs FILEENGINE_MCP_KEY/FILEENGINE_MCP_SECRET "
                "(scope 'mcp', minted from ldap_manager: "
                "POST /v1/me/service-credentials)")
    return (f"the mcp key:secret did not verify for tenant {cfg.tenant!r} — check "
            f"ldap_manager at {getattr(cfg, 'ldap_manager_url', '(unset)')} and the "
            f"credential's scope")


_LIVE_REASON = _live_reason()

import pytest  # noqa: E402 — after the path setup above

live = pytest.mark.skipif(bool(_LIVE_REASON), reason=_LIVE_REASON or "live")


# ── the delete capability ───────────────────────────────────────────────────
#
# MCP's service identity holds `read write` and nothing else, ON PURPOSE. The
# launcher states the reasoning where it issues the credentials: "mcp gets neither
# `delete` nor `destroy`, so 'append-only, recoverable' becomes a property of the
# core rather than of MCP's own restraint." The core therefore refuses
# RemoveFile/UndeleteFile/RestoreVersion to this identity with PERMISSION_DENIED
# — "service lacks the 'delete' capability" — however the tools are gated above.
#
# Tests that soft-delete, undelete or restore (several only as CLEANUP) cannot
# pass under that matrix. They are skipped with the reason rather than failing as
# if the feature were broken, and test_append_only_is_enforced_by_the_core below
# asserts the refusal itself, which is the property the matrix exists to create
# and which nothing tested before.
def _delete_capability() -> bool:
    if _LIVE_REASON:
        return False
    try:
        import os as _os
        from fileengine_mcp import server
        d = server.mf.mkdir("", f"mcp_capprobe_{_os.getpid()}")
        try:
            server.mf.remove(d)
            return True
        except Exception:
            return False
    except Exception:
        return False


HAS_DELETE = _delete_capability()

needs_delete = pytest.mark.skipif(
    not HAS_DELETE,
    reason="the mcp service identity holds no 'delete' capability — by deployment "
           "design (see scripts/start_backend_services.sh: append-only is enforced "
           "by the core). Grant it with `fileengine_cli service grant mcp delete` to "
           "run these.",
)
