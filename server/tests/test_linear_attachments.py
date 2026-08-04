"""Plan section 3.9: finding a Linear issue's files, and the gates on fetching them.

Two halves, and they fail differently. Discovery is a sweep over text somebody
typed into a tracker, and its failure is *under*-covering: measured across 43
issues on 2026-08-03, the ``attachments`` connection carried 6 distinct upload
URLs, ``description`` bodies carried 6, and the overlap was zero. A design that
reads only the structured connection returns half the corpus and no error.

The fetch is the other half and its failure is a credential leak. An attachment
URL is a string that arrived in tracker data and the Linear API key is
server-held, so every test below about scheme, host and redirects is about
keeping those two apart.
"""

from __future__ import annotations

import logging

import httpx
import pytest
from agent_control_models.attachments import AttachmentRefusalCode

from agent_control_server.config import LinearSettings
from agent_control_server.services.linear_attachment_discovery import (
    discover_files,
    origin_ref_for,
)
from agent_control_server.services.linear_attachments import (
    DownloadRefusedError,
    HttpLinearAttachmentClient,
    collect_issue_files,
)

API_KEY = "lin_api_test"

UPLOAD = "https://uploads.linear.app/3387082b-0000-0000-0000-000000000000"
DECK = f"{UPLOAD}/e47010e1-0000-0000-0000-000000000001"
SPEC = f"{UPLOAD}/e47010e1-0000-0000-0000-000000000002"
NOTES = f"{UPLOAD}/e47010e1-0000-0000-0000-000000000003"
ELSEWHERE = "https://drive.google.com/file/d/abc/view"

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
PDF = b"%PDF-1.7\n" + b"x" * 64


def _settings(**overrides: object) -> LinearSettings:
    return LinearSettings(api_key="lin_api_test", **overrides)  # type: ignore[arg-type]


def _issue(**overrides: object) -> dict[str, object]:
    issue: dict[str, object] = {
        "id": "OPS-2",
        "description": "",
        "attachments": {"nodes": []},
        "comments": {"nodes": []},
        "documentContent": None,
    }
    issue.update(overrides)
    return issue


# --- discovery --------------------------------------------------------------


def test_the_deck_on_ops_2_is_found_in_the_body_with_no_attachment_row() -> None:
    """The measured case, and the reason this module exists.

    OPS-2 carries ``attachments: 0``. Its deck reaches the issue only as a
    markdown link in ``description``, because a person dragging a file into an
    issue gets a link and no ``Attachment`` row at all.
    """
    found = discover_files(
        _issue(description=f"Please review [EarlyCore_MSSP_Deck (1).pptx]({DECK})"),
        settings=_settings(),
    )

    assert [item.url for item in found] == [DECK]
    assert found[0].display_name == "EarlyCore_MSSP_Deck (1).pptx"


def test_every_text_field_is_swept_and_the_union_is_deduped_by_url() -> None:
    """Over-covering is the design. ``documentContent`` is the rich-text
    rendering of ``description`` and returns the identical set, which is exactly
    why dedup by URL is the mechanism rather than careful field selection."""
    found = discover_files(
        _issue(
            description=f"deck [d]({DECK})",
            documentContent={"content": f"deck [d]({DECK})"},
            comments={"nodes": [{"body": f"and the spec {SPEC}"}]},
            attachments={"nodes": [{"id": "att-1", "url": NOTES, "title": "notes.pdf"}]},
        ),
        settings=_settings(),
    )

    assert sorted(item.url for item in found) == sorted([DECK, SPEC, NOTES])


def test_a_link_to_any_other_host_is_not_returned_at_all() -> None:
    """Never fetched, and never reported either. Putting a tracker author's
    arbitrary URL into an envelope is the other half of the same problem."""
    found = discover_files(
        _issue(description=f"see [drive]({ELSEWHERE}) and [deck]({DECK})"),
        settings=_settings(),
    )

    assert [item.url for item in found] == [DECK]


def test_a_lookalike_host_does_not_match_the_allowlist() -> None:
    found = discover_files(
        _issue(description="https://uploads.linear.app.evil.test/a/b"),
        settings=_settings(),
    )

    assert found == []


