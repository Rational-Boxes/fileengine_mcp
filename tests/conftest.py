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


# ── removal is not on this door ─────────────────────────────────────────────
#
# MCP's service identity holds `read write restore` and NOTHING that removes, on
# purpose. The deployment states the reasoning where it grants the capabilities
# (scripts/Ansible/roles/service_auth/defaults/main.yml, and the dev launcher):
# "append-only, recoverable" is a property of the core, not of MCP's restraint.
# The tool surface is cut to match — there is no soft_delete, no undelete, no
# with-deleted listing — so nothing below tests one.
#
# What the tests still want is to tidy up after themselves, and mostly they
# cannot: RemoveFile/RemoveDirectory are refused for this identity. That used to
# skip seventeen tests whose actual subject was something else entirely (an
# append-only write, a byte cap, an audit record), because they happened to end
# with two `mf.remove` calls. `cleanup()` attempts the removal where the
# deployment allows it and accepts the refusal where it does not, so a test is
# gated on what it measures and not on how it tidies.
def _has_delete() -> bool:
    """Does this deployment's mcp credential hold `delete` after all?

    Probed against a uid that does not exist, so this creates nothing. The
    capability check runs in the core's interceptor BEFORE the handler, so a
    credential without it is refused with "... capability" while one with it gets
    as far as a not-found. The earlier probe made a directory and removed it,
    which on the deployment it was written for left one behind on every run —
    unremovable for exactly the reason being probed.
    """
    if _LIVE_REASON:
        return False
    try:
        import uuid as _uuid
        from fileengine_mcp import server
        server.mf.remove(str(_uuid.uuid4()))
        return True                      # removed a uid that did not exist: odd, but permitted
    except Exception as e:               # noqa: BLE001 - the message is the answer
        return "capability" not in str(e).lower()


HAS_DELETE = _has_delete()


_FIXTURE_ROOT_NAME = "mcp-test-fixtures"
_fixture_root = None


def fixtures_root() -> str:
    """The uid of the one directory every live test builds its fixtures inside.

    Removal is not on this door, so a test that creates a fixture cannot take it
    away again — `cleanup()` below is a no-op wherever the credential holds no
    `delete`, which is every correctly configured deployment. Creating fixtures
    at the tenant root therefore meant a dozen new folders in the top-level
    listing on every live run, permanently. They go in here instead: one folder,
    reused across runs, that an operator can see for what it is and purge with
    the CLI (`fileengine_cli` as an identity that may delete).
    """
    global _fixture_root
    if _fixture_root is not None:
        return _fixture_root
    from fileengine_mcp import server
    for e in server.mf.dir(""):
        if e.name == _FIXTURE_ROOT_NAME and e.is_container:
            _fixture_root = e.uid
            return _fixture_root
    _fixture_root = server.mf.mkdir("", _FIXTURE_ROOT_NAME)
    return _fixture_root


def cleanup(*uids) -> None:
    """Best-effort tidy-up of fixtures, in creation-reverse order.

    Never fails a test: on a door with no `delete` capability the leftovers are
    the cost of the guarantee, and a test that passed must not then fail in its
    own housekeeping.
    """
    if not HAS_DELETE:
        return
    from fileengine_mcp import server
    for uid in uids:
        try:
            server.mf.remove(uid)
        except Exception:  # noqa: BLE001 - housekeeping, never the subject
            pass
