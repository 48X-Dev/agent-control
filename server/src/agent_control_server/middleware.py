"""The one place a request body is bounded before anything buffers it.

FastAPI's ``UploadFile`` parameter is parsed during dependency solving, which
means Starlette's multipart parser has already consumed the whole body - and
spooled anything past a megabyte to a temp file - before the first line of the
handler runs. A check inside the handler is therefore post-hoc no matter how it
is written, and so is one inside a ``require_operation`` dependency. This
server has no body limit anywhere else, so without this the upload route would
accept an unbounded POST to disk from anyone who can reach it and only then
answer 413.

Pure ASGI rather than ``BaseHTTPMiddleware`` because the control is the
``receive`` channel itself: the body is counted as the chunks arrive and the
request is abandoned the moment the count passes the cap. A ``Content-Length``
over the cap never gets that far, and a request with no ``Content-Length`` is
refused outright, because a body whose size cannot be checked in advance is one
whose size the sender declined to declare.

Scoped to the attachment upload route by path. Widening it to every route would
be a larger change than this slice, and a global body limit set to the
attachment cap would silently break any other endpoint someone later gives a
large payload.
"""

from __future__ import annotations

import re
from typing import Any

from agent_control_models.errors import ErrorCode, ErrorReason
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .config import executor_settings
from .errors import APIError, api_error_handler
from .services.executor_metrics import (
    ATTACHMENT_UPLOAD_TOO_LARGE,
    ATTACHMENT_UPLOADS,
)

_UPLOAD_PATH = re.compile(r"/agent-sessions/[^/]+/attachments/?$")

_ENVELOPE_ALLOWANCE_BYTES = 4096
"""What the multipart wrapper is allowed to cost on top of the file itself.

``attachment_max_bytes`` is a ceiling on *a file*. A request body is that file
plus a boundary repeated three times, two sets of part headers, a client-chosen
filename and the ``declared_name`` field, and ``Content-Length`` counts all of
it. Compared naively, a file of exactly the ceiling arrives as a body a few
hundred bytes over it and is refused - and the refusal quotes the envelope's
size, so an operator is told their 20,971,520-byte file is 20,971,797 bytes. The
UI pre-check measures the file and would have accepted it, which is the
accepted-here-refused-there mismatch this codebase avoids elsewhere by refusing
the configuration outright.

So the two counts below bound the *body* at the ceiling plus this allowance, and
the exact per-file bound stays where it can be measured exactly: the chunked
read in the upload handler, which counts the file part alone and refuses past
``attachment_max_bytes`` to the byte. Nothing is weakened by the gap. A body in
it is at most four kilobytes over, it is refused a moment later by a count that
can name the real number, and what this middleware exists to stop - an unbounded
POST spooled to the temp filesystem - is stopped exactly as before.

Four kilobytes rather than a tight computation from the boundary and the field
lengths: the filename on the wire is client-chosen and this server caps only
what it stores, so any exact figure would be a guess with a worse failure mode
than a generous constant.
"""


def _body_limit() -> int:
    """The ceiling this middleware applies to a whole request body."""
    return executor_settings.attachment_max_bytes + _ENVELOPE_ALLOWANCE_BYTES


def is_attachment_upload(scope: Scope) -> bool:
    """Whether this request is a POST to the attachment upload route.

    Matched on the path suffix rather than on the configured API prefix so a
    deployment that renames the prefix does not silently lose its only body
    limit.
    """
    return (
        scope.get("type") == "http"
        and scope.get("method") == "POST"
        and _UPLOAD_PATH.search(scope.get("path", "")) is not None
    )


def attachment_too_large(counted: int | None) -> APIError:
    """The one 413 this feature returns for size, wherever it is raised."""
    cap = executor_settings.attachment_max_bytes
    measured = (
        f"This upload is {counted} bytes."
        if counted is not None
        else "This upload did not declare its length."
    )
    return APIError(
        status_code=413,
        error_code=ErrorCode.ATTACHMENT_TOO_LARGE,
        reason=ErrorReason.INVALID,
        detail=f"{measured} The ceiling is {cap} bytes.",
        hint=(
            "Send a smaller file, or raise "
            "AGENT_CONTROL_EXECUTOR_ATTACHMENT_MAX_BYTES."
        ),
    )


def _declared_length(scope: Scope) -> int | None:
    for name, value in scope.get("headers", ()):
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


class AttachmentUploadBodyLimit:
    """Refuse an oversize attachment upload before its bytes are buffered."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not is_attachment_upload(scope):
            await self.app(scope, receive, send)
            return

        cap = _body_limit()
        declared = _declared_length(scope)
        if declared is None or declared > cap:
            await self._refuse(scope, send, counted=declared)
            return

        counted = 0
        over_cap = False
        started = False

        async def counting_receive() -> Message:
            """Feed the app until the cap, then disconnect it.

            An exception raised out of here does not survive: FastAPI parses the
            multipart body during routing, inside ``except Exception: raise
            HTTPException(400, "There was an error parsing the body")``. A
            middleware that raised would therefore answer an untyped 400 with a
            flat ``too_large`` counter, which is the one case section 9 calls
            out by name - a ``Content-Length`` that lies is what an attacker
            sends. So the overrun is recorded and the stream is closed instead.
            Starlette turns the disconnect into ``ClientDisconnect``, the app
            answers something, and ``watching_send`` throws that answer away.
            """
            nonlocal counted, over_cap
            if over_cap:
                return {"type": "http.disconnect"}
            message = await receive()
            if message.get("type") == "http.request":
                counted += len(message.get("body", b""))
                if counted > cap:
                    over_cap = True
                    return {"type": "http.disconnect"}
            return message

        async def watching_send(message: Message) -> None:
            """Forward the app's response, unless the body already overran.

            Swallowing is conditional on nothing having been forwarded yet.
            Past the first ``http.response.start`` the status line is on the
            wire and a second response would corrupt it, so a stream that
            overruns after the app began replying is passed through untouched
            and the refusal below is skipped.
            """
            nonlocal started
            if over_cap and not started:
                return
            if message.get("type") == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, counting_receive, watching_send)
        except Exception:
            # An app that raises rather than answering after its body was cut
            # off is describing the cut-off, not a fault, and a 500 would bury
            # the real refusal. Anything raised on a request that stayed under
            # the cap is left alone.
            if not (over_cap and not started):
                raise
        if over_cap and not started:
            await self._refuse(scope, send, counted=counted)

    async def _refuse(self, scope: Scope, send: Send, *, counted: int | None) -> None:
        ATTACHMENT_UPLOADS.labels(result=ATTACHMENT_UPLOAD_TOO_LARGE).inc()
        request: Any = Request(scope)
        response = await api_error_handler(request, attachment_too_large(counted))
        await response(scope, _no_more_body, send)


async def _no_more_body() -> Message:
    """A receive channel that never yields another chunk.

    The response is written without draining what is left of the request, which
    is the entire point: the bytes past the cap are never read.
    """
    return {"type": "http.disconnect"}