def test_the_structured_connection_keeps_its_name_when_a_body_repeats_the_url() -> None:
    found = discover_files(
        _issue(
            description=f"[whatever the body called it]({DECK})",
            attachments={"nodes": [{"id": "att-9", "url": DECK, "title": "deck.pdf"}]},
        ),
        settings=_settings(),
    )

    assert len(found) == 1
    assert found[0].display_name == "deck.pdf"
    assert origin_ref_for(found[0]) == "att-9"


def test_the_order_is_stable_so_a_cap_picks_the_same_files_twice() -> None:
    """Two reads of an unchanged issue must deliver the same three files, or a
    resumed chain hands its steps a different set than the earlier steps saw."""
    issue = _issue(description=f"{NOTES} {DECK} {SPEC}")
    first = [item.url for item in discover_files(issue, settings=_settings())]
    second = [item.url for item in discover_files(issue, settings=_settings())]

    assert first == second


def test_a_name_made_entirely_of_bidi_overrides_gets_a_boring_one() -> None:
    found = discover_files(
        _issue(description=f"[‮‭]({DECK})"), settings=_settings()
    )

    assert found[0].display_name not in ("", "‮‭")


# --- the fetch --------------------------------------------------------------


def _client(handler: object, **overrides: object) -> HttpLinearAttachmentClient:
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    return HttpLinearAttachmentClient(
        api_key="lin_api_test",
        settings=_settings(**overrides),
        client=httpx.AsyncClient(transport=transport, follow_redirects=False),
    )


async def test_plain_http_on_an_allowlisted_host_costs_no_request() -> None:
    """A host allowlist alone does not exclude ``http://``, and the credential
    would then be on the wire in cleartext."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=PDF)

    client = _client(handler)
    with pytest.raises(DownloadRefusedError) as raised:
        await client.download(DECK.replace("https://", "http://"), max_bytes=1000)

    assert raised.value.code is AttachmentRefusalCode.BLOCKED_HOST
    assert requests == []
    await client.aclose()


async def test_a_host_outside_the_allowlist_costs_no_request() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=PDF)

    client = _client(handler)
    with pytest.raises(DownloadRefusedError):
        await client.download(ELSEWHERE, max_bytes=1000)

    assert requests == []
    await client.aclose()


async def test_a_redirect_off_the_allowlist_refuses_rather_than_retrying_anonymously() -> None:
    """A file worth having is not worth a credential, and an anonymous retry
    would be this server deciding the file matters more.

    Proof by absence, plan section 11's L2. The assertion is over every request
    the transport recorded and not over the second one: "the second request did
    not carry the key" is a weaker claim than "no request did", and the second
    request is the one that was never made.
    """
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "uploads.linear.app":
            return httpx.Response(302, headers={"Location": "https://evil.test/take-this"})
        return httpx.Response(200, content=b"the body the second host would have returned")

    client = _client(handler)
    with pytest.raises(DownloadRefusedError) as raised:
        await client.download(DECK, max_bytes=10_000)

    assert raised.value.code is AttachmentRefusalCode.BLOCKED_HOST
    assert [str(request.url) for request in requests] == [DECK]
    assert not [
        request
        for request in requests
        if request.url.host != "uploads.linear.app"
    ]
    assert not [
        request
        for request in requests
        if request.url.host != "uploads.linear.app"
        and API_KEY in " ".join(request.headers.values())
    ]
    await client.aclose()


async def test_no_request_to_a_host_off_the_allowlist_is_ever_made() -> None:
    """Proof by absence, and the assertion that matters most in this file.

    A gate that fetched and then discarded returns exactly the same refusal to
    a caller, and it would have spent the bytes and put the server-held API key
    in front of whatever host a tracker author typed. Only the transport can
    tell the two designs apart.
    """
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=PDF)

    client = _client(handler)
    for target in (
        ELSEWHERE,
        "https://uploads.linear.app.evil.test/a/b",
        "http://uploads.linear.app/a/b",
        "https://127.0.0.1/a/b",
        "file:///etc/passwd",
    ):
        with pytest.raises(DownloadRefusedError) as raised:
            await client.download(target, max_bytes=10_000)
        assert raised.value.code is AttachmentRefusalCode.BLOCKED_HOST, target

    assert requests == []
    await client.aclose()


async def test_a_redirect_inside_the_allowlist_is_followed_and_bounded() -> None:
    hops: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hops.append(request.url.path)
        if len(hops) <= 3:
            return httpx.Response(302, headers={"Location": f"{UPLOAD}/hop-{len(hops)}"})
        return httpx.Response(200, content=PDF)

    client = _client(handler, attachment_max_redirects=2)
    with pytest.raises(DownloadRefusedError):
        await client.download(DECK, max_bytes=10_000)

    assert len(hops) == 3
    await client.aclose()


async def test_the_stream_is_aborted_past_the_ceiling_rather_than_read_whole() -> None:
    """``Content-Length`` decides nothing: a server can understate it, and an
    ``Attachment`` carries neither a size nor a content type to check against."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"Content-Length": "10"}, content=b"x" * 100_000
        )

    client = _client(handler)
    with pytest.raises(DownloadRefusedError) as raised:
        await client.download(DECK, max_bytes=1024)

    assert raised.value.code is AttachmentRefusalCode.TOO_LARGE
    await client.aclose()


