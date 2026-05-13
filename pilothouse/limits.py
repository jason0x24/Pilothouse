"""Per-tenant rate limiting + trigger deduplication.

Both are in-memory because the bottleneck is the LLM, not the limiter,
and a single Pilothouse process is the unit of deployment for the MVP.
A horizontal scale-out would swap the in-memory dicts for Redis with
no API changes — both helpers are pure-function in their public surface.

Rate limit: sliding 60-second window. Each trigger appends `now` to the
tenant's deque; we trim values older than 60s and reject if the deque
length exceeds the limit.

Dedup: a (tenant_id, agent_id, payload_digest) → run_id mapping with TTL.
A second trigger with the same key inside the window returns the existing
run_id rather than starting a new one. Useful when a webhook source
retries (Datadog, GitHub all do this on transient 5xx).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections import deque
from dataclasses import dataclass


@dataclass
class _DedupEntry:
    run_id: str
    expires_at: float


class _LimiterState:
    def __init__(self) -> None:
        self.windows: dict[str, deque[float]] = {}
        self.dedup: dict[str, _DedupEntry] = {}
        self.lock = asyncio.Lock()


_state = _LimiterState()


def reset() -> None:
    """Test helper — clear all in-memory state."""
    _state.windows.clear()
    _state.dedup.clear()


# --- rate limiting --------------------------------------------------------


async def check_rate_limit(tenant_id: str, *, limit_per_minute: int) -> bool:
    """Return True if this trigger fits under the tenant's per-minute cap.

    `limit_per_minute=0` disables limiting entirely. The check is
    fire-and-record: True means "the trigger has been counted and may
    proceed". A False return should map to HTTP 429 at the transport.
    """
    if limit_per_minute <= 0:
        return True
    now = time.monotonic()
    cutoff = now - 60.0
    async with _state.lock:
        window = _state.windows.setdefault(tenant_id, deque())
        while window and window[0] < cutoff:
            window.popleft()
        if len(window) >= limit_per_minute:
            return False
        window.append(now)
        return True


def current_usage(tenant_id: str) -> int:
    """Inspector for /metrics — count of recorded triggers in the last 60s."""
    now = time.monotonic()
    cutoff = now - 60.0
    window = _state.windows.get(tenant_id)
    if not window:
        return 0
    return sum(1 for t in window if t >= cutoff)


# --- deduplication --------------------------------------------------------


def payload_digest(agent_id: str, payload: dict) -> str:
    """Stable digest of the trigger payload — used as the dedup key.

    `sort_keys=True` keeps the digest invariant under arbitrary key order
    in the JSON blob (Datadog re-emits the same payload with reshuffled
    keys sometimes; we shouldn't be tricked by that).
    """
    canonical = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(f"{agent_id}|{canonical}".encode()).hexdigest()[:16]


async def check_dedup(
    tenant_id: str, agent_id: str, payload: dict, *, window_seconds: int
) -> tuple[bool, str | None]:
    """Return (is_duplicate, existing_run_id_if_so).

    `window_seconds=0` disables dedup. Otherwise we look up the digest
    in the tenant-scoped map; if a fresh entry exists, we report the
    existing run_id without starting a new one.
    """
    if window_seconds <= 0:
        return False, None
    digest = payload_digest(agent_id, payload)
    key = f"{tenant_id}:{digest}"
    now = time.monotonic()
    async with _state.lock:
        entry = _state.dedup.get(key)
        if entry is not None and entry.expires_at > now:
            return True, entry.run_id
        return False, None


async def record_dedup(
    tenant_id: str,
    agent_id: str,
    payload: dict,
    run_id: str,
    *,
    window_seconds: int,
) -> None:
    """Stamp this trigger so a subsequent call inside the window dedupes."""
    if window_seconds <= 0:
        return
    digest = payload_digest(agent_id, payload)
    key = f"{tenant_id}:{digest}"
    expiry = time.monotonic() + float(window_seconds)
    async with _state.lock:
        _state.dedup[key] = _DedupEntry(run_id=run_id, expires_at=expiry)
        # Cheap sweep: prune anything expired so the dict doesn't grow
        # unboundedly under steady load. O(N) over current entries —
        # fine at MVP scale; a real impl would use a sorted heap.
        if len(_state.dedup) > 4096:
            now = time.monotonic()
            stale = [k for k, v in _state.dedup.items() if v.expires_at < now]
            for k in stale:
                _state.dedup.pop(k, None)
