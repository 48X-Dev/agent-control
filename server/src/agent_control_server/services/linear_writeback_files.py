"""The file half of write-back: ``fileUpload``, the PUT, then ``attachmentCreate``.

Split from :mod:`.linear_writeback` the way the runtime was, so each module
stays one size and one subject. Plan ``agent-file-outputs.md`` sections 3 and 7.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from .linear_client import LinearError

_logger = logging.getLogger(__name__)

GraphQLPost = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
"""``HttpLinearWritebackClient._post``, so its error mapping is not duplicated."""

UPLOAD_CACHE_CONTROL = "public, max-age=31536000"
"""The cache header Linear's upload contract requires on the PUT."""

ATTACHMENTS_DISABLED_MESSAGE = (
    "Linear attachment write is disabled on this deployment."
)

_FILE_UPLOAD = """
mutation AgentControlFileUpload($contentType: String!, $filename: String!, $size: Int!) {
  fileUpload(contentType: $contentType, filename: $filename, size: $size) {
    success
    uploadFile { uploadUrl assetUrl headers { key value } }
  }
}
"""

_CREATE_ATTACHMENT = """
mutation AgentControlAttachmentCreate(
  $issueId: String!
  $title: String!
  $url: String!
  $subtitle: String
) {
  attachmentCreate(
    input: { issueId: $issueId, title: $title, url: $url, subtitle: $subtitle }
  ) {
    success
    attachment { id }
  }
}
"""

_UPLOAD_REJECTED_MESSAGE = "Linear refused to reserve an upload."
_PUT_FAILED_MESSAGE = "The file was written and could not be uploaded to Linear."
_ATTACH_REJECTED_MESSAGE = "Linear refused to attach the uploaded file."

_POINTER_HEADING = "**Files written by this agent**"
_POINTER_TITLE_MAX = 120


def upload_headers(raw: Any) -> dict[str, str]:
    """Linear returns ``headers`` as ``[{key, value}]``; the PUT needs a mapping."""
    if not isinstance(raw, list):
        return {}
    pairs: dict[str, str] = {}
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        key, value = entry.get("key"), entry.get("value")
        if isinstance(key, str) and key and isinstance(value, str):
            pairs[key] = value
    return pairs


class LinearFileWriter:
    """The two file mutations, over a caller's GraphQL post and HTTP client."""

    def __init__(
        self,
        *,
        post: GraphQLPost,
        client: httpx.AsyncClient,
        timeout_seconds: float,
    ) -> None:
        self._post = post
        self._client = client
        self._timeout_seconds = timeout_seconds

    async def file_upload(
        self, *, filename: str, content_type: str, content: bytes
    ) -> str:
        """Reserve an upload, PUT the bytes, return the durable asset URL."""
        data = await self._post(
            _FILE_UPLOAD,
            {
                "contentType": content_type,
                "filename": filename,
                # The true length, never a caller's claim about it.
                "size": len(content),
            },
        )
        payload = _mapping(data, "fileUpload")
        upload = _mapping(payload, "uploadFile")
        upload_url = _optional_str(upload.get("uploadUrl"))
        asset_url = _optional_str(upload.get("assetUrl"))
        if not payload.get("success") or upload_url is None or asset_url is None:
            raise LinearError(_UPLOAD_REJECTED_MESSAGE)
        await self._put(
            upload_url,
            content=content,
            content_type=content_type,
            returned=upload_headers(upload.get("headers")),
        )
        return asset_url

    async def create_attachment(
        self, *, issue_id: str, title: str, url: str, subtitle: str | None
    ) -> str:
        """Hang an asset on an issue. Linear updates a repeated ``(issue, url)``."""
        data = await self._post(
            _CREATE_ATTACHMENT,
            {"issueId": issue_id, "title": title, "url": url, "subtitle": subtitle},
        )
        payload = _mapping(data, "attachmentCreate")
        attachment_id = _optional_str(_mapping(payload, "attachment").get("id"))
        if not payload.get("success") or attachment_id is None:
            raise LinearError(_ATTACH_REJECTED_MESSAGE)
        return attachment_id

    async def _put(
        self,
        upload_url: str,
        *,
        content: bytes,
        content_type: str,
        returned: Mapping[str, str],
    ) -> None:
        # A signed storage URL, not Linear's API host: it carries the headers
        # the mutation handed back and never the API key.
        headers = {
            "Content-Type": content_type,
            "Cache-Control": UPLOAD_CACHE_CONTROL,
            **returned,
        }
        try:
            response = await self._client.put(
                upload_url,
                content=content,
                headers=headers,
                timeout=self._timeout_seconds,
            )
        except httpx.HTTPError as exc:
            _logger.warning("Linear upload PUT failed: %s", type(exc).__name__)
            raise LinearError(_PUT_FAILED_MESSAGE) from exc
        if response.status_code >= 400:
            _logger.warning(
                "Linear upload PUT returned HTTP %s.", response.status_code
            )
            raise LinearError(_PUT_FAILED_MESSAGE)


class LinearFileClient(Protocol):
    """The file half as :func:`push_agent_file` needs it, so tests substitute one."""

    async def file_upload(
        self, *, filename: str, content_type: str, content: bytes
    ) -> str: ...

    async def create_attachment(
        self, *, issue_id: str, title: str, url: str, subtitle: str | None
    ) -> str: ...


@dataclass(frozen=True)
class AgentFile:
    """One agent-authored file as the write-back sends it."""

    title: str
    filename: str
    content_type: str
    content: bytes
    subtitle: str | None = None
    asset_url: str | None = None


@dataclass(frozen=True)
class AgentFileDelivery:
    """What became of one file, so the comment can say which half failed."""

    title: str
    asset_url: str | None
    attachment_id: str | None
    error: str | None

    @property
    def delivered(self) -> bool:
        return self.attachment_id is not None


async def push_agent_file(
    client: LinearFileClient, *, issue_id: str, file: AgentFile
) -> AgentFileDelivery:
    """Upload then attach, reporting each failure instead of raising it.

    A failed attach keeps the asset URL, so a retry attaches rather than
    uploading a second copy.
    """
    asset_url = file.asset_url
    if asset_url is None:
        try:
            asset_url = await client.file_upload(
                filename=file.filename,
                content_type=file.content_type,
                content=file.content,
            )
        except LinearError as exc:
            return AgentFileDelivery(file.title, None, None, exc.message)
    try:
        attachment_id = await client.create_attachment(
            issue_id=issue_id,
            title=file.title,
            url=asset_url,
            subtitle=file.subtitle,
        )
    except LinearError as exc:
        return AgentFileDelivery(file.title, asset_url, None, exc.message)
    return AgentFileDelivery(file.title, asset_url, attachment_id, None)


def render_file_lines(deliveries: Sequence[AgentFileDelivery]) -> list[str]:
    """The pointer the comment carries in place of the file's contents."""
    if not deliveries:
        return []
    lines = [_POINTER_HEADING]
    for delivery in deliveries:
        name = _code_span(delivery.title)
        if delivery.delivered:
            lines.append(f"- {name} is attached to this issue.")
        else:
            reason = delivery.error or ATTACHMENTS_DISABLED_MESSAGE
            lines.append(f"- {name} was written and not delivered. {reason}")
    return lines


def _code_span(title: str) -> str:
    """A name inside a code span that the name itself cannot end."""
    cleaned = "".join(ch for ch in title if ch.isprintable() and ch != "`")
    return f"`{cleaned[:_POINTER_TITLE_MAX] or '(unnamed)'}`"


def _mapping(value: Any, key: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    nested = value.get(key)
    return nested if isinstance(nested, dict) else {}


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