async def test_a_missing_file_is_not_a_failed_fetch() -> None:
    client = _client(lambda request: httpx.Response(404))
    with pytest.raises(DownloadRefusedError) as raised:
        await client.download(DECK, max_bytes=1024)

    assert raised.value.code is AttachmentRefusalCode.NOT_FOUND
    await client.aclose()


async def test_the_url_is_never_logged(caplog: pytest.LogCaptureFixture) -> None:
    """This module logs only the status. ``httpx`` logs the whole request line
    at INFO from its own logger, and the root level here is INFO by default, so
    without :mod:`services.linear_attachment_logging` every attachment URL a
    step fetched would sit in the deployment's log. The host is configuration
    and stays; the path names somebody's file and goes."""
    client = _client(lambda request: httpx.Response(500))
    with caplog.at_level(logging.DEBUG), pytest.raises(DownloadRefusedError):
        await client.download(DECK, max_bytes=1024)

    assert DECK not in caplog.text
    assert "e47010e1" not in caplog.text
    assert "[redacted]" in caplog.text
    await client.aclose()


# --- the union, its ceilings, and the reconciliation ------------------------


class FakeClient:
    """A :class:`LinearAttachmentClient` that answers from a dict."""

    def __init__(self, issue: dict[str, object], bodies: dict[str, bytes]) -> None:
        self.issue = issue
        self.bodies = bodies
        self.downloaded: list[str] = []

    async def read_issue(self, issue_ref: str) -> dict[str, object]:
        return self.issue

    async def download(self, url: str, *, max_bytes: int) -> bytes:
        self.downloaded.append(url)
        body = self.bodies.get(url, b"")
        if len(body) > max_bytes:
            raise DownloadRefusedError(AttachmentRefusalCode.TOO_LARGE)
        return body

    async def aclose(self) -> None:
        return None


async def _collect(client: FakeClient, **overrides: object):
    return await collect_issue_files(
        client,  # type: ignore[arg-type]
        issue_ref="OPS-2",
        settings=_settings(**overrides),
        max_bytes_per_file=overrides.pop("max_bytes_per_file", 10_000),  # type: ignore[arg-type]
        remaining_task_bytes=overrides.pop("remaining_task_bytes", 10_000),  # type: ignore[arg-type]
        accepted_mimes={"application/pdf", "image/png"},
    )


async def test_found_is_reconciled_against_delivered_rather_than_assumed() -> None:
    """Silent under-delivery is the failure this whole section exists to
    prevent: an agent told nothing was attached answers from the title."""
    client = FakeClient(
        _issue(description=f"{DECK} {SPEC} {NOTES}"),
        {DECK: PDF, SPEC: PNG, NOTES: b"<!DOCTYPE html><html>login</html>"},
    )
    files = await _collect(client)

    assert files.found == 3
    assert sum(1 for outcome in files.outcomes if outcome.fetched is not None) == 2
    refusals = [outcome.refusal for outcome in files.outcomes if outcome.fetched is None]
    assert refusals == [AttachmentRefusalCode.FETCH_FAILED]


async def test_the_per_issue_cap_narrows_what_is_attempted_and_not_what_is_counted() -> None:
    client = FakeClient(
        _issue(description=f"{DECK} {SPEC} {NOTES}"), {DECK: PDF, SPEC: PDF, NOTES: PDF}
    )
    files = await _collect(client, attachments_max_per_issue=1)

    assert files.found == 3
    assert len(files.outcomes) == 1
    assert len(client.downloaded) == 1


