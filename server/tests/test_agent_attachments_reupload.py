"""Uploading a file again after its bytes are gone.

Both ways a row loses its bytes end at the same place. A delete tombstones it
and says so; the TTL sweep tombstones it and the 410 on the download tells the
caller to "upload the file again if a turn still needs it". Follow that advice
against a dedupe that matches on ``source_sha256`` alone and the server answers
201 with a row that is still a tombstone and stores nothing, which is silent
data loss on the one recovery path the product advertises.

So the assertions here are never on the status code. Every one of them checks
the bytes came back: one blob row, a 200 download, and the original content.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from agent_control_server.config import executor_settings
from agent_control_server.services.attachment_quota import reset_attachment_quota

from .conftest import engine
from .test_agent_attachments_endpoints import (
    _SESSIONS_URL,
    PDF_BYTES,
    PNG_BYTES,
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


def _blobs_for(attachment_key: str) -> int:
    with engine.begin() as conn:
        return int(
            conn.execute(
                text(
                    "SELECT count(*) FROM agent_session_attachment_blobs b "
                    "  JOIN agent_session_attachments a ON a.id = b.attachment_id "
                    " WHERE a.attachment_key = :key"
                ),
                {"key": attachment_key},
            ).scalar()
            or 0
        )


def _rows_with_content(session_key: str) -> int:
    with engine.begin() as conn:
        return int(
            conn.execute(
                text(
                    "SELECT count(*) FROM agent_session_attachments a "
                    "  JOIN agent_sessions s ON s.id = a.session_id "
                    " WHERE s.session_key = :key"
                ),
                {"key": session_key},
            ).scalar()
            or 0
        )


def _tombstone_by_ttl(client: TestClient, attachment_key: str) -> None:
    """Age the row past the blob TTL and trigger the sweep with another upload."""
    with engine.begin() as conn:
        session_id, attachment_id = conn.execute(
            text(
                "SELECT session_id, id FROM agent_session_attachments "
                " WHERE attachment_key = :key"
            ),
            {"key": attachment_key},
        ).one()
        conn.execute(
            text(
                "INSERT INTO agent_turn_attachments "
                "(namespace_key, session_id, trace_id, attachment_id, position, "
                " verdict, created_at) "
                "VALUES ('default', :sid, 'trace', :aid, 0, 'sent', "
                "        now() - interval '40 days')"
            ),
            {"sid": session_id, "aid": attachment_id},
        )
    upload(client, make_session(), data=PNG_BYTES, content_type="image/png")


# ---------------------------------------------------------------------------
# After a delete
# ---------------------------------------------------------------------------


def test_reuploading_after_a_delete_puts_the_bytes_back(client: TestClient) -> None:
    session_key = make_session()
    first = upload(client, session_key).json()["attachment"]["attachment_key"]
    assert client.delete(f"{_SESSIONS_URL}/{session_key}/attachments/{first}").status_code == 200
    assert _blobs_for(first) == 0

    again = upload(client, session_key)

    assert again.status_code == 201, again.text
    attachment = again.json()["attachment"]
    assert attachment["status"] == "ready"
    # Not a dedupe: a dedupe hit means "you already have these bytes", and a
    # moment ago the caller did not.
    assert again.json()["deduplicated"] is False
    assert _blobs_for(attachment["attachment_key"]) == 1

    downloaded = client.get(
        f"{_SESSIONS_URL}/{session_key}/attachments/{attachment['attachment_key']}/content"
    )
    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.content == PDF_BYTES


def test_a_resurrected_attachment_keeps_its_key_and_its_history(
    client: TestClient,
) -> None:
    """One row, not two.

    Minting a second row would break the chain the tombstone exists to keep:
    ``agent_turn_attachments`` still points at the original id, and a second row
    would leave the turn that carried this file pointing at a tombstone while a
    different row held the bytes.
    """
    session_key = make_session()
    first = upload(client, session_key).json()["attachment"]
    client.delete(f"{_SESSIONS_URL}/{session_key}/attachments/{first['attachment_key']}")

    again = upload(client, session_key).json()["attachment"]

    assert again["attachment_key"] == first["attachment_key"]
    assert again["created_at"] == first["created_at"]
    assert again["updated_at"] > first["updated_at"]
    assert _rows_with_content(session_key) == 1


def test_a_live_row_is_still_a_dedupe_hit(client: TestClient) -> None:
    """The control group. Without it, a fix that simply stopped deduplicating
    would pass every test above."""
    session_key = make_session()
    first = upload(client, session_key).json()

    second = upload(client, session_key).json()

    assert second["deduplicated"] is True
    assert second["attachment"]["attachment_key"] == first["attachment"]["attachment_key"]
    assert _blobs_for(second["attachment"]["attachment_key"]) == 1


def _force_status(attachment_key: str, status: str) -> None:
    """Write a status this phase cannot reach on its own.

    Nothing here converts anything, so ``rejected`` and ``failed`` have no
    writer until the sidecar lands. The rule they need is a property of the
    dedupe branch rather than of the converter, so it is pinned now, against a
    row put into that state by hand.
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE agent_session_attachments SET status = :status "
                " WHERE attachment_key = :key"
            ),
            {"key": attachment_key, "status": status},
        )


