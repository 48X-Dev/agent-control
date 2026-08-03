"""Per-credential ceiling on attachment uploads per minute.

Every storage ceiling in this feature is a stored-bytes total, and a byte total
is not a rate. Upload flooding fills the namespace ceiling in seconds and the
retention sweep then holds it there for a fortnight, so the byte quota alone
turns a burst into a lasting outage of the feature for everyone in the
namespace.

The bucket shape is :class:`~.turn_quota.TurnQuota`'s, reused rather than
reimplemented: same sliding minute, same ``(namespace_key, caller_hash)`` key,
same bounded dictionary, same eviction that forgives the quietest rather than
the loudest. A second implementation of a rate limiter is a second place for the
window arithmetic to be subtly wrong.

The instance is separate because the ceilings are different in kind. A turn
spends model quota; an upload spends disk. Sharing one counter would let a
person who attached three files fail to send a message.

Per process, with the same honest limitation the turn quota states: with N
replicas a principal gets N times the allowance. What it delivers is that a
runaway loop or a leaked key cannot fill the disk without bound.
"""

from __future__ import annotations

import threading

from .turn_quota import TurnQuota

_quota: TurnQuota | None = None
_quota_lock = threading.Lock()


def get_attachment_quota(*, max_per_minute: int) -> TurnQuota:
    """Return the process-wide upload limiter, building it on first use."""
    global _quota
    with _quota_lock:
        if _quota is None or _quota.max_per_minute != max_per_minute:
            _quota = TurnQuota(max_per_minute=max_per_minute)
        return _quota


def reset_attachment_quota() -> None:
    """Forget every bucket. For tests, so one does not leak into the next."""
    global _quota
    with _quota_lock:
        _quota = None
