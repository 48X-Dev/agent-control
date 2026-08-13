"""Name refusal, draft/final, and the upload call - with the HTTP boundary mocked."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from agent_control._state import state
from my_agent import file_outputs
from my_agent.file_outputs import (
    build_file_output_tools,
    file_outputs_enabled,
    resolve_filename,
    write_docx_file,
    write_pptx_file,
    write_xlsx_file,
)

pytest.importorskip("openpyxl")
pytest.importorskip("docx")
pytest.importorskip("pptx")

SESSION_KEY = "sess-abc123"
GOOD_ROWS = [["Index", "12000000"]]


class _Context:
    """The slice of an ADK ToolContext these tools read."""

    def __init__(self, token: str | None = "tok-live") -> None:
        # The turn block wins, and a token absent from both is a session with no
        # credential at all - not a session falling back to the seeded one.
        self.state: dict[str, Any] = {
            "agent_control": {"session_key": SESSION_KEY},
            "agent_control_turn": {"session_key": SESSION_KEY},
        }
        if token is not None:
            self.state["agent_control_turn"]["runtime_token"] = token


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture every upload instead of making one, and answer 201."""
    recorded: list[dict[str, Any]] = []
    monkeypatch.setattr(state, "server_url", "http://control.example", raising=False)

    async def fake_post(self: httpx.AsyncClient, path: str, **kwargs: Any) -> httpx.Response:
        recorded.append({"path": path, "base_url": str(self.base_url), **kwargs})
        return httpx.Response(
            201,
            request=httpx.Request("POST", f"http://control.example{path}"),
            content=json.dumps(
                {"attachment": {"attachment_key": "a" * 32}, "deduplicated": False}
            ),
            headers={"content-type": "application/json"},
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    return recorded


def run(coro: Any) -> dict[str, Any]:
    result: dict[str, Any] = asyncio.run(coro)
    return result


# =============================================================================
# Section 4.5: the name is required and generic names are refused
# =============================================================================


@pytest.mark.parametrize(
    "given",
    ["output.xlsx", "output", "FILE", " result ", "untitled", "document.docx", "sheet1", "xlsx"],
)
def test_a_generic_name_is_refused(given: str) -> None:
    assert resolve_filename(given, extension=".xlsx") is None


@pytest.mark.parametrize("given", ["", "   ", ".xlsx", "..", "a/b.xlsx", "a\\b.xlsx", "x\x00y"])
def test_an_empty_or_path_like_name_is_refused(given: str) -> None:
    assert resolve_filename(given, extension=".xlsx") is None


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("series-b-investor-shortlist", "series-b-investor-shortlist.xlsx"),
        ("series-b-investor-shortlist.xlsx", "series-b-investor-shortlist.xlsx"),
        # The tool chooses the encoding, so a mismatched extension is replaced.
        ("series-b-investor-shortlist.docx", "series-b-investor-shortlist.xlsx"),
    ],
)
def test_a_representative_name_keeps_its_stem(given: str, expected: str) -> None:
    assert resolve_filename(given, extension=".xlsx") == expected


def test_a_very_long_stem_is_truncated_rather_than_refused() -> None:
    resolved = resolve_filename("a" * 400, extension=".pptx")

    assert resolved == "a" * file_outputs.STEM_MAX_CHARS + ".pptx"


def test_a_refused_name_blocks_and_names_the_rule(calls: list[dict[str, Any]]) -> None:
    result = run(
        write_xlsx_file(
            filename="output.xlsx",
            sheet_name="S",
            header=["Fund"],
            rows=GOOD_ROWS,
            tool_context=_Context(),
        )
    )

    assert result["status"] == "blocked"
    assert "output" in result["message"]
    assert "representative filename" in result["message"]
    assert calls == [], "a blocked name must not reach the store"


# =============================================================================
# Section 4.7: draft or final, and nothing else
# =============================================================================


