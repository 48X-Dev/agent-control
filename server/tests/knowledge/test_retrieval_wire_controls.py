"""What the shipped controls make of a result that came off the real wire.

The control tests beside this file drive the engine over payloads written by
hand, which is the right way to pin a control's own logic. This file asks the
next question: when the number in the dict was computed by the server, from a
document seeded in a corpus, does the control that was written for it fire?

That is where the two wrong controls this design already met would have been
caught by behaviour rather than by review - the deny that fired on zero, and
the control scoped to a bare tool name - and it is the level at which W-K3 and
W-K5 can be proved without a live model. Each test says which half of its wire
test it is proving and which half it is not.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from agent_control._state import state
from agent_control.integrations.google_adk.knowledge_controls import (
    DENY_EXTERNAL_AUTHOR,
    DENY_FENCE_IN_WEB_ARGS,
    OBSERVE_REFUSAL,
    SEARCH_STEP_NAME,
    WEB_STEP_NAMES,
)
from agent_control_models.knowledge import KnowledgeRefusalCode
from agent_control_models.knowledge_render import FENCE_PREFIX
from agent_control_server.auth_framework import Operation, set_authorizer
from agent_control_server.auth_framework.config import (
    RuntimeAuthConfig,
    set_runtime_auth_config,
)
from agent_control_server.auth_framework.providers import LocalJwtVerifyProvider
from agent_control_server.config import knowledge_settings
from agent_control_server.knowledge.seed import SeedDocument
from agent_control_server.services.knowledge_quota import reset_knowledge_quota

from tests.knowledge.support import LAPTOPS, handbook, seed
from tests.knowledge.wire_support import (
    RUNTIME_SECRET,
    SESSION,
    bound,
    content_control,
    context,
    judge,
    search,
)
from tests.knowledge_provisioning import Corpus


@pytest.fixture(autouse=True)
def corpus_wired(corpus: Corpus) -> Iterator[None]:
    """Point the process-wide settings at the throwaway corpus and put them back."""
    fields = type(knowledge_settings).model_fields
    saved = {name: getattr(knowledge_settings, name) for name in fields}
    knowledge_settings.enabled = True
    knowledge_settings.db_url = corpus.read_url
    reset_knowledge_quota()
    yield
    for name, value in saved.items():
        setattr(knowledge_settings, name, value)
    reset_knowledge_quota()


@pytest.fixture()
async def tool_wire(live_server: Any) -> AsyncIterator[None]:
    """Serve the real app, route the operation through the token verifier.

    ``state.server_url`` is what the tool builds its request from, so pointing
    it at the loopback port is the whole of the wiring an executor container
    does with an environment variable.
    """
    set_runtime_auth_config(RuntimeAuthConfig(secret=RUNTIME_SECRET, ttl_seconds=900))
    set_authorizer(
        LocalJwtVerifyProvider(secret=RUNTIME_SECRET),
        operation=Operation.COMPANY_KNOWLEDGE_SEARCH,
    )
    saved = state.server_url
    state.server_url = live_server.base_url
    yield
    state.server_url = saved
    set_runtime_auth_config(None)


SSN = "123-45-6789"


async def test_a_sensitive_string_in_a_snippet_is_visible_to_the_post_control(
    corpus: Corpus, tool_wire: None
) -> None:
    """W-K3's provable half: the control sees it, on the real dict, and denies.

    What is proved here is the visibility and the verdict. What is not proved
    is the last hop, that the plugin drops the denied result before the model
    reads it, because that hop needs a live turn. Stated rather than implied,
    so nobody reads this test as the whole of W-K3.

    Both selector spellings are checked. The plan recommends ``output.text``;
    the ``block-ssn`` control in this repo's own README selects ``output``, and
    an operator copying that onto this tool must not silently get a control
    that never fires.
    """
    body = LAPTOPS + f"\nThe claimant's reference number is {SSN} on the form.\n"
    seed(corpus, docs=[SeedDocument(path="Ops Handbook/laptops.md", body=body)])

    result = await search(query="claimant reference number")
    assert SSN in result["text"], "the snippet never carried the string the control keys on"

    pattern = r"\b\d{3}-\d{2}-\d{4}\b"
    for path in ("output.text", "output"):
        matched = await judge(
            content_control(pattern, step_names=[SEARCH_STEP_NAME], stage="post", path=path),
            step_name=SEARCH_STEP_NAME,
            stage="post",
            step_output=result,
        )
        assert matched == ["block-ssn"], f"selector {path!r} did not fire"


async def test_a_snippet_nobody_can_vouch_for_denies_through_the_shipped_control(
    corpus: Corpus, tool_wire: None
) -> None:
    """The example deny, fed the number the server computed rather than one a
    test wrote down.

    Authorship the sync could not establish arrives as ``unknown``, and this
    count exists for exactly this control, so reading "we could not tell" as
    "safe" would fail open in the one field built to fail closed. A
    workspace-authored corpus goes through the same control in the same run,
    because a deny that fires on everything is a deny the first inconvenienced
    operator switches off.
    """
    seed(
        corpus,
        source_ref="shared",
        source_name="Shared Folder",
        docs=[SeedDocument(path="Shared Folder/laptops.md", body=LAPTOPS, author_kind="unknown")],
    )
    unattributed = await search()

    seed(corpus, source_ref="shared", source_name="Shared Folder", enabled=False, docs=[])
    seed(corpus, **handbook())
    workspace = await search()

    assert unattributed["external_author_count"] == unattributed["result_count"] >= 1
    assert workspace["external_author_count"] == 0
    assert await judge(
        bound(DENY_EXTERNAL_AUTHOR),
        step_name=SEARCH_STEP_NAME,
        stage="post",
        step_output=unattributed,
    ) == [DENY_EXTERNAL_AUTHOR]
    assert (
        await judge(
            bound(DENY_EXTERNAL_AUTHOR),
            step_name=SEARCH_STEP_NAME,
            stage="post",
            step_output=workspace,
        )
        == []
    )


async def test_every_refusal_the_server_puts_on_the_wire_is_one_the_bound_control_reads(
    corpus: Corpus, tool_wire: None, live_server: Any
) -> None:
    """The shipped regex and the server's enum are two lists that must agree.

    Asserting them against each other by hand proves two strings match. Asking
    the running server for every refusal it can produce, over HTTP, and feeding
    each code it returned into the real evaluator proves the pair that actually
    meets in a deployment. Every code below came off the wire; none is typed
    into this file.
    """
    token = context().state["agent_control"]["runtime_token"]
    client = live_server.client(headers={"Authorization": f"Bearer {token}"})
    path = f"/api/v1/agent-sessions/{SESSION}/knowledge/search"

    async def code_for(**body: Any) -> str | None:
        body.setdefault("query", "laptop reimbursement")
        response = await client.post(path, json=body)
        assert response.status_code == 200, response.text
        return response.json()["refusal_code"]

    seen: list[str | None] = [await code_for(), await code_for(query="ab")]
    seen.append(await code_for(query="x" * 501))

    seed(corpus, **handbook())
    knowledge_settings.searches_per_minute = 1
    await code_for()
    seen.append(await code_for())
    knowledge_settings.searches_per_minute = 6
    reset_knowledge_quota()

    knowledge_settings.db_url = corpus.read_url.replace(corpus.database, "no_such_corpus")
    seen.append(await code_for())
    knowledge_settings.db_url = corpus.read_url

    knowledge_settings.enabled = False
    seen.append(await code_for())
    knowledge_settings.enabled = True

    assert set(seen) == {code.value for code in KnowledgeRefusalCode}, (
        "the server produced a different set of refusals than the enum names"
    )
    for code in seen:
        matched = await judge(
            bound(OBSERVE_REFUSAL),
            step_name=SEARCH_STEP_NAME,
            stage="post",
            step_output={
                "text": "…",
                "result_count": 0,
                "external_author_count": 0,
                "stale_seconds": None,
                "refusal_code": code,
            },
        )
        assert matched == [OBSERVE_REFUSAL], f"{code} went unobserved"


async def test_an_unreachable_corpus_is_a_sentence_with_no_postgres_in_it(
    corpus: Corpus, tool_wire: None
) -> None:
    """The failure an agent is most likely to meet, and the one most likely to
    leak. A driver message names the host, the database and sometimes the role;
    what reaches the model is a constant, and the observe control turns the
    event into something an operator can find."""
    seed(corpus, **handbook())
    knowledge_settings.db_url = corpus.read_url.replace(corpus.database, "no_such_corpus")

    result = await search()

    assert result["refusal_code"] == KnowledgeRefusalCode.KNOWLEDGE_UNAVAILABLE
    for leak in ("psycopg", "FATAL", "does not exist", corpus.database, "5432", "Traceback"):
        assert leak not in result["text"]
    assert await judge(
        bound(OBSERVE_REFUSAL), step_name=SEARCH_STEP_NAME, stage="post", step_output=result
    ) == [OBSERVE_REFUSAL]


# ---------------------------------------------------------------------------
# The egress pair (plan 3.1), with real corpus text in the argument
# ---------------------------------------------------------------------------


MARKER = "Zarquon-Seven"


async def test_snippet_text_reaches_a_web_tool_argument_where_a_pre_control_reads_it(
    corpus: Corpus, tool_wire: None
) -> None:
    """W-K5's provable half, and the honest limit of the tripwire beside it.

    The string in the argument is not typed into this test; it is taken out of
    what the corpus returned, so the path this asserts is the real one: a
    document's words, through the tool, into a free-form outbound argument.

    Three claims, in the order plan 3.1 ranks the mechanisms it has:

    1. a content control scoped to the web step sees the composed argument and
       denies it, which is the mechanism that actually generalizes;
    2. the shipped tripwire denies the whole-block copy-paste form;
    3. the tripwire does **not** catch the same text with the fence stripped,
       which is a model paraphrasing rather than pasting. That is asserted on
       purpose. A tripwire whose limit is untested gets read as a boundary.
    """
    body = LAPTOPS.replace("Laptops are", f"The {MARKER} laptop scheme is")
    seed(corpus, docs=[SeedDocument(path="Ops Handbook/laptops.md", body=body)])

    fenced = (await search())["text"]
    assert MARKER in fenced, "the corpus never produced the text this test carries outward"
    paraphrased = fenced.replace(FENCE_PREFIX, "").replace(">>>", "")

    tripwire = bound(DENY_FENCE_IN_WEB_ARGS)
    content = content_control(MARKER, step_names=WEB_STEP_NAMES, stage="pre", path="input")
    web_step = WEB_STEP_NAMES[0]

    assert await judge(
        content, step_name=web_step, stage="pre", step_input={"query": paraphrased}
    ) == ["block-ssn"]
    assert await judge(tripwire, step_name=web_step, stage="pre", step_input={"query": fenced}) == [
        DENY_FENCE_IN_WEB_ARGS
    ]
    assert (
        await judge(tripwire, step_name=web_step, stage="pre", step_input={"query": paraphrased})
        == []
    ), "the tripwire is a copy-paste catch, not a boundary; see plan 3.1"
