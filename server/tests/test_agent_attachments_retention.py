"""The two sweeps, and the ceiling that is unreachable-by-remedy without them.

The last test in this file is the point of the whole module. Dispatch sessions
persist by default, so the cascade that would reclaim their attachments may
never fire; without a sweep on the byte total, a namespace that has done a
fortnight of autonomous work refuses every upload forever and no documented
action clears it. That failure lands first on the path with no operator watching
it, which is why it is asserted in both directions here: reachable without the
sweep, cleared with it.

Ages are set by backdating rows rather than by waiting, which is the only way to
test a fourteen-day TTL. The statements under test read ``now()`` from the
database, so the clock being moved is the row's rather than the server's.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY
from sqlalchemy import text

from agent_control_server.config import executor_settings
from agent_control_server.services.attachment_quota import reset_attachment_quota

from .conftest import engine
from .test_agent_attachments_endpoints import PDF_BYTES, PNG_BYTES, make_session, upload

_SESSIONS_URL = "/api/v1/agent-sessions"


@pytest.fixture(autouse=True)
def attachments_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(executor_settings, "enabled", True)
    monkeypatch.setattr(executor_settings, "attachments_enabled", True)
    reset_attachment_quota()
    yield
    reset_attachment_quota()


def _backdate_attachment(attachment_key: str, *, days: int) -> None:
    """Make a row look like it was last written that many days ago.

    Both timestamps, because the orphan sweep reads the later of them and a row
    written a fortnight ago has neither one recent. Moving ``created_at`` alone
    would describe a row that does not occur: one created a fortnight ago and
    touched a moment ago, which is a resurrected tombstone and has its own test.
    """
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


def _bind_to_a_turn(attachment_key: str, *, days_ago: int) -> None:
    """Record that this file was carried by a turn, that long ago."""
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
                "VALUES ('default', :sid, :trace, :aid, 0, 'sent', "
                "        now() - make_interval(days => :days))"
            ),
            {
                "sid": session_id,
                "trace": uuid.uuid4().hex,
                "aid": attachment_id,
                "days": days_ago,
            },
        )


def _row_state(attachment_key: str) -> tuple[str, int]:
    with engine.begin() as conn:
        status = conn.execute(
            text(
                "SELECT status FROM agent_session_attachments WHERE attachment_key = :key"
            ),
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
    return status, int(blobs or 0)


def _reclaimed_total(sweep: str) -> float:
    return (
        REGISTRY.get_sample_value(
            "agent_control_server_attachment_blobs_reclaimed_total", {"sweep": sweep}
        )
        or 0.0
    )


def _exists(attachment_key: str) -> bool:
    with engine.begin() as conn:
        return (
            conn.execute(
                text(
                    "SELECT count(*) FROM agent_session_attachments "
                    " WHERE attachment_key = :key"
                ),
                {"key": attachment_key},
            ).scalar()
            or 0
        ) > 0


# ---------------------------------------------------------------------------
# The orphan sweep
# ---------------------------------------------------------------------------


def test_an_attachment_never_bound_to_a_turn_is_deleted_whole(
    client: TestClient,
) -> None:
    """Somebody attached a file, changed their mind and closed the tab. Nothing
    else in this system would ever look at that row again."""
    stale = upload(client, make_session()).json()["attachment"]["attachment_key"]
    _backdate_attachment(stale, days=5)

    upload(client, make_session(), data=PNG_BYTES, content_type="image/png")

    assert _exists(stale) is False


def test_a_recent_unbound_attachment_survives_the_sweep(client: TestClient) -> None:
    """The ordinary case: a file uploaded a minute ago and not yet sent."""
    recent = upload(client, make_session()).json()["attachment"]["attachment_key"]

    upload(client, make_session(), data=PNG_BYTES, content_type="image/png")

    assert _exists(recent) is True


def test_a_bound_attachment_is_never_removed_by_the_orphan_sweep(
    client: TestClient,
) -> None:
    """Bound means it did its job, and its record is what the audit reads.
    Removing the row would delete the evidence rather than the bytes."""
    bound = upload(client, make_session()).json()["attachment"]["attachment_key"]
    _backdate_attachment(bound, days=90)
    _bind_to_a_turn(bound, days_ago=90)

    upload(client, make_session(), data=PNG_BYTES, content_type="image/png")

    assert _exists(bound) is True


def test_a_resurrected_attachment_is_not_swept_as_the_orphan_it_used_to_be(
    client: TestClient,
) -> None:
    """The bytes came back a second ago. The row is not an orphan.

    A resurrection keeps the original ``created_at`` on purpose, so a sweep
    reading that column alone sees a fortnight-old row the instant it turns
    ``ready`` again - and deletes it whole, bytes, audit row and the key the
    caller was just handed a 201 for. Nothing about the response distinguishes
    that from a resurrection that lasts, which is why the sweep is triggered
    here and the row is read afterwards.
    """
    session_key = make_session()
    key = upload(client, session_key).json()["attachment"]["attachment_key"]
    client.delete(f"{_SESSIONS_URL}/{session_key}/attachments/{key}")
    _backdate_attachment(key, days=5)

    again = upload(client, session_key)
    assert again.status_code == 201, again.text
    assert again.json()["attachment"]["attachment_key"] == key

    # Any upload into the namespace runs both sweeps.
    upload(client, make_session(), data=PNG_BYTES, content_type="image/png")

    assert _exists(key) is True
    assert _row_state(key) == ("ready", 1)
    downloaded = client.get(f"{_SESSIONS_URL}/{session_key}/attachments/{key}/content")
    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.content == PDF_BYTES


# ---------------------------------------------------------------------------
# The blob TTL sweep
# ---------------------------------------------------------------------------


def test_bytes_are_reclaimed_and_the_tombstone_is_intact(client: TestClient) -> None:
    session_key = make_session()
    key = upload(client, session_key).json()["attachment"]["attachment_key"]
    _backdate_attachment(key, days=40)
    _bind_to_a_turn(key, days_ago=40)

    upload(client, make_session(), data=PNG_BYTES, content_type="image/png")

    status, blobs = _row_state(key)
    assert status == "tombstoned"
    assert blobs == 0

    remembered = client.get(f"{_SESSIONS_URL}/{session_key}/attachments/{key}").json()
    assert remembered["attachment"]["display_name"] == "spec.pdf"
    assert remembered["attachment"]["size_bytes"] == len(PDF_BYTES)
    assert remembered["attachment"]["source_sha256"]
    assert remembered["attachment"]["origin"] == "operator_upload"


def test_a_recently_used_attachment_keeps_its_bytes(client: TestClient) -> None:
    key = upload(client, make_session()).json()["attachment"]["attachment_key"]
    _backdate_attachment(key, days=40)
    _bind_to_a_turn(key, days_ago=1)

    upload(client, make_session(), data=PNG_BYTES, content_type="image/png")

    status, blobs = _row_state(key)
    assert status == "ready"
    assert blobs == 1


def test_a_tombstoned_download_answers_with_a_notice(client: TestClient) -> None:
    session_key = make_session()
    key = upload(client, session_key).json()["attachment"]["attachment_key"]
    _backdate_attachment(key, days=40)
    _bind_to_a_turn(key, days_ago=40)
    upload(client, make_session(), data=PNG_BYTES, content_type="image/png")

    resp = client.get(f"{_SESSIONS_URL}/{session_key}/attachments/{key}/content")

    assert resp.status_code == 410, resp.text
    assert "reclaimed" in resp.json()["detail"]
    assert "audited" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# The reason the second sweep exists
# ---------------------------------------------------------------------------


def test_the_namespace_ceiling_is_reachable_without_the_sweep_and_not_with_it(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both directions, because either alone proves the wrong thing.

    With the TTL far in the future the ceiling refuses, which is the failure a
    fortnight of autonomous dispatch produces and which no operator action
    clears. With the shipped TTL the same upload succeeds, because the bytes
    holding the namespace down were reclaimed on the way in.
    """
    # Room for either file but not both, so the second upload turns entirely on
    # whether the first one's bytes were reclaimed.
    monkeypatch.setattr(
        executor_settings,
        "attachment_namespace_total_bytes",
        max(len(PDF_BYTES), len(PNG_BYTES)),
    )
    monkeypatch.setattr(executor_settings, "attachment_blob_ttl_days", 3650)
    old = upload(client, make_session()).json()["attachment"]["attachment_key"]
    _backdate_attachment(old, days=40)
    _bind_to_a_turn(old, days_ago=40)

    blocked = upload(client, make_session(), data=PNG_BYTES, content_type="image/png")
    assert blocked.status_code == 413, blocked.text

    monkeypatch.setattr(executor_settings, "attachment_blob_ttl_days", 14)
    cleared = upload(client, make_session(), data=PNG_BYTES, content_type="image/png")

    assert cleared.status_code == 201, cleared.text
    assert _row_state(old)[0] == "tombstoned"