@pytest.mark.parametrize("stage", ["draft", "final"])
def test_a_valid_stage_travels_to_the_store(calls: list[dict[str, Any]], stage: str) -> None:
    result = run(
        write_xlsx_file(
            filename="investor-shortlist",
            sheet_name="Shortlist",
            header=["Fund"],
            rows=GOOD_ROWS,
            stage=stage,
            tool_context=_Context(),
        )
    )

    assert result["status"] == "ok"
    assert result["stage"] == stage
    assert calls[0]["data"]["stage"] == stage


def test_the_default_stage_is_draft(calls: list[dict[str, Any]]) -> None:
    result = run(
        write_xlsx_file(
            filename="investor-shortlist",
            sheet_name="Shortlist",
            header=["Fund"],
            rows=GOOD_ROWS,
            tool_context=_Context(),
        )
    )

    assert result["stage"] == "draft"
    assert calls[0]["data"]["stage"] == "draft"
    assert "does not reach the ticket" in result["message"]


def test_an_unknown_stage_blocks(calls: list[dict[str, Any]]) -> None:
    result = run(
        write_xlsx_file(
            filename="investor-shortlist",
            sheet_name="S",
            header=["Fund"],
            rows=GOOD_ROWS,
            stage="published",
            tool_context=_Context(),
        )
    )

    assert result["status"] == "blocked"
    assert "'draft' or 'final'" in result["message"]
    assert calls == []


# =============================================================================
# Section 4.1: the session token, this session's own path, and nothing else
# =============================================================================


def test_the_upload_uses_the_session_token_and_its_own_session_path(
    calls: list[dict[str, Any]],
) -> None:
    run(
        write_xlsx_file(
            filename="investor-shortlist",
            sheet_name="Shortlist",
            header=["Fund", "Cheque"],
            rows=GOOD_ROWS,
            stage="final",
            tool_context=_Context(token="tok-live"),
        )
    )

    sent = calls[0]
    assert sent["path"] == f"/api/v1/agent-sessions/{SESSION_KEY}/attachments"
    assert sent["headers"]["Authorization"] == "Bearer tok-live"
    assert sent["headers"]["X-Requested-With"] == "XMLHttpRequest"
    assert sent["data"]["declared_name"] == "investor-shortlist.xlsx"
    name, payload, mime = sent["files"]["file"]
    assert name == "investor-shortlist.xlsx"
    assert payload[:2] == b"PK"
    assert mime.endswith("spreadsheetml.sheet")


def test_the_per_turn_token_wins_over_the_seeded_one(calls: list[dict[str, Any]]) -> None:
    """The seeded token expires long before an ADK session does."""
    context = _Context(token="tok-live")
    context.state["agent_control"]["runtime_token"] = "tok-seed-and-stale"

    run(
        write_xlsx_file(
            filename="investor-shortlist",
            sheet_name="S",
            header=["Fund"],
            rows=GOOD_ROWS,
            tool_context=context,
        )
    )

    assert calls[0]["headers"]["Authorization"] == "Bearer tok-live"


def test_no_session_token_means_no_upload_and_no_raise(calls: list[dict[str, Any]]) -> None:
    result = run(
        write_docx_file(
            filename="vendor-review",
            title="Vendor review",
            sections=[["Findings", "Two met the bar."]],
            tool_context=_Context(token=None),
        )
    )

    assert result["status"] == "failed"
    assert calls == []


def test_no_tool_context_means_no_upload(calls: list[dict[str, Any]]) -> None:
    result = run(
        write_pptx_file(filename="q3-review", title="Q3", slides=[["Wins", "Shipped"]])
    )

    assert result["status"] == "failed"
    assert calls == []


# =============================================================================
# Failures come back as results, never as exceptions
# =============================================================================


