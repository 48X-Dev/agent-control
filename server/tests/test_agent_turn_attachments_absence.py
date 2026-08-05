"""Delivery, proved by what does **not** happen.

Following the pattern this branch already uses twice - E2 in
``test_google_adk_mcp_tools.py`` and H2 in ``test_google_adk_adk_contract.py`` -
every case here is one where the response payload cannot tell a correct
implementation from a wrong one. Each asserts on the recorded executor calls,
on a table's row count, or on a scheduler that was never asked, and none of them
would fail if it only checked the status code.

Five things this file is about.

**No bytes, ever.** Against this deployment's configured endpoint ``POST
/v1/files`` answers 404, an inline file block answers 200 with the file silently
dropped and a ``data:`` image answers 500. A delivery that posted bytes would
therefore return exactly the 200 the text path returns, cost a model call and
leave the agent answering from a filename. The only assertion that separates
them is over the whole recorded request.

**A refusal spends nothing and writes nothing.** 409 and 413 are returned both
by a path that refused before touching anything and by one that wrote bindings,
resolved documents and then unwound badly. Row counts are what tell them apart.

**A turn never waits on a conversion.** OCR is roughly twenty seconds a file
against a request an operator is watching. An implementation that awaited it
would answer 200 with the text in it - a *better*-looking response - and the
absence of a cache row written during the request is what catches it.

**Cached text is scoped by namespace as well as by content.** A cache keyed on
content alone answers the same 200 while handing one tenant's document to
another.

**What a turn carried is written before the call, not after.** The one presence
assertion here, and it earns its place: it is only visible on a turn that never
came back.
"""

from __future__ import annotations

import base64
from typing import Any

import pytest
from agent_control_models.sessions import TURN_MESSAGE_MAX_LENGTH
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from agent_control_server.config import executor_settings
from agent_control_server.services import attachment_binding
from agent_control_server.services.attachment_converter_cache import (
    conversion_cache_key,
)
from agent_control_server.services.attachment_quota import reset_attachment_quota
from agent_control_server.services.executor_client import (
    EXECUTOR_TURN_TIMEOUT_MESSAGE,
    ExecutorTurnTimeoutError,
)
from agent_control_server.services.executor_factory import get_executor_client_factory
from agent_control_server.services.turn_quota import reset_turn_quota

from .conftest import engine
from .test_agent_attachments_endpoints import PDF_BYTES, upload
from .test_agent_session_turns import FakeTurnExecutorFactory, _bound_session

_SESSIONS_URL = "/api/v1/agent-sessions"

SECRET_TEXT = "THE MERGER CLOSES ON THE FOURTEENTH"