async def test_a_body_with_no_magic_number_that_is_not_html_is_a_type_refusal() -> None:
    """Markdown and CSV match no magic number. Telling an agent that a spec
    "could not be retrieved" when the server holds every byte of it sends
    whoever reads that line after a fault that never happened."""
    client = FakeClient(_issue(description=DECK), {DECK: b"# A spec\n\nWith prose."})
    files = await _collect(client)

    assert files.outcomes[0].refusal is AttachmentRefusalCode.UNSUPPORTED_TYPE


async def test_the_task_budget_refuses_with_its_own_code_and_not_too_large() -> None:
    """A body aborted at a ceiling the task budget set is not an oversized file,
    and telling an agent it was sends them looking for a file that is not big."""
    client = FakeClient(_issue(description=DECK), {DECK: PDF})
    files = await _collect(client, remaining_task_bytes=4)

    assert files.outcomes[0].refusal is AttachmentRefusalCode.OVER_TASK_BUDGET




# --- what one bad string in a description costs -----------------------------


def test_a_control_character_never_reaches_the_fetch_from_a_description() -> None:
    """The first of two defences, and the cheaper one.

    ``urlsplit`` parses ``https://uploads.linear.app/a\x00b`` into a perfectly
    good scheme and hostname, so the allowlist passes it and ``httpx`` raises
    ``InvalidURL`` at the request. The sweep excludes control bytes so the
    request is never attempted with one.
    """
    found = discover_files(
        _issue(description=f"{UPLOAD}/a\x00b"), settings=_settings()
    )

    assert [item.url for item in found] == [f"{UPLOAD}/a"]
    assert not any(char in item.url for item in found for char in "\x00\x1f\x7f")


async def test_one_unfetchable_url_does_not_discard_every_file_on_the_issue() -> None:
    """The second defence, and the one that matters if the sweep ever widens.

    ``httpx.InvalidURL`` is **not** a subclass of ``httpx.HTTPError`` - checked
    against the installed 0.28.1 - so a handler catching only ``HTTPError``,
    ``TimeoutError`` and ``DownloadRefusedError`` lets it escape ``_attempt``,
    escape ``collect_issue_files`` whose contract says it never raises for a
    file, and reach the endpoint's catch-all. One string in a tracker field
    would then silence the whole feature for that issue and be logged as a
    defect in this server rather than reported as a refusal to the agent.
    """

    class OneBadUrl(FakeClient):
        async def download(self, url: str, *, max_bytes: int) -> bytes:
            self.downloaded.append(url)
            if url == SPEC:
                raise httpx.InvalidURL(f"Invalid non-printable ASCII character in URL {url}")
            return self.bodies[url]

    client = OneBadUrl(_issue(description=f"{DECK} {SPEC}"), {DECK: PDF})

    files = await _collect(client, attachments_max_per_issue=2)

    assert files.found == 2
    delivered = [outcome for outcome in files.outcomes if outcome.fetched is not None]
    assert [outcome.display_name for outcome in delivered] == [
        DECK.rsplit("/", 1)[-1]
    ]
    refused = [outcome for outcome in files.outcomes if outcome.fetched is None]
    assert [outcome.refusal for outcome in refused] == [
        AttachmentRefusalCode.FETCH_FAILED
    ]


async def test_the_url_inside_an_unfetchable_error_is_never_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``InvalidURL``'s own message embeds the URL, so the handler logs the
    exception class and nothing else - the rule ``linear_issues.py:_post``
    already keeps."""

    class OneBadUrl(FakeClient):
        async def download(self, url: str, *, max_bytes: int) -> bytes:
            raise httpx.InvalidURL(f"Invalid non-printable ASCII character in URL {url}")

    with caplog.at_level(logging.DEBUG):
        await _collect(OneBadUrl(_issue(description=DECK), {}))

    assert DECK not in caplog.text
    assert "e47010e1" not in caplog.text
    assert "InvalidURL" in caplog.text


async def test_a_broken_location_header_is_a_refusal_and_not_a_raise() -> None:
    """``urljoin`` raises ``ValueError: Invalid IPv6 URL`` for ``//[bad``, which
    is neither an ``httpx`` error nor a refusal, and would escape the same way."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "//[bad"})

    client = _client(handler)
    with pytest.raises(DownloadRefusedError) as raised:
        await client.download(DECK, max_bytes=10_000)

    assert raised.value.code is AttachmentRefusalCode.FETCH_FAILED
    await client.aclose()


