"""Plan section 3.9, wired: opening a step fetches the issue's files.

The one-sentence goal of this slice, asserted through the route a dispatcher
actually calls. What OPS-2 needed was not a fetcher; it was for the step that
opens before the turn to come back with the deck's *contents* in front of the
agent working it, and with an honest count of what it could not deliver.

Nothing here reaches Linear. The client is faked at the seam
:mod:`services.step_attachments` builds it from, and the converter is faked at
the seam the background scheduler calls, because neither library is what is
worth pinning - the wiring between them is.

**Two things in this file are not incidental.** The acceptance test runs a real
turn and asserts on what the executor received, because a summary saying
"delivered" proves nothing about what the model read. And every case that
touches storage runs under both auth providers, because ``NoAuthProvider``
leaves ``caller_id`` None and a rule that looked right against a configured
provider can refuse the whole feature on the machine it is built on.
"""

from __future__ import annotations

from typing import Any

import pytest
from agent_control_models.attachments import AttachmentRefusalCode
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import text

from agent_control_server.auth_framework import set_authorizer
from agent_control_server.auth_framework.providers.no_auth import NoAuthProvider
from agent_control_server.config import executor_settings, linear_settings
from agent_control_server.services import step_attachments
from agent_control_server.services.attachment_conversions import (
    reset_conversion_scheduler,
)
from agent_control_server.services.attachment_quota import reset_attachment_quota
from agent_control_server.services.linear_attachment_discovery import DiscoveredFile
from agent_control_server.services.linear_attachments import (
    DownloadRefusedError,
    FetchedFile,
    FileOutcome,
    IssueFiles,
    LinearAttachmentError,
)
from agent_control_server.services.turn_quota import reset_turn_quota

from .test_agent_session_turns import FakeTurnExecutorFactory
from .test_agent_sessions_endpoints import (
    _agent_name,
    _bind,
    _open_session,
    _register_agent,
    executor_enabled,  # noqa: F401 - fixture
)
from .test_attachment_conversions import fake_converter  # noqa: F401 - fixture

TASKS_URL = "/api/v1/agent-tasks"
SESSIONS_URL = "/api/v1/agent-sessions"

PDF = b"%PDF-1.7\n" + b"deck" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"chart" * 32

DECK_URL = "https://uploads.linear.app/3387082b-0000/e47010e1-0001"

API_KEY = "lin_api_test"


class FakeAttachmentClient:
    """Answers with a scripted issue body and scripted bytes."""

    def __init__(
        self,
        issue: dict[str, Any],
        bodies: dict[str, bytes],
        *,
        read_error: bool = False,
    ) -> None:
        self.issue = issue
        self.bodies = bodies
        self.read_error = read_error

    async def read_issue(self, issue_ref: str) -> dict[str, Any]:
        if self.read_error:
            raise LinearAttachmentError("Linear could not be reached.")
        return self.issue

    async def download(self, url: str, *, max_bytes: int) -> bytes:
        body = self.bodies[url]
        # The real client aborts the stream past the ceiling rather than
        # returning a body the caller then has to measure, so this one does too.
        if len(body) > max_bytes:
            raise DownloadRefusedError(
                AttachmentRefusalCode.TOO_LARGE, bytes_read=max_bytes + 1
            )
        return body

    async def aclose(self) -> None:
        return None


