"""Turning a caller identifier into something safe to store.

``Principal.caller_id`` is opaque and provider-supplied, and under the default
local-credential provider it is ``AuthenticatedClient.key_id`` - the first
eight characters of a live API key. ``Principal``'s own docstring says never to
echo it back to a client, and storing it raw on a row that is then serialized
would do exactly that. So rows keep a hash, and the column is named for what it
holds.

An honest limitation, stated here because the column looks like it answers a
question it cannot: this identifies a *credential*, not a person. Browser
callers authenticate by cookie, where ``AuthenticatedClient(api_key="")`` makes
``key_id`` the literal string ``"***"`` for everyone, and the session JWT
carries no subject claim. For UI traffic every caller therefore hashes to the
same value. "Which key opened this session" is answerable; "which human" is
not, until the session token grows a subject.
"""

from __future__ import annotations

import hashlib

CALLER_HASH_LENGTH = 16
"""Prefix length of the hex digest stored. Matches ``_log_hash`` in
``endpoints/auth.py``, which is where this pattern started."""


def hash_caller_id(caller_id: str | None) -> str | None:
    """Return a stable, non-reversible tag for a caller, or ``None``.

    ``None`` in, ``None`` out: an unauthenticated request has no caller to
    attribute, and inventing a placeholder would make "nobody" look like a
    specific somebody in every query that groups on this column.
    """
    if not caller_id:
        return None
    return hashlib.sha256(caller_id.encode("utf-8")).hexdigest()[:CALLER_HASH_LENGTH]
