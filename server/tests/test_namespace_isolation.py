"""Database-level coverage for namespace-scoped uniqueness and same-namespace
foreign key enforcement.

These tests use raw SQL against the configured test database so they exercise
the actual constraints created by the migration (and reflected by
``Base.metadata.create_all`` in tests).
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError


def _agent_name() -> str:
    """Return a name that satisfies the agents check constraints."""
    suffix = uuid.uuid4().hex[:12]
    return f"agent-{suffix}"


@pytest.fixture
def clean_tables(db_engine: Engine):
    """Truncate the data-model tables before and after each test."""

    def _truncate() -> None:
        with db_engine.begin() as conn:
            conn.execute(
                text(
                    "TRUNCATE TABLE control_bindings, agent_controls, "
                    "agent_policies, policy_controls, agents, policies, "
                    "controls RESTART IDENTITY CASCADE"
                )
            )

    _truncate()
    yield
    _truncate()


def _insert_agent(engine: Engine, *, namespace_key: str, name: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO agents (namespace_key, name, data) "
                "VALUES (:ns, :name, '{}'::jsonb)"
            ),
            {"ns": namespace_key, "name": name},
        )


def _insert_control(
    engine: Engine,
    *,
    namespace_key: str,
    name: str,
    deleted: bool = False,
) -> int:
    with engine.begin() as conn:
        deleted_at = dt.datetime.now(dt.UTC) if deleted else None
        return int(
            conn.execute(
                text(
                    "INSERT INTO controls (namespace_key, name, data, deleted_at) "
                    "VALUES (:ns, :name, '{}'::jsonb, :deleted_at) "
                    "RETURNING id"
                ),
                {
                    "ns": namespace_key,
                    "name": name,
                    "deleted_at": deleted_at,
                },
            ).scalar_one()
        )


def _insert_binding(
    engine: Engine,
    *,
    namespace_key: str,
    agent_name: str,
    base_url: str = "http://agent-executor:8080",
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO agent_runtimes "
                "(namespace_key, agent_name, base_url, executor_app_name) "
                "VALUES (:ns, :agent_name, :base_url, 'my_agent')"
            ),
            {"ns": namespace_key, "agent_name": agent_name, "base_url": base_url},
        )


def _insert_policy(engine: Engine, *, namespace_key: str, name: str) -> int:
    with engine.begin() as conn:
        return int(
            conn.execute(
                text(
                    "INSERT INTO policies (namespace_key, name) "
                    "VALUES (:ns, :name) RETURNING id"
                ),
                {"ns": namespace_key, "name": name},
            ).scalar_one()
        )


# Cross-namespace uniqueness (the same natural key may appear in different
# namespaces).


def test_agents_same_name_different_namespaces_allowed(
    db_engine: Engine, clean_tables: None
) -> None:
    name = _agent_name()
    _insert_agent(db_engine, namespace_key="ns-one", name=name)
    _insert_agent(db_engine, namespace_key="ns-two", name=name)


def test_controls_same_live_name_different_namespaces_allowed(
    db_engine: Engine, clean_tables: None
) -> None:
    _insert_control(db_engine, namespace_key="ns-one", name="pii-blocker")
    _insert_control(db_engine, namespace_key="ns-two", name="pii-blocker")


def test_policies_same_name_different_namespaces_allowed(
    db_engine: Engine, clean_tables: None
) -> None:
    _insert_policy(db_engine, namespace_key="ns-one", name="default-policy")
    _insert_policy(db_engine, namespace_key="ns-two", name="default-policy")


# Same-namespace duplicate rejection.


def test_agents_same_namespace_duplicate_name_rejected(
    db_engine: Engine, clean_tables: None
) -> None:
    name = _agent_name()
    _insert_agent(db_engine, namespace_key="ns-one", name=name)
    with pytest.raises(IntegrityError):
        _insert_agent(db_engine, namespace_key="ns-one", name=name)


def test_controls_same_namespace_duplicate_live_name_rejected(
    db_engine: Engine, clean_tables: None
) -> None:
    _insert_control(db_engine, namespace_key="ns-one", name="pii-blocker")
    with pytest.raises(IntegrityError):
        _insert_control(db_engine, namespace_key="ns-one", name="pii-blocker")


def test_policies_same_namespace_duplicate_name_rejected(
    db_engine: Engine, clean_tables: None
) -> None:
    _insert_policy(db_engine, namespace_key="ns-one", name="default-policy")
    with pytest.raises(IntegrityError):
        _insert_policy(db_engine, namespace_key="ns-one", name="default-policy")


# Soft-deleted controls still allow name reuse within the same namespace, since
# the partial unique index excludes soft-deleted rows.


def test_controls_soft_deleted_name_can_be_reused_within_namespace(
    db_engine: Engine, clean_tables: None
) -> None:
    _insert_control(
        db_engine, namespace_key="ns-one", name="pii-blocker", deleted=True
    )
    _insert_control(db_engine, namespace_key="ns-one", name="pii-blocker")


# Same-namespace foreign key enforcement.


def test_agent_controls_rejects_cross_namespace_agent_reference(
    db_engine: Engine, clean_tables: None
) -> None:
    name = _agent_name()
    _insert_agent(db_engine, namespace_key="ns-one", name=name)
    control_id = _insert_control(
        db_engine, namespace_key="ns-two", name="pii-blocker"
    )
    with pytest.raises(IntegrityError), db_engine.begin() as conn:
        # The agent lives in ns-one but we try to attach a ns-two control to it
        # via a row stamped ns-two; ns-two has no agent with this name.
        conn.execute(
            text(
                "INSERT INTO agent_controls (namespace_key, agent_name, control_id) "
                "VALUES (:ns, :agent_name, :control_id)"
            ),
            {"ns": "ns-two", "agent_name": name, "control_id": control_id},
        )


def test_agent_controls_rejects_cross_namespace_control_reference(
    db_engine: Engine, clean_tables: None
) -> None:
    name = _agent_name()
    _insert_agent(db_engine, namespace_key="ns-one", name=name)
    control_id = _insert_control(
        db_engine, namespace_key="ns-two", name="pii-blocker"
    )
    with pytest.raises(IntegrityError), db_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO agent_controls (namespace_key, agent_name, control_id) "
                "VALUES (:ns, :agent_name, :control_id)"
            ),
            {"ns": "ns-one", "agent_name": name, "control_id": control_id},
        )


def test_agent_policies_rejects_cross_namespace_policy_reference(
    db_engine: Engine, clean_tables: None
) -> None:
    name = _agent_name()
    _insert_agent(db_engine, namespace_key="ns-one", name=name)
    policy_id = _insert_policy(
        db_engine, namespace_key="ns-two", name="default-policy"
    )
    with pytest.raises(IntegrityError), db_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO agent_policies (namespace_key, agent_name, policy_id) "
                "VALUES (:ns, :agent_name, :policy_id)"
            ),
            {"ns": "ns-one", "agent_name": name, "policy_id": policy_id},
        )


def test_policy_controls_rejects_cross_namespace_reference(
    db_engine: Engine, clean_tables: None
) -> None:
    policy_id = _insert_policy(
        db_engine, namespace_key="ns-one", name="default-policy"
    )
    control_id = _insert_control(
        db_engine, namespace_key="ns-two", name="pii-blocker"
    )
    with pytest.raises(IntegrityError), db_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO policy_controls (namespace_key, policy_id, control_id) "
                "VALUES (:ns, :policy_id, :control_id)"
            ),
            {"ns": "ns-one", "policy_id": policy_id, "control_id": control_id},
        )


# The executor-binding registry. Its key is (namespace_key, agent_name) and its
# foreign key is the composite one, so two namespaces naming their agents the
# same way stay independent - including when one of them deregisters an agent.


def test_agent_runtimes_same_agent_name_different_namespaces_allowed(
    db_engine: Engine, clean_tables: None
) -> None:
    name = _agent_name()
    _insert_agent(db_engine, namespace_key="ns-one", name=name)
    _insert_agent(db_engine, namespace_key="ns-two", name=name)
    _insert_binding(db_engine, namespace_key="ns-one", agent_name=name)
    _insert_binding(
        db_engine,
        namespace_key="ns-two",
        agent_name=name,
        base_url="http://other-executor:8080",
    )


def test_agent_runtimes_same_namespace_duplicate_agent_rejected(
    db_engine: Engine, clean_tables: None
) -> None:
    """One agent, one executor. The primary key is what enforces it."""
    name = _agent_name()
    _insert_agent(db_engine, namespace_key="ns-one", name=name)
    _insert_binding(db_engine, namespace_key="ns-one", agent_name=name)
    with pytest.raises(IntegrityError):
        _insert_binding(
            db_engine,
            namespace_key="ns-one",
            agent_name=name,
            base_url="http://second-executor:8080",
        )


def test_agent_runtimes_rejects_cross_namespace_agent_reference(
    db_engine: Engine, clean_tables: None
) -> None:
    """A binding cannot borrow an agent registered in a different namespace.

    Without the namespace in the foreign key, ns-two could bind ns-one's agent
    name and point it at an executor of its choosing.
    """
    name = _agent_name()
    _insert_agent(db_engine, namespace_key="ns-one", name=name)
    with pytest.raises(IntegrityError):
        _insert_binding(db_engine, namespace_key="ns-two", agent_name=name)


def test_deregistering_an_agent_leaves_the_other_namespaces_binding(
    db_engine: Engine, clean_tables: None
) -> None:
    """The cascade follows the composite key, so it stops at the namespace."""
    name = _agent_name()
    for namespace_key in ("ns-one", "ns-two"):
        _insert_agent(db_engine, namespace_key=namespace_key, name=name)
        _insert_binding(db_engine, namespace_key=namespace_key, agent_name=name)

    with db_engine.begin() as conn:
        conn.execute(
            text("DELETE FROM agents WHERE namespace_key = 'ns-one' AND name = :name"),
            {"name": name},
        )
        survivors = conn.execute(
            text(
                "SELECT namespace_key FROM agent_runtimes WHERE agent_name = :name"
            ),
            {"name": name},
        ).scalars().all()
    assert list(survivors) == ["ns-two"]


# The two Phase 5 child tables. Both hang off ``agent_sessions`` by the
# composite key, which is what keeps one namespace's operator actions out of
# another's conversations - and this is the only boundary there is, because the
# executor's own session store has no namespace concept at all.


def _insert_session(engine: Engine, *, namespace_key: str) -> int:
    with engine.begin() as conn:
        return int(
            conn.execute(
                text(
                    "INSERT INTO agent_sessions (namespace_key, session_key, "
                    "  agent_name, executor_app_name, executor_user_id, "
                    "  executor_session_id) "
                    "VALUES (:ns, :key, :agent, 'app', :user, :sid) RETURNING id"
                ),
                {
                    "ns": namespace_key,
                    "key": uuid.uuid4().hex,
                    "agent": _agent_name(),
                    "user": f"{namespace_key}:u",
                    "sid": uuid.uuid4().hex,
                },
            ).scalar_one()
        )


def _insert_nudge(engine: Engine, *, namespace_key: str, session_id: int) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO agent_session_nudges (namespace_key, session_id, body) "
                "VALUES (:ns, :sid, 'guidance')"
            ),
            {"ns": namespace_key, "sid": session_id},
        )


def _insert_halt(
    engine: Engine, *, namespace_key: str, session_id: int, trace_id: str
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO agent_session_halts "
                "  (namespace_key, session_id, target_trace_id) "
                "VALUES (:ns, :sid, :trace)"
            ),
            {"ns": namespace_key, "sid": session_id, "trace": trace_id},
        )


def test_a_nudge_cannot_reference_a_session_in_another_namespace(
    db_engine: Engine,
) -> None:
    """Guidance typed in one namespace must not be queued onto another's chat."""
    session_id = _insert_session(db_engine, namespace_key="ns-one")

    with pytest.raises(IntegrityError):
        _insert_nudge(db_engine, namespace_key="ns-two", session_id=session_id)


