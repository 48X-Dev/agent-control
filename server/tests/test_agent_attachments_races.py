"""Two writers, one constraint.

Neither race here is reachable from a sequential ``TestClient``, and neither is
exotic. The console retries a dropped upload, so identical bytes arriving twice
at once is routine; a session deleted while a file is going into it is one
impatient operator. Both end at a constraint, and the constraint is the only
thing that actually decides - a ``SELECT`` then ``INSERT`` cannot, because both
racers select before either inserts.

Each test opens a second, committed transaction from inside the request, at the
seam between the type gate and the insert. That is a real concurrent writer
rather than a mocked one: the row it writes is committed by another connection
and the constraint below is the production constraint.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from agent_control_server.config import executor_settings
from agent_control_server.services import agent_attachments as service_module
from agent_control_server.services.agent_attachments import (
    CONTENT_UNIQUE_CONSTRAINT,
    SESSION_FOREIGN_KEY,
    _constraint_name,
)
from agent_control_server.services.attachment_quota import reset_attachment_quota

from .conftest import engine
from .test_agent_attachments_endpoints import (
    _SESSIONS_URL,
    PDF_BYTES,
    make_session,
    upload,
)


@pytest.fixture(autouse=True)
def attachments_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(executor_settings, "enabled", True)
    monkeypatch.setattr(executor_settings, "attachments_enabled", True)
    reset_attachment_quota()
    yield
    reset_attachment_quota()


def _run_between_the_gate_and_the_insert(
    monkeypatch: pytest.MonkeyPatch, action: Any
) -> None:
    """Wedge another committed writer into the upload, mid-request.

    The retention sweep already runs at exactly this point in its own
    transaction, so replacing it is how a second writer lands after this
    request's dedupe ``SELECT`` and before its ``INSERT`` without adding a hook
    that exists only for tests.
    """

    async def sweep_and_interfere(**_: Any) -> tuple[int, int]:
        action()
        return 0, 0

    monkeypatch.setattr(
        service_module, "run_attachment_retention_committed", sweep_and_interfere
    )


def _insert_conflicting_row(session_key: str) -> str:
    """Write a row with the same content hash as ``PDF_BYTES``, from elsewhere."""
    import hashlib

    winner_key = uuid.uuid4().hex
    with engine.begin() as conn:
        session_id = conn.execute(
            text("SELECT id FROM agent_sessions WHERE session_key = :k"),
            {"k": session_key},
        ).scalar()
        conn.execute(
            text(
                "INSERT INTO agent_session_attachments "
                "(namespace_key, session_id, attachment_key, display_name, "
                " display_name_normalized, original_name_sha256, declared_mime, "
                " sniffed_mime, size_bytes, source_sha256, status, origin) "
                "VALUES ('default', :sid, :key, 'winner.pdf', false, :nh, "
                "        'application/pdf', 'application/pdf', :size, :sha, "
                "        'ready', 'operator_upload')"
            ),
            {
                "sid": session_id,
                "key": winner_key,
                "nh": "0" * 64,
                "size": len(PDF_BYTES),
                "sha": hashlib.sha256(PDF_BYTES).hexdigest(),
            },
        )
    return winner_key


# ---------------------------------------------------------------------------
# Identical bytes, at the same time
# ---------------------------------------------------------------------------


def test_losing_the_identical_bytes_race_returns_the_winners_key(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The plan forbids the alternative by name: "rather than a 500"."""
    session_key = make_session()
    winner_key: list[str] = []
    _run_between_the_gate_and_the_insert(
        monkeypatch, lambda: winner_key.append(_insert_conflicting_row(session_key))
    )

    resp = upload(client, session_key)

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["deduplicated"] is True
    assert body["attachment"]["attachment_key"] == winner_key[0]
    assert body["attachment"]["display_name"] == "winner.pdf"


