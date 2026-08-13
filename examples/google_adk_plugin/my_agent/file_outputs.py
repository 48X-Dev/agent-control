"""Three tools that author a file and post it to this session's own store.

Write and upload only: nothing here lists, fetches or modifies a file, and a
draft returns because the server delivers it. Plan sections 4.5 to 4.7 and 6.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx
from agent_control._state import state
from agent_control.integrations.google_adk._session_state import SessionIdentity

from .file_builders import (
    DOCX_MIME,
    PPTX_MIME,
    XLSX_MIME,
    BuilderUnavailableError,
    build_docx,
    build_pptx,
    build_xlsx,
    missing_libraries,
)

logger = logging.getLogger(__name__)

FILE_OUTPUTS_FLAG = "AGENT_CONTROL_AGENT_FILE_OUTPUTS_ENABLED"
"""Default false. Read here and forwarded by the fleet, never by the server."""

UPLOAD_TIMEOUT_SECONDS = 30.0
"""Longer than a knowledge search because this carries a body, still under a turn."""

STAGES = ("draft", "final")
DEFAULT_STAGE = "draft"

REFUSED_STEMS = ("output", "file", "result", "untitled", "document", "sheet1")
"""Section 4.5's list. A tool that can be called without naming the thing will be."""

EXTENSIONS = (".xlsx", ".docx", ".pptx")
STEM_MAX_CHARS = 120

# "xlsx" on its own is a bare extension once the leading dot is optional.
_BARE = frozenset(REFUSED_STEMS) | {extension.lstrip(".") for extension in EXTENSIONS}

_BLOCKED_NAME = (
    "Blocked: {given!r} is not a representative filename. It must say what the file "
    "contains, and must not be a path, a bare extension, or one of: {refused}. Call "
    "this tool again with a name like 'series-b-investor-shortlist'."
)
_BLOCKED_STAGE = (
    "Blocked: stage must be 'draft' or 'final'. A draft is working state kept for "
    "your next turn; a final is the deliverable that reaches the ticket."
)
_NO_SESSION = (
    "This session cannot store files, so nothing was written. Carry on and say in "
    "your report that the file could not be saved."
)
_UPLOAD_FAILED = (
    "The file was built but the store did not accept it, so it was not saved. Say "
    "so in your report rather than trying again."
)


async def write_xlsx_file(
    filename: str,
    sheet_name: str,
    header: list[str],
    rows: list[list[str]],
    stage: str = "draft",
    tool_context: Any = None,
) -> dict[str, Any]:
    """Write a spreadsheet and save it to this task, so a person can open it in Excel.

    Use this whenever the answer is a table: a shortlist, a comparison, anything
    with columns. Do not paste a table into your report instead.

    Args:
        filename: What to call the file. Required, and it must say what the file
            contains. Generic names ('output', 'file', 'result', 'document') are
            refused. The .xlsx extension is added for you.
        sheet_name: The worksheet tab name, for example 'Shortlist'.
        header: The column headings, left to right.
        rows: One list of cell values per row, in the same column order as the
            header. Numbers written as plain digits stay sortable.
        stage: 'draft' while you are still working, so the file comes back to you
            next turn. 'final' when it is the finished deliverable. Defaults to
            'draft'.

    Returns:
        Whether it was saved, the name used, and its size. A status of 'blocked'
        explains a rule: read it, fix the call once, and do not retry blindly.
    """
    return await _author(
        tool_context,
        filename=filename,
        stage=stage,
        extension=".xlsx",
        mime=XLSX_MIME,
        build=lambda: build_xlsx(sheet_name=sheet_name, header=header, rows=rows),
    )