def test_a_halt_cannot_reference_a_session_in_another_namespace(
    db_engine: Engine,
) -> None:
    """And a stop is worse than a nudge if it crosses: it ends somebody's turn."""
    session_id = _insert_session(db_engine, namespace_key="ns-one")

    with pytest.raises(IntegrityError):
        _insert_halt(
            db_engine,
            namespace_key="ns-two",
            session_id=session_id,
            trace_id="trace-a",
        )


def test_two_namespaces_may_hold_a_halt_for_the_same_trace_id(
    db_engine: Engine,
) -> None:
    """One-halt-per-turn is scoped to the namespace, not to the trace string.

    Trace ids are minted independently per turn, so a collision across
    namespaces is improbable rather than impossible - and a global constraint
    would make one namespace's stop refuse another's, which is a cross-tenant
    denial of an availability-critical action.
    """
    first = _insert_session(db_engine, namespace_key="ns-one")
    second = _insert_session(db_engine, namespace_key="ns-two")

    _insert_halt(db_engine, namespace_key="ns-one", session_id=first, trace_id="same")
    _insert_halt(db_engine, namespace_key="ns-two", session_id=second, trace_id="same")

    with db_engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT namespace_key FROM agent_session_halts "
                " WHERE target_trace_id = 'same' ORDER BY namespace_key"
            )
        ).scalars().all()
    assert list(rows) == ["ns-one", "ns-two"]


