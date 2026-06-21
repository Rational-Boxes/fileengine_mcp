"""Pure guardrail helpers — caps and subtree containment.

These layer *on top of* the LDAP/ACL decision in the core; they sandbox an agent
but never grant access the core would deny. Kept dependency-free so they unit
test without a live stack."""
from typing import Callable, Iterable, Optional


class GuardError(Exception):
    """A guardrail rejected the call (size cap, result cap, or out-of-subtree)."""


def cap_write_bytes(data: bytes, limit: int) -> None:
    if limit and len(data) > limit:
        raise GuardError(f"write of {len(data)} bytes exceeds MCP_MAX_WRITE_BYTES={limit}")


def cap_read_bytes(data: bytes, limit: int) -> None:
    if limit and len(data) > limit:
        raise GuardError(
            f"content is {len(data)} bytes, over MCP_MAX_READ_BYTES={limit}; "
            "read a specific version or narrow the request")


def cap_results(items: list, limit: int) -> tuple[list, bool]:
    """Truncate a listing to ``limit`` entries; returns (items, truncated)."""
    if limit and len(items) > limit:
        return items[:limit], True
    return items, False


def within_allowlist(uid: str, allowlist: Iterable[str], *,
                     parent_of: Callable[[str], Optional[str]],
                     root_uid: str = "", max_depth: int = 256) -> bool:
    """True if ``uid`` is one of the allow-listed UIDs or a descendant of one.

    Walks parents via ``parent_of`` (returns the parent UID, or None/``root_uid``
    at the top). An empty allow-list means unrestricted (always True)."""
    allowed = {a for a in allowlist}
    if not allowed:
        return True
    current = uid
    seen = set()
    for _ in range(max_depth):
        if current in allowed:
            return True
        if current in seen:           # cycle guard
            return False
        seen.add(current)
        if current == root_uid or current is None:
            return False
        current = parent_of(current)
        if current is None:
            return root_uid in allowed
    return False