# ---------------------------------------------------------------------------
# The sweep commits on its own
# ---------------------------------------------------------------------------


def test_a_reclaim_survives_the_413_of_the_upload_that_triggered_it(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run inside the upload's transaction, the sweep is undone by the refusal.

    The refusal message proves the sweep ran - it reports the post-sweep total -
    so the response alone cannot tell a durable reclaim from one that was rolled
    back a moment later. Only the row can. A namespace whose incoming files are
    all larger than the post-sweep headroom would otherwise repeat and discard
    the same reclamation on every attempt forever.
    """
    old = upload(client, make_session()).json()["attachment"]["attachment_key"]
    _backdate_attachment(old, days=40)
    _bind_to_a_turn(old, days_ago=40)
    # Room for nothing at all, so the upload that triggers the sweep is certain
    # to be refused after it.
    monkeypatch.setattr(executor_settings, "attachment_namespace_total_bytes", 1)

    refused = upload(client, make_session(), data=PNG_BYTES, content_type="image/png")

    assert refused.status_code == 413, refused.text
    status, blobs = _row_state(old)
    assert status == "tombstoned"
    assert blobs == 0


def test_an_orphan_deletion_survives_the_413_too(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    orphan = upload(client, make_session()).json()["attachment"]["attachment_key"]
    _backdate_attachment(orphan, days=40)
    monkeypatch.setattr(executor_settings, "attachment_namespace_total_bytes", 1)

    refused = upload(client, make_session(), data=PNG_BYTES, content_type="image/png")

    assert refused.status_code == 413, refused.text
    assert _exists(orphan) is False


def test_the_reclaimed_counter_only_records_durable_reclaims(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Prometheus counter cannot be rolled back, so it must not be touched
    until the transaction that made it true has committed. Incremented inside
    the upload's transaction, this is the one metric an operator would trust to
    diagnose a full namespace, reporting work that was discarded."""
    before = _reclaimed_total("blob_ttl")
    old = upload(client, make_session()).json()["attachment"]["attachment_key"]
    _backdate_attachment(old, days=40)
    _bind_to_a_turn(old, days_ago=40)
    monkeypatch.setattr(executor_settings, "attachment_namespace_total_bytes", 1)

    upload(client, make_session(), data=PNG_BYTES, content_type="image/png")

    assert _reclaimed_total("blob_ttl") == before + 1
    assert _row_state(old)[0] == "tombstoned"


def test_an_exhausted_connection_pool_costs_a_sweep_not_an_upload(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The price of committing the sweep separately is a second connection.

    Under enough concurrent uploads to drain the pool, that checkout times out.
    A missed sweep is recoverable by the next upload; a failed upload is what
    the operator sees and reports.
    """
    from sqlalchemy.exc import TimeoutError as PoolTimeoutError

    from agent_control_server.services import attachment_retention

    def refuse_a_connection(*_: object, **__: object) -> None:
        raise PoolTimeoutError("QueuePool limit reached")

    monkeypatch.setattr(
        attachment_retention, "run_attachment_retention", refuse_a_connection
    )

    resp = upload(client, make_session())

    assert resp.status_code == 201, resp.text


def test_a_sweep_failure_that_is_not_a_timeout_is_not_swallowed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broad ``except`` here would turn a broken sweep into a namespace that
    silently never reclaims, which is the failure the sweep exists to prevent."""
    from agent_control_server.services import attachment_retention

    def fail(*_: object, **__: object) -> None:
        raise RuntimeError("the sweep is broken")

    monkeypatch.setattr(attachment_retention, "run_attachment_retention", fail)

    with pytest.raises(RuntimeError):
        upload(client, make_session())


def test_deleting_the_session_takes_the_attachments_and_their_bytes(
    client: TestClient,
) -> None:
    """The cascade, asserted rather than assumed. It is composite and
    namespace-leading, and a single-column foreign key would look identical
    until the day two namespaces used the same session id."""
    session_key = make_session()
    key = upload(client, session_key).json()["attachment"]["attachment_key"]

    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM agent_sessions WHERE session_key = :key"),
            {"key": session_key},
        )

    assert _exists(key) is False
    with engine.begin() as conn:
        assert (
            conn.execute(
                text("SELECT count(*) FROM agent_session_attachment_blobs")
            ).scalar()
            == 0
        )