def test_a_refused_upload_is_reported_without_the_upstream_body(
    monkeypatch: pytest.MonkeyPatch, calls: list[dict[str, Any]]
) -> None:
    async def refuse(self: httpx.AsyncClient, path: str, **kwargs: Any) -> httpx.Response:
        return httpx.Response(
            413,
            request=httpx.Request("POST", "http://control.example"),
            content=b"attachment exceeds 20971520 bytes",
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", refuse)
    result = run(
        write_xlsx_file(
            filename="investor-shortlist",
            sheet_name="S",
            header=["Fund"],
            rows=GOOD_ROWS,
            tool_context=_Context(),
        )
    )

    assert result["status"] == "failed"
    assert "20971520" not in result["message"]
    assert result["attachment_key"] is None


def test_an_unreachable_control_plane_is_reported_not_raised(
    monkeypatch: pytest.MonkeyPatch, calls: list[dict[str, Any]]
) -> None:
    async def boom(self: httpx.AsyncClient, path: str, **kwargs: Any) -> httpx.Response:
        raise httpx.ConnectError("no route")

    monkeypatch.setattr(httpx.AsyncClient, "post", boom)
    result = run(
        write_pptx_file(
            filename="q3-review",
            title="Q3 review",
            slides=[["Wins", "Shipped the store"]],
            tool_context=_Context(),
        )
    )

    assert result["status"] == "failed"


def test_a_successful_upload_returns_the_attachment_key(calls: list[dict[str, Any]]) -> None:
    result = run(
        write_docx_file(
            filename="vendor-review",
            title="Vendor review",
            sections=[["Findings", "Two met the bar."]],
            stage="final",
            tool_context=_Context(),
        )
    )

    assert result["status"] == "ok"
    assert result["attachment_key"] == "a" * 32
    assert result["size_bytes"] > 0


def test_every_outcome_carries_the_same_keys(calls: list[dict[str, Any]]) -> None:
    expected = {"status", "message", "filename", "stage", "size_bytes", "attachment_key"}
    blocked = run(
        write_xlsx_file(
            filename="output", sheet_name="S", header=["A"], rows=GOOD_ROWS, tool_context=_Context()
        )
    )
    saved = run(
        write_xlsx_file(
            filename="investor-shortlist",
            sheet_name="S",
            header=["A"],
            rows=GOOD_ROWS,
            tool_context=_Context(),
        )
    )

    assert set(blocked) == expected
    assert set(saved) == expected


# =============================================================================
# Section 5: the flag, and section 6: write only
# =============================================================================


def test_the_flag_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(file_outputs.FILE_OUTPUTS_FLAG, raising=False)

    assert file_outputs_enabled() is False
    assert build_file_output_tools() == []


@pytest.mark.parametrize("raw", ["1", "true", "ON", "yes"])
def test_the_flag_turns_the_tools_on(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv(file_outputs.FILE_OUTPUTS_FLAG, raw)

    assert file_outputs_enabled() is True


def test_a_missing_builder_library_logs_one_line_naming_the_flag(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Half-on: the flag is set and the image cannot honour it."""
    monkeypatch.setenv(file_outputs.FILE_OUTPUTS_FLAG, "1")
    monkeypatch.setattr(file_outputs, "missing_libraries", lambda: ["pptx"])

    with caplog.at_level("WARNING"):
        tools = build_file_output_tools()

    assert tools == []
    assert len(caplog.records) == 1
    assert file_outputs.FILE_OUTPUTS_FLAG in caplog.records[0].getMessage()
    assert "pptx" in caplog.records[0].getMessage()


def test_the_module_exposes_no_read_list_or_delete_tool() -> None:
    """Section 6 is the reason this file exists in this shape. Asserted by name."""
    public = {name for name in dir(file_outputs) if not name.startswith("_")}
    verbs = ("read", "list", "fetch", "open", "get", "download", "delete", "update")

    assert {name for name in public if name.startswith(verbs)} == set()
    assert {name for name in public if name.startswith("write_")} == {
        "write_xlsx_file",
        "write_docx_file",
        "write_pptx_file",
    }
