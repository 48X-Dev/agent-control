"""Files the agent wrote: who may store one, what marks it, and what keeps it.

Three properties, and each one fails differently.

*Provenance.* ``origin`` is read off the caller's token binding and never off
the request, so an operator cannot post a file that claims a model wrote it and
an agent cannot post one into somebody else's session.

*Survival.* An agent's file is the only copy of itself. It survives the orphan
sweep because it is bound to the turn that produced it, and it survives the
blob TTL until ``linear_asset_url`` says the tracker holds a copy too. Both are
asserted in the negative as well: an unbound agent row is swept like any other
orphan, and a row whose asset URL is set is reclaimed on the ordinary clock.

*Supersession.* One live draft per step. A step with a five-turn ceiling must
not leave five near-identical workbooks bound to a ticket.

Ages are set by backdating rows rather than by waiting, and the sweeps run from
the upload path, so every retention test here triggers one with a second
upload.

The agent's file is a PDF here rather than the workbook the design is about.
Nothing in this phase writes a workbook and nothing on this path opens either,
so the format would assert only that the accept gate accepts it.
"""

from __future__ import annotations

import uuid
from typing import Any, cast

import pytest
from agent_control_models.attachments import AttachmentOrigin
from agent_control_server.auth_framework import Operation
from agent_control_server.auth_framework.config import (
    RUNTIME_TOKEN_BOUND_OPERATIONS,
    configure_auth_from_env,
)
from agent_control_server.config import executor_settings
from agent_control_server.services.agent_sessions import (
    SESSION_TOKEN_SCOPES,
    mint_session_runtime_token,
)
from agent_control_server.services.agent_file_writeback import push_step_files
from agent_control_server.services.attachment_quota import reset_attachment_quota
from agent_control_server.services.linear_writeback_runtime import WritebackRuntime
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from .conftest import TEST_ADMIN_API_KEY, engine
from .test_agent_attachments_endpoints import PNG_BYTES, make_session, upload

_SESSIONS_URL = "/api/v1/agent-sessions"
_XHR = {"X-Requested-With": "XMLHttpRequest"}
_RUNTIME_SECRET = "test-runtime-secret-that-is-long-enough-for-hs256"
_REPORT = b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n%%EOF\n"


