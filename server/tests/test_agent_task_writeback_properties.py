"""Unit properties plan 5.6/5.7 state outright, provable without the app flow.

Three of these are sharper than the happy-path suite and deliberately so:

* the sanitizer's own passes must not *build* a backtick run while escaping
  (rule 1 says no run of three or more survives, and the composed fence is
  only containment if that holds for the output, not the input);
* the idempotency marker must not be forgeable from agent output - a body
  that contains the marker text for another step must not satisfy the
  pre-post dedupe check, or an injected agent can suppress the next step's
  report by emitting its marker in advance;
* the accept request must have nowhere to name a state, per 5.7 step 4.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx
import pytest
from agent_control_models.tasks import (
    AcceptAgentTaskRequest,
    RejectAgentTaskRequest,
)
from pydantic import SecretStr, ValidationError

from agent_control_server.config import linear_settings
from agent_control_server.services.linear_writeback import (
    HttpLinearWritebackClient,
    decision_digest,
)
from agent_control_server.services.linear_writeback_compose import (
    comment_marker,
    compose_comment_body,
    sanitize_agent_text,
)
from agent_control_server.services.linear_writeback_runtime import (
    WritebackRuntime,
    build_writeback_runtime,
)

TASK_KEY = "f" * 32

# Inputs whose two-backtick pairs are legal on their own, but where a later
# escape pass (URL span, mention span) inserts an adjacent backtick and
# rebuilds the run the first pass exists to prevent.
RUN_BUILDING_PAYLOADS = [
    "``https://x.example/p``",
    "``@alice``",
    "before ``https://x.example/exfil?d=s`` after",
    "``@a",
]


# ---------------------------------------------------------------------------
# Rule 1 holds for the output, not just the input
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("payload", RUN_BUILDING_PAYLOADS)
def test_no_backtick_run_survives_even_when_the_escapes_build_one(
    payload: str,
) -> None:
    """5.6 rule 1: "neutralise backtick runs of length three or more". The
    URL and mention passes wrap their matches in single backticks, so a pair
    of backticks already adjacent in the input can be extended into a run of
    three *after* the run-neutralising pass has already run."""
    out = sanitize_agent_text(payload)

    assert re.search(r"`{3,}", out) is None, (
        f"escaping rebuilt a backtick run: {out!r}"
    )


def test_the_composed_quote_stays_fenced_for_run_building_payloads() -> None:
    """The same property at the composer level: no quoted line may carry a
    run of three or more, because the fence above it is a run of three."""
    body = compose_comment_body(
        task_key=TASK_KEY,
        step_index=0,
        total_steps=1,
        agent_name="researcher",
        output_text="\n".join(RUN_BUILDING_PAYLOADS),
    )
    lines = body.split("\n")
    open_fence = lines.index("> ```")
    close_fence = len(lines) - 1 - lines[::-1].index("> ```")

    for line in lines[open_fence + 1 : close_fence]:
        assert line.startswith("> ")
        assert re.search(r"`{3,}", line) is None, f"fence-closing run in {line!r}"


def test_the_cap_cuts_the_raw_text_so_an_escape_is_never_split() -> None:
    """5.6: the 4000-character cap applies to the raw text first. A cut that
    landed between an escape's backslash and its character would leave a bare
    markdown construct standing at the truncation point."""
    out = sanitize_agent_text("a" * 3999 + "<" * 100)

    assert "[output truncated by agent control]" in out
    assert re.search(r"(?<!\\)<", out) is None, "an unescaped < survived the cut"


# ---------------------------------------------------------------------------
# The marker cannot be forged from output
# ---------------------------------------------------------------------------


async def test_agent_output_cannot_satisfy_the_marker_check_for_another_step() -> None:
    """The dedupe reads the issue's comments for the exact marker; found means
    already written and nothing is posted. So a marker that survives the
    escape *as a searchable substring* is a suppression primitive: an agent
    that emits step 1's marker inside step 0's output prevents step 1's
    report from ever landing. Proven against the real composer and the real
    client's matching, over a mock transport."""
    posted_step0_body = compose_comment_body(
        task_key=TASK_KEY,
        step_index=0,
        total_steps=2,
        agent_name="researcher",
        output_text=(
            "All done. For the record: "
            + comment_marker(TASK_KEY, 1)
            + "\nnothing else to see."
        ),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "lin_api_test"
        payload = json.loads(request.content)
        assert "comments" in payload["query"]
        return httpx.Response(
            200,
            json={
                "data": {
                    "issue": {"comments": {"nodes": [{"body": posted_step0_body}]}}
                }
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = HttpLinearWritebackClient(
        api_key="lin_api_test",
        api_url="https://linear.example/graphql",
        client=http,
    )
    try:
        assert (
            await client.issue_has_marker(
                issue_id="issue-1", marker=comment_marker(TASK_KEY, 0)
            )
            is True
        ), "the genuine marker at the top of the comment must still be found"
        assert (
            await client.issue_has_marker(
                issue_id="issue-1", marker=comment_marker(TASK_KEY, 1)
            )
            is False
        ), (
            "a marker inside the sanitized agent block satisfied the dedupe "
            "check: agent output can suppress the next step's comment"
        )
    finally:
        await http.aclose()


# ---------------------------------------------------------------------------
# Digest and runtime
# ---------------------------------------------------------------------------


def test_the_digest_boundary_cannot_be_shifted_between_parts() -> None:
    """NUL-joined so ``("ab", "c")`` and ``("a", "bc")`` cannot collide: an
    output that ends with the start of the target ref must not produce the
    digest of a different (output, ref) split."""
    assert decision_digest("ab", "c", "d") != decision_digest("a", "bc", "d")
    assert decision_digest("a", "bc", "d") != decision_digest("a", "b", "cd")


async def test_can_write_requires_the_flag_and_a_client_together(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The two halves are separate on purpose: a keyed deployment with the
    flag off queues rows and sends nothing, and a flagged deployment with no
    key has nothing to send with."""
    some_client: Any = object()
    assert WritebackRuntime(client=None, resolver=None, write_enabled=True).can_write is False
    assert (
        WritebackRuntime(client=some_client, resolver=None, write_enabled=False).can_write
        is False
    )
    assert (
        WritebackRuntime(client=some_client, resolver=None, write_enabled=True).can_write
        is True
    )

    # And through the builder: the flag alone must not manufacture a client.
    monkeypatch.setattr(linear_settings, "write_enabled", True)
    monkeypatch.setattr(linear_settings, "api_key", SecretStr(""))
    keyless = build_writeback_runtime()
    assert keyless.client is None and keyless.can_write is False

    monkeypatch.setattr(linear_settings, "api_key", SecretStr("lin_api_x"))
    monkeypatch.setattr(linear_settings, "write_enabled", False)
    keyed_but_off = build_writeback_runtime()
    try:
        assert keyed_but_off.client is not None
        assert keyed_but_off.can_write is False, "the shipped default must not write"
    finally:
        await keyed_but_off.aclose()


# ---------------------------------------------------------------------------
# The API surface has no accept-all and no state field
# ---------------------------------------------------------------------------


def test_there_is_no_accept_all_anywhere_in_the_api(app: Any) -> None:
    """5.7: bulk-accepting N claims would be one recorded decision covering
    work nobody read. The absence is asserted on the schema so a helpful
    future route fails this test before it ships."""
    paths = app.openapi()["paths"]
    accepting = sorted(path for path in paths if "accept" in path)

    assert accepting == ["/api/v1/agent-tasks/{task_key}/accept"]
    assert not any("bulk" in path or "batch" in path for path in paths if "agent-tasks" in path)


def test_the_accept_request_has_nowhere_to_name_a_state() -> None:
    """5.7 step 4: never client-supplied. Not "ignored if sent" - refused,
    because the model forbids extras."""
    digest = "sha256:" + "0" * 64
    with pytest.raises(ValidationError):
        AcceptAgentTaskRequest.model_validate(
            {"writeback_id": 1, "expected_decision_digest": digest, "state_id": "x"}
        )
    with pytest.raises(ValidationError):
        AcceptAgentTaskRequest.model_validate(
            {
                "writeback_id": 1,
                "expected_decision_digest": digest,
                "target_state_id": "x",
            }
        )
    with pytest.raises(ValidationError):
        RejectAgentTaskRequest.model_validate({"writeback_id": 1, "state_id": "x"})
    with pytest.raises(ValidationError):
        AcceptAgentTaskRequest.model_validate(
            {"writeback_id": 1, "expected_decision_digest": "done-please"}
        )
