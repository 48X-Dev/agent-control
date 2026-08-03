"""Namespace isolation on the attachment path, where a 404 proves nothing.

``test_agent_attachments_auth.py`` already asserts that a session belonging to
another namespace answers 404. That case is satisfied by an implementation
which looks a row up globally and then compares, and the difference only shows
when two namespaces hold **the same key**. Every route below is exercised with
a ``session_key`` present in both namespaces, so a query that forgot its
namespace filter returns somebody else's row with a 200 rather than a 404, and
the assertion is over the bytes and the names rather than over the status.

Two layers, because they fail independently.

*The route layer.* ``session_key`` is unique per namespace, not globally, so an
unscoped session lookup resolves to whichever tenant's row was written first.
The scoping here is deliberately doubled - the session query and the attachment
query each carry the namespace - and the cases below pin both directions of
every route so either failure is caught. Measured against the real code: drop
the filter from the session lookup alone and the rightful tenant gets a 404,
drop it from the attachment lookup too and that tenant is handed the other
one's document. Asserting only "the intruder gets 404" sees neither.

*The blob store.* ``attachment_id`` **is** globally unique, so the
``namespace_key`` filter in ``PostgresAttachmentBlobStore`` decides nothing that
the id has not already decided, and dropping it would pass the whole route
suite. It is exercised directly here, with a valid id and the wrong namespace,
because that is the only place the argument is load-bearing - and because the
first implementation to store bytes outside this database will have no id
uniqueness to hide behind.

The storage ceilings get the same treatment. A quota that summed across
namespaces would let one tenant's uploads refuse another's, which is an outage
one tenant can cause for every other and which no single-namespace test sees.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from agent_control_models.attachments import AttachmentVariant
from agent_control_server.auth_framework import Operation, Principal, set_authorizer
from agent_control_server.config import executor_settings
from agent_control_server.services.attachment_blobs import (
    PostgresAttachmentBlobStore,
)
from agent_control_server.services.attachment_quota import reset_attachment_quota
from fastapi import Request
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .conftest import engine
from .test_agent_attachments_endpoints import PDF_BYTES, upload

_SESSIONS_URL = "/api/v1/agent-sessions"

TENANT_A = "tenant-a"
TENANT_B = "tenant-b"

A_BYTES = b"%PDF-1.7\ntenant a's own document\n%%EOF\n"
B_BYTES = b"%PDF-1.7\ntenant b's confidential document\n%%EOF\n"


@pytest.fixture(autouse=True)
def attachments_enabled(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(executor_settings, "enabled", True)
    monkeypatch.setattr(executor_settings, "attachments_enabled", True)
    reset_attachment_quota()
    yield
    reset_attachment_quota()


class NamespaceAuthorizer:
    """Maps ``X-Test-Namespace`` onto the principal, admin everywhere.

    Admin on purpose: this file is about namespace scoping and nothing else, so
    every other refusal is taken off the board and a leak cannot be mistaken for
    an authorization pass.
    """

    async def authorize(
        self,
        request: Request,
        operation: Operation,
        context: dict[str, Any] | None = None,
    ) -> Principal:
        del operation, context
        return Principal(
            namespace_key=request.headers.get("X-Test-Namespace", "default"),
            is_admin=True,
        )


@pytest.fixture()
def tenants(app: Any) -> tuple[TestClient, TestClient]:
    set_authorizer(NamespaceAuthorizer())
    return (
        TestClient(app, raise_server_exceptions=True, headers={"X-Test-Namespace": TENANT_A}),
        TestClient(app, raise_server_exceptions=True, headers={"X-Test-Namespace": TENANT_B}),
    )


def _shared_session_key() -> str:
    """One ``session_key``, a session row under it in each namespace.

    ``uq_agent_sessions_namespace_key`` is composite, so this is a shape the
    schema allows and a shape a global lookup cannot tell apart.
    """
    session_key = uuid.uuid4().hex
    for namespace_key in (TENANT_A, TENANT_B):
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO agent_sessions "
                    "(namespace_key, session_key, agent_name, executor_kind, "
                    " executor_app_name, executor_user_id, executor_session_id, "
                    " status) "
                    "VALUES (:ns, :key, :agent, 'google_adk', 'app', :user, :sid, "
                    "        'active')"
                ),
                {
                    "ns": namespace_key,
                    "key": session_key,
                    "agent": f"agent-{uuid.uuid4().hex[:8]}",
                    "user": f"{namespace_key}:{uuid.uuid4().hex}",
                    "sid": uuid.uuid4().hex,
                },
            )
    return session_key


def _insert_attachment(
    *,
    namespace_key: str,
    session_key: str,
    display_name: str,
    data: bytes,
    attachment_key: str,
) -> int:
    """Write one attachment and its bytes directly, past every route gate."""
    digest = hashlib.sha256(data).hexdigest()
    with engine.begin() as conn:
        session_id = conn.execute(
            text(
                "SELECT id FROM agent_sessions "
                " WHERE session_key = :key AND namespace_key = :ns"
            ),
            {"key": session_key, "ns": namespace_key},
        ).scalar_one()
        attachment_id = conn.execute(
            text(
                "INSERT INTO agent_session_attachments "
                "(namespace_key, session_id, attachment_key, display_name, "
                " display_name_normalized, original_name_sha256, declared_mime, "
                " sniffed_mime, size_bytes, source_sha256, status, origin) "
                "VALUES (:ns, :sid, :key, :name, false, :namehash, "
                "        'application/pdf', 'application/pdf', :size, :sha, "
                "        'ready', 'operator_upload') "
                "RETURNING id"
            ),
            {
                "ns": namespace_key,
                "sid": session_id,
                "key": attachment_key,
                "name": display_name,
                "namehash": digest,
                "size": len(data),
                "sha": digest,
            },
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO agent_session_attachment_blobs "
                "(namespace_key, attachment_id, variant, content_type, size_bytes, "
                " sha256, data) "
                "VALUES (:ns, :aid, 'original', 'application/pdf', :size, :sha, :data)"
            ),
            {
                "ns": namespace_key,
                "aid": attachment_id,
                "size": len(data),
                "sha": digest,
                "data": data,
            },
        )
    return int(attachment_id)


def _colliding_pair() -> tuple[str, str, int, int]:
    """One session key and one attachment key, in both namespaces.

    Returns ``(session_key, attachment_key, a_id, b_id)``.
    """
    session_key = _shared_session_key()
    attachment_key = uuid.uuid4().hex
    a_id = _insert_attachment(
        namespace_key=TENANT_A,
        session_key=session_key,
        display_name="tenant-a.pdf",
        data=A_BYTES,
        attachment_key=attachment_key,
    )
    b_id = _insert_attachment(
        namespace_key=TENANT_B,
        session_key=session_key,
        display_name="tenant-b.pdf",
        data=B_BYTES,
        attachment_key=attachment_key,
    )
    return session_key, attachment_key, a_id, b_id


def _blob_count(attachment_id: int) -> int:
    with engine.begin() as conn:
        return int(
            conn.execute(
                text(
                    "SELECT count(*) FROM agent_session_attachment_blobs "
                    " WHERE attachment_id = :aid"
                ),
                {"aid": attachment_id},
            ).scalar_one()
        )


def _status_of(attachment_id: int) -> str:
    with engine.begin() as conn:
        return str(
            conn.execute(
                text("SELECT status FROM agent_session_attachments WHERE id = :aid"),
                {"aid": attachment_id},
            ).scalar_one()
        )


# ---------------------------------------------------------------------------
# The routes, with a key that exists in both namespaces
# ---------------------------------------------------------------------------


def test_a_download_serves_this_namespaces_bytes_and_not_the_other_ones(
    tenants: tuple[TestClient, TestClient],
) -> None:
    """The case a 404 test cannot reach: both rows exist and both match the key.

    Both directions, because tenant A's rows are written first and every
    unscoped query therefore resolves to A. A's read keeps working under the
    mutation; B's is the one that turns into a 404, or into A's document once
    the attachment lookup loses its filter too.
    """
    tenant_a, tenant_b = tenants
    session_key, attachment_key, _, _ = _colliding_pair()
    path = f"{_SESSIONS_URL}/{session_key}/attachments/{attachment_key}/content"

    for_a = tenant_a.get(path)
    for_b = tenant_b.get(path)

    assert (for_a.status_code, for_b.status_code) == (200, 200), for_b.text
    assert for_a.content == A_BYTES
    assert for_b.content == B_BYTES
    assert b"confidential" not in for_a.content


def test_reading_the_metadata_of_a_colliding_key_stays_in_this_namespace(
    tenants: tuple[TestClient, TestClient],
) -> None:
    tenant_a, tenant_b = tenants
    session_key, attachment_key, _, _ = _colliding_pair()

    a_view = tenant_a.get(f"{_SESSIONS_URL}/{session_key}/attachments/{attachment_key}")
    b_view = tenant_b.get(f"{_SESSIONS_URL}/{session_key}/attachments/{attachment_key}")

    assert a_view.json()["attachment"]["display_name"] == "tenant-a.pdf"
    assert b_view.json()["attachment"]["display_name"] == "tenant-b.pdf"
    assert "tenant-b" not in a_view.text


def test_a_listing_shows_one_namespaces_files_only(
    tenants: tuple[TestClient, TestClient],
) -> None:
    tenant_a, tenant_b = tenants
    session_key, _, _, _ = _colliding_pair()

    for_a = tenant_a.get(f"{_SESSIONS_URL}/{session_key}/attachments")
    for_b = tenant_b.get(f"{_SESSIONS_URL}/{session_key}/attachments")

    assert (for_a.status_code, for_b.status_code) == (200, 200), for_b.text
    assert [a["display_name"] for a in for_a.json()["attachments"]] == ["tenant-a.pdf"]
    assert [a["display_name"] for a in for_b.json()["attachments"]] == ["tenant-b.pdf"]


def test_a_delete_reclaims_this_namespaces_bytes_and_leaves_the_others(
    tenants: tuple[TestClient, TestClient],
) -> None:
    """The destructive half, and the one worth the fixture.

    An unscoped delete on a colliding key answers exactly the same 200 while
    taking a file out of another tenant's conversation. Tenant B does the
    deleting because its rows are inserted second: an unscoped lookup resolves
    to A's, so the wrong implementation reclaims the bytes of a conversation
    the caller cannot even see.
    """
    _, tenant_b = tenants
    session_key, attachment_key, a_id, b_id = _colliding_pair()

    resp = tenant_b.delete(f"{_SESSIONS_URL}/{session_key}/attachments/{attachment_key}")

    assert resp.status_code == 200, resp.text
    assert (_status_of(b_id), _blob_count(b_id)) == ("tombstoned", 0)
    assert (_status_of(a_id), _blob_count(a_id)) == ("ready", 1)


def test_an_upload_lands_in_this_namespaces_session(
    tenants: tuple[TestClient, TestClient],
) -> None:
    """Tenant B again, for the same reason: an unscoped session lookup resolves
    to tenant A's row, and the file is then written into A's conversation."""
    _, tenant_b = tenants
    session_key = _shared_session_key()

    assert upload(tenant_b, session_key).status_code == 201

    with engine.begin() as conn:
        namespaces = conn.execute(
            text(
                "SELECT a.namespace_key FROM agent_session_attachments a "
                " JOIN agent_sessions s ON s.id = a.session_id "
                " WHERE s.session_key = :key"
            ),
            {"key": session_key},
        ).scalars().all()
    assert list(namespaces) == [TENANT_B]


# ---------------------------------------------------------------------------
# The blob store, where the id alone would be enough and the filter still is not
# ---------------------------------------------------------------------------


async def test_the_blob_store_returns_nothing_for_another_namespaces_id(
    async_db: AsyncSession,
) -> None:
    """A valid attachment id and the wrong namespace hands back no bytes.

    ``attachment_id`` is globally unique, so every route test in this file
    passes with the namespace filter deleted from this query. This is the only
    assertion that does not.
    """
    _, _, _, b_id = _colliding_pair()
    store = PostgresAttachmentBlobStore()

    blob = await store.open(
        async_db,
        namespace_key=TENANT_A,
        attachment_id=b_id,
        variant=AttachmentVariant.ORIGINAL,
    )

    assert blob is None


async def test_the_blob_store_finds_the_bytes_under_the_right_namespace(
    async_db: AsyncSession,
) -> None:
    """The control case: the id is right, so only the namespace was refusing."""
    _, _, _, b_id = _colliding_pair()
    store = PostgresAttachmentBlobStore()

    blob = await store.open(
        async_db,
        namespace_key=TENANT_B,
        attachment_id=b_id,
        variant=AttachmentVariant.ORIGINAL,
    )

    assert blob is not None
    assert blob.data == B_BYTES


async def test_a_delete_through_the_store_cannot_cross_a_namespace(
    async_db: AsyncSession,
) -> None:
    _, _, _, b_id = _colliding_pair()
    store = PostgresAttachmentBlobStore()

    deleted = await store.delete(async_db, namespace_key=TENANT_A, attachment_id=b_id)
    await async_db.commit()

    assert deleted == 0
    assert _blob_count(b_id) == 1


async def test_delete_for_session_cannot_cross_a_namespace(
    async_db: AsyncSession,
) -> None:
    """The Protocol method the Postgres implementation's own callers never use.

    It exists for a store with no cascade, which is exactly the implementation
    that would have no id uniqueness protecting it either, so its scoping is
    asserted before anybody writes one.
    """
    session_key, _, _, b_id = _colliding_pair()
    with engine.begin() as conn:
        b_session_id = conn.execute(
            text(
                "SELECT id FROM agent_sessions "
                " WHERE session_key = :key AND namespace_key = :ns"
            ),
            {"key": session_key, "ns": TENANT_B},
        ).scalar_one()
    store = PostgresAttachmentBlobStore()

    deleted = await store.delete_for_session(
        async_db, namespace_key=TENANT_A, session_id=int(b_session_id)
    )
    await async_db.commit()

    assert deleted == 0
    assert _blob_count(b_id) == 1


# ---------------------------------------------------------------------------
# The ceilings count one namespace, not the database
# ---------------------------------------------------------------------------


def test_the_namespace_byte_ceiling_counts_only_this_namespace(
    tenants: tuple[TestClient, TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    """One tenant at its ceiling must not refuse another tenant's upload.

    A ``sum(size_bytes)`` without the namespace predicate returns 413 here, and
    every single-namespace quota test in the suite still passes.
    """
    monkeypatch.setattr(
        executor_settings, "attachment_namespace_total_bytes", len(PDF_BYTES) + 1
    )
    tenant_a, tenant_b = tenants
    b_session = _shared_session_key()
    assert upload(tenant_b, b_session).status_code == 201
    assert upload(tenant_b, b_session, data=PDF_BYTES + b"more").status_code == 413

    resp = upload(tenant_a, b_session)

    assert resp.status_code == 201, resp.text


def test_the_hourly_ceiling_counts_only_this_namespace(
    tenants: tuple[TestClient, TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(executor_settings, "attachment_uploads_per_namespace_hour", 1)
    tenant_a, tenant_b = tenants
    session_key = _shared_session_key()
    assert upload(tenant_b, session_key).status_code == 201
    assert upload(tenant_b, session_key, data=PDF_BYTES + b"more").status_code == 413

    resp = upload(tenant_a, session_key)

    assert resp.status_code == 201, resp.text
