"""The Linear half of agent file outputs, proven without a network.

The tripwire Linear's own documentation names is here first: ``headers`` comes
back as an array of ``{key, value}`` and the PUT needs a mapping. The rest is
the two failure modes plan section 7 refuses to let fail silently - a reserved
upload whose PUT died, and a delivered asset whose attach died - plus the flag
that must be off in a checkout and reach the process in all three wiring files.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import httpx
import pytest
from agent_control_models.tasks import WRITEBACK_BODY_MAX_LENGTH
from pydantic import SecretStr

from agent_control_server.config import (
    check_linear_attachment_write_state,
    linear_settings,
)
from agent_control_server.services.linear_client import LinearError
from agent_control_server.services.linear_writeback import (
    HttpLinearWritebackClient,
    compose_comment_body,
)
from agent_control_server.services.linear_writeback_files import (
    UPLOAD_CACHE_CONTROL,
    AgentFile,
    AgentFileDelivery,
    push_agent_file,
    render_file_lines,
    upload_headers,
)
from agent_control_server.services.linear_writeback_runtime import (
    WritebackRuntime,
    build_writeback_runtime,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

UPLOAD_URL = "https://uploads.linear.example/signed/abc"
ASSET_URL = "https://uploads.linear.app/asset/abc/shortlist.xlsx"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

FILE = AgentFile(
    title="q3-investor-shortlist.xlsx",
    filename="q3-investor-shortlist.xlsx",
    content_type=XLSX,
    content=b"PK\x03\x04 twelve",
)


def _upload_payload(headers: list[dict[str, str]] | Any) -> dict[str, Any]:
    return {
        "data": {
            "fileUpload": {
                "success": True,
                "uploadFile": {
                    "uploadUrl": UPLOAD_URL,
                    "assetUrl": ASSET_URL,
                    "headers": headers,
                },
            }
        }
    }


def _client(handler: Any) -> tuple[HttpLinearWritebackClient, httpx.AsyncClient]:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return (
        HttpLinearWritebackClient(
            api_key="lin_api_test",
            api_url="https://linear.example/graphql",
            client=http,
        ),
        http,
    )


# ---------------------------------------------------------------------------
# The header array, which is the documented tripwire
# ---------------------------------------------------------------------------


def test_the_returned_header_array_becomes_a_mapping_with_every_key_kept() -> None:
    """Linear returns ``[{key, value}]``. A two-element array is two keys."""
    mapping = upload_headers(
        [
            {"key": "x-amz-meta-one", "value": "first"},
            {"key": "x-amz-server-side-encryption", "value": "AES256"},
        ]
    )

    assert mapping == {
        "x-amz-meta-one": "first",
        "x-amz-server-side-encryption": "AES256",
    }


@pytest.mark.parametrize(
    "raw",
    [None, {}, {"x-one": "first"}, ["x-one"], [{"key": "", "value": "v"}], [{"v": 1}]],
)
def test_a_header_shape_that_is_not_the_documented_array_yields_no_headers(
    raw: Any,
) -> None:
    """Including a mapping: reading one as an array is the same bug inverted."""
    assert upload_headers(raw) == {}


async def test_the_put_carries_the_content_type_the_cache_rule_and_every_header() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            seen["url"] = str(request.url)
            seen["headers"] = dict(request.headers)
            seen["body"] = request.content
            return httpx.Response(200)
        seen["variables"] = json.loads(request.content)["variables"]
        return httpx.Response(
            200,
            json=_upload_payload(
                [
                    {"key": "x-amz-meta-one", "value": "first"},
                    {"key": "x-amz-acl", "value": "private"},
                ]
            ),
        )

    client, http = _client(handler)
    try:
        asset_url = await client.file_upload(
            filename=FILE.filename, content_type=FILE.content_type, content=FILE.content
        )
    finally:
        await http.aclose()

    assert asset_url == ASSET_URL
    assert seen["url"] == UPLOAD_URL
    assert seen["body"] == FILE.content
    assert seen["headers"]["content-type"] == XLSX
    assert seen["headers"]["cache-control"] == UPLOAD_CACHE_CONTROL
    assert seen["headers"]["x-amz-meta-one"] == "first"
    assert seen["headers"]["x-amz-acl"] == "private"
    # The signed URL is not Linear's API host and must not carry its key.
    assert "authorization" not in seen["headers"]
    assert seen["variables"] == {
        "contentType": XLSX,
        "filename": FILE.filename,
        "size": len(FILE.content),
    }


async def test_the_size_sent_is_the_true_byte_count() -> None:
    """Whether Linear enforces it is unverified; the client does not guess."""
    sizes: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            return httpx.Response(200)
        sizes.append(json.loads(request.content)["variables"]["size"])
        return httpx.Response(200, json=_upload_payload([]))

    client, http = _client(handler)
    try:
        await client.file_upload(
            filename="a.xlsx", content_type=XLSX, content=b"x" * 4097
        )
    finally:
        await http.aclose()

    assert sizes == [4097]


# ---------------------------------------------------------------------------
# The two failure modes section 7 refuses to let fail silently
# ---------------------------------------------------------------------------


async def test_a_refused_reservation_raises_and_never_reaches_a_put() -> None:
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        return httpx.Response(200, json={"data": {"fileUpload": {"success": False}}})

    client, http = _client(handler)
    try:
        with pytest.raises(LinearError):
            await client.file_upload(
                filename="a.xlsx", content_type=XLSX, content=b"x"
            )
    finally:
        await http.aclose()

    assert methods == ["POST"]


async def test_a_reserved_upload_whose_put_fails_raises_rather_than_returning_a_url() -> None:
    """Returning ``assetUrl`` here would record a delivery that never happened."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            return httpx.Response(500)
        return httpx.Response(200, json=_upload_payload([]))

    client, http = _client(handler)
    try:
        with pytest.raises(LinearError) as raised:
            await client.file_upload(
                filename="a.xlsx", content_type=XLSX, content=b"x"
            )
    finally:
        await http.aclose()

    assert "could not be uploaded" in raised.value.message