def test_deleting_one_namespaces_session_leaves_the_others_operator_actions(
    db_engine: Engine,
) -> None:
    """The cascade follows the composite key, so it stops at the namespace."""
    first = _insert_session(db_engine, namespace_key="ns-one")
    second = _insert_session(db_engine, namespace_key="ns-two")
    for namespace_key, session_id in (("ns-one", first), ("ns-two", second)):
        _insert_nudge(db_engine, namespace_key=namespace_key, session_id=session_id)
        _insert_halt(
            db_engine,
            namespace_key=namespace_key,
            session_id=session_id,
            trace_id=f"{namespace_key}-trace",
        )

    with db_engine.begin() as conn:
        conn.execute(text("DELETE FROM agent_sessions WHERE id = :id"), {"id": first})
        nudges = conn.execute(
            text("SELECT namespace_key FROM agent_session_nudges")
        ).scalars().all()
        halts = conn.execute(
            text("SELECT namespace_key FROM agent_session_halts")
        ).scalars().all()

    assert list(nudges) == ["ns-two"]
    assert list(halts) == ["ns-two"]


# The Phase 6 child table. A plan is the agent's own account of its work, and
# it hangs off ``agent_sessions`` by the same composite key for the same
# reason: the executor's session store has no namespace concept, so this key is
# the only thing keeping one tenant's declared steps out of another's console.