async def write_docx_file(
    filename: str,
    title: str,
    sections: list[list[str]],
    stage: str = "draft",
    tool_context: Any = None,
) -> dict[str, Any]:
    """Write a Word document and save it to this task, so a person can open it.

    Use this for prose somebody will read as a document: a memo, a brief, a
    written recommendation.

    Args:
        filename: What to call the file. Required, and it must say what the file
            contains. Generic names ('output', 'file', 'result', 'document') are
            refused. The .docx extension is added for you.
        title: The document title, shown at the top.
        sections: One list per section. The first item is that section's heading
            and every item after it is a paragraph.
        stage: 'draft' while you are still working, so the file comes back to you
            next turn. 'final' when it is the finished deliverable. Defaults to
            'draft'.

    Returns:
        Whether it was saved, the name used, and its size. A status of 'blocked'
        explains a rule: read it, fix the call once, and do not retry blindly.
    """
    return await _author(
        tool_context,
        filename=filename,
        stage=stage,
        extension=".docx",
        mime=DOCX_MIME,
        build=lambda: build_docx(title=title, sections=sections),
    )


async def write_pptx_file(
    filename: str,
    title: str,
    slides: list[list[str]],
    stage: str = "draft",
    tool_context: Any = None,
) -> dict[str, Any]:
    """Write a slide deck and save it to this task, so a person can open it.

    Use this when the answer is meant to be presented rather than read.

    Args:
        filename: What to call the file. Required, and it must say what the file
            contains. Generic names ('output', 'file', 'result', 'document') are
            refused. The .pptx extension is added for you.
        title: The title slide's text.
        slides: One list per slide. The first item is that slide's title and
            every item after it is a bullet.
        stage: 'draft' while you are still working, so the file comes back to you
            next turn. 'final' when it is the finished deliverable. Defaults to
            'draft'.

    Returns:
        Whether it was saved, the name used, and its size. A status of 'blocked'
        explains a rule: read it, fix the call once, and do not retry blindly.
    """
    return await _author(
        tool_context,
        filename=filename,
        stage=stage,
        extension=".pptx",
        mime=PPTX_MIME,
        build=lambda: build_pptx(title=title, slides=slides),
    )


# =============================================================================
# ADK wiring
# =============================================================================

INSTRUCTION = (
    " When the answer wants to be a file rather than a paragraph, write it with "
    "write_xlsx_file, write_docx_file or write_pptx_file and name the file for "
    "what it contains. Save it as a draft while you are still working and as a "
    "final when it is the deliverable, then say in your report which file you "
    "produced. You cannot open, list or change a file once it is saved."
)


def file_outputs_enabled() -> bool:
    """Whether to attach the file-authoring tools. Default off."""
    return os.getenv(FILE_OUTPUTS_FLAG, "0").strip().lower() in {"1", "true", "on", "yes"}


def build_file_output_tools() -> list[Any]:
    """The three tools for an ADK agent's ``tools=[...]``, or nothing when off."""
    if not file_outputs_enabled():
        return []

    missing = missing_libraries()
    if missing:
        logger.warning(
            "%s is on but %s could not be imported, so no file-authoring tools are "
            "attached and nothing will be written.",
            FILE_OUTPUTS_FLAG,
            ", ".join(missing),
        )
        return []

    from google.adk.tools import (  # type: ignore[import-not-found,import-untyped]
        FunctionTool,
    )

    return [
        FunctionTool(write_xlsx_file),
        FunctionTool(write_docx_file),
        FunctionTool(write_pptx_file),
    ]


# =============================================================================
# Internals
# =============================================================================


def resolve_filename(given: str, *, extension: str) -> str | None:
    """The name to store under, or ``None`` when the refusal list rejects it."""
    candidate = (given or "").strip()
    if not candidate or "\x00" in candidate or "/" in candidate or "\\" in candidate:
        return None

    stem = candidate
    for known in EXTENSIONS:
        if stem.lower().endswith(known):
            stem = stem[: -len(known)]
            break

    stem = stem.strip().strip(".").strip()
    if not stem or stem.lower() in _BARE:
        return None
    return f"{stem[:STEM_MAX_CHARS]}{extension}"


def _identity(tool_context: Any) -> SessionIdentity | None:
    """Which session this call belongs to, from ADK state, never raising."""
    if tool_context is None:
        return None
    identity = SessionIdentity.read(getattr(tool_context, "state", None))
    if identity is None or identity.token is None:
        # No session token means no per-session upload allowance to spend, and
        # the process API key must not stand in for one.
        return None
    return identity