async def test_a_transport_failure_on_the_put_names_the_upload_not_the_api() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(200, json=_upload_payload([]))

    client, http = _client(handler)
    try:
        with pytest.raises(LinearError) as raised:
            await client.file_upload(
                filename="a.xlsx", content_type=XLSX, content=b"x"
            )
    finally:
        await http.aclose()

    assert "could not be uploaded" in raised.value.message


async def test_create_attachment_returns_the_id_and_refuses_a_failed_mutation() -> None:
    payloads = [
        {"data": {"attachmentCreate": {"success": True, "attachment": {"id": "att-1"}}}},
        {"data": {"attachmentCreate": {"success": False, "attachment": None}}},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payloads.pop(0))

    client, http = _client(handler)
    try:
        assert (
            await client.create_attachment(
                issue_id="issue-1", title="t", url=ASSET_URL, subtitle=None
            )
            == "att-1"
        )
        with pytest.raises(LinearError):
            await client.create_attachment(
                issue_id="issue-1", title="t", url=ASSET_URL, subtitle=None
            )
    finally:
        await http.aclose()


# ---------------------------------------------------------------------------
# Delivery: which half failed decides what the retry does
# ---------------------------------------------------------------------------


class FakeFileClient:
    """Linear's file half, including its idempotency on ``(issueId, url)``."""

    def __init__(self) -> None:
        self.uploads: list[str] = []
        self.attachments: dict[tuple[str, str], dict[str, Any]] = {}
        self.upload_error: str | None = None
        self.attach_error: str | None = None

    async def file_upload(
        self, *, filename: str, content_type: str, content: bytes
    ) -> str:
        if self.upload_error is not None:
            raise LinearError(self.upload_error)
        self.uploads.append(filename)
        return ASSET_URL

    async def create_attachment(
        self, *, issue_id: str, title: str, url: str, subtitle: str | None
    ) -> str:
        if self.attach_error is not None:
            raise LinearError(self.attach_error)
        # Linear updates the original rather than adding a second row.
        self.attachments[(issue_id, url)] = {"title": title, "subtitle": subtitle}
        return f"att-{list(self.attachments).index((issue_id, url))}"