@pytest.fixture(autouse=True)
def attachments_enabled(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(executor_settings, "enabled", True)
    monkeypatch.setattr(executor_settings, "attachments_enabled", True)
    reset_attachment_quota()
    yield
    reset_attachment_quota()


@pytest.fixture()
def as_the_agent(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Wire auth the way production wires it, and hand back an unauthenticated client.

    Through ``configure_auth_from_env`` rather than by installing a provider
    directly: an override this test picks itself would pass whether or not the
    operation is one production actually routes to the verifier, which is the
    exact defect this fixture used to hide.
    """

    def install() -> TestClient:
        monkeypatch.setenv("AGENT_CONTROL_RUNTIME_TOKEN_SECRET", _RUNTIME_SECRET)
        monkeypatch.setenv("AGENT_CONTROL_AUTH_MODE", "api_key")
        configure_auth_from_env()
        return TestClient(app, raise_server_exceptions=True)

    yield install
    monkeypatch.delenv("AGENT_CONTROL_RUNTIME_TOKEN_SECRET", raising=False)
    monkeypatch.delenv("AGENT_CONTROL_AUTH_MODE", raising=False)
    configure_auth_from_env()


def _token(session_key: str) -> dict[str, str]:
    minted = mint_session_runtime_token(
        namespace_key="default", session_key=session_key, actor_id="0123456789abcdef"
    )
    assert minted is not None
    return {"Authorization": f"Bearer {minted[0]}", **_XHR}


def _agent_step() -> str:
    """A session shaped the way the dispatcher leaves one mid-turn."""
    return make_session(
        created_by_hash="the-dispatcher", agent_task_id=77, in_flight_trace_id=uuid.uuid4().hex
    )


def _write(
    machine: TestClient,
    session_key: str,
    *,
    kind: str = "draft",
    data: bytes = _REPORT,
    filename: str = "investor-shortlist.pdf",
) -> Any:
    return machine.post(
        f"{_SESSIONS_URL}/{session_key}/attachments/agent-output",
        files={"file": (filename, data, "application/pdf")},
        data={"declared_name": filename, "agent_output": kind},
        headers=_token(session_key),
    )


def _rows(session_key: str) -> list[tuple[str, str, str | None]]:
    """``(display_name, status, agent_output_kind)`` for one session, oldest first."""
    with engine.begin() as conn:
        return [
            (row[0], row[1], row[2])
            for row in conn.execute(
                text(
                    "SELECT a.display_name, a.status, a.agent_output_kind "
                    "  FROM agent_session_attachments a "
                    "  JOIN agent_sessions s ON s.id = a.session_id "
                    " WHERE s.session_key = :key "
                    " ORDER BY a.id"
                ),
                {"key": session_key},
            )
        ]


def _backdate(attachment_key: str, *, days: int) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE agent_session_attachments "
                "   SET created_at = now() - make_interval(days => :days), "
                "       updated_at = now() - make_interval(days => :days) "
                " WHERE attachment_key = :key"
            ),
            {"key": attachment_key, "days": days},
        )
        conn.execute(
            text(
                "UPDATE agent_turn_attachments t "
                "   SET created_at = now() - make_interval(days => :days) "
                "  FROM agent_session_attachments a "
                " WHERE a.id = t.attachment_id AND a.attachment_key = :key"
            ),
            {"key": attachment_key, "days": days},
        )


def _set_asset_url(attachment_key: str) -> None:
    """What Phase 2 writes once the tracker holds a copy of these bytes."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE agent_session_attachments "
                "   SET linear_asset_url = 'https://uploads.linear.app/deadbeef' "
                " WHERE attachment_key = :key"
            ),
            {"key": attachment_key},
        )


def _state(attachment_key: str) -> tuple[str, int]:
    with engine.begin() as conn:
        status = conn.execute(
            text("SELECT status FROM agent_session_attachments WHERE attachment_key = :key"),
            {"key": attachment_key},
        ).scalar()
        blobs = conn.execute(
            text(
                "SELECT count(*) FROM agent_session_attachment_blobs b "
                "  JOIN agent_session_attachments a ON a.id = b.attachment_id "
                " WHERE a.attachment_key = :key"
            ),
            {"key": attachment_key},
        ).scalar()
    return str(status), int(blobs or 0)


def _exists(attachment_key: str) -> bool:
    with engine.begin() as conn:
        return bool(
            conn.execute(
                text("SELECT count(*) FROM agent_session_attachments WHERE attachment_key = :key"),
                {"key": attachment_key},
            ).scalar()
        )


def _sweep(machine: TestClient) -> None:
    """Any upload into the namespace runs both sweeps.

    Through the agent's own route, because the token verifier owns the write
    operation for the length of these tests. Fresh bytes each time: a dedupe hit
    returns before retention is reached.
    """
    stored = _write(machine, _agent_step(), data=_REPORT + uuid.uuid4().bytes)
    assert stored.status_code == 201, stored.text


# ---------------------------------------------------------------------------
# Which credential, and what it may claim
# ---------------------------------------------------------------------------


def test_the_scope_is_on_the_session_token() -> None:
    """A fleet key would not be confined to one conversation at all."""
    assert Operation.AGENT_ATTACHMENTS_WRITE_SELF.value in SESSION_TOKEN_SCOPES


def test_every_session_scope_is_an_operation_the_runtime_override_serves() -> None:
    """The invariant that makes a session token usable at all.

    A scope the runtime override does not serve reaches the default authorizer,
    which never reads a Bearer token: 401 with no API key, 403 with one. Adding
    a scope without adding the operation here is silent until an agent tries it.
    """
    assert set(SESSION_TOKEN_SCOPES) <= {op.value for op in RUNTIME_TOKEN_BOUND_OPERATIONS}