def _insert_plan_step(
    engine: Engine,
    *,
    namespace_key: str,
    session_id: int,
    revision: int = 1,
    index: int = 0,
    title: str = "a step",
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO agent_session_plan_steps "
                "  (namespace_key, session_id, plan_revision, step_index, title) "
                "VALUES (:ns, :sid, :rev, :idx, :title)"
            ),
            {
                "ns": namespace_key,
                "sid": session_id,
                "rev": revision,
                "idx": index,
                "title": title,
            },
        )


def test_a_plan_step_cannot_reference_a_session_in_another_namespace(
    db_engine: Engine,
) -> None:
    """One agent's declared plan must not attach to another tenant's chat."""
    session_id = _insert_session(db_engine, namespace_key="ns-one")

    with pytest.raises(IntegrityError):
        _insert_plan_step(db_engine, namespace_key="ns-two", session_id=session_id)


def test_two_namespaces_may_hold_the_same_revision_and_step_index(
    db_engine: Engine,
) -> None:
    """Revision numbering is per session, so it cannot collide across tenants.

    A key that omitted ``namespace_key`` would make one namespace's replan
    refuse another's, which is a cross-tenant denial of a write that is
    otherwise entirely routine.
    """
    first = _insert_session(db_engine, namespace_key="ns-one")
    second = _insert_session(db_engine, namespace_key="ns-two")

    _insert_plan_step(
        db_engine, namespace_key="ns-one", session_id=first, title="ns-one's step"
    )
    _insert_plan_step(
        db_engine, namespace_key="ns-two", session_id=second, title="ns-two's step"
    )

    with db_engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT namespace_key, title FROM agent_session_plan_steps "
                " WHERE plan_revision = 1 AND step_index = 0 ORDER BY namespace_key"
            )
        ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("ns-one", "ns-one's step"),
        ("ns-two", "ns-two's step"),
    ]


