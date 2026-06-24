"""Phase 4 tests — hardening guardrails, audit log, confirmation hints, and the
must-pass "undo any mistake" recoverability scenarios.

Unit tests run anywhere; integration tests need a live LDAP + core and skip."""
import asyncio
import json
import os
import tempfile

import pytest

os.environ.setdefault("FILEENGINE_MCP_USER", "testuser")
os.environ.setdefault("FILEENGINE_MCP_PASSWORD", "password")
os.environ.setdefault("FILEENGINE_MCP_TENANT", "default")

# The configured agent identity — audit records attribute tool calls to this
# user. Assert against it rather than a hardcoded literal.
_USER = os.environ["FILEENGINE_MCP_USER"]


# Importing fileengine_mcp.server authenticates the agent against LDAP at import
# time, so any test that imports it is an integration test. Define the live gate
# up front so those tests can be skipped when LDAP/core aren't reachable.
def _services_up() -> bool:
    try:
        from fileengine_mcp.config import Config
        from fileengine_mcp.ldap_auth import authenticate
        cfg = Config()
        return authenticate(cfg, cfg.agent_user, cfg.agent_password).authenticated
    except Exception:
        return False


live = pytest.mark.skipif(not _services_up(), reason="LDAP/core not reachable")


# ------------------------------ unit: guards ------------------------------
def test_byte_and_result_caps():
    from fileengine_mcp.guards import GuardError, cap_read_bytes, cap_results, cap_write_bytes
    cap_write_bytes(b"12345", 10)            # under: ok
    with pytest.raises(GuardError):
        cap_write_bytes(b"12345678901", 10)  # over
    cap_write_bytes(b"x" * 999, 0)           # 0 disables
    with pytest.raises(GuardError):
        cap_read_bytes(b"toolong", 3)
    assert cap_results([1, 2, 3], 2) == ([1, 2], True)
    assert cap_results([1], 5) == ([1], False)
    assert cap_results([1, 2, 3], 0) == ([1, 2, 3], False)


def test_within_allowlist():
    from fileengine_mcp.guards import within_allowlist
    tree = {"c": "b", "b": "a", "a": ""}
    parent_of = lambda u: tree.get(u)
    assert within_allowlist("c", [], parent_of=parent_of) is True       # empty = unrestricted
    assert within_allowlist("c", ["a"], parent_of=parent_of) is True    # descendant
    assert within_allowlist("a", ["a"], parent_of=parent_of) is True    # the node itself
    assert within_allowlist("b", ["a"], parent_of=parent_of) is True
    assert within_allowlist("a", ["b"], parent_of=parent_of) is False   # ancestor not allowed
    assert within_allowlist("x", ["a"], parent_of=parent_of) is False   # unknown / outside


def test_audit_record_is_structured_and_secret_free():
    from fileengine_mcp import audit
    path = tempfile.mktemp(suffix=".log")
    audit.configure(path)
    try:
        audit.record(tool="read_file", uid="u1", result="ok", user="alice",
                     tenant="default", session="s1")
        line = open(path).read().strip()
        assert line.startswith("audit ")
        entry = json.loads(line[len("audit "):])
        assert entry["tool"] == "read_file" and entry["user"] == "alice"
        assert entry["result"] == "ok" and entry["uid"] == "u1" and entry["tenant"] == "default"
        assert "ts" in entry
        assert "content" not in entry and "password" not in line.lower()
    finally:
        audit.configure("")  # detach the temp file


@live
def test_confirmation_hint_annotations():
    from fileengine_mcp import server
    tools = {t.name: t for t in asyncio.run(server.server.list_tools())}
    assert tools["read_file"].annotations.readOnlyHint is True
    assert tools["list_versions"].annotations.readOnlyHint is True
    assert tools["write_file"].annotations.readOnlyHint is False
    assert tools["rename"].annotations.idempotentHint is True


# --------------------------- integration: live ---------------------------


def _tree(server, tag):
    a = server.create_directory("root", f"mcp_p4_{tag}_{os.getpid()}_A")
    b = server.create_directory("root", f"mcp_p4_{tag}_{os.getpid()}_B")
    f = server.create_file(a, "doc.txt")
    return a, b, f


