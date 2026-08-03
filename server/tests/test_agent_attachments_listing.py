"""The list query: its two filters, its order, and what it never carries.

``?origin=`` is the one section 5 adds for a reason that is not convenience.
"What did the tracker put into this conversation" is the question asked after an
injection is suspected, and answering it by fetching every attachment and
filtering in a client is how the answer stops being available on a session with
a hundred files.

``total_bytes`` is not ``sum(size_bytes)``. Tombstoned rows are still listed -
that is the whole point of a tombstone - and they hold no bytes, so counting
them would make the number disagree with the quota that refuses the next upload
and would leave a namespace unable to clear itself by deleting.

The last case is an absence one: ``created_by_hash`` is a column on the row and
is absent from the wire model. Nothing else pins that, and it is one
``model_config`` change away from being serialized to every reader of a session
somebody else's credential opened.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterator

import pytest
from agent_control_server.config import executor_settings
from agent_control_server.services.attachment_quota import reset_attachment_quota
from fastapi.testclient import TestClient
from sqlalchemy import text

from .conftest import engine
from .test_agent_attachments_endpoints import PDF_BYTES, PNG_BYTES, make_session, upload

_SESSIONS_URL = "/api/v1/agent-sessions"

A_RECOGNIZABLE_HASH = "3d5a1f9c0000000000000000000000000000000000000000000000000000beef"


@pytest.fixture(autouse=True)
def attachments_enabled(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(executor_settings, "enabled", True)
    monkeypatch.setattr(executor_settings, "attachments_enabled", True)
    reset_attachment_quota()
    yield
    reset_attachment_quota()


def _insert_attachment(
    session_key: str,
    *,
    display_name: str,
    origin: str,
    origin_ref: str | None = None,
    status: str = "ready",
    size_bytes: int = 1024,
    created_by_hash: str | None = None,
) -> str:
    """Write one metadata row directly.

    No blob: nothing in this file downloads anything, and a listing that needed
    one to answer would be the defect ``test_agent_attachments_absence.py``
    exists to catch.
    """
    attachment_key = uuid.uuid4().hex
    digest = hashlib.sha256(attachment_key.encode()).hexdigest()
    with engine.begin() as conn:
        session_id = conn.execute(
            text("SELECT id FROM agent_sessions WHERE session_key = :key"),
            {"key": session_key},
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO agent_session_attachments "
                "(namespace_key, session_id, attachment_key, display_name, "
                " display_name_normalized, original_name_sha256, declared_mime, "
                " sniffed_mime, size_bytes, source_sha256, status, origin, "
                " origin_ref, created_by_hash) "
                "VALUES ('default', :sid, :key, :name, false, :namehash, "
                "        'application/pdf', 'application/pdf', :size, :sha, "
                "        :status, :origin, :ref, :hash)"
            ),
            {
                "sid": session_id,
                "key": attachment_key,
                "name": display_name,
                "namehash": digest,
                "size": size_bytes,
                "sha": digest,
                "status": status,
                "origin": origin,
                "ref": origin_ref,
                "hash": created_by_hash,
            },
        )
    return attachment_key


def _names(payload: dict) -> list[str]:
    return [attachment["display_name"] for attachment in payload["attachments"]]


# ---------------------------------------------------------------------------
# The filters
# ---------------------------------------------------------------------------


def test_the_origin_filter_answers_what_the_tracker_put_here(
    client: TestClient,
) -> None:
    session_key = make_session()
    _insert_attachment(session_key, display_name="from-linear.pdf", origin="linear")
    _insert_attachment(session_key, display_name="typed-in.pdf", origin="operator_upload")

    resp = client.get(
        f"{_SESSIONS_URL}/{session_key}/attachments", params={"origin": "linear"}
    )

    assert resp.status_code == 200, resp.text
    assert _names(resp.json()) == ["from-linear.pdf"]
    assert resp.json()["count"] == 1


def test_the_origin_filter_works_in_the_other_direction_too(
    client: TestClient,
) -> None:
    """One filter value proves the ``WHERE`` fires, not that it discriminates."""
    session_key = make_session()
    _insert_attachment(session_key, display_name="from-linear.pdf", origin="linear")
    _insert_attachment(session_key, display_name="typed-in.pdf", origin="operator_upload")

    resp = client.get(
        f"{_SESSIONS_URL}/{session_key}/attachments",
        params={"origin": "operator_upload"},
    )

    assert _names(resp.json()) == ["typed-in.pdf"]


def test_the_status_filter_separates_the_living_from_the_tombstoned(
    client: TestClient,
) -> None:
    session_key = make_session()
    _insert_attachment(session_key, display_name="live.pdf", origin="operator_upload")
    _insert_attachment(
        session_key,
        display_name="reclaimed.pdf",
        origin="operator_upload",
        status="tombstoned",
    )

    ready = client.get(
        f"{_SESSIONS_URL}/{session_key}/attachments", params={"status": "ready"}
    )
    gone = client.get(
        f"{_SESSIONS_URL}/{session_key}/attachments", params={"status": "tombstoned"}
    )

    assert _names(ready.json()) == ["live.pdf"]
    assert _names(gone.json()) == ["reclaimed.pdf"]


def test_both_filters_apply_together(client: TestClient) -> None:
    session_key = make_session()
    _insert_attachment(session_key, display_name="live-linear.pdf", origin="linear")
    _insert_attachment(
        session_key, display_name="gone-linear.pdf", origin="linear", status="tombstoned"
    )
    _insert_attachment(
        session_key, display_name="live-upload.pdf", origin="operator_upload"
    )

    resp = client.get(
        f"{_SESSIONS_URL}/{session_key}/attachments",
        params={"origin": "linear", "status": "ready"},
    )

    assert _names(resp.json()) == ["live-linear.pdf"]


def test_a_filter_value_outside_the_enum_is_refused(client: TestClient) -> None:
    """An unknown value must not fall through to an unfiltered listing."""
    session_key = make_session()
    _insert_attachment(session_key, display_name="live.pdf", origin="operator_upload")

    resp = client.get(
        f"{_SESSIONS_URL}/{session_key}/attachments", params={"origin": "dropbox"}
    )

    assert resp.status_code == 422, resp.text


def test_no_filter_lists_everything(client: TestClient) -> None:
    session_key = make_session()
    _insert_attachment(session_key, display_name="live.pdf", origin="operator_upload")
    _insert_attachment(
        session_key,
        display_name="reclaimed.pdf",
        origin="linear",
        status="tombstoned",
    )

    resp = client.get(f"{_SESSIONS_URL}/{session_key}/attachments")

    assert sorted(_names(resp.json())) == ["live.pdf", "reclaimed.pdf"]
    assert resp.json()["count"] == 2


# ---------------------------------------------------------------------------
# The order and the totals
# ---------------------------------------------------------------------------


def test_attachments_come_back_oldest_first(client: TestClient) -> None:
    """The composer renders them in this order, so it is part of the contract
    rather than whatever the planner returns."""
    session_key = make_session()
    upload(client, session_key, data=PDF_BYTES, filename="first.pdf")
    upload(
        client,
        session_key,
        data=PNG_BYTES,
        filename="second.png",
        content_type="image/png",
    )

    resp = client.get(f"{_SESSIONS_URL}/{session_key}/attachments")

    assert _names(resp.json()) == ["first.pdf", "second.png"]


def test_a_tombstone_is_listed_and_counted_but_holds_no_bytes(
    client: TestClient,
) -> None:
    """``count`` and ``total_bytes`` deliberately disagree.

    The row is still the record of what the conversation saw; its bytes are the
    thing that was reclaimed, and the quota that refuses the next upload counts
    the same way.
    """
    session_key = make_session()
    _insert_attachment(
        session_key, display_name="live.pdf", origin="operator_upload", size_bytes=1000
    )
    _insert_attachment(
        session_key,
        display_name="reclaimed.pdf",
        origin="linear",
        status="tombstoned",
        size_bytes=5_000_000,
    )

    payload = client.get(f"{_SESSIONS_URL}/{session_key}/attachments").json()

    assert payload["count"] == 2
    assert payload["total_bytes"] == 1000


def test_an_empty_session_lists_nothing_rather_than_404(client: TestClient) -> None:
    session_key = make_session()

    resp = client.get(f"{_SESSIONS_URL}/{session_key}/attachments")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"attachments": [], "count": 0, "total_bytes": 0}


# ---------------------------------------------------------------------------
# What the wire never carries
# ---------------------------------------------------------------------------


def test_the_creator_hash_appears_in_no_listing_and_no_read(
    client: TestClient,
) -> None:
    """Absence, because there is no field to assert the value of.

    Every reader of a session has ``agent_sessions.content_read``, which is not
    the same permission as knowing which credential attached a file. The column
    exists on the row and the wire model leaves it out; one config change would
    put it back and no other test would notice.
    """
    session_key = make_session()
    key = _insert_attachment(
        session_key,
        display_name="live.pdf",
        origin="operator_upload",
        created_by_hash=A_RECOGNIZABLE_HASH,
    )

    listing = client.get(f"{_SESSIONS_URL}/{session_key}/attachments")
    one = client.get(f"{_SESSIONS_URL}/{session_key}/attachments/{key}")

    assert A_RECOGNIZABLE_HASH not in listing.text
    assert A_RECOGNIZABLE_HASH not in one.text
    assert "created_by" not in listing.text


def test_the_origin_reference_is_carried_through(client: TestClient) -> None:
    """The other half: what the row records about provenance is not dropped,
    because ``origin`` without its reference cannot be traced back to an issue."""
    session_key = make_session()
    key = _insert_attachment(
        session_key,
        display_name="from-linear.pdf",
        origin="linear",
        origin_ref="ENG-1234",
    )

    payload = client.get(f"{_SESSIONS_URL}/{session_key}/attachments/{key}").json()

    assert payload["attachment"]["origin"] == "linear"
    assert payload["attachment"]["origin_ref"] == "ENG-1234"
