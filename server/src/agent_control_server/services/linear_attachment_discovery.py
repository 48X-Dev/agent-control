"""Which files an issue exposes, found by sweeping its text and deduped by URL.

Plan section 3.9. Nothing here opens a socket: this module turns one GraphQL
issue payload into a list of upload URLs, and :mod:`services.linear_attachments`
decides which of them are worth spending a fetch and a credential on.

**Ingress does not enumerate channels; it sweeps text and dedupes by URL.**
Measured across 43 issues on 2026-08-03: the ``attachments`` connection carries
6 distinct upload URLs, ``description`` bodies carry 6, and the overlap between
them is **zero**. ``documentContent.content`` returns the identical set to
``description`` because it is its rich-text rendering. Every ``Attachment`` row
in that workspace carries ``sourceType: "oauthClient"``, so that connection is
where *integrations* put files; a person dragging a file into an issue gets a
markdown link in the body and no ``Attachment`` row at all. A design that reads
only the structured connection therefore misses exactly the human-authored case
this feature exists for, and it fails silently - the query succeeds and returns
an empty list. That is not hypothetical: an agent asked to review a deck on
OPS-2 reported, correctly, that it could not fetch the upload URL, and answered
from the title.

So over-covering is the design and :func:`discover_files` reads every text field
the issue exposes. Dedup by URL is what makes that safe: an overlapping field is
harmless and a field nobody thought of is still covered.

**A link to any other host is not returned at all.** Not fetched, and not
reported either. The promise is "every file uploaded to Linear", never "every
file linked from Linear", and putting a tracker author's arbitrary URL into an
envelope is the other half of the same problem.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from agent_control_models.files import normalize_display_name

from ..config import LinearSettings

_MARKDOWN_LINK = re.compile(r"\[([^\]\n]{0,300})\]\(\s*(?P<url>[^\s()]+)\s*\)")

_URL_TAIL = r"[^\s\)\]\}\"'<>\x00-\x1f\x7f]+"
"""What may follow the host. Control characters are excluded rather than left
to the fetch, because ``urlsplit`` parses ``https://uploads.linear.app/a\\x00b``
into a perfectly good scheme and hostname and ``httpx`` then raises
``InvalidURL`` - which is not an ``HTTPError`` and would escape the fetch's own
handling."""

_BARE_URL_TRAILING = ".,;:!?"
"""Stripped from a URL that ended a sentence. ``See https://uploads.linear.app/a/b.``
otherwise yields a URL with the full stop on it, which 404s and produces a
``not_found`` line about a file that does not exist - and does not dedupe
against the same file linked properly elsewhere on the issue, so the count line
is inflated by one as well. Markdown links are delimited by their closing paren
and need none of this."""

UNNAMED_FILE = "attachment"
"""What a file with no readable name is called. A name whose every character was
a bidi override gets something boring rather than an empty quoted string."""


@dataclass(frozen=True, slots=True)
class DiscoveredFile:
    """One distinct upload URL, and where on the issue it was seen.

    ``order_key`` is what makes the per-issue cap deterministic: two reads of an
    unchanged issue must deliver the same files, or a resumed chain would hand
    its steps a different set than the one the earlier steps saw.
    """

    url: str
    display_name: str
    order_key: str
    source_type: str | None


def _upload_url_pattern(settings: LinearSettings) -> re.Pattern[str]:
    """Match an upload URL on an allowlisted host, either scheme.

    ``http`` is matched here and refused at the fetch rather than skipped in the
    sweep. A file linked over plain HTTP was still attached to the issue, and an
    agent told nothing about it is back in the failure this section exists for;
    told "this deployment will not fetch from there", it can ask a person.
    """
    hosts = "|".join(sorted(re.escape(host.lower()) for host in settings.attachment_host_allowlist))
    if not hosts:
        # An empty allowlist fetches nothing, so it finds nothing. A pattern
        # that matched everything here would hand the refusal path work it can
        # only refuse, and would report "12 files found, 0 delivered" for an
        # issue with no uploads at all.
        return re.compile(r"(?!)")
    return re.compile(rf"https?://(?:{hosts})/{_URL_TAIL}", re.IGNORECASE)


def discover_files(issue: dict[str, Any], *, settings: LinearSettings) -> list[DiscoveredFile]:
    """Every distinct upload URL this issue exposes, in a stable order.

    Two passes and one dedupe. The structured connection first, so a file that
    appears in both channels keeps the name and the id the connection gave it;
    then the text sweep over description, comment bodies and documentContent.
    Anything on a host outside the allowlist is not returned at all - it is not
    a file this server has any relationship with, and reporting it would put a
    tracker author's arbitrary URL into an envelope.
    """
    pattern = _upload_url_pattern(settings)
    seen: dict[str, DiscoveredFile] = {}

    for node in _nodes(issue.get("attachments")):
        url = node.get("url")
        if not isinstance(url, str) or not pattern.fullmatch(url):
            continue
        source_type = node.get("sourceType")
        attachment_id = node.get("id")
        seen.setdefault(
            url,
            DiscoveredFile(
                url=url,
                display_name=_readable_name(node.get("title"), url=url),
                order_key=f"0:{attachment_id if isinstance(attachment_id, str) else ''}",
                source_type=source_type if isinstance(source_type, str) else None,
            ),
        )

    for text in _text_fields(issue):
        for match in _MARKDOWN_LINK.finditer(text):
            url = match.group("url")
            if pattern.fullmatch(url):
                seen.setdefault(
                    url,
                    DiscoveredFile(
                        url=url,
                        display_name=_readable_name(match.group(1), url=url),
                        order_key=f"1:{url}",
                        source_type=None,
                    ),
                )
        for match in pattern.finditer(text):
            url = match.group(0).rstrip(_BARE_URL_TRAILING)
            if not pattern.fullmatch(url):
                continue
            seen.setdefault(
                url,
                DiscoveredFile(
                    url=url,
                    display_name=_readable_name(None, url=url),
                    order_key=f"1:{url}",
                    source_type=None,
                ),
            )

    return sorted(seen.values(), key=lambda found: (found.order_key, found.url))


def _text_fields(issue: dict[str, Any]) -> list[str]:
    """Every text field the issue exposes, without deciding which ones matter.

    Picking fields carefully is what produced six of twelve files and no error.
    Over-covering costs a regex pass over text this process has already read.
    """
    fields: list[str] = []
    description = issue.get("description")
    if isinstance(description, str):
        fields.append(description)
    content = issue.get("documentContent")
    if isinstance(content, dict) and isinstance(content.get("content"), str):
        fields.append(content["content"])
    for comment in _nodes(issue.get("comments")):
        body = comment.get("body")
        if isinstance(body, str):
            fields.append(body)
    return fields


def _nodes(connection: Any) -> list[dict[str, Any]]:
    if not isinstance(connection, dict):
        return []
    nodes = connection.get("nodes")
    if not isinstance(nodes, list):
        return []
    return [node for node in nodes if isinstance(node, dict)]


def _readable_name(raw: Any, *, url: str) -> str:
    """A display name from the tracker's text, or from the URL's last segment.

    Both are untrusted and both go through the shipped normalizer, which strips
    C0 and C1 controls, bidi overrides and path separators and caps at 128. A
    name is the one part of an attachment line a tracker author writes, and
    without this a file called ``x" delivered "`` forges the line describing it.
    """
    name, _ = normalize_display_name(raw)
    if name:
        return name
    tail = urlsplit(url).path.rsplit("/", 1)[-1]
    fallback, _ = normalize_display_name(tail)
    return fallback or UNNAMED_FILE


def origin_ref_for(found: DiscoveredFile) -> str:
    """A stable handle for one upload that is not its URL.

    The URL never leaves this module, so the audit row cannot carry one. The
    attachment id when the connection gave one, and otherwise a hash of the URL:
    both answer "is this the same file as last time" without being a thing
    anybody or anything downstream could dereference.
    """
    _, _, identifier = found.order_key.partition(":")
    if found.order_key.startswith("0:") and identifier:
        return identifier[:128]
    return f"sha256:{hashlib.sha256(found.url.encode('utf-8')).hexdigest()[:48]}"


