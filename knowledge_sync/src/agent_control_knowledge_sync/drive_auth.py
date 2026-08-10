"""OAuth token loop for the corpus reader. Refresh token only, no key on disk.

The reader is the agent's own account under a separate ``drive.readonly``
client, and it reaches exactly one shared root. See ``company-knowledge.md``
2.1 for the identity decision and 5.7 for the scope.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import httpx

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"

DRIVE_READONLY_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
"""The only scope this identity ever requests.

Not ``drive.metadata.readonly``, which cannot export a Doc, and emphatically not
``drive``, which would grant write to an identity whose whole purpose is that it
cannot write. Section 13's refusal list names the widening; this constant is
where a future reader would have to type it.
"""

_EXPIRY_SKEW_SECONDS = 60.0
"""Refresh a minute early rather than discover expiry mid-walk.

A token that dies between two pages of a changes feed turns one 401 into a
partial sync whose cursor has already advanced past rows nobody indexed.
"""


class DriveAuthError(RuntimeError):
    """Every failure in here, with the provider's text and never the secret."""


@dataclass(slots=True)
class DriveCredentials:
    """What the sync container holds: a client, a refresh token, no key file."""

    client_id: str
    client_secret: str
    refresh_token: str

    def redacted(self) -> str:
        """For a log line that has to name the credential without carrying it."""
        return f"client_id={self.client_id[:12]}… refresh_token=<{len(self.refresh_token)} chars>"


@dataclass(slots=True)
class _CachedToken:
    value: str
    expires_at: float


@dataclass(slots=True)
class DriveTokenProvider:
    """Exchanges a refresh token for access tokens, once per expiry window.

    Deliberately not a Google client library. The exchange is one form POST and
    one JSON field; taking ``google-auth`` for it would add a transitive tree to
    the one container holding source credentials, and ``agent-drive.md`` 1.4's
    reasoning about owning the token loop rather than brokering it applies here
    unchanged.
    """

    credentials: DriveCredentials
    client: httpx.AsyncClient
    _cached: _CachedToken | None = field(default=None, repr=False)

    async def bearer_token(self, *, now: float | None = None) -> str:
        """A valid access token, refreshing only when the cached one is stale."""
        moment = time.monotonic() if now is None else now
        cached = self._cached
        if cached is not None and cached.expires_at - _EXPIRY_SKEW_SECONDS > moment:
            return cached.value

        try:
            response = await self.client.post(
                GOOGLE_TOKEN_URL,
                data={
                    "client_id": self.credentials.client_id,
                    "client_secret": self.credentials.client_secret,
                    "refresh_token": self.credentials.refresh_token,
                    "grant_type": "refresh_token",
                },
            )
        except httpx.HTTPError as exc:
            raise DriveAuthError(
                f"Could not reach Google's token endpoint ({type(exc).__name__})."
            ) from exc

        if response.status_code != 200:
            # Google returns the reason in `error`; the body can echo the request
            # it was sent, so only the two named fields are ever surfaced.
            detail = ""
            try:
                body = response.json()
                detail = str(body.get("error") or "")
                description = str(body.get("error_description") or "")
                if description:
                    detail = f"{detail}: {description}" if detail else description
            except Exception:
                detail = ""
            raise DriveAuthError(
                f"Google refused the refresh (HTTP {response.status_code})"
                + (f": {detail}" if detail else "")
                + ". An invalid_grant here usually means the refresh token was "
                "revoked, the account's password changed, or the OAuth consent "
                "screen is still in Testing - where refresh tokens expire after "
                "seven days. Publishing status Internal is what makes this "
                "credential survive."
            )

        payload = response.json()
        token = payload.get("access_token")
        if not token:
            raise DriveAuthError("Google's token response carried no access_token.")
        lifetime = float(payload.get("expires_in") or 0.0)
        self._cached = _CachedToken(value=str(token), expires_at=moment + lifetime)
        return str(token)

    def forget(self) -> None:
        """Drop the cached token so the next call refreshes.

        For the 401-mid-walk case: a token can stop being valid before it stops
        being unexpired (a revoked grant, a changed password), and retrying the
        same cached string forever is how that becomes an infinite loop instead
        of one refused run.
        """
        self._cached = None