def test_the_console_upload_survives_the_runtime_override(as_the_agent: Any, app: FastAPI) -> None:
    """The console authenticates by cookie on the operation the agent's route
    does not use, so installing the verifier must not take its upload away."""
    as_the_agent()
    browser = TestClient(app, base_url="http://localhost", raise_server_exceptions=True)
    assert browser.post("/api/login", json={"api_key": TEST_ADMIN_API_KEY}).status_code == 200

    stored = browser.post(
        f"{_SESSIONS_URL}/{make_session()}/attachments",
        files={"file": ("brief.pdf", _REPORT, "application/pdf")},
        data={"declared_name": "brief.pdf"},
        headers=_XHR,
    )

    assert stored.status_code == 201, stored.text
    assert stored.json()["attachment"]["origin"] == AttachmentOrigin.OPERATOR_UPLOAD


def test_the_session_token_stores_a_file_against_its_own_session(as_the_agent: Any) -> None:
    machine = as_the_agent()
    session_key = _agent_step()

    stored = _write(machine, session_key)

    assert stored.status_code == 201, stored.text
    body = stored.json()["attachment"]
    assert body["origin"] == AttachmentOrigin.AGENT
    assert body["agent_output"] == "draft"


def test_a_token_minted_for_another_session_cannot_upload_to_this_one(as_the_agent: Any) -> None:
    """The whole authorization design in one assertion.

    The context builder plucks the session key out of the path and the verifier
    compares it against the token's own target, so an agent physically cannot
    put a file into a conversation it is not running in.
    """
    machine = as_the_agent()
    mine = _agent_step()
    theirs = _agent_step()

    refused = machine.post(
        f"{_SESSIONS_URL}/{theirs}/attachments/agent-output",
        files={"file": ("x.pdf", _REPORT, "application/pdf")},
        data={"declared_name": "x.pdf", "agent_output": "draft"},
        headers=_token(mine),
    )

    assert refused.status_code == 403, refused.text
    assert _rows(theirs) == []


def test_the_dispatch_task_refusal_does_not_reach_the_agent_inside_it(as_the_agent: Any) -> None:
    """A task's session refuses every bystander, and the agent running in it is
    not one. Without this branch the scope in 4.1 would grant nothing."""
    machine = as_the_agent()

    assert _write(machine, _agent_step()).status_code == 201


def test_an_operator_cannot_mark_a_file_draft_or_final(client: TestClient) -> None:
    """Provenance is decided by the route the caller reached, so the marker that
    depends on it cannot be something a form field asserts."""
    session_key = make_session()

    refused = client.post(
        f"{_SESSIONS_URL}/{session_key}/attachments",
        files={"file": ("spec.pdf", b"%PDF-1.7\n%%EOF\n", "application/pdf")},
        data={"declared_name": "spec.pdf", "agent_output": "final"},
        headers=_XHR,
    )

    assert refused.status_code == 403, refused.text
    assert "draft or final" in refused.json()["detail"]


def test_an_agent_has_to_say_which_kind_of_file_it_is(as_the_agent: Any) -> None:
    machine = as_the_agent()
    session_key = _agent_step()

    refused = machine.post(
        f"{_SESSIONS_URL}/{session_key}/attachments/agent-output",
        files={"file": ("x.pdf", _REPORT, "application/pdf")},
        data={"declared_name": "x.pdf"},
        headers=_token(session_key),
    )

    assert refused.status_code == 422, refused.text


def test_a_file_produced_outside_a_turn_is_refused(as_the_agent: Any) -> None:
    """No turn means no binding, and an unbound agent file is an orphan the
    sweep reclaims hours later on somebody else's upload."""
    machine = as_the_agent()
    session_key = make_session(agent_task_id=77, in_flight_trace_id=None)

    refused = _write(machine, session_key)

    assert refused.status_code == 409, refused.text
    assert _rows(session_key) == []


# ---------------------------------------------------------------------------
# The orphan sweep
# ---------------------------------------------------------------------------


