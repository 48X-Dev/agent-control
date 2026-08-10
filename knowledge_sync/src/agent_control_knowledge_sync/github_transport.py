"""How the sync talks to GitHub: one credential, one budget, one retry ladder.

Split out of ``github_client.py`` along the seam ``drive_transport.py`` marks:
how a call is made, apart from what it asks for. The ladder itself, the backoff
cap and the Retry-After reading are the Drive transport's, imported rather than
restated.

Two behaviours here are this API's own. An exhausted ladder raises rather than
returning, so a transport failure and a genuine 404 never reach a caller as the
same value. And ``X-RateLimit-Remaining`` is counted on this one object because
both channels spend the same hourly budget (5,000, measured at K4); a run waits
out a short reset and refuses a long one rather than sleeping through most of an
hour holding the lease.

The whole ``GitHubError`` family lives here, including the four codes only the
reading layer raises, so the hierarchy sits in one file and the import between
the two runs one way.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from typing import Any

import httpx

from .config import SyncConfig
from .drive_transport import BACKOFF_SECONDS, MAX_ATTEMPTS, MAX_BACKOFF_SECONDS, retry_after

__all__ = [
    "GITHUB_API_BASE",
    "GITHUB_API_VERSION",
    "MAX_PAGE_SIZE",
    "MAX_RATE_LIMIT_WAIT_SECONDS",
    "GitHubError",
    "GitHubRateLimitedError",
    "GitHubRefusalError",
    "GitHubRepoError",
    "GitHubResyncError",
    "GitHubScopeError",
    "GitHubTransport",
    "GitHubTreeTruncatedError",
    "GitHubUnreachableError",
]

GITHUB_API_BASE = "https://api.github.com"
GITHUB_API_VERSION = "2022-11-28"

# GitHub's own ceiling for `per_page` on every list endpoint this sync reads.
MAX_PAGE_SIZE = 100

# 5,000/hour measured on this credential (K4). A run waits out a short reset and
# refuses a long one rather than sleeping through most of an hour holding the lease.
MAX_RATE_LIMIT_WAIT_SECONDS = 60.0


class GitHubError(RuntimeError):
    """Anything the GitHub read path gives up on, always with a named code."""

    code: str = "github_error"


class GitHubUnreachableError(GitHubError):
    """The API could not be reached. Never a statement about whether anything exists."""

    code = "github_unreachable"


class GitHubScopeError(GitHubError):
    """A call was made for a repo the allowlist does not name."""

    code = "repo_not_allowlisted"


class GitHubTreeTruncatedError(GitHubError):
    """GitHub truncated the tree, so indexing it would be silently partial (K4)."""

    code = "tree_truncated"


class GitHubRateLimitedError(GitHubError):
    """The hourly budget is spent and its reset is further out than a run will wait."""

    code = "rate_limited"


class GitHubRepoError(GitHubError):
    """One repo the run cannot read, by a named cause."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class GitHubRefusalError(GitHubError):
    """One file refused by name, so a run counts it instead of losing it."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class GitHubResyncError(GitHubError):
    """The stored cursor cannot yield a diff; this repo must be walked whole again."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


async def _sleep(seconds: float) -> None:
    """Indirection so the retry and rate-limit paths are testable without waiting."""
    await asyncio.sleep(seconds)