async def test_a_failed_upload_records_no_asset_url_and_never_attaches() -> None:
    fake = FakeFileClient()
    fake.upload_error = "Linear could not be reached."

    delivery = await push_agent_file(fake, issue_id="issue-1", file=FILE)

    assert delivery.asset_url is None
    assert delivery.delivered is False
    assert delivery.error == "Linear could not be reached."
    assert fake.attachments == {}


async def test_a_failed_attach_keeps_the_asset_url_the_upload_earned() -> None:
    """Section 7: the retry attaches again rather than uploading a second copy."""
    fake = FakeFileClient()
    fake.attach_error = "Linear rejected the request."

    delivery = await push_agent_file(fake, issue_id="issue-1", file=FILE)

    assert delivery.asset_url == ASSET_URL
    assert delivery.delivered is False
    assert delivery.error == "Linear rejected the request."

    fake.attach_error = None
    retry = await push_agent_file(
        fake,
        issue_id="issue-1",
        file=AgentFile(**{**FILE.__dict__, "asset_url": delivery.asset_url}),
    )

    assert retry.delivered is True
    assert fake.uploads == [FILE.filename], "the retry uploaded a second copy"


async def test_a_write_back_run_twice_leaves_one_attachment_not_two() -> None:
    """``attachmentCreate`` is idempotent on ``(issueId, url)``, so the comment
    dedupe machinery has no counterpart here and none is built."""
    fake = FakeFileClient()

    first = await push_agent_file(fake, issue_id="issue-1", file=FILE)
    second = await push_agent_file(
        fake,
        issue_id="issue-1",
        file=AgentFile(**{**FILE.__dict__, "asset_url": first.asset_url}),
    )

    assert first.delivered and second.delivered
    assert len(fake.attachments) == 1
    assert first.attachment_id == second.attachment_id


# ---------------------------------------------------------------------------
# The comment carries a pointer, not the file
# ---------------------------------------------------------------------------


def test_a_delivered_file_and_an_undelivered_one_read_differently() -> None:
    lines = render_file_lines(
        [
            AgentFileDelivery("shortlist.xlsx", ASSET_URL, "att-1", None),
            AgentFileDelivery("notes.docx", None, None, "Linear could not be reached."),
        ]
    )

    assert lines[0] == "**Files written by this agent**"
    assert lines[1] == "- `shortlist.xlsx` is attached to this issue."
    assert lines[2] == (
        "- `notes.docx` was written and not delivered. Linear could not be reached."
    )


def test_a_backtick_in_a_name_cannot_end_the_code_span_around_it() -> None:
    lines = render_file_lines(
        [AgentFileDelivery("ev`il`.xlsx", ASSET_URL, "att-1", None)]
    )

    assert lines[1].count("`") == 2
    assert "`evil.xlsx`" in lines[1]


def test_no_deliveries_render_no_pointer_at_all() -> None:
    assert render_file_lines([]) == []


def test_the_comment_carries_the_pointer_and_the_cap_does_not_move() -> None:
    """Plan section 1 withdraws raising the cap: the file is the destination."""
    body = compose_comment_body(
        task_key="a" * 32,
        step_index=0,
        total_steps=1,
        agent_name="researcher",
        output_text="Shortlist attached.",
        file_lines=render_file_lines(
            [AgentFileDelivery("shortlist.xlsx", ASSET_URL, "att-1", None)]
        ),
    )

    assert WRITEBACK_BODY_MAX_LENGTH == 4000
    assert "- `shortlist.xlsx` is attached to this issue." in body.split("\n")
    assert len(body) < 500, "the comment is a pointer, not a wall of markdown"


