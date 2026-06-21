"""Structured audit log for every tool call.

Each record is a single JSON line with {ts, user, session, tenant, tool, uid,
result, ...}. Content bytes, passwords, and bearer tokens are **never** logged —
only the operation's shape and outcome, so the log is safe to retain."""
import json
import logging
import sys
import time

_logger = logging.getLogger("fileengine_mcp.audit")
_configured = False


def configure(path: str = "") -> None:
    """Send audit records to ``path`` (a file) or to stderr when empty."""
    global _configured
    _logger.setLevel(logging.INFO)
    for h in list(_logger.handlers):
        _logger.removeHandler(h)
    handler = logging.FileHandler(path) if path else logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("audit %(message)s"))
    _logger.addHandler(handler)
    _logger.propagate = False
    _configured = True


def record(*, tool: str, uid: str, result: str, user: str, tenant: str,
           session: str = "stdio", **extra) -> None:
    """Append one audit record. ``result`` is ok|error|denied."""
    if not _configured:
        configure()
    entry = {"ts": round(time.time(), 3), "user": user, "session": session,
             "tenant": tenant, "tool": tool, "uid": uid, "result": result}
    entry.update(extra)
    _logger.info(json.dumps(entry, separators=(",", ":")))
