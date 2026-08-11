"""``POST /turns`` with files on it, end to end through the real endpoint.

The delivery renderer has its own file; this one asserts the wiring around it,
which is where the interesting failures live:

* what the executor is actually handed - text, never bytes, because this
  deployment's endpoint answers 200 and silently drops an inline file;
* that a file nobody could send refuses the turn **before** a model call is
  paid for, and that nothing reached the executor when it does;
* that a turn never waits on a conversion, whatever state the cache is in;
* that what a turn carried is recorded, per file, with a verdict.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from agent_control_models.attachment_converter_cache import (
    conversion_cache_key,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from agent_control_server.config import executor_settings
from agent_control_server.services import attachment_delivery as delivery
from agent_control_server.services.attachment_quota import reset_attachment_quota
from agent_control_server.services.executor_factory import get_executor_client_factory
from agent_control_server.services.turn_quota import reset_turn_quota

from .conftest import engine
from .test_agent_attachments_endpoints import PDF_BYTES, upload
from .test_agent_session_turns import FakeTurnExecutorFactory, _bound_session

_SESSIONS_URL = "/api/v1/agent-sessions"


@pytest.fixture(autouse=True)
def attachments_enabled(monkeypatch: pytest.MonkeyPatch):
    """Both switches, because a turn with a file needs both features on."""
    monkeypatch.setattr(executor_settings, "enabled", True)
    monkeypatch.setattr(executor_settings, "attachments_enabled", True)
    reset_turn_quota()
    reset_attachment_quota()
    yield
    reset_turn_quota()
    reset_attachment_quota()


@pytest.fixture()
def fake_executor(app: FastAPI) -> Any:
    """The executor, recorded rather than called.

    Declared here rather than imported: pytest resolves a fixture by name, and
    importing one into a second module makes both files' names the same object
    to the linter without making the dependency any clearer to a reader.
    """
    factory = FakeTurnExecutorFactory()
    app.dependency_overrides[get_executor_client_factory] = lambda: factory
    yield factory
    app.dependency_overrides.pop(get_executor_client_factory, None)


def _seed_conversion(source_sha256: str, body: str) -> None:
    """Pretend the background worker already read this content."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO agent_attachment_conversions "
                "(namespace_key, cache_key, source_sha256, state, status, "
                " text_body, text_chars, meaningful_chars) "
                "VALUES ('default', :key, :sha, 'done', 'text_layer_extracted', "
                "        :body, :chars, :chars)"
            ),
            {
                "key": conversion_cache_key(source_sha256),
                "sha": source_sha256,
                "body": body,
                "chars": len(body),
            },
        )


def _turn(client: TestClient, session_key: str, keys: list[str], message: str = "Read this") -> Any:
    return client.post(
        f"{_SESSIONS_URL}/{session_key}/turns",
        json={"message": message, "attachment_keys": keys},
    )


def _bindings(session_key: str) -> list[tuple[str, str | None]]:
    with engine.begin() as conn:
        return [
            (row[0], row[1])
            for row in conn.execute(
                text(
                    "SELECT b.verdict, b.blocked_reason "
                    "  FROM agent_turn_attachments b "
                    "  JOIN agent_sessions s "
                    "    ON s.id = b.session_id AND s.namespace_key = b.namespace_key "
                    " WHERE s.session_key = :key ORDER BY b.position"
                ),
                {"key": session_key},
            )
        ]


def test_a_converted_file_reaches_the_executor_as_text(
    client: TestClient, fake_executor: Any
) -> None:
    """The measured constraint, wired: text on the wire and no bytes anywhere.

    ``POST /v1/files`` is a 404 against this deployment's endpoint and an inline
    file block is a 200 with the file dropped, so a delivery that posted bytes
    would look like it worked and leave the agent answering from the filename.
    """
    session = _bound_session(client)
    created = upload(client, session["session_key"]).json()["attachment"]
    _seed_conversion(created["source_sha256"], "REVENUE GREW 12 PER CENT")

    response = _turn(client, session["session_key"], [created["attachment_key"]])
    assert response.status_code == 200, response.text

    sent = fake_executor.runs[-1]["message"]
    assert sent.startswith("Read this\n\n")
    assert "REVENUE GREW 12 PER CENT" in sent
    assert "1 file attached" in sent
    assert "%PDF" not in sent


def test_a_file_the_cache_has_not_read_yet_is_named_and_never_waited_on(
    client: TestClient, fake_executor: Any
) -> None:
    """A turn sent straight after an upload. Conversion is tens of seconds.

    The turn runs anyway, the agent is told the file exists and that its
    contents are not available, and nothing here blocks - which is the whole
    reason conversion is out of band rather than on a timeout.
    """
    session = _bound_session(client)
    created = upload(client, session["session_key"]).json()["attachment"]

    response = _turn(client, session["session_key"], [created["attachment_key"]])
    assert response.status_code == 200, response.text

    sent = fake_executor.runs[-1]["message"]
    assert "NOT INCLUDED" in sent
    assert "spec.pdf" in sent
    assert "<<<FILE_BEGIN" not in sent


def test_an_unknown_attachment_key_refuses_the_turn_and_spends_nothing(
    client: TestClient, fake_executor: Any
) -> None:
    session = _bound_session(client)
    response = _turn(client, session["session_key"], [uuid.uuid4().hex])
    assert response.status_code == 404
    assert response.json()["error_code"] == "ATTACHMENT_NOT_FOUND"
    assert fake_executor.runs == []


