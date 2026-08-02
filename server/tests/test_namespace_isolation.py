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