def test_an_agent_authored_file_survives_the_orphan_sweep(as_the_agent: Any) -> None:
    """Binding at upload is what makes this true, and it is the reason the
    upload path records one rather than leaving it to the write-back: a task can
    fail after the file is written and the file is still the best thing that
    turn produced."""
    machine = as_the_agent()
    key = _write(machine, _agent_step()).json()["attachment"]["attachment_key"]
    _backdate(key, days=5)

    _sweep(machine)

    assert _exists(key) is True


def test_an_agent_row_that_was_never_bound_is_swept_like_any_other_orphan(
    as_the_agent: Any, client: TestClient
) -> None:
    """The contrast that makes the test above mean something.

    The exemption comes from the binding, not from the origin. Nothing in this
    server writes an unbound agent row, which is exactly why the property is
    worth pinning: a future path that uploaded without binding would inherit
    the orphan sweep rather than a silent exemption.
    """
    session_key = make_session()
    key = upload(client, session_key).json()["attachment"]["attachment_key"]
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE agent_session_attachments "
                "   SET origin = :agent, agent_output_kind = 'draft' "
                " WHERE attachment_key = :key"
            ),
            {"key": key, "agent": AttachmentOrigin.AGENT.value},
        )
    _backdate(key, days=5)

    _sweep(as_the_agent())

    assert _exists(key) is False


# ---------------------------------------------------------------------------
# The blob TTL sweep
# ---------------------------------------------------------------------------


def test_the_blob_sweep_leaves_a_file_the_tracker_has_no_copy_of(as_the_agent: Any) -> None:
    """Every other row this sweep touches is a copy of something held elsewhere.
    Reclaiming this one deletes the deliverable itself."""
    machine = as_the_agent()
    key = _write(machine, _agent_step(), kind="final").json()["attachment"]["attachment_key"]
    _backdate(key, days=40)

    _sweep(machine)

    assert _state(key) == ("ready", 1)


def test_the_blob_sweep_reclaims_the_same_file_once_the_tracker_holds_it(
    as_the_agent: Any,
) -> None:
    """One predicate rather than a second retention system: with the asset URL
    set the row is a copy like any other and the ordinary TTL resumes."""
    machine = as_the_agent()
    key = _write(machine, _agent_step(), kind="final").json()["attachment"]["attachment_key"]
    _backdate(key, days=40)
    _set_asset_url(key)

    _sweep(machine)

    assert _state(key) == ("tombstoned", 0)


# ---------------------------------------------------------------------------
# One live draft per step
# ---------------------------------------------------------------------------


def test_a_second_draft_tombstones_the_first(as_the_agent: Any) -> None:
    machine = as_the_agent()
    session_key = _agent_step()

    _write(machine, session_key, filename="shortlist-v1.pdf")
    _write(machine, session_key, data=_REPORT + b"\x01", filename="shortlist-v2.pdf")

    assert _rows(session_key) == [
        ("shortlist-v1.pdf", "tombstoned", "draft"),
        ("shortlist-v2.pdf", "ready", "draft"),
    ]


def test_a_final_tombstones_every_draft_of_that_step(as_the_agent: Any) -> None:
    """Otherwise a step with a five-turn ceiling leaves five near-identical
    workbooks bound to one ticket."""
    machine = as_the_agent()
    session_key = _agent_step()

    _write(machine, session_key, filename="shortlist-draft.pdf")
    _write(
        machine,
        session_key,
        kind="final",
        data=_REPORT + b"\x02",
        filename="shortlist.pdf",
    )

    assert _rows(session_key) == [
        ("shortlist-draft.pdf", "tombstoned", "draft"),
        ("shortlist.pdf", "ready", "final"),
    ]


def test_another_steps_draft_is_left_alone(as_the_agent: Any) -> None:
    """Supersession is per step. A chain's second step must not tombstone the
    workbook its first step is still holding."""
    machine = as_the_agent()
    first = _agent_step()
    second = _agent_step()

    _write(machine, first, filename="step-one.pdf")
    _write(machine, second, data=_REPORT + b"\x03", filename="step-two.pdf")

    assert _rows(first) == [("step-one.pdf", "ready", "draft")]