def test_deleting_one_namespaces_session_leaves_the_others_declared_plan(
    db_engine: Engine,
) -> None:
    first = _insert_session(db_engine, namespace_key="ns-one")
    second = _insert_session(db_engine, namespace_key="ns-two")
    _insert_plan_step(db_engine, namespace_key="ns-one", session_id=first)
    _insert_plan_step(db_engine, namespace_key="ns-two", session_id=second)

    with db_engine.begin() as conn:
        conn.execute(text("DELETE FROM agent_sessions WHERE id = :id"), {"id": first})
        survivors = conn.execute(
            text("SELECT namespace_key FROM agent_session_plan_steps")
        ).scalars().all()

    assert list(survivors) == ["ns-two"]


# ---------------------------------------------------------------------------
# Agent configuration: the system prompt and the model
#
# This row decides what a live agent says and which vendor it says it to, and
# its version log keeps every body that was ever saved. Both tables lead with
# ``namespace_key`` in the primary key and in the foreign key, and the version
# table carries its own copy rather than relying on a parent lookup, so the
# isolation filter is local to the query instead of a property of the call site.
# ---------------------------------------------------------------------------


def _insert_agent_config(
    engine: Engine,
    *,
    namespace_key: str,
    agent_name: str,
    body: str | None = "body",
    model_id: str | None = None,
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO agent_configs "
                "(namespace_key, agent_name, body, model_id, current_version) "
                "VALUES (:ns, :name, :body, :model_id, 1)"
            ),
            {
                "ns": namespace_key,
                "name": agent_name,
                "body": body,
                "model_id": model_id,
            },
        )


def _insert_agent_config_version(
    engine: Engine,
    *,
    namespace_key: str,
    agent_name: str,
    version_num: int = 1,
    body: str | None = "body",
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO agent_config_versions "
                "(namespace_key, agent_name, version_num, event_type, body) "
                "VALUES (:ns, :name, :num, 'created', :body)"
            ),
            {
                "ns": namespace_key,
                "name": agent_name,
                "num": version_num,
                "body": body,
            },
        )


def test_two_namespaces_may_configure_an_agent_of_the_same_name(
    db_engine: Engine, clean_tables: None
) -> None:
    """One tenant naming their agent ``marketing-copywriter`` must not block another."""
    name = _agent_name()
    _insert_agent(db_engine, namespace_key="ns-one", name=name)
    _insert_agent(db_engine, namespace_key="ns-two", name=name)

    _insert_agent_config(
        db_engine, namespace_key="ns-one", agent_name=name, body="ns-one's prompt"
    )
    _insert_agent_config(
        db_engine, namespace_key="ns-two", agent_name=name, body="ns-two's prompt"
    )

    with db_engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT namespace_key, body FROM agent_configs "
                " WHERE agent_name = :name ORDER BY namespace_key"
            ),
            {"name": name},
        ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("ns-one", "ns-one's prompt"),
        ("ns-two", "ns-two's prompt"),
    ]


