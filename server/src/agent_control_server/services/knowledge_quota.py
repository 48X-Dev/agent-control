"""Per-session ceiling on how often the corpus may be searched.

The window itself is ``turn_quota.TurnQuota``, imported rather than
reimplemented: a sliding minute, a plain lock, coldest-bucket eviction, and the
honest note that the bucket is per process so N replicas mean N times the
allowance. Roughly right is the goal here too. This bounds a runaway retrieval
loop; it does not meter anything anybody is billed for.

**A second bucket, not a share of the first.** A search must not spend a
namespace's turn allowance, or one agent reading documents would stop another
agent starting work, and the ceiling that fired would name the wrong thing.

**The key is the binding the server verified, never the path.** Under the
runtime-token provider that binding is the token's own ``target_id``, which the
verifier has already refused on mismatch, so each session gets its own window
and no caller can pick which one it spends. Under ``NoAuthProvider`` and the
header fallback there is no verified binding at all, and there the key is
``None``: every caller shares one namespace-wide bucket, which is plan 8.1's
prescribed fallback and ``turn_quota``'s own stated direction. Keying on the
path segment instead would read as per-session and be nothing of the sort - a
caller inventing a new session key per request would mint itself a fresh
allowance every time, which is not a ceiling at all. One bucket that fires
beats six that a loop walks straight past.

"Per turn" is thereby approximated per session-minute, said plainly rather than
implied: the server cannot see a turn boundary from a tool call, and six
searches a minute bounds the same runaway the per-turn phrasing intends.
"""

from __future__ import annotations

import threading

from .turn_quota import TurnQuota

_quota: TurnQuota | None = None
_lock = threading.Lock()


def get_knowledge_quota(*, max_per_minute: int) -> TurnQuota:
    """Return the process-wide search window, building it on first use.

    Rebuilt when the configured ceiling changes, which in practice happens only
    in tests: settings are read once at import in a running server.
    """
    global _quota
    with _lock:
        if _quota is None or _quota.max_per_minute != max_per_minute:
            _quota = TurnQuota(max_per_minute=max_per_minute)
        return _quota


def try_acquire(
    *, namespace_key: str, meter_key: str | None, max_per_minute: int
) -> float | None:
    """Record one search, or return the seconds until one is worth trying.

    ``None`` means go ahead. The number is when the oldest search in the window
    ages out, which is a real answer to "when can I retry" rather than a fixed
    guess a caller would have to invent for itself.

    A ``meter_key`` of ``None`` is not an error: it is a caller whose identity
    nothing verified, and it shares the namespace's one anonymous bucket.
    """
    quota = get_knowledge_quota(max_per_minute=max_per_minute)
    return quota.try_acquire(namespace_key=namespace_key, caller_hash=meter_key)


def reset_knowledge_quota() -> None:
    """Forget every bucket. For tests, so one does not leak into the next."""
    global _quota
    with _lock:
        _quota = None