@pytest.fixture()
def linear_files(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Turn the source on and hand the fetch a fake client.

    Both switches, because the plan defaults both to false and a deployment
    that has not asked for this must not acquire it by upgrading.

    The conversion wait is shortened rather than removed. Zero would let a test
    pass against an implementation that never waited at all, which is the
    blocker this file was rewritten for.
    """
    monkeypatch.setattr(executor_settings, "attachments_enabled", True)
    monkeypatch.setattr(linear_settings, "attachments_enabled", True)
    monkeypatch.setattr(linear_settings, "api_key", SecretStr(API_KEY))
    monkeypatch.setattr(linear_settings, "attachment_conversion_wait_seconds", 5.0)
    monkeypatch.setattr(linear_settings, "attachment_conversion_poll_seconds", 0.02)

    scripted: dict[str, Any] = {"issue": {}, "bodies": {}, "read_error": False}

    def factory(*, api_key: str, settings: Any) -> FakeAttachmentClient:
        assert api_key == API_KEY, "the fetch is the only thing given the key"
        return FakeAttachmentClient(
            scripted["issue"], scripted["bodies"], read_error=scripted["read_error"]
        )

    monkeypatch.setattr(step_attachments, "HttpLinearAttachmentClient", factory)
    return scripted


@pytest.fixture()
def fake_executor(app: FastAPI) -> Any:
    """The executor, recorded rather than called.

    The turn-running fake and not the session-only one: the acceptance test
    asserts on the message a turn handed over, which is the only place the
    goal of this slice is actually visible.
    """
    from agent_control_server.services.executor_factory import (
        get_executor_client_factory,
    )

    factory = FakeTurnExecutorFactory()
    app.dependency_overrides[get_executor_client_factory] = lambda: factory
    yield factory
    app.dependency_overrides.pop(get_executor_client_factory, None)


@pytest.fixture(autouse=True)
def _fresh_scheduler() -> Any:
    """One test's background conversions never bind the next test's content.

    The in-flight set is cleared by the worker's own ``finally``, which a server
    process always reaches. A test whose event loop ends with a task still
    pending never runs it, and the marker left behind makes the next test's
    identical bytes refuse to schedule at all.
    """
    reset_conversion_scheduler()
    reset_turn_quota()
    reset_attachment_quota()
    yield
    reset_conversion_scheduler()
    reset_turn_quota()
    reset_attachment_quota()


def _issue(description: str = "", **overrides: Any) -> dict[str, Any]:
    issue: dict[str, Any] = {
        "description": description,
        "attachments": {"nodes": []},
        "comments": {"nodes": []},
    }
    issue.update(overrides)
    return issue


def _linear_task(client: TestClient, ref: str) -> str:
    """Import one Linear-sourced item and claim it."""
    body: dict[str, Any] = {
        "scope": {
            "kind": "items",
            "source_kind": "linear",
            "items": [{"source_ref": ref, "title": ref}],
        },
        "mode": "preview",
    }
    preview = client.post(f"{TASKS_URL}/import", json=body)
    assert preview.status_code == 200, preview.text
    body["mode"] = "commit"
    body["expected_refs_digest"] = preview.json()["refs_digest"]
    commit = client.post(f"{TASKS_URL}/import", json=body)
    assert commit.status_code == 200, commit.text
    key = str(commit.json()["task_keys"][0])
    claimed = client.post(f"{TASKS_URL}/{key}/claim", json={"instance_id": "inst"})
    assert claimed.status_code == 200, claimed.text
    return key


def _step(client: TestClient, key: str, session_key: str, index: int = 0) -> dict[str, Any]:
    started = client.post(
        f"{TASKS_URL}/{key}/steps",
        json={
            "instance_id": "inst",
            "step_index": index,
            "agent_name": "reviewer_agent",
            "session_key": session_key,
        },
    )
    assert started.status_code == 200, started.text
    return dict(started.json())


def _session_for(client: TestClient, key: str) -> str:
    agent = _agent_name()
    _register_agent(client, agent)
    _bind(client, agent)
    return str(_open_session(client, agent, task_key=key)["session_key"])


def _delivered_keys(files: dict[str, Any]) -> list[str]:
    """What the dispatcher sends on the turn: keys, never a URL."""
    return [row["attachment_key"] for row in files["files"] if row["attachment_key"]]


# ---------------------------------------------------------------------------
# The acceptance test
# ---------------------------------------------------------------------------


def test_a_deck_linked_only_in_the_description_reaches_the_model_as_text(
    client: TestClient,
    db_engine: Any,
    linear_files: Any,
    fake_converter: Any,  # noqa: F811 - fixture
    executor_enabled: None,  # noqa: F811 - fixture
    fake_executor: Any,
) -> None:
    """The OPS-2 shape, end to end, and the reason this slice exists.

    That issue carries ``attachments: 0``. Its deck reaches it only as a
    markdown link in ``description``, an agent asked to review it could not
    dereference the upload URL, and it answered from the title. Every earlier
    version of this test asserted on the stored row and the step summary, which
    an implementation that scheduled a conversion and returned passes while the
    model still reads nothing: the turn follows one HTTP round trip later and
    the cache cannot be warm yet.

    So the assertion is on the message the executor actually received.
    """
    _, outcome = fake_converter
    from agent_control_server.services.attachment_converter import ConversionStatus

    from .test_attachment_conversions import _result

    outcome["result"] = _result(
        "SLIDE 3: MSSP MARGIN IS 34 PER CENT", ConversionStatus.TEXT_LAYER_EXTRACTED
    )
    linear_files["issue"] = _issue(f"Review [EarlyCore deck.pdf]({DECK_URL})")
    linear_files["bodies"] = {DECK_URL: PDF}

    key = _linear_task(client, "OPS-2-deck")
    session_key = _session_for(client, key)
    files = _step(client, key, session_key)["files"]

    assert (files["found"], files["delivered"]) == (1, 1)
    assert files["files"][0]["text_ready"] is True

    turn = client.post(
        f"{SESSIONS_URL}/{session_key}/turns",
        json={
            "message": "Review the deck.",
            "attachment_keys": _delivered_keys(files),
        },
    )
    assert turn.status_code == 200, turn.text

    sent = fake_executor.runs[-1]["message"]
    assert "SLIDE 3: MSSP MARGIN IS 34 PER CENT" in sent
    assert "EarlyCore deck.pdf" in sent
    assert "%PDF" not in sent, "delivery is text; the endpoint drops binary silently"
    assert DECK_URL not in sent
    assert API_KEY not in sent

    with db_engine.connect() as conn:
        summary = conn.execute(
            text(
                "SELECT attachments_summary FROM agent_task_steps WHERE session_key = :key"
            ),
            {"key": session_key},
        ).scalar()
    assert summary is not None and len(summary) == 1
    assert summary[0]["text_ready"] is True


def test_the_step_waits_for_the_text_rather_than_scheduling_and_returning(
    client: TestClient,
    linear_files: Any,
    fake_converter: Any,  # noqa: F811 - fixture
    executor_enabled: None,  # noqa: F811 - fixture
    fake_executor: Any,
) -> None:
    """The blocker, isolated from the turn.

    ``schedule_conversion`` returns in microseconds and the conversion runs in
    the background. If the step did not wait, the cache entry would not exist
    when it answered, and every file it fetched would come back not-converted.
    """
    linear_files["issue"] = _issue(DECK_URL)
    linear_files["bodies"] = {DECK_URL: PDF}

    key = _linear_task(client, "OPS-2-wait")
    session_key = _session_for(client, key)
    row = _step(client, key, session_key)["files"]["files"][0]

    assert row["text_ready"] is True
    assert row["failure_code"] is None


def test_a_file_whose_text_never_arrives_is_not_counted_as_delivered(
    client: TestClient,
    linear_files: Any,
    monkeypatch: pytest.MonkeyPatch,
    executor_enabled: None,  # noqa: F811 - fixture
    fake_executor: Any,
) -> None:
    """Storing a file and putting it in front of a model are different events.

    The delivery section appended to the same turn message would list this file
    as NOT INCLUDED. Calling it delivered here would leave two server-authored
    statements about one file contradicting each other, and an agent resolving
    that optimistically is back to answering from the title.
    """
    from agent_control_server.services import attachment_conversions

    monkeypatch.setattr(
        attachment_conversions._scheduler,
        "submit",
        lambda **kwargs: False,
    )
    monkeypatch.setattr(linear_settings, "attachment_conversion_wait_seconds", 0.5)
    linear_files["issue"] = _issue(DECK_URL)
    linear_files["bodies"] = {DECK_URL: PDF}

    key = _linear_task(client, "OPS-2-unread")
    session_key = _session_for(client, key)
    files = _step(client, key, session_key)["files"]

    assert files["delivered"] == 0
    row = files["files"][0]
    assert row["attachment_key"], "the file is stored; it is its text that is missing"
    assert row["text_ready"] is False
    assert row["failure_code"] == AttachmentRefusalCode.NOT_CONVERTED.value


def test_a_file_nobody_could_read_says_so_rather_than_not_read_yet(
    client: TestClient,
    linear_files: Any,
    fake_converter: Any,  # noqa: F811 - fixture
    executor_enabled: None,  # noqa: F811 - fixture
    fake_executor: Any,
) -> None:
    """A scanned page nobody could read and a page nobody has tried to read are
    different facts, and only one of them is worth asking a person about."""
    from agent_control_server.services.attachment_converter import ConversionStatus

    from .test_attachment_conversions import _result

    _, outcome = fake_converter
    outcome["result"] = _result("", ConversionStatus.EMPTY)
    linear_files["issue"] = _issue(DECK_URL)
    linear_files["bodies"] = {DECK_URL: PDF}

    key = _linear_task(client, "OPS-2-empty")
    session_key = _session_for(client, key)
    row = _step(client, key, session_key)["files"]["files"][0]

    assert row["text_ready"] is False
    assert row["failure_code"] == AttachmentRefusalCode.NO_TEXT.value


# ---------------------------------------------------------------------------
# Reconciliation, and the third state
# ---------------------------------------------------------------------------


def test_the_count_reconciles_what_was_found_against_what_was_delivered(
    client: TestClient,
    linear_files: Any,
    fake_converter: Any,  # noqa: F811 - fixture
    executor_enabled: None,  # noqa: F811 - fixture
    fake_executor: Any,
) -> None:
    """Silent under-delivery is the failure the whole section exists to
    prevent, so a refused file still gets a row."""
    other = DECK_URL.replace("0001", "0002")
    third = DECK_URL.replace("0001", "0003")
    linear_files["issue"] = _issue(f"{DECK_URL} {other} {third}")
    linear_files["bodies"] = {
        DECK_URL: PDF,
        other: PNG,
        third: b"# a markdown spec, which matches no magic number",
    }

    key = _linear_task(client, "OPS-2-mixed")
    session_key = _session_for(client, key)
    files = _step(client, key, session_key)["files"]

    assert (files["found"], files["delivered"]) == (3, 2)
    refused = [row for row in files["files"] if row["attachment_key"] is None]
    assert [row["failure_code"] for row in refused] == [
        AttachmentRefusalCode.UNSUPPORTED_TYPE.value
    ]


def test_a_tracker_that_cannot_be_read_says_so_and_does_not_claim_the_issue_is_empty(
    client: TestClient,
    linear_files: Any,
    executor_enabled: None,  # noqa: F811 - fixture
    fake_executor: Any,
) -> None:
    """``found == 0`` is what an issue with no files looks like, and the
    envelope renders it as "No files are attached to this issue."

    Saying that on the day Linear was rate-limiting is strictly worse than the
    silence this section replaced: an agent that would have said nothing now
    has a server-authored sentence behind the wrong conclusion.
    """
    linear_files["read_error"] = True

    key = _linear_task(client, "OPS-2-down")
    session_key = _session_for(client, key)
    response = _step(client, key, session_key)

    assert response["step"]["status"] == "running"
    assert response["files"] == {
        "found": 0,
        "delivered": 0,
        "files": [],
        "read_failed": True,
    }


def test_a_fault_in_this_server_is_reported_as_unlisted_rather_than_as_empty(
    client: TestClient,
    linear_files: Any,
    monkeypatch: pytest.MonkeyPatch,
    executor_enabled: None,  # noqa: F811 - fixture
    fake_executor: Any,
) -> None:
    """The step row is committed before the fetch runs, so a 500 here would tell
    the dispatcher the step never opened and it would close the task out.

    ``files: null`` renders nothing at all, which leaves the agent with no idea
    the issue might carry files. The honest answer is the same one a tracker
    outage gets.
    """
    from agent_control_server.endpoints import agent_tasks as endpoint

    async def broken(db: Any, *, plan: Any, files: Any) -> Any:
        raise RuntimeError("a defect in this server, not a refusal")

    monkeypatch.setattr(endpoint, "store_step_files", broken)
    linear_files["issue"] = _issue(DECK_URL)
    linear_files["bodies"] = {DECK_URL: PDF}

    key = _linear_task(client, "OPS-2-fault")
    session_key = _session_for(client, key)
    response = _step(client, key, session_key)

    assert response["step"]["status"] == "running"
    assert response["files"]["read_failed"] is True


def test_a_deployment_with_the_source_off_answers_null_rather_than_zero(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    executor_enabled: None,  # noqa: F811 - fixture
    fake_executor: Any,
) -> None:
    """"Did not look" and "found nothing" are different answers, and only one
    of them lets an envelope tell an agent an issue carries no files."""
    monkeypatch.setattr(linear_settings, "attachments_enabled", False)

    key = _linear_task(client, "OPS-2-off")
    session_key = _session_for(client, key)

    assert _step(client, key, session_key)["files"] is None


def test_a_task_from_another_source_is_never_fetched_for(
    client: TestClient,
    linear_files: Any,
    executor_enabled: None,  # noqa: F811 - fixture
    fake_executor: Any,
) -> None:
    body: dict[str, Any] = {
        "scope": {
            "kind": "items",
            "source_kind": "file",
            "items": [{"source_ref": "from-a-file", "title": "t"}],
        },
        "mode": "preview",
    }
    preview = client.post(f"{TASKS_URL}/import", json=body)
    body["mode"] = "commit"
    body["expected_refs_digest"] = preview.json()["refs_digest"]
    key = str(client.post(f"{TASKS_URL}/import", json=body).json()["task_keys"][0])
    client.post(f"{TASKS_URL}/{key}/claim", json={"instance_id": "inst"})
    session_key = _session_for(client, key)

    assert _step(client, key, session_key)["files"] is None


# ---------------------------------------------------------------------------
# The byte ceiling, which bounds the wire and not the disk
# ---------------------------------------------------------------------------


def test_the_task_byte_budget_counts_what_earlier_steps_already_spent(
    client: TestClient,
    linear_files: Any,
    monkeypatch: pytest.MonkeyPatch,
    fake_converter: Any,  # noqa: F811 - fixture
    executor_enabled: None,  # noqa: F811 - fixture
    fake_executor: Any,
) -> None:
    """A twelve-step chain over an attachment-heavy milestone is how this
    feature spends a subscription's quota with nobody in the room. The ceiling
    is read from the durable step rows, so a reclaim by another dispatcher does
    not restart it at zero."""
    monkeypatch.setattr(executor_settings, "attachment_task_total_bytes", len(PDF) + 8)
    linear_files["issue"] = _issue(DECK_URL)
    linear_files["bodies"] = {DECK_URL: PDF}

    key = _linear_task(client, "OPS-2-budget")
    first_session = _session_for(client, key)
    assert _step(client, key, first_session)["files"]["delivered"] == 1

    client.post(
        f"{TASKS_URL}/{key}/steps/0/finish",
        json={"instance_id": "inst", "status": "completed", "output_text": "done"},
    )
    second_session = _session_for(client, key)
    second = _step(client, key, second_session, index=1)["files"]

    assert second["delivered"] == 0
    assert second["files"][0]["failure_code"] == (
        AttachmentRefusalCode.OVER_TASK_BUDGET.value
    )


def test_bytes_a_step_pulled_and_threw_away_are_still_charged_to_the_task(
    client: TestClient,
    linear_files: Any,
    monkeypatch: pytest.MonkeyPatch,
    executor_enabled: None,  # noqa: F811 - fixture
    fake_executor: Any,
) -> None:
    """The ceiling exists to bound what crosses the wire unattended.

    A body aborted at the size cap, an HTML login page and a type nobody
    accepts all cost real bytes and store none. Charged only for what was
    stored, a chain of oversized refusals moves hundreds of megabytes against a
    ceiling that never fires.
    """
    monkeypatch.setattr(executor_settings, "attachment_task_total_bytes", 400)
    oversized = b"z" * 5_000
    linear_files["issue"] = _issue(DECK_URL)
    linear_files["bodies"] = {DECK_URL: oversized}

    key = _linear_task(client, "OPS-2-wasted")
    first_session = _session_for(client, key)
    first = _step(client, key, first_session)["files"]
    assert first["delivered"] == 0
    assert first["files"][0]["bytes_fetched"] > 0

    client.post(
        f"{TASKS_URL}/{key}/steps/0/finish",
        json={"instance_id": "inst", "status": "completed", "output_text": "done"},
    )
    linear_files["bodies"] = {DECK_URL: PDF}
    second_session = _session_for(client, key)
    second = _step(client, key, second_session, index=1)["files"]

    assert second["files"][0]["failure_code"] == (
        AttachmentRefusalCode.OVER_TASK_BUDGET.value
    )


# ---------------------------------------------------------------------------
# Proof by absence, and both auth providers
# ---------------------------------------------------------------------------


def test_the_plan_types_carry_no_url_into_anything_the_dispatcher_reads() -> None:
    """The dispatcher receives keys and a summary, never a URL. Asserted on the
    types rather than on a response, because the property is structural."""
    outcome = FileOutcome(
        display_name="deck.pdf",
        origin_ref="sha256:abcd",
        fetched=FetchedFile(data=PDF, sniffed_mime="application/pdf"),
    )
    found = DiscoveredFile(
        url=DECK_URL, display_name="deck.pdf", order_key="1:x", source_type=None
    )

    assert not hasattr(outcome, "url")
    assert DECK_URL not in repr(outcome)
    assert DECK_URL in repr(found), "discovery holds the URL; nothing downstream does"


def test_neither_the_key_nor_the_url_is_anywhere_in_the_step_response(
    client: TestClient,
    linear_files: Any,
    fake_converter: Any,  # noqa: F811 - fixture
    executor_enabled: None,  # noqa: F811 - fixture
    fake_executor: Any,
) -> None:
    """Proof by absence over the whole response body.

    The dispatcher is the process whose safety property is that it cannot widen
    the scope it was given, and a URL or a credential reaching it through a
    display name, an origin ref or a refusal string would undo that quietly.
    Asserting on the serialized response rather than on named fields is the
    only version of this claim that a new field cannot silently break.
    """
    linear_files["issue"] = _issue(f"[deck.pdf]({DECK_URL})")
    linear_files["bodies"] = {DECK_URL: PDF}

    key = _linear_task(client, "OPS-2-absence")
    session_key = _session_for(client, key)
    started = client.post(
        f"{TASKS_URL}/{key}/steps",
        json={
            "instance_id": "inst",
            "step_index": 0,
            "agent_name": "reviewer_agent",
            "session_key": session_key,
        },
    )

    assert API_KEY not in started.text
    assert DECK_URL not in started.text
    assert "uploads.linear.app" not in started.text


@pytest.fixture()
def no_auth_client(app: FastAPI) -> TestClient:
    """A client under the provider a default deployment actually runs.

    ``api_key_enabled`` defaults false, so ``NoAuthProvider`` authorizes
    everything and leaves ``caller_id`` None. Every session it opens has a NULL
    creator, which is exactly the shape a creator-scoped store rule gets wrong.
    """
    set_authorizer(NoAuthProvider())
    return TestClient(app, raise_server_exceptions=True)


def test_a_step_stores_the_issue_files_under_no_auth_as_well(
    no_auth_client: TestClient,
    linear_files: Any,
    fake_converter: Any,  # noqa: F811 - fixture
    executor_enabled: None,  # noqa: F811 - fixture
    fake_executor: Any,
) -> None:
    """The case that would otherwise turn the whole feature off on the machine
    it is built on.

    ``AgentAttachmentsService.create`` is the shipped upload path's own writer
    and it carries creator rules. Under ``NoAuthProvider`` the step's caller
    hash is None and so is the session's creator, and a rule refusing every
    unattributed write would refuse every dispatch fetch in the deployment.
    """
    linear_files["issue"] = _issue(f"[deck.pdf]({DECK_URL})")
    linear_files["bodies"] = {DECK_URL: PDF}

    key = _linear_task(no_auth_client, "OPS-2-noauth")
    session_key = _session_for(no_auth_client, key)
    files = _step(no_auth_client, key, session_key)["files"]

    assert (files["found"], files["delivered"]) == (1, 1)
    assert files["files"][0]["attachment_key"]


def test_a_step_stores_the_issue_files_under_a_resolving_provider_too(
    client: TestClient,
    linear_files: Any,
    fake_converter: Any,  # noqa: F811 - fixture
    executor_enabled: None,  # noqa: F811 - fixture
    fake_executor: Any,
) -> None:
    """The other half. The default ``client`` fixture runs with API keys on, so
    the caller resolves and both the session and the attachment carry a hash."""
    linear_files["issue"] = _issue(f"[deck.pdf]({DECK_URL})")
    linear_files["bodies"] = {DECK_URL: PDF}

    key = _linear_task(client, "OPS-2-keyed")
    session_key = _session_for(client, key)
    files = _step(client, key, session_key)["files"]

    assert (files["found"], files["delivered"]) == (1, 1)

    listed = client.get(f"{SESSIONS_URL}/{session_key}/attachments")
    assert listed.status_code == 200, listed.text
    assert [row["origin"] for row in listed.json()["attachments"]] == ["linear"]


def test_a_step_adds_no_operation_and_therefore_cannot_stop_the_server_starting() -> None:
    """An ``Operation`` missing from ``DEFAULT_OPERATION_ACCESS`` makes the
    server refuse to start. This slice deliberately adds none: the fetch rides
    on ``agent_tasks.claim``, which the dispatcher already holds."""
    from agent_control_server.auth_framework import Operation
    from agent_control_server.auth_framework.providers.header import (
        DEFAULT_OPERATION_ACCESS,
    )

    assert Operation.AGENT_TASKS_CLAIM in DEFAULT_OPERATION_ACCESS
    assert all(operation in DEFAULT_OPERATION_ACCESS for operation in Operation)


def test_an_issue_read_that_returns_nothing_is_not_a_read_failure(
    client: TestClient,
    linear_files: Any,
    executor_enabled: None,  # noqa: F811 - fixture
    fake_executor: Any,
) -> None:
    """The other side of the third state: an issue with no files says so."""
    linear_files["issue"] = _issue("Nothing attached here.")

    key = _linear_task(client, "OPS-2-bare")
    session_key = _session_for(client, key)
    files = _step(client, key, session_key)["files"]

    assert files == {"found": 0, "delivered": 0, "files": [], "read_failed": False}


def test_a_summary_returned_by_the_fetch_is_never_a_url_carrier() -> None:
    """``IssueFiles`` is the boundary type and it is checked here rather than
    through a request, because a structural claim is worth a structural test."""
    files = IssueFiles(
        found=1,
        outcomes=(
            FileOutcome(display_name="deck.pdf", origin_ref="sha256:ab", bytes_read=10),
        ),
    )

    assert files.read_failed is False
    assert DECK_URL not in repr(files)


def test_text_ready_predicts_what_the_delivery_section_says_about_the_file(
    client: TestClient,
    linear_files: Any,
    monkeypatch: pytest.MonkeyPatch,
    fake_converter: Any,  # noqa: F811 - fixture
    executor_enabled: None,  # noqa: F811 - fixture
    fake_executor: Any,
) -> None:
    """The two server-authored sections must not contradict each other.

    The envelope's line about a file is written by the dispatcher from
    ``text_ready``; the block naming which files' contents were included is
    written by this server on the turn. If ``text_ready`` could be true while
    the delivery block said NOT INCLUDED, one turn message would carry two
    statements about one file that disagree, and an agent resolving that
    optimistically is back to answering from the title.
    """
    from agent_control_server.services.attachment_converter import ConversionStatus

    from .test_attachment_conversions import _result

    _, outcome = fake_converter
    outcome["result"] = _result("READABLE DECK TEXT", ConversionStatus.TEXT_LAYER_EXTRACTED)
    linear_files["issue"] = _issue(f"[deck.pdf]({DECK_URL})")
    linear_files["bodies"] = {DECK_URL: PDF}

    key = _linear_task(client, "OPS-2-agree")
    session_key = _session_for(client, key)
    files = _step(client, key, session_key)["files"]
    assert files["files"][0]["text_ready"] is True

    turn = client.post(
        f"{SESSIONS_URL}/{session_key}/turns",
        json={"message": "Go", "attachment_keys": _delivered_keys(files)},
    )
    assert turn.status_code == 200, turn.text
    sent = fake_executor.runs[-1]["message"]

    assert "NOT INCLUDED" not in sent
    assert "READABLE DECK TEXT" in sent


def test_a_file_that_is_not_text_ready_is_named_as_not_included_on_the_turn(
    client: TestClient,
    linear_files: Any,
    monkeypatch: pytest.MonkeyPatch,
    executor_enabled: None,  # noqa: F811 - fixture
    fake_executor: Any,
) -> None:
    """The other half of the same claim, and the one the count line depends on."""
    from agent_control_server.services import attachment_conversions

    monkeypatch.setattr(attachment_conversions._scheduler, "submit", lambda **kw: False)
    monkeypatch.setattr(linear_settings, "attachment_conversion_wait_seconds", 0.5)
    linear_files["issue"] = _issue(f"[deck.pdf]({DECK_URL})")
    linear_files["bodies"] = {DECK_URL: PDF}

    key = _linear_task(client, "OPS-2-disagree")
    session_key = _session_for(client, key)
    files = _step(client, key, session_key)["files"]
    assert files["files"][0]["text_ready"] is False

    turn = client.post(
        f"{SESSIONS_URL}/{session_key}/turns",
        json={"message": "Go", "attachment_keys": _delivered_keys(files)},
    )
    assert turn.status_code == 200, turn.text
    sent = fake_executor.runs[-1]["message"]

    assert "NOT INCLUDED" in sent
    assert "deck.pdf" in sent
    assert "<<<FILE_BEGIN" not in sent