def test_one_namespace_cannot_configure_another_namespaces_agent(
    db_engine: Engine, clean_tables: None
) -> None:
    """The composite foreign key is the tenancy boundary, not a convention."""
    name = _agent_name()
    _insert_agent(db_engine, namespace_key="ns-one", name=name)

    with pytest.raises(IntegrityError):
        _insert_agent_config(db_engine, namespace_key="ns-two", agent_name=name)


def test_a_version_row_cannot_reference_another_namespaces_agent(
    db_engine: Engine, clean_tables: None
) -> None:
    """Every saved body lives here, so a cross-namespace row would be a leak."""
    name = _agent_name()
    _insert_agent(db_engine, namespace_key="ns-one", name=name)
    _insert_agent_config(db_engine, namespace_key="ns-one", agent_name=name)

    with pytest.raises(IntegrityError):
        _insert_agent_config_version(
            db_engine, namespace_key="ns-two", agent_name=name
        )


def test_two_namespaces_may_hold_the_same_version_number_for_one_agent_name(
    db_engine: Engine, clean_tables: None
) -> None:
    """Version numbering is per agent per namespace.

    A unique key that omitted ``namespace_key`` would make one tenant's save
    refuse another's, which is a cross-tenant denial of an entirely routine
    write.
    """
    name = _agent_name()
    for namespace in ("ns-one", "ns-two"):
        _insert_agent(db_engine, namespace_key=namespace, name=name)
        _insert_agent_config(db_engine, namespace_key=namespace, agent_name=name)
        _insert_agent_config_version(
            db_engine,
            namespace_key=namespace,
            agent_name=name,
            version_num=1,
            body=f"{namespace}'s body",
        )

    with db_engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT namespace_key, body FROM agent_config_versions "
                " WHERE agent_name = :name AND version_num = 1 "
                " ORDER BY namespace_key"
            ),
            {"name": name},
        ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("ns-one", "ns-one's body"),
        ("ns-two", "ns-two's body"),
    ]


def test_deleting_one_namespaces_agent_leaves_the_others_configuration(
    db_engine: Engine, clean_tables: None
) -> None:
    """The cascade follows the tenancy anchor and stops at the boundary."""
    name = _agent_name()
    for namespace in ("ns-one", "ns-two"):
        _insert_agent(db_engine, namespace_key=namespace, name=name)
        _insert_agent_config(db_engine, namespace_key=namespace, agent_name=name)
        _insert_agent_config_version(
            db_engine, namespace_key=namespace, agent_name=name
        )

    with db_engine.begin() as conn:
        conn.execute(
            text("DELETE FROM agents WHERE namespace_key = 'ns-one' AND name = :name"),
            {"name": name},
        )
        configs = conn.execute(
            text("SELECT namespace_key FROM agent_configs WHERE agent_name = :name"),
            {"name": name},
        ).scalars().all()
        versions = conn.execute(
            text(
                "SELECT namespace_key FROM agent_config_versions "
                " WHERE agent_name = :name"
            ),
            {"name": name},
        ).scalars().all()

    assert list(configs) == ["ns-two"]
    assert list(versions) == ["ns-two"]


# ---------------------------------------------------------------------------
# Attachments: the file, its bytes, and which turn carried it
#
# Three tables, and the chain is two hops deep: a session owns an attachment,
# an attachment owns its bytes. Every hop is a composite foreign key leading
# with ``namespace_key``, and that is the only thing making a mistake here fail
# rather than succeed quietly. A single-column key would leave every statement
# in ``services/attachment_blobs.py`` valid SQL against another namespace's
# rows, and every one of those statements would return a plausible answer.
#
# The bytes are why this is the sharpest boundary in the schema. A leaked
# control name is a configuration disclosure; a leaked blob is somebody's
# document.
# ---------------------------------------------------------------------------