def test_a_url_ending_a_sentence_does_not_swallow_the_full_stop() -> None:
    """Otherwise the same file linked twice on one issue does not dedupe, the
    count line reads "1 of 2", and the extra row describes a file that 404s
    because it does not exist."""
    found = discover_files(
        _issue(description=f"The deck is at {DECK}. See also [deck]({DECK})"),
        settings=_settings(),
    )

    assert [item.url for item in found] == [DECK]


# --- what the byte ceiling actually bounds -----------------------------------


class CountingClient(FakeClient):
    """A client that aborts past the ceiling the way the real one does."""

    async def download(self, url: str, *, max_bytes: int) -> bytes:
        self.downloaded.append(url)
        body = self.bodies.get(url, b"")
        if len(body) > max_bytes:
            raise DownloadRefusedError(
                AttachmentRefusalCode.TOO_LARGE, bytes_read=max_bytes + 1
            )
        return body


async def test_a_body_aborted_at_the_ceiling_is_charged_to_the_task_budget() -> None:
    """``.env.example`` sells this ceiling as bounding bytes fetched with nobody
    in the room. Charged only on success it bounds close to nothing: three
    twenty-megabyte refusals a step, twelve steps, against a forty-megabyte
    ceiling that never fires."""
    client = CountingClient(
        _issue(description=f"{DECK} {SPEC}"), {DECK: b"x" * 5_000, SPEC: PDF}
    )

    files = await _collect(client, max_bytes_per_file=1_000, remaining_task_bytes=1_001)

    assert files.outcomes[0].refusal is AttachmentRefusalCode.TOO_LARGE
    assert files.outcomes[0].bytes_read > 0
    # The second file is small and acceptable; it is refused because the first
    # one's aborted body already spent the task's remaining bytes.
    assert files.outcomes[1].refusal is AttachmentRefusalCode.OVER_TASK_BUDGET
    assert files.outcomes[1].fetched is None


async def test_a_login_page_costs_the_bytes_it_took_to_read_it() -> None:
    """An expired signed URL answers 200 with markup. Those bytes crossed the
    wire and a ceiling that ignored them would let an attachment-heavy
    milestone pull an order of magnitude past it."""
    body = b"<!DOCTYPE html><html>" + b"login" * 200
    client = FakeClient(_issue(description=DECK), {DECK: body})

    files = await _collect(client)

    assert files.outcomes[0].refusal is AttachmentRefusalCode.FETCH_FAILED
    assert files.outcomes[0].bytes_read == len(body)


async def test_a_tracker_that_cannot_be_read_says_so_rather_than_reporting_zero() -> None:
    """``found == 0`` is what an issue with no files looks like. A read that
    failed has to be a third state, or the envelope tells an agent positively
    that nothing is attached on the day Linear was rate-limiting."""

    class Broken(FakeClient):
        async def read_issue(self, issue_ref: str) -> dict[str, object]:
            from agent_control_server.services.linear_attachments import (
                LinearAttachmentError,
            )

            raise LinearAttachmentError("Linear could not be reached.")

    files = await _collect(Broken(_issue(), {}))

    assert files.read_failed is True
    assert (files.found, files.outcomes) == (0, ())


async def test_an_issue_that_reads_clean_and_has_nothing_is_not_a_read_failure() -> None:
    files = await _collect(FakeClient(_issue(), {}))

    assert files.read_failed is False
    assert files.found == 0


# --- the credential, asserted by absence ------------------------------------


async def test_the_api_key_is_in_no_part_of_what_the_step_path_receives() -> None:
    """The key is server-held and the dispatcher receives keys and codes.

    Asserted over the whole returned structure rather than over one field: a
    key that reached a display name, an origin ref or a refusal string would be
    on its way into an envelope, and every one of those is a plausible place
    for it to arrive by accident.
    """
    client = FakeClient(_issue(description=f"{DECK} {SPEC}"), {DECK: PDF, SPEC: PNG})

    files = await _collect(client)

    assert API_KEY not in repr(files)
    assert not any(API_KEY in outcome.display_name for outcome in files.outcomes)
    assert not any(API_KEY in outcome.origin_ref for outcome in files.outcomes)