class GitHubTransport:
    """One GitHub call, with the shared ladder and the hourly budget it is paced by."""

    def __init__(self, token: str, client: httpx.AsyncClient, config: SyncConfig) -> None:
        self._token = token
        self._client = client
        self._config = config
        self.rate_limit_remaining: int | None = None
        self.rate_limit_reset_at: float | None = None

    async def paginate(
        self,
        path: str,
        params: Mapping[str, str],
        *,
        limit: int,
        page_size: int = MAX_PAGE_SIZE,
    ) -> list[dict[str, Any]]:
        """One list endpoint, paged on the shared ladder until it runs short or `limit` lands."""
        collected: list[dict[str, Any]] = []
        page = 1
        while len(collected) < limit:
            query = dict(params, per_page=str(page_size), page=str(page))
            rows = (await self.json(path, query, subject=path, expect_list=True))["items"]
            collected.extend(row for row in rows if isinstance(row, dict))
            if len(rows) < page_size:
                break
            page += 1
        return collected[:limit]

    async def json(
        self,
        path: str,
        params: Mapping[str, str],
        *,
        subject: str,
        expect_list: bool = False,
    ) -> dict[str, Any]:
        """One call whose 403 and 404 are the caller's answer rather than its parse error."""
        response = await self.request(path, params)
        if response.status_code in (403, 404):
            raise GitHubRepoError(
                "repo_unreachable",
                f"GitHub answered HTTP {response.status_code} for {subject}. Under the "
                "credential in use this means the repository or ref does not exist, or the "
                "token cannot see it.",
            )
        return self.payload(response, subject, expect_list=expect_list)

    def payload(
        self, response: httpx.Response, subject: str, *, expect_list: bool = False
    ) -> dict[str, Any]:
        """A body in the shape asked for, public because callers read the status first."""
        if response.status_code != 200:
            raise GitHubError(f"GitHub answered HTTP {response.status_code} for {subject}.")
        try:
            body = response.json()
        except ValueError as exc:
            raise GitHubError(f"GitHub answered {subject} with a body that is not JSON.") from exc
        if expect_list:
            if not isinstance(body, list):
                raise GitHubError(f"GitHub answered {subject} with a body that is not a list.")
            return {"items": body}
        if not isinstance(body, dict):
            raise GitHubError(f"GitHub answered {subject} with a body that is not an object.")
        return body

    async def request(self, path: str, params: Mapping[str, str]) -> httpx.Response:
        """One call on ``drive_transport``'s ladder; exhaustion raises, never returns."""
        # Repo-scoped callers assert the allowlist; the membership probe this also
        # serves is an /orgs/ path. A 302 is an answer that path reads, so it stands.
        await self._await_rate_limit()
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
        }
        delay = BACKOFF_SECONDS
        failure: Exception | None = None
        status: int | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = await self._client.get(
                    f"{GITHUB_API_BASE}{path}",
                    params=dict(params),
                    headers=headers,
                    timeout=self._config.request_timeout_seconds,
                    follow_redirects=False,
                )
            except httpx.HTTPError as exc:
                failure = exc
                if attempt >= MAX_ATTEMPTS:
                    break
                await _sleep(delay)
                delay = min(delay * 2, MAX_BACKOFF_SECONDS)
                continue
            self._read_rate_limit(response)
            if _is_retryable(response):
                status = response.status_code
                if attempt >= MAX_ATTEMPTS:
                    break
                await _sleep(retry_after(response, delay))
                delay = min(delay * 2, MAX_BACKOFF_SECONDS)
                await self._await_rate_limit()
                continue
            return response
        raise GitHubUnreachableError(
            f"GitHub was unreachable for {path} after {MAX_ATTEMPTS} attempts "
            f"({type(failure).__name__ if failure is not None else f'HTTP {status}'}). This is "
            "a transport failure, not an answer about whether anything exists."
        )

    def _read_rate_limit(self, response: httpx.Response) -> None:
        self.rate_limit_remaining = _header_int(response, "X-RateLimit-Remaining")
        reset = _header_int(response, "X-RateLimit-Reset")
        self.rate_limit_reset_at = None if reset is None else float(reset)

    async def _await_rate_limit(self) -> None:
        """Spend the last of the budget waiting, not burning; refuse a long reset."""
        if self.rate_limit_remaining is None or self.rate_limit_remaining > 0:
            return
        reset_at = self.rate_limit_reset_at
        wait = 0.0 if reset_at is None else reset_at - time.time()
        if wait <= 0:
            self.rate_limit_remaining = None
            return
        if wait > MAX_RATE_LIMIT_WAIT_SECONDS:
            raise GitHubRateLimitedError(
                f"The GitHub rate limit is spent and resets in {wait:.0f}s, over the "
                f"{MAX_RATE_LIMIT_WAIT_SECONDS:.0f}s a run will hold the lease waiting. "
                "The cursor stays where the last committed batch left it."
            )
        await _sleep(wait)
        self.rate_limit_remaining = None


def _is_retryable(response: httpx.Response) -> bool:
    """429, 5xx, and the 403 GitHub uses for a spent budget rather than a refusal."""
    if response.status_code >= 500 or response.status_code == 429:
        return True
    return response.status_code == 403 and _header_int(response, "X-RateLimit-Remaining") == 0


def _header_int(response: httpx.Response, name: str) -> int | None:
    raw = response.headers.get(name)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None