def _insert_attachment(
    engine: Engine,
    *,
    namespace_key: str,
    session_id: int,
    attachment_key: str | None = None,
    source_sha256: str | None = None,
    size_bytes: int = 11,
) -> int:
    with engine.begin() as conn:
        return int(
            conn.execute(
                text(
                    "INSERT INTO agent_session_attachments "
                    "  (namespace_key, session_id, attachment_key, display_name, "
                    "   original_name_sha256, declared_mime, sniffed_mime, "
                    "   size_bytes, source_sha256) "
                    "VALUES (:ns, :sid, :key, 'brief.pdf', :name_sha, "
                    "        'application/pdf', 'application/pdf', :size, :sha) "
                    "RETURNING id"
                ),
                {
                    "ns": namespace_key,
                    "sid": session_id,
                    "key": attachment_key or uuid.uuid4().hex,
                    "name_sha": uuid.uuid4().hex + uuid.uuid4().hex,
                    "size": size_bytes,
                    "sha": source_sha256 or (uuid.uuid4().hex + uuid.uuid4().hex),
                },
            ).scalar_one()
        )


def _insert_attachment_blob(
    engine: Engine, *, namespace_key: str, attachment_id: int
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO agent_session_attachment_blobs "
                "  (namespace_key, attachment_id, variant, content_type, "
                "   size_bytes, sha256, data) "
                "VALUES (:ns, :aid, 'original', 'application/pdf', 11, :sha, :data)"
            ),
            {
                "ns": namespace_key,
                "aid": attachment_id,
                "sha": uuid.uuid4().hex + uuid.uuid4().hex,
                "data": b"%PDF-1.4\n%%",
            },
        )


def _insert_turn_attachment(
    engine: Engine, *, namespace_key: str, session_id: int, attachment_id: int
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO agent_turn_attachments "
                "  (namespace_key, session_id, trace_id, attachment_id, position) "
                "VALUES (:ns, :sid, :trace, :aid, 0)"
            ),
            {
                "ns": namespace_key,
                "sid": session_id,
                "trace": uuid.uuid4().hex,
                "aid": attachment_id,
            },
        )


def test_an_attachment_cannot_reference_a_session_in_another_namespace(
    db_engine: Engine,
) -> None:
    """A file uploaded in one namespace must not land in another's chat."""
    session_id = _insert_session(db_engine, namespace_key="ns-one")

    with pytest.raises(IntegrityError):
        _insert_attachment(db_engine, namespace_key="ns-two", session_id=session_id)


def test_blob_bytes_cannot_reference_an_attachment_in_another_namespace(
    db_engine: Engine,
) -> None:
    """The second hop, and the one that decides whether bytes can cross.

    Every statement in the blob store filters on ``namespace_key`` beside the
    attachment id. This asserts the database would refuse one that did not,
    rather than trusting each call site to remember.
    """
    session_id = _insert_session(db_engine, namespace_key="ns-one")
    attachment_id = _insert_attachment(
        db_engine, namespace_key="ns-one", session_id=session_id
    )

    with pytest.raises(IntegrityError):
        _insert_attachment_blob(
            db_engine, namespace_key="ns-two", attachment_id=attachment_id
        )


def test_a_turn_binding_cannot_reference_an_attachment_in_another_namespace(
    db_engine: Engine,
) -> None:
    """Binding is what puts a file in front of a model, so it crosses last."""
    session_id = _insert_session(db_engine, namespace_key="ns-one")
    attachment_id = _insert_attachment(
        db_engine, namespace_key="ns-one", session_id=session_id
    )

    with pytest.raises(IntegrityError):
        _insert_turn_attachment(
            db_engine,
            namespace_key="ns-two",
            session_id=session_id,
            attachment_id=attachment_id,
        )