def test_an_attachment_that_is_not_ready_refuses_the_turn(
    client: TestClient, fake_executor: Any
) -> None:
    """The chat panel's deliberate asymmetry with the dispatch path.

    Nobody is watching a dispatch chain, so an undelivered file there becomes a
    line the agent reads. Here the operator is standing in front of the
    composer, so the turn is refused and nothing is spent.
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
    assert response.status_code == 409
    assert response.json()["error_code"] == "ATTACHMENT_NOT_READY"
    assert fake_executor.runs == []


def test_another_sessions_attachment_is_not_reachable_from_this_turn(
    client: TestClient, fake_executor: Any
) -> None:
    """Scoped by session as well as by namespace, and asserted on the executor.

    A post-filter and a scoped query answer the same 404, and only one of them
    keeps another conversation's document out of this one's model call.
    """
    mine = _bound_session(client)
    theirs = _bound_session(client)
    created = upload(client, theirs["session_key"]).json()["attachment"]

    response = _turn(client, mine["session_key"], [created["attachment_key"]])
    assert response.status_code == 404
    assert fake_executor.runs == []


def test_what_the_turn_carried_is_recorded_per_file_with_a_verdict(
    client: TestClient, fake_executor: Any
) -> None:
    """ "Named to the agent" and "read by the agent" are different facts.

    Both files are on the turn; only one had text. A record that called both
    ``sent`` would be answering the wrong question a year later.
    """
    session = _bound_session(client)
    readable = upload(client, session["session_key"], data=PDF_BYTES, filename="read.pdf").json()[
        "attachment"
    ]
    unread = upload(
        client,
        session["session_key"],
        data=PDF_BYTES + b"different",
        filename="unread.pdf",
    ).json()["attachment"]
    _seed_conversion(readable["source_sha256"], "the contents")

    response = _turn(
        client,
        session["session_key"],
        [readable["attachment_key"], unread["attachment_key"]],
    )
    assert response.status_code == 200, response.text

    verdicts = _bindings(session["session_key"])
    assert [row[0] for row in verdicts] == ["sent", "blocked"]
    assert verdicts[0][1] is None
    assert verdicts[1][1] is not None


def test_a_bug_in_the_renderer_does_not_become_advice_to_shorten_the_message(
    client: TestClient, fake_executor: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The remedies are different, so the answers have to be.

    A fault in the delivery renderer belongs to this server. Answering it with
    the overflow refusal - "this message is too long to carry its files as
    well, shorten it" - sends the operator round a loop with no exit: the
    message was never too long, shortening it changes nothing, and the only
    trace is a warning nobody is watching. So the turn runs, and the agent is
    told in the count line that it got none of the files.
    """

    def _explode(message: str, attachments: list[Any]) -> Any:
        del message, attachments
        raise RuntimeError("a bug in this module")

    monkeypatch.setattr(delivery, "_render", _explode)
    session = _bound_session(client)
    created = upload(client, session["session_key"]).json()["attachment"]
    _seed_conversion(created["source_sha256"], "REVENUE GREW 12 PER CENT")

    response = _turn(client, session["session_key"], [created["attachment_key"]])
    assert response.status_code == 200, response.text

    sent = fake_executor.runs[-1]["message"]
    assert sent.startswith("Read this\n\n")
    assert "0 of 1 file" in sent
    # The document did not go, and the ledger says so rather than claiming it.
    assert "REVENUE GREW 12 PER CENT" not in sent
    assert [row[0] for row in _bindings(session["session_key"])] == ["blocked"]


def test_the_same_key_twice_carries_one_file_and_says_one_file(
    client: TestClient, fake_executor: Any
) -> None:
    """The count line is the one sentence this design asks an agent to trust.

    A key repeated by a caller is one file. Delivered twice it would be two
    copies of one document under a line saying two files were attached, paid
    for twice out of a budget that is already turning genuinely different files
    away - and recorded once, because the ledger's primary key does not repeat.
    """
    session = _bound_session(client)
    created = upload(client, session["session_key"]).json()["attachment"]
    _seed_conversion(created["source_sha256"], "REVENUE GREW 12 PER CENT")
    key = created["attachment_key"]

    response = _turn(client, session["session_key"], [key, key])
    assert response.status_code == 200, response.text

    sent = fake_executor.runs[-1]["message"]
    assert "1 file attached" in sent
    assert sent.count("<<<FILE_BEGIN") == 1
    assert sent.count("REVENUE GREW 12 PER CENT") == 1
    assert _bindings(session["session_key"]) == [("sent", None)]


def test_a_turn_refused_over_its_files_is_not_counted_as_abandoned(
    client: TestClient, fake_executor: Any
) -> None:
    """A configured refusal and a client hanging up are opposite facts.

    ``abandoned`` reads as people giving up on an agent. A deployment whose
    per-turn attachment ceiling is set too low would file itself under that and
    look like a user-behaviour problem rather than a configuration one.
    """
    session = _bound_session(client)
    assert _turn(client, session["session_key"], [uuid.uuid4().hex]).status_code == 404
    assert fake_executor.runs == []

    metrics = client.get("/metrics")
    assert metrics.status_code == 200, metrics.text
    assert 'outcome="attachment_refused"' in metrics.text


def test_a_turn_with_no_attachment_keys_is_unchanged(
    client: TestClient, fake_executor: Any
) -> None:
    """The regression guard for every existing caller of this endpoint."""
    session = _bound_session(client)
    response = client.post(
        f"{_SESSIONS_URL}/{session['session_key']}/turns", json={"message": "Hello"}
    )
    assert response.status_code == 200, response.text
    assert fake_executor.runs[-1]["message"] == "Hello"