@live
def test_subtree_allowlist_enforced():
    from fileengine_mcp import server
    from fileengine_mcp.guards import GuardError
    a, b, f = _tree(server, "allow")
    server.write_file(f, "hi")
    server.config.subtree_allowlist = [a]            # sandbox the agent to A
    try:
        assert server.read_file(f) == "hi"           # descendant of A: allowed
        assert isinstance(server.list_directory(a), list)
        with pytest.raises(GuardError):
            server.list_directory("root")            # outside the sandbox
        with pytest.raises(GuardError):
            server.list_directory(b)                 # sibling subtree
    finally:
        server.config.subtree_allowlist = []
    server.mf.remove(f); server.mf.remove(a); server.mf.remove(b)


@live
def test_write_byte_cap_rejects_and_leaves_file_unchanged():
    from fileengine_mcp import server
    from fileengine_mcp.guards import GuardError
    a, b, f = _tree(server, "cap")
    server.write_file(f, "ok")
    versions_before = len(server.list_versions(f))
    server.config.max_write_bytes = 4
    try:
        with pytest.raises(GuardError):
            server.write_file(f, "way too long")
    finally:
        server.config.max_write_bytes = 10 * 1024 * 1024
    assert len(server.list_versions(f)) == versions_before  # rejected before put
    assert server.read_file(f) == "ok"
    server.mf.remove(f); server.mf.remove(a); server.mf.remove(b)


@live
def test_audit_emitted_for_tool_call():
    from fileengine_mcp import server, audit
    a, b, f = _tree(server, "audit")
    server.write_file(f, "x")
    path = tempfile.mktemp(suffix=".log")
    audit.configure(path)
    try:
        server.read_file(f)
    finally:
        audit.configure(server.config.audit_log_file)
    entries = [json.loads(ln[len("audit "):]) for ln in open(path).read().splitlines() if ln.startswith("audit ")]
    rec = [e for e in entries if e["tool"] == "read_file" and e["uid"] == f]
    assert rec and rec[-1]["result"] == "ok" and rec[-1]["user"] == _USER
    server.mf.remove(f); server.mf.remove(a); server.mf.remove(b)


# ---- recoverability: "undo any mistake" (the product guarantee, §10) ----
@live
def test_undo_clobber_via_restore():
    from fileengine_mcp import server
    a, b, f = _tree(server, "clob")
    server.write_file(f, "good")
    snapshot = server.list_versions(f)[0]
    server.write_file(f, "CLOBBERED")
    server.restore_version(f, snapshot)              # the agent's undo
    assert server.read_file(f) == "good"
    server.mf.remove(f); server.mf.remove(a); server.mf.remove(b)


@live
def test_undo_soft_delete_via_undelete():
    from fileengine_mcp import server
    a, b, f = _tree(server, "del")
    server.write_file(f, "alive")
    assert server.soft_delete(f) is True
    assert server.exists(f) is False                 # hidden
    assert server.undelete(f) is True
    assert server.exists(f) is True and server.read_file(f) == "alive"
    server.mf.remove(f); server.mf.remove(a); server.mf.remove(b)


@live
def test_agent_gone_wrong_rolls_back_to_snapshot():
    """A chaotic multi-step sequence, then every change reversed with the tools
    the agent has — file returns to its pre-run snapshot (content, name, parent)."""
    from fileengine_mcp import server
    a, b, f = _tree(server, "chaos")
    server.write_file(f, "ORIGINAL")
    snapshot = server.list_versions(f)[0]
    orig_name = server.stat(f)["name"]
    orig_parent = server.stat(f)["parent_uid"]

    # --- agent goes wrong ---
    server.write_file(f, "garbage 1")
    server.write_file(f, "garbage 2")
    server.rename(f, "WRONG.txt")
    server.move(f, b)
    server.soft_delete(f)

    # --- recovery, using only exposed tools ---
    server.undelete(f)
    server.move(f, orig_parent)
    server.rename(f, orig_name)
    server.restore_version(f, snapshot)

    assert server.read_file(f) == "ORIGINAL"
    assert server.stat(f)["name"] == orig_name
    assert server.stat(f)["parent_uid"] == orig_parent
    # the whole mistaken history is still there — nothing was ever culled
    assert "garbage 2" in [server.read_version(f, v) for v in server.list_versions(f)]
    server.mf.remove(f); server.mf.remove(a); server.mf.remove(b)