def test_two_namespaces_may_hold_the_same_attachment_key(db_engine: Engine) -> None:
    """The key a browser sees is unique per namespace, not globally.

    It is a ``uuid4`` so a natural collision will not happen, but a uniqueness
    constraint that omitted ``namespace_key`` would let one tenant's insert
    refuse another's, which is a cross-tenant denial of an ordinary write.
    """
    shared_key = uuid.uuid4().hex
    first = _insert_session(db_engine, namespace_key="ns-one")
    second = _insert_session(db_engine, namespace_key="ns-two")

    _insert_attachment(
        db_engine,
        namespace_key="ns-one",
        session_id=first,
        attachment_key=shared_key,
    )
    _insert_attachment(
        db_engine,
        namespace_key="ns-two",
        session_id=second,
        attachment_key=shared_key,
    )

    with db_engine.begin() as conn:
        owners = conn.execute(
            text(
                "SELECT namespace_key FROM agent_session_attachments "
                " WHERE attachment_key = :key ORDER BY namespace_key"
            ),
            {"key": shared_key},
        ).scalars().all()
    assert list(owners) == ["ns-one", "ns-two"]


def test_the_same_file_in_two_namespaces_is_two_attachments(
    db_engine: Engine,
) -> None:
    """Content uniqueness is per session, so it never spans a namespace.

    Scoped any wider it would be a content oracle: a caller could learn that
    somebody else already held a given file by watching for a dedupe hit. Here
    the same bytes in two tenants are simply two rows.
    """
    shared_sha = uuid.uuid4().hex + uuid.uuid4().hex
    first = _insert_session(db_engine, namespace_key="ns-one")
    second = _insert_session(db_engine, namespace_key="ns-two")

    _insert_attachment(
        db_engine, namespace_key="ns-one", session_id=first, source_sha256=shared_sha
    )
    _insert_attachment(
        db_engine, namespace_key="ns-two", session_id=second, source_sha256=shared_sha
    )

    with db_engine.begin() as conn:
        holders = conn.execute(
            text(
                "SELECT namespace_key FROM agent_session_attachments "
                " WHERE source_sha256 = :sha ORDER BY namespace_key"
            ),
            {"sha": shared_sha},
        ).scalars().all()
    assert list(holders) == ["ns-one", "ns-two"]


def test_deleting_one_namespaces_session_leaves_the_others_file_and_bytes(
    db_engine: Engine,
) -> None:
    """The cascade follows the tenancy anchor two hops down and stops there.

    Session delete is the one path that reclaims attachment *metadata*, so a
    cascade that overreached would destroy another tenant's audit record along
    with their document.
    """
    first = _insert_session(db_engine, namespace_key="ns-one")
    second = _insert_session(db_engine, namespace_key="ns-two")
    doomed = _insert_attachment(db_engine, namespace_key="ns-one", session_id=first)
    survivor = _insert_attachment(db_engine, namespace_key="ns-two", session_id=second)
    _insert_attachment_blob(db_engine, namespace_key="ns-one", attachment_id=doomed)
    _insert_attachment_blob(db_engine, namespace_key="ns-two", attachment_id=survivor)
    _insert_turn_attachment(
        db_engine, namespace_key="ns-one", session_id=first, attachment_id=doomed
    )
    _insert_turn_attachment(
        db_engine, namespace_key="ns-two", session_id=second, attachment_id=survivor
    )

    with db_engine.begin() as conn:
        conn.execute(text("DELETE FROM agent_sessions WHERE id = :id"), {"id": first})
        attachments = conn.execute(
            text("SELECT namespace_key FROM agent_session_attachments")
        ).scalars().all()
        blobs = conn.execute(
            text("SELECT namespace_key FROM agent_session_attachment_blobs")
        ).scalars().all()
        bindings = conn.execute(
            text("SELECT namespace_key FROM agent_turn_attachments")
        ).scalars().all()

    assert list(attachments) == ["ns-two"]
    assert list(blobs) == ["ns-two"]
    assert list(bindings) == ["ns-two"]