@pytest.mark.parametrize("dead_status", ["failed", "rejected"])
def test_a_dead_row_is_re_uploaded_rather_than_deduplicated(
    client: TestClient, dead_status: str
) -> None:
    """Retry after a conversion failure is a fresh upload, not a pointer at it.

    A dedupe that asks only "is this a tombstone" answers 201 ``deduplicated``
    with the key of a row that will never convert, and the caller has no way to
    tell that from success. The status is what the assertion is on: a row still
    reading ``failed`` is the defect, whatever the response code says.
    """
    session_key = make_session()
    key = upload(client, session_key).json()["attachment"]["attachment_key"]
    _force_status(key, dead_status)

    again = upload(client, session_key)

    assert again.status_code == 201, again.text
    body = again.json()
    assert body["deduplicated"] is False
    assert body["attachment"]["attachment_key"] == key
    assert body["attachment"]["status"] == "ready"
    assert _blobs_for(key) == 1


# ---------------------------------------------------------------------------
# After the TTL sweep
# ---------------------------------------------------------------------------


def test_reuploading_after_the_ttl_sweep_puts_the_bytes_back(
    client: TestClient,
) -> None:
    """The sweep's own 410 says to do this, so it has to work.

    This is the path nobody performs by hand: the bytes went on a timer, the
    operator did not delete anything, and the first they know of it is a turn
    that needs the file.
    """
    session_key = make_session()
    key = upload(client, session_key).json()["attachment"]["attachment_key"]
    _tombstone_by_ttl(client, key)
    assert (
        client.get(f"{_SESSIONS_URL}/{session_key}/attachments/{key}/content").status_code
        == 410
    )

    again = upload(client, session_key)

    assert again.status_code == 201, again.text
    assert again.json()["attachment"]["status"] == "ready"
    assert _blobs_for(key) == 1
    downloaded = client.get(f"{_SESSIONS_URL}/{session_key}/attachments/{key}/content")
    assert downloaded.status_code == 200
    assert downloaded.content == PDF_BYTES


# ---------------------------------------------------------------------------
# A resurrection is a write, and every gate still applies to it
# ---------------------------------------------------------------------------


def test_a_resurrection_refused_by_a_quota_leaves_the_tombstone_empty(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Proof by absence. The 413 is returned whether or not the bytes were
    written first and rolled back badly, so the count is the assertion."""
    session_key = make_session()
    key = upload(client, session_key).json()["attachment"]["attachment_key"]
    client.delete(f"{_SESSIONS_URL}/{session_key}/attachments/{key}")
    monkeypatch.setattr(executor_settings, "attachment_namespace_total_bytes", 1)

    refused = upload(client, session_key)

    assert refused.status_code == 413, refused.text
    assert _blobs_for(key) == 0
    with engine.begin() as conn:
        status = conn.execute(
            text("SELECT status FROM agent_session_attachments WHERE attachment_key = :k"),
            {"k": key},
        ).scalar()
    assert status == "tombstoned"


def test_a_resurrection_still_counts_against_the_session_ceiling(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tombstone holds no bytes and no slot, so bringing one back has to be
    charged like any other file rather than waved through as an update."""
    monkeypatch.setattr(executor_settings, "attachment_max_per_session", 1)
    session_key = make_session()
    key = upload(client, session_key).json()["attachment"]["attachment_key"]
    client.delete(f"{_SESSIONS_URL}/{session_key}/attachments/{key}")
    assert upload(client, session_key, data=PNG_BYTES, content_type="image/png").status_code == 201

    refused = upload(client, session_key)

    assert refused.status_code == 413, refused.text
    assert refused.json()["error_code"] == "QUOTA_EXCEEDED"
    assert _blobs_for(key) == 0