async def _author(
    tool_context: Any,
    *,
    filename: str,
    stage: str,
    extension: str,
    mime: str,
    build: Any,
) -> dict[str, Any]:
    """Validate, build and upload one file, never raising."""
    chosen_stage = (stage or DEFAULT_STAGE).strip().lower()
    if chosen_stage not in STAGES:
        return _result("blocked", _BLOCKED_STAGE, filename=filename, stage=stage)

    name = resolve_filename(filename, extension=extension)
    if name is None:
        message = _BLOCKED_NAME.format(given=filename, refused=", ".join(REFUSED_STEMS))
        return _result("blocked", message, filename=filename, stage=chosen_stage)

    identity = _identity(tool_context)
    if identity is None or not state.server_url:
        return _result("failed", _NO_SESSION, filename=name, stage=chosen_stage)

    try:
        payload = build()
    except BuilderUnavailableError as exc:
        logger.warning("File output builder unavailable: %s", exc)
        return _result("failed", _NO_SESSION, filename=name, stage=chosen_stage)
    except Exception:
        logger.debug("File output builder failed", exc_info=True)
        return _result(
            "failed",
            "The content given did not make a valid file. Check the rows or "
            "sections you passed and describe the problem in your report.",
            filename=name,
            stage=chosen_stage,
        )

    return await _upload(identity, name=name, mime=mime, payload=payload, stage=chosen_stage)


async def _upload(
    identity: SessionIdentity,
    *,
    name: str,
    mime: str,
    payload: bytes,
    stage: str,
) -> dict[str, Any]:
    """POST one built file to this session's attachment store, never raising."""
    path = f"/api/v1/agent-sessions/{identity.session_key}/attachments/agent-output"
    headers = {
        "Authorization": f"Bearer {identity.token}",
        # The upload route refuses a request without it, as a CSRF guard.
        "X-Requested-With": "XMLHttpRequest",
    }
    try:
        async with httpx.AsyncClient(
            base_url=str(state.server_url).rstrip("/"),
            timeout=UPLOAD_TIMEOUT_SECONDS,
            follow_redirects=False,
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=1),
        ) as client:
            response = await client.post(
                path,
                files={"file": (name, payload, mime)},
                data={"declared_name": name, "agent_output": stage},
                headers=headers,
            )
    except (TimeoutError, httpx.HTTPError):
        logger.debug("Agent file output upload failed", exc_info=True)
        return _result("failed", _UPLOAD_FAILED, filename=name, stage=stage)

    if response.status_code >= 400:
        # No upstream body reaches the model: a rejected upload is a fact about
        # the control plane, not text the agent should read and act on.
        logger.warning("Agent file output upload refused: %s", response.status_code)
        return _result("failed", _UPLOAD_FAILED, filename=name, stage=stage)

    return _result(
        "ok",
        f"Saved {name} as a {stage}."
        + (
            " It stays with this task and does not reach the ticket."
            if stage == "draft"
            else " It is the deliverable for this task."
        ),
        filename=name,
        stage=stage,
        size_bytes=len(payload),
        attachment_key=_attachment_key(response),
    )


def _attachment_key(response: httpx.Response) -> str | None:
    """The stored attachment's key, or ``None`` when the body did not carry one."""
    try:
        body = response.json()
    except ValueError:
        return None
    attachment = body.get("attachment") if isinstance(body, dict) else None
    key = attachment.get("attachment_key") if isinstance(attachment, dict) else None
    return key if isinstance(key, str) and key else None


def _result(
    status: str,
    message: str,
    *,
    filename: str,
    stage: str,
    size_bytes: int = 0,
    attachment_key: str | None = None,
) -> dict[str, Any]:
    """One shape for every outcome, so a control can select the same keys."""
    return {
        "status": status,
        "message": message,
        "filename": filename,
        "stage": stage,
        "size_bytes": size_bytes,
        "attachment_key": attachment_key,
    }