@pytest.fixture(autouse=True)
def attachments_enabled(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(executor_settings, "enabled", True)
    monkeypatch.setattr(executor_settings, "attachments_enabled", True)
    reset_turn_quota()
    reset_attachment_quota()
    yield
    reset_turn_quota()
    reset_attachment_quota()


@pytest.fixture()
def fake_executor(app: FastAPI) -> Any:
    factory = FakeTurnExecutorFactory()
    app.dependency_overrides[get_executor_client_factory] = lambda: factory
    yield factory
    app.dependency_overrides.pop(get_executor_client_factory, None)


@pytest.fixture()
def scheduled(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Every conversion the turn path asks for, recorded and not run.

    Replacing the submission rather than the converter is deliberate: the real
    scheduler creates a task on the running loop, and a background task that
    lands mid-assertion would make "no cache row was written during the
    request" a race rather than a fact.
    """
    calls: list[dict[str, Any]] = []

    def _record(**kwargs: Any) -> bool:
        calls.append(kwargs)
        return True

    monkeypatch.setattr(attachment_binding, "schedule_conversion", _record)
    return calls


def _seed_conversion(source_sha256: str, body: str, *, namespace_key: str = "default") -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO agent_attachment_conversions "
                "(namespace_key, cache_key, source_sha256, state, status, "
                " text_body, text_chars, meaningful_chars) "
                "VALUES (:ns, :key, :sha, 'done', 'text_layer_extracted', "
                "        :body, :chars, :chars)"
            ),
            {
                "ns": namespace_key,
                "key": conversion_cache_key(source_sha256),
                "sha": source_sha256,
                "body": body,
                "chars": len(body),
            },
        )


def _turn(
    client: TestClient,
    session_key: str,
    keys: list[str],
    message: str = "Read this",
) -> Any:
    return client.post(
        f"{_SESSIONS_URL}/{session_key}/turns",
        json={"message": message, "attachment_keys": keys},
    )


def _binding_count(session_key: str) -> int:
    with engine.begin() as conn:
        return int(
            conn.execute(
                text(
                    "SELECT count(*) FROM agent_turn_attachments b "
                    "  JOIN agent_sessions s ON s.id = b.session_id "
                    "   AND s.namespace_key = b.namespace_key "
                    " WHERE s.session_key = :key"
                ),
                {"key": session_key},
            ).scalar()
            or 0
        )


def _conversion_row_count() -> int:
    with engine.begin() as conn:
        return int(
            conn.execute(text("SELECT count(*) FROM agent_attachment_conversions")).scalar() or 0
        )


# ---------------------------------------------------------------------------
# The measured constraint: text on the wire, and nothing else
# ---------------------------------------------------------------------------


def test_no_file_bytes_reach_the_executor_in_any_form(
    client: TestClient, fake_executor: Any
) -> None:
    """The assertion the 200 cannot make.

    An inline file part is answered 200 by this deployment's endpoint with the
    file dropped, so a delivery that built one looks identical from here: same
    status, same messages, same duration. What separates it is that the bytes,
    their base64 and every spelling of a binary part are absent from the whole
    recorded call - not merely from the part of it somebody thought to check.
    """
    session = _bound_session(client)
    created = upload(client, session["session_key"]).json()["attachment"]
    _seed_conversion(created["source_sha256"], SECRET_TEXT)

    response = _turn(client, session["session_key"], [created["attachment_key"]])
    assert response.status_code == 200, response.text

    recorded = repr(fake_executor.runs)
    assert SECRET_TEXT in recorded, "the text was supposed to be delivered"
    assert PDF_BYTES.decode("latin-1") not in recorded
    assert base64.b64encode(PDF_BYTES).decode("ascii") not in recorded
    for binary_shape in (
        "inlineData",
        "inline_data",
        "fileData",
        "file_data",
        "data:application/pdf",
        "%PDF",
    ):
        assert binary_shape not in recorded, binary_shape


# ---------------------------------------------------------------------------
# A refusal spends nothing and writes nothing
# ---------------------------------------------------------------------------


def test_a_turn_refused_for_an_unready_file_writes_no_binding_row(
    client: TestClient, fake_executor: Any
) -> None:
    """A record of a turn that never ran is worse than no record.

    ``agent_turn_attachments`` answers "what did this conversation put in front
    of a model". An implementation that wrote the rows and then refused returns
    this same 409 while leaving that question answered wrongly, permanently.
    """
    session = _bound_session(client)
    created = upload(client, session["session_key"]).json()["attachment"]
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE agent_session_attachments SET status = 'tombstoned' "
                " WHERE attachment_key = :key"
            ),
            {"key": created["attachment_key"]},
        )

    response = _turn(client, session["session_key"], [created["attachment_key"]])

    assert response.status_code == 409, response.text
    assert fake_executor.runs == []
    assert _binding_count(session["session_key"]) == 0


def test_files_over_the_per_turn_ceiling_refuse_before_anything_is_read(
    client: TestClient, fake_executor: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ceiling bounds resident memory during one delivery.

    Checked before the blobs are read, so the refusal is what stops the bytes
    entering the process rather than a report that they already had.
    """
    monkeypatch.setattr(executor_settings, "attachment_turn_total_bytes", 8)
    session = _bound_session(client)
    created = upload(client, session["session_key"]).json()["attachment"]
    _seed_conversion(created["source_sha256"], SECRET_TEXT)

    response = _turn(client, session["session_key"], [created["attachment_key"]])

    assert response.status_code == 413, response.text
    assert response.json()["error_code"] == "ATTACHMENT_TOO_LARGE"
    assert fake_executor.runs == []
    assert SECRET_TEXT not in repr(fake_executor.runs)
    assert _binding_count(session["session_key"]) == 0


def test_a_message_leaving_no_room_refuses_rather_than_dropping_the_files(
    client: TestClient, fake_executor: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one rendering outcome that cannot be rendered away.

    Sending the message without its files is the tempting answer and it is the
    wrong one: the operator attached them deliberately and is watching the
    composer. This asserts the turn did not run at all, which is what
    distinguishes the refusal from a quiet drop that would have answered 200.

    Pinned at the delivery ceiling's floor. At the shipped default of 48000 a
    maximum-length chat message still leaves room, so the overflow is only
    reachable on a deployment that configured the ceiling down to the 16000
    minimum - which is therefore the deployment this test runs as.
    """
    from agent_control_server.config import executor_settings

    monkeypatch.setattr(executor_settings, "attachment_delivery_max_chars", 16000)
    session = _bound_session(client)
    created = upload(client, session["session_key"]).json()["attachment"]
    _seed_conversion(created["source_sha256"], SECRET_TEXT)

    response = _turn(
        client,
        session["session_key"],
        [created["attachment_key"]],
        message="m" * TURN_MESSAGE_MAX_LENGTH,
    )

    assert response.status_code == 413, response.text
    assert response.json()["error_code"] == "ATTACHMENT_TOO_LARGE"
    assert fake_executor.runs == []
    assert _binding_count(session["session_key"]) == 0


# ---------------------------------------------------------------------------
# Nothing on this path ever waits for a conversion
# ---------------------------------------------------------------------------


def test_a_turn_on_unread_content_writes_no_cache_row_before_answering(
    client: TestClient, fake_executor: Any, scheduled: list[dict[str, Any]]
) -> None:
    """Awaiting the conversion would produce a *better*-looking response.

    Twenty seconds later the same turn could have carried the text, and every
    assertion about the reply would still pass. The absence of a cache entry at
    the moment it answered is what says the request did not stop and wait for
    OCR to happen inside it.
    """
    session = _bound_session(client)
    created = upload(client, session["session_key"]).json()["attachment"]

    response = _turn(client, session["session_key"], [created["attachment_key"]])

    assert response.status_code == 200, response.text
    assert _conversion_row_count() == 0
    assert "NOT INCLUDED" in fake_executor.runs[-1]["message"]
    assert [call["source_sha256"] for call in scheduled] == [created["source_sha256"]]


def test_content_already_in_the_cache_is_not_scheduled_again(
    client: TestClient, fake_executor: Any, scheduled: list[dict[str, Any]]
) -> None:
    """Rescheduling every turn costs an OCR run per message and answers the same.

    One conversion per distinct file is the entire reason the cache is keyed on
    content, and a submission the queue silently deduplicates is not the same
    as a submission that was never made: the dedupe only holds while the first
    one is in flight.
    """
    session = _bound_session(client)
    created = upload(client, session["session_key"]).json()["attachment"]
    _seed_conversion(created["source_sha256"], SECRET_TEXT)

    response = _turn(client, session["session_key"], [created["attachment_key"]])

    assert response.status_code == 200, response.text
    assert SECRET_TEXT in fake_executor.runs[-1]["message"]
    assert scheduled == []


# ---------------------------------------------------------------------------
# The cache is keyed on content *and* on namespace
# ---------------------------------------------------------------------------


def test_another_namespaces_cached_text_is_never_delivered(
    client: TestClient, fake_executor: Any, scheduled: list[dict[str, Any]]
) -> None:
    """Identical bytes in two tenants are two conversions, and that is the point.

    A cache keyed on content alone is a cross-tenant read that answers 200 with
    a document this namespace never stored. The turn here has to behave exactly
    as though nothing were cached, which is the only outcome that is both safe
    and honest.
    """
    session = _bound_session(client)
    created = upload(client, session["session_key"]).json()["attachment"]
    _seed_conversion(created["source_sha256"], SECRET_TEXT, namespace_key="tenant-a")

    response = _turn(client, session["session_key"], [created["attachment_key"]])

    assert response.status_code == 200, response.text
    sent = fake_executor.runs[-1]["message"]
    assert SECRET_TEXT not in sent
    assert "NOT INCLUDED" in sent
    assert [call["source_sha256"] for call in scheduled] == [created["source_sha256"]]


# ---------------------------------------------------------------------------
# The one presence assertion, and it is only visible on a turn that never
# came back
# ---------------------------------------------------------------------------


def test_a_turn_that_timed_out_still_recorded_what_it_had_already_sent(
    client: TestClient, fake_executor: Any
) -> None:
    """Writing the bindings after the call would lose exactly this case.

    A 504 does not stop the invocation: the agent is still running and has
    already been handed the document. A transcript that recorded nothing here
    would answer "no files" to the one question worth asking about a turn
    nobody watched finish.
    """
    session = _bound_session(client)
    created = upload(client, session["session_key"]).json()["attachment"]
    _seed_conversion(created["source_sha256"], SECRET_TEXT)
    fake_executor.run_error = ExecutorTurnTimeoutError(EXECUTOR_TURN_TIMEOUT_MESSAGE)

    response = _turn(client, session["session_key"], [created["attachment_key"]])

    assert response.status_code == 504, response.text
    with engine.begin() as conn:
        verdicts = [
            row[0]
            for row in conn.execute(
                text(
                    "SELECT b.verdict FROM agent_turn_attachments b "
                    "  JOIN agent_sessions s ON s.id = b.session_id "
                    "   AND s.namespace_key = b.namespace_key "
                    " WHERE s.session_key = :key"
                ),
                {"key": session["session_key"]},
            )
        ]
    assert verdicts == ["sent"]