def test_the_loser_writes_no_second_row_and_no_orphan_blob(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Proof by absence. The response is identical whether the loser rolled its
    own insert back cleanly or left a blob behind pointing at nothing."""
    session_key = make_session()
    _run_between_the_gate_and_the_insert(
        monkeypatch, lambda: _insert_conflicting_row(session_key)
    )

    upload(client, session_key)

    with engine.begin() as conn:
        rows = conn.execute(
            text("SELECT count(*) FROM agent_session_attachments")
        ).scalar()
        blobs = conn.execute(
            text("SELECT count(*) FROM agent_session_attachment_blobs")
        ).scalar()
    assert rows == 1
    assert blobs == 0


def test_the_transaction_survives_the_conflict(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A savepoint, not a poisoned session.

    Without ``begin_nested`` the failed flush leaves the request's transaction
    unusable, so the recovery read that finds the winner would itself raise and
    the caller would get a 500 by a longer route.
    """
    session_key = make_session()
    _run_between_the_gate_and_the_insert(
        monkeypatch, lambda: _insert_conflicting_row(session_key)
    )
    resp = upload(client, session_key)

    assert resp.status_code == 201, resp.text
    # The request went on to commit, and the read after it is consistent. Both
    # are impossible on a session poisoned by an un-savepointed failed flush.
    listed = client.get(f"{_SESSIONS_URL}/{session_key}/attachments")
    assert listed.status_code == 200, listed.text
    assert listed.json()["count"] == 1


# ---------------------------------------------------------------------------
# A session deleted mid-upload
# ---------------------------------------------------------------------------


def test_a_session_deleted_mid_upload_is_a_404_not_a_500(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_key = make_session()

    def delete_the_session() -> None:
        with engine.begin() as conn:
            conn.execute(
                text("DELETE FROM agent_sessions WHERE session_key = :k"),
                {"k": session_key},
            )

    _run_between_the_gate_and_the_insert(monkeypatch, delete_the_session)

    resp = upload(client, session_key)

    assert resp.status_code == 404, resp.text
    assert resp.json()["error_code"] == "AGENT_SESSION_NOT_FOUND"


def test_a_session_deleted_mid_upload_leaves_no_orphaned_bytes(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The row the plan cares about: "No orphaned blob under either ordering"."""
    session_key = make_session()
    _run_between_the_gate_and_the_insert(
        monkeypatch,
        lambda: _delete_session(session_key),
    )

    upload(client, session_key)

    with engine.begin() as conn:
        blobs = conn.execute(
            text("SELECT count(*) FROM agent_session_attachment_blobs")
        ).scalar()
        rows = conn.execute(
            text("SELECT count(*) FROM agent_session_attachments")
        ).scalar()
    assert blobs == 0
    assert rows == 0


def _delete_session(session_key: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM agent_sessions WHERE session_key = :k"),
            {"k": session_key},
        )


# ---------------------------------------------------------------------------
# Discrimination, because the wrong branch returns somebody else's file
# ---------------------------------------------------------------------------


def test_without_the_recovery_the_same_race_reaches_the_constraint(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Proof the tests above are not vacuous.

    A wedge that never actually collided with the unique constraint would let
    every assertion in this file pass against code that does nothing. With the
    recovery replaced by a re-raise, the same setup puts a raw
    ``UniqueViolation`` through the handler - which in a running server is the
    500 the plan forbids by name, and here escapes ``TestClient`` because it
    re-raises server exceptions rather than rendering them.
    """
    session_key = make_session()
    _run_between_the_gate_and_the_insert(
        monkeypatch, lambda: _insert_conflicting_row(session_key)
    )

    async def reraise(self: Any, exc: IntegrityError, **_: Any) -> Any:
        raise exc

    monkeypatch.setattr(
        service_module.AgentAttachmentsService, "_resolve_write_conflict", reraise
    )

    with pytest.raises(IntegrityError) as caught:
        upload(client, session_key)

    assert _constraint_name(caught.value) == CONTENT_UNIQUE_CONSTRAINT


def test_an_unrelated_integrity_error_is_not_swallowed() -> None:
    """A bare ``except IntegrityError`` answering 201 would hide the next
    defect, so anything the two named constraints do not explain re-raises."""
    unrelated = IntegrityError("INSERT ...", {}, Exception("some other violation"))

    assert _constraint_name(unrelated) is None


def test_the_constraint_names_are_read_from_the_driver_diagnostics() -> None:
    class Diag:
        constraint_name = CONTENT_UNIQUE_CONSTRAINT

    class OrigError(Exception):
        diag = Diag()

    assert (
        _constraint_name(IntegrityError("stmt", {}, OrigError()))
        == CONTENT_UNIQUE_CONSTRAINT
    )


def test_the_constraint_names_fall_back_to_the_message_text() -> None:
    """psycopg populates ``diag``; not every driver does, and reading only the
    text would be as wrong as reading only the diagnostics."""
    orig = Exception(f'violates foreign key constraint "{SESSION_FOREIGN_KEY}"')

    assert _constraint_name(IntegrityError("stmt", {}, orig)) == SESSION_FOREIGN_KEY