# ---------------------------------------------------------------------------
# The flag: off, separate, and reaching the process
# ---------------------------------------------------------------------------


def test_the_attachment_write_flag_defaults_off_and_reaches_the_process() -> None:
    """All three wiring files in the same commit, or the setting exists and
    never arrives. Removing any one of these lines fails this test."""
    assert linear_settings.attachments_write_enabled is False

    compose = (REPO_ROOT / "docker-compose.yml").read_text()
    assert (
        "AGENT_CONTROL_LINEAR_ATTACHMENTS_WRITE_ENABLED: "
        "${AGENT_CONTROL_LINEAR_ATTACHMENTS_WRITE_ENABLED:-false}" in compose
    )

    apple = (REPO_ROOT / "scripts" / "apple-container-up.sh").read_text()
    assert (
        'AGENT_CONTROL_LINEAR_ATTACHMENTS_WRITE_ENABLED='
        '${AGENT_CONTROL_LINEAR_ATTACHMENTS_WRITE_ENABLED:-false}' in apple
    )

    env_example = (REPO_ROOT / "server" / ".env.example").read_text()
    assert "AGENT_CONTROL_LINEAR_ATTACHMENTS_WRITE_ENABLED=false" in env_example


def test_the_two_write_flags_gate_separately(monkeypatch: pytest.MonkeyPatch) -> None:
    """An operator who accepted a comment has not thereby accepted an upload."""
    some_client: Any = object()
    on = WritebackRuntime(
        client=some_client,
        resolver=None,
        write_enabled=False,
        attachments_write_enabled=True,
    )
    assert on.can_write is False
    assert on.can_write_attachments is True

    off = WritebackRuntime(client=some_client, resolver=None, write_enabled=True)
    assert off.can_write is True
    assert off.can_write_attachments is False, "the comment flag opened the file half"

    monkeypatch.setattr(linear_settings, "attachments_write_enabled", True)
    monkeypatch.setattr(linear_settings, "api_key", SecretStr(""))
    assert build_writeback_runtime().can_write_attachments is False


async def test_the_flag_off_says_so_rather_than_dropping_the_file() -> None:
    fake = FakeFileClient()
    runtime = WritebackRuntime(client=fake, resolver=None, write_enabled=True)

    delivery = await runtime.deliver_agent_file(issue_id="issue-1", file=FILE)

    assert delivery.delivered is False
    assert delivery.error is not None and "disabled" in delivery.error
    assert fake.uploads == [] and fake.attachments == {}
    assert render_file_lines([delivery])[1].endswith(delivery.error)


async def test_the_flag_on_pushes_the_file() -> None:
    fake = FakeFileClient()
    runtime = WritebackRuntime(
        client=fake,
        resolver=None,
        write_enabled=False,
        attachments_write_enabled=True,
    )

    delivery = await runtime.deliver_agent_file(issue_id="issue-1", file=FILE)

    assert delivery.delivered is True
    assert fake.uploads == [FILE.filename]


@pytest.mark.parametrize(
    ("attachments", "write", "key", "expected"),
    [
        (False, False, "", 0),
        (False, True, "lin_api_x", 0),
        (True, True, "", 1),
        (True, False, "lin_api_x", 1),
        (True, True, "lin_api_x", 0),
    ],
)
def test_every_half_on_state_logs_one_line_naming_itself(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    attachments: bool,
    write: bool,
    key: str,
    expected: int,
) -> None:
    monkeypatch.setattr(linear_settings, "attachments_write_enabled", attachments)
    monkeypatch.setattr(linear_settings, "write_enabled", write)
    monkeypatch.setattr(linear_settings, "api_key", SecretStr(key))

    with caplog.at_level(logging.WARNING):
        check_linear_attachment_write_state(linear_settings)

    warnings = [
        record
        for record in caplog.records
        if "AGENT_CONTROL_LINEAR_ATTACHMENTS_WRITE_ENABLED" in record.getMessage()
    ]
    assert len(warnings) == expected
