"""Files uploaded to a Linear issue, found and fetched for the step working it.

Plan section 3.9. This module is the only place in the server that dereferences
a URL that arrived in tracker data, and every rule below exists because of that
one sentence.

**Which files exist is answered next door**, in
:mod:`services.linear_attachment_discovery`, because sweeping an issue's text
is pure string work and this module is the one holding a credential. What comes
back is a deduped list of upload URLs on allowlisted hosts, and nothing here
widens it.

**A link to any other host is dropped without a fetch, never followed.** The
promise is "every file uploaded to Linear", never "every file linked from
Linear". Dereferencing arbitrary URLs is the SSRF pivot ``orchestration-plan.md``
section 5 names, and no trust decision about a tracker's authors touches egress.
An operator who needs a Drive file attaches it to the issue.

**The credential is the thing being protected.** An attachment URL is a string
somebody typed into a tracker; the Linear API key is server-held. Four controls
keep them apart, and they are the whole of the mitigation:

* the scheme must be ``https``, stated separately because a host allowlist alone
  does not exclude ``http://uploads.linear.app`` and the credential would then
  be on the wire in cleartext;
* the host is checked against an exact-match allowlist **before** the request,
  so a refused host costs no outbound request at all;
* redirects are followed by hand, at most twice, with the scheme and host
  re-checked at every hop - a hop outside the allowlist drops the header and
  **refuses**, rather than retrying anonymously, because a file worth having is
  not worth a credential;
* the URL is never logged at any level, matching the rule
  ``linear_issues.py:_post`` already keeps when it logs only the exception class.

**What is not mitigated, said plainly.** Checking a resolved address and then
letting httpx resolve again at connect time *is* a DNS rebinding race rather
than a defence against one. Closing it means pinning the resolved address into
the connection through a custom transport, which this slice does not build. The
control is the exact-host allowlist over a single hostname under Linear's own
control; rebinding is a stated residual.

**Nothing here converts anything.** Fetched bytes go to the shipped attachment
store and the shipped out-of-band converter, keyed by content sha256. A step
reads a cache entry and never blocks on a parser, so a cache miss is a stated
"not yet converted" line rather than a wait. There is no second converter and no
second delivery path in this file.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urljoin, urlsplit

import httpx
from agent_control_models.attachment_converter_containers import refine_container_mime
from agent_control_models.attachments import AttachmentRefusalCode
from agent_control_models.files import sniff_mime

from ..config import LinearSettings
from .executor_metrics import LINEAR_ATTACHMENT_BYTES, LINEAR_ATTACHMENT_FETCHES, LINEAR_FETCH_OK
from .linear_attachment_discovery import (
    DiscoveredFile,
    discover_files,
    origin_ref_for,
)
from .linear_attachment_logging import redact_upload_urls

_logger = logging.getLogger(__name__)

COMMENT_PAGE_CAP = 50
"""Comments swept for upload links on one issue. Bounded for the same reason
every other connection in this package is: one enormous thread must not turn a
step into an unbounded read. Comments measure zero upload links in this
workspace today, which is a fact about the workspace and not about Linear - a
person answering "can you look at this?" in a comment is the most natural way
for a file to arrive."""

ATTACHMENT_PAGE_CAP = 25
"""Rows read from the structured connection before the per-issue cap narrows
them. Wider than that cap on purpose: the cap picks deterministically from what
was seen, so seeing fewer rows would make the choice depend on paging."""

_ISSUE_FILES_QUERY = """
query AgentControlIssueAttachments($id: String!, $attachmentLimit: Int!, $commentLimit: Int!) {
  issue(id: $id) {
    id
    description
    attachments(first: $attachmentLimit) { nodes { id title url sourceType } }
    comments(first: $commentLimit) { nodes { body } }
    documentContent { content }
  }
}
"""
# `bodyData`, `metadata`, `subtitle` and `creator` are deliberately unselected.
# They are free text written by whoever attached the file, nothing reads them,
# and `sources/linear.py` already establishes the discipline of dropping
# provenance fields at the boundary rather than letting them drift somewhere.

_UNREACHABLE_MESSAGE = "Linear could not be reached."
_REJECTED_MESSAGE = "Linear rejected the request."


class LinearAttachmentError(Exception):
    """The issue read failed. Carries hand-written text and never a URL."""


@dataclass(frozen=True, slots=True)
class FetchedFile:
    """Bytes that cleared every gate, with the type the bytes actually are."""

    data: bytes
    sniffed_mime: str


@dataclass(frozen=True, slots=True)
class FileOutcome:
    """What happened to one discovered file. Exactly one of the two halves is set.

    ``bytes_read`` is carried on both halves, and that is the point of it. A
    download aborted at the ceiling, an HTML login page and a body of a type
    nobody accepts all cost real bytes on the wire and store none, so a task
    ceiling charged only on success would bound roughly nothing: three
    twenty-megabyte refusals per step, twelve steps, against a forty-megabyte
    ceiling that never fires.
    """

    display_name: str
    origin_ref: str
    fetched: FetchedFile | None = None
    refusal: AttachmentRefusalCode | None = None
    bytes_read: int = 0


@dataclass(frozen=True, slots=True)
class IssueFiles:
    """Distinct URLs found against the files this step actually attempted.

    ``found`` is not ``len(outcomes)``. The per-issue cap means most of a
    file-heavy issue may never be attempted, and a summary that reported only
    what it tried would let an envelope say "1 of 1" about an issue carrying
    twelve. Reconciling the two is the entire reason this type exists.

    ``read_failed`` is why a failure is not simply ``found=0``. An issue with
    nothing attached and an issue nobody could read produce the same counts,
    and only one of them may be reported to an agent as "no files are attached
    to this issue" - the other has to say that files may exist and could not be
    listed, or this path has replaced silence with a confident wrong answer.
    """

    found: int
    outcomes: tuple[FileOutcome, ...]
    read_failed: bool = False


class LinearAttachmentClient(Protocol):
    """The narrow surface the step path depends on, so tests fake an object."""

    async def read_issue(self, issue_ref: str) -> dict[str, Any]:
        """Return the raw ``issue`` object, or raise :class:`LinearAttachmentError`."""
        ...

    async def download(self, url: str, *, max_bytes: int) -> bytes:
        """Return the body, or raise :class:`DownloadRefusedError`."""
        ...

    async def aclose(self) -> None:
        """Release any transport this client owns."""
        ...


class DownloadRefusedError(Exception):
    """A fetch did not produce usable bytes, with the code the agent is told.

    ``bytes_read`` is what the wire actually cost before the refusal, which the
    task byte ceiling charges for. A twenty-megabyte body aborted at the cap is
    twenty megabytes of somebody's quota whether or not a file came out of it.
    """

    def __init__(self, code: AttachmentRefusalCode, *, bytes_read: int = 0) -> None:
        super().__init__(code.value)
        self.code = code
        self.bytes_read = bytes_read


class HttpLinearAttachmentClient:
    """:class:`LinearAttachmentClient` over Linear's GraphQL API and upload host."""

    def __init__(
        self,
        *,
        api_key: str,
        settings: LinearSettings,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("api_key must not be empty")
        self._api_key = api_key
        self._settings = settings
        # httpx logs the full request URL at INFO from its own module logger,
        # and this deployment's root level is INFO. Without this the rule two
        # paragraphs of this module's docstring rest on would be false.
        redact_upload_urls(set(settings.attachment_host_allowlist))
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=settings.timeout_seconds, follow_redirects=False
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def read_issue(self, issue_ref: str) -> dict[str, Any]:
        payload = {
            "query": _ISSUE_FILES_QUERY,
            "variables": {
                "id": issue_ref,
                "attachmentLimit": ATTACHMENT_PAGE_CAP,
                "commentLimit": COMMENT_PAGE_CAP,
            },
        }
        try:
            response = await self._client.post(
                self._settings.api_url,
                json=payload,
                headers={"Authorization": self._api_key, "Content-Type": "application/json"},
            )
        except httpx.HTTPError as exc:
            _logger.warning("Linear issue attachment read failed: %s", type(exc).__name__)
            raise LinearAttachmentError(_UNREACHABLE_MESSAGE) from exc
        if response.status_code >= 400:
            _logger.warning("Linear returned HTTP %s for an issue read.", response.status_code)
            raise LinearAttachmentError(_REJECTED_MESSAGE)
        try:
            body = response.json()
        except ValueError as exc:
            raise LinearAttachmentError(_REJECTED_MESSAGE) from exc
        if not isinstance(body, dict) or body.get("errors"):
            raise LinearAttachmentError(_REJECTED_MESSAGE)
        data = body.get("data")
        issue = data.get("issue") if isinstance(data, dict) else None
        return issue if isinstance(issue, dict) else {}

    async def download(self, url: str, *, max_bytes: int) -> bytes:
        """Stream one upload, re-checking the destination at every hop.

        The byte count is kept while the body arrives and the stream is aborted
        past the ceiling. ``Content-Length`` decides nothing: a server can
        understate it, and there is nothing to compare it against anyway because
        an ``Attachment`` carries neither a size nor a content type.
        """
        target = url
        for _ in range(self._settings.attachment_max_redirects + 1):
            _require_fetchable(target, settings=self._settings)
            try:
                async with self._client.stream(
                    "GET",
                    target,
                    headers={"Authorization": self._api_key},
                ) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        target = _next_hop(target, response.headers.get("Location"))
                        # Resolved, then re-checked at the top of the next
                        # iteration. The header is not sent to a host that has
                        # not cleared the allowlist, because the request that
                        # would carry it is never made.
                        continue
                    if response.status_code in (404, 410):
                        raise DownloadRefusedError(AttachmentRefusalCode.NOT_FOUND)
                    if response.status_code >= 400:
                        _logger.warning(
                            "Linear upload host returned HTTP %s.", response.status_code
                        )
                        raise DownloadRefusedError(AttachmentRefusalCode.FETCH_FAILED)
                    return await _read_bounded(response, max_bytes=max_bytes)
            except httpx.HTTPError as exc:
                _logger.warning("Linear upload fetch failed: %s", type(exc).__name__)
                raise DownloadRefusedError(AttachmentRefusalCode.FETCH_FAILED) from exc
        raise DownloadRefusedError(AttachmentRefusalCode.FETCH_FAILED)


def _next_hop(target: str, location: str | None) -> str:
    """Resolve one redirect, refusing rather than raising on a broken one.

    ``urljoin`` raises ``ValueError`` for a ``Location`` like ``//[bad`` - not
    an ``httpx`` error, so it would escape the handler around this call and
    reach the endpoint as a server defect instead of a line the agent reads.
    """
    if not location:
        raise DownloadRefusedError(AttachmentRefusalCode.FETCH_FAILED)
    try:
        return urljoin(target, location)
    except ValueError as exc:
        raise DownloadRefusedError(AttachmentRefusalCode.FETCH_FAILED) from exc


async def _read_bounded(response: httpx.Response, *, max_bytes: int) -> bytes:
    """Stream the body, counting as it arrives, and report the count on refusal.

    The count rides on every refusal because the bytes were spent either way.
    A body aborted at the ceiling has already crossed the wire, and a task
    ceiling that charged only for stored files would let three oversized
    refusals per step cost nothing at all.
    """
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        LINEAR_ATTACHMENT_BYTES.inc(len(chunk))
        if total > max_bytes:
            raise DownloadRefusedError(AttachmentRefusalCode.TOO_LARGE, bytes_read=total)
        chunks.append(chunk)
    if total == 0:
        raise DownloadRefusedError(AttachmentRefusalCode.FETCH_FAILED)
    return b"".join(chunks)


def _looks_like_html(data: bytes) -> bool:
    """Whether a body with no magic number is a web page rather than a document.

    Read from the first bytes and nothing else. Nothing here parses the page: an
    upload host that answers a document request with markup answered the wrong
    question, and that is the whole of what this decides.
    """
    head = data[:256].lstrip().lower()
    return head.startswith((b"<!doctype html", b"<html", b"<head", b"<?xml"))


def _require_fetchable(url: str, *, settings: LinearSettings) -> None:
    """Refuse before any request is made. The order of these two is not free.

    Scheme first, because a host allowlist alone admits ``http://`` on an
    allowlisted host, which puts the API key on the wire in cleartext. Neither
    check reports the URL: the raised code names the condition and the caller
    turns it into a sentence.
    """
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise DownloadRefusedError(AttachmentRefusalCode.BLOCKED_HOST)
    if not parts.hostname or not settings.allows_host(parts.hostname):
        raise DownloadRefusedError(AttachmentRefusalCode.BLOCKED_HOST)


async def collect_issue_files(
    client: LinearAttachmentClient,
    *,
    issue_ref: str,
    settings: LinearSettings,
    max_bytes_per_file: int,
    remaining_task_bytes: int,
    accepted_mimes: set[str],
) -> IssueFiles:
    """Find every file on one issue and fetch the ones this step is allowed.

    Runs outside any database session. ``linear_issues.py`` caps its outbound
    call at ten seconds because it "runs on a request path holding a database
    session"; three attachments at a per-file timeout is a minute of network
    wait, and holding a pooled connection across that is the defect
    ``orchestration-plan.md`` section 8.3 forbids outright. The caller commits
    and releases before calling this, and writes again afterwards.

    Never raises for a file. A refusal is a code, and the count of what was
    found is returned whatever happened to any of it.
    """
    try:
        issue = await client.read_issue(issue_ref)
    except LinearAttachmentError:
        # Not ``found=0``. A tracker that was down has not told this server
        # that the issue carries nothing, and an envelope that turned this into
        # "no files are attached to this issue" would put a server-authored
        # sentence behind exactly the confident half-answer OPS-2 produced.
        return IssueFiles(found=0, outcomes=(), read_failed=True)

    found = discover_files(issue, settings=settings)
    attempted = found[: settings.attachments_max_per_issue]
    budget = remaining_task_bytes
    outcomes: list[FileOutcome] = []
    # One wall clock across every attachment on this step, not one per file, so
    # three slow uploads cannot serialize into a minute of waiting.
    deadline = asyncio.get_running_loop().time() + settings.attachment_step_budget_seconds

    for item in attempted:
        outcome = await _attempt(
            client,
            item,
            settings=settings,
            byte_ceiling=min(max_bytes_per_file, budget),
            ceiling_is_the_task_budget=budget < max_bytes_per_file,
            deadline=deadline,
            accepted_mimes=accepted_mimes,
        )
        outcomes.append(outcome)
        # Charged whatever happened to the file. Bytes that crossed the wire
        # and were then discarded are bytes somebody paid for.
        budget -= outcome.bytes_read

    return IssueFiles(found=len(found), outcomes=tuple(outcomes))


async def _attempt(
    client: LinearAttachmentClient,
    item: DiscoveredFile,
    *,
    settings: LinearSettings,
    byte_ceiling: int,
    ceiling_is_the_task_budget: bool,
    deadline: float,
    accepted_mimes: set[str],
) -> FileOutcome:
    """One file, from the gates that cost nothing to the one that costs bytes."""
    origin_ref = origin_ref_for(item)

    def refused(code: AttachmentRefusalCode, *, bytes_read: int = 0) -> FileOutcome:
        LINEAR_ATTACHMENT_FETCHES.labels(result=code.value).inc()
        return FileOutcome(
            display_name=item.display_name,
            origin_ref=origin_ref,
            refusal=code,
            bytes_read=bytes_read,
        )

    if byte_ceiling <= 0:
        return refused(AttachmentRefusalCode.OVER_TASK_BUDGET)
    if settings.attachment_source_types and (
        item.source_type is None or item.source_type not in settings.attachment_source_types
    ):
        # Refused here, before any request. A gate that fetched and then
        # discarded would return the same code and spend the bytes and the
        # credential it exists to protect.
        return refused(AttachmentRefusalCode.LINK_ONLY)

    try:
        async with asyncio.timeout_at(deadline):
            data = await client.download(item.url, max_bytes=byte_ceiling)
    except TimeoutError:
        return refused(AttachmentRefusalCode.FETCH_FAILED)
    except DownloadRefusedError as exc:
        # A body aborted at a ceiling the task budget set is not an oversized
        # file, and telling an agent it was would send whoever reads that line
        # looking for a file that is not too big.
        if exc.code is AttachmentRefusalCode.TOO_LARGE and ceiling_is_the_task_budget:
            return refused(AttachmentRefusalCode.OVER_TASK_BUDGET, bytes_read=exc.bytes_read)
        return refused(exc.code, bytes_read=exc.bytes_read)
    except Exception as exc:
        # Everything else, deliberately, because "never raises for a file" is
        # this module's contract and two reachable failures are neither of the
        # two above. ``httpx.InvalidURL`` is not an ``HTTPError`` - a raw
        # control byte in a description reaches it through a ``urlsplit`` that
        # parses perfectly well - and one bad string in a tracker field would
        # otherwise discard every file on the issue and be logged as a defect
        # in this server. Only the class is logged: ``InvalidURL``'s message
        # embeds the URL this module has promised never to write down.
        _logger.warning("Fetching one Linear upload failed: %s", type(exc).__name__)
        return refused(AttachmentRefusalCode.FETCH_FAILED)

    sniffed = refine_container_mime(data, sniff_mime(data))
    if sniffed is None:
        # Nothing matched a magic number, and the two ways that happens need
        # different sentences. An expired signed URL answers 200 with an HTML
        # login page - a fetch that failed while looking exactly like one that
        # worked. Markdown, CSV and plain text also match no magic number, and
        # telling an agent that a spec "could not be retrieved" when the server
        # holds every byte of it sends whoever reads that line looking for a
        # network fault that never happened.
        return refused(
            AttachmentRefusalCode.FETCH_FAILED
            if _looks_like_html(data)
            else AttachmentRefusalCode.UNSUPPORTED_TYPE,
            bytes_read=len(data),
        )
    if sniffed not in accepted_mimes:
        return refused(AttachmentRefusalCode.UNSUPPORTED_TYPE, bytes_read=len(data))

    LINEAR_ATTACHMENT_FETCHES.labels(result=LINEAR_FETCH_OK).inc()
    return FileOutcome(
        display_name=item.display_name,
        origin_ref=origin_ref,
        fetched=FetchedFile(data=data, sniffed_mime=sniffed),
        bytes_read=len(data),
    )