def test_the_same_bytes_again_promote_the_draft_rather_than_duplicating_it(
    as_the_agent: Any,
) -> None:
    """Section 7's ceiling case: a step out of turns ships the draft it has.
    Re-posting identical bytes as the final has to move the row, not mint a
    second one and tombstone the bytes behind both."""
    machine = as_the_agent()
    session_key = _agent_step()

    _write(machine, session_key, filename="shortlist.pdf")
    promoted = _write(machine, session_key, kind="final", filename="shortlist.pdf")

    assert promoted.status_code == 201, promoted.text
    assert _rows(session_key) == [("shortlist.pdf", "ready", "final")]


def test_an_agent_cannot_claim_a_file_somebody_else_attached(
    as_the_agent: Any, client: TestClient
) -> None:
    """Dedupe is per session and per content, so identical bytes land on the
    operator's row. Stamping it would rewrite provenance, which is the one thing
    the origin column may never be."""
    session_key = _agent_step()
    theirs = upload(
        client, session_key, data=_REPORT, filename="theirs.pdf", content_type="application/pdf"
    )
    assert theirs.status_code == 201, theirs.text

    refused = _write(as_the_agent(), session_key)

    assert refused.status_code == 409, refused.text
    assert _rows(session_key) == [("theirs.pdf", "ready", None)]


# ---------------------------------------------------------------------------
# What the tracker is given, and what that leaves behind
# ---------------------------------------------------------------------------


class _FakeFileClient:
    """The file half of the write-back client, and nothing else."""

    def __init__(self) -> None:
        self.uploaded: list[tuple[str, int]] = []
        self.attached: list[tuple[str, str, str | None]] = []

    async def file_upload(self, *, filename: str, content_type: str, content: bytes) -> str:
        self.uploaded.append((filename, len(content)))
        return "https://uploads.linear.app/deadbeef"

    async def create_attachment(
        self, *, issue_id: str, title: str, url: str, subtitle: str | None
    ) -> str:
        self.attached.append((issue_id, title, subtitle))
        return f"att-{len(self.attached)}"


async def test_a_final_is_pushed_to_the_issue_and_recorded_as_delivered(
    as_the_agent: Any, async_db: Any
) -> None:
    """The push writes ``linear_asset_url``, which is what lets the blob sweep
    reclaim the bytes: until it is set the row is the only copy there is."""
    machine = as_the_agent()
    session_key = _agent_step()
    stored = _write(machine, session_key, kind="final", filename="shortlist.pdf")
    assert stored.status_code == 201, stored.text

    fake = _FakeFileClient()
    lines = await push_step_files(
        async_db,
        WritebackRuntime(
            client=cast(Any, fake),
            resolver=None,
            write_enabled=True,
            attachments_write_enabled=True,
        ),
        namespace_key="default",
        session_key=session_key,
        issue_id="issue-1",
        agent_name="researcher",
        step_index=2,
    )
    await async_db.commit()

    assert fake.uploaded == [("shortlist.pdf", len(_REPORT))]
    assert fake.attached == [("issue-1", "shortlist.pdf", "researcher, step 3")]
    assert any("shortlist.pdf" in line for line in lines)
    assert _asset_url(stored.json()["attachment"]["attachment_key"]) is not None


async def test_a_draft_is_never_offered_to_the_tracker(
    as_the_agent: Any, async_db: Any
) -> None:
    machine = as_the_agent()
    session_key = _agent_step()
    assert _write(machine, session_key, kind="draft").status_code == 201

    fake = _FakeFileClient()
    lines = await push_step_files(
        async_db,
        WritebackRuntime(
            client=cast(Any, fake),
            resolver=None,
            write_enabled=True,
            attachments_write_enabled=True,
        ),
        namespace_key="default",
        session_key=session_key,
        issue_id="issue-1",
        agent_name="researcher",
        step_index=0,
    )

    assert lines == []
    assert fake.uploaded == []


def _asset_url(attachment_key: str) -> str | None:
    with engine.begin() as conn:
        return conn.execute(
            text(
                "SELECT linear_asset_url FROM agent_session_attachments "
                " WHERE attachment_key = :key"
            ),
            {"key": attachment_key},
        ).scalar()
