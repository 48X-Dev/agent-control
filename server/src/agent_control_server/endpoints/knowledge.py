"""The two machine-side reads of the company-knowledge mirror.

Beside the nudge-consume route, on the same path prefix and through the *same*
context builder rather than a copy of it: the runtime token is the session
identity, so the session key has to come out of the path and be handed to the
verifier, which refuses a token bound to any other session. A token minted for
session A physically cannot search as session B.

**The window spends ``principal.target_id``, not the path.** Only a provider
that verified a binding populates that field, and the verifier has already
refused it on mismatch, so under the runtime-token path it is the session and
is a ceiling. Under ``NoAuthProvider`` - the shipped default - and under the
header fallback it is ``None``, and every caller shares one namespace-wide
bucket. That is plan 8.1's stated fallback, and it is the only honest one: the
``{session_key}`` segment is a string the caller typed, so a window keyed on it
would be refreshed for free by a loop that counts upwards, which is not a
ceiling however much it reads like one.

**Registered whether or not the corpus is configured, and that is deliberate.**
A deployment with the feature off answers ``knowledge_disabled``, a stated
refusal an agent can read and carry on from. Not registering the routes would
answer 404, which the tool would have to guess at, and would make the generated
OpenAPI spec - and therefore every SDK built from it - depend on one
deployment's environment.

**Neither route looks the session up, and that is not an oversight.** These
read a corpus; they touch no session row and no control-plane table at all. The
session key is here to be *compared against the token*, which the verifier does
before the handler runs. Adding a lookup would put a control-plane query on the
path of every search to enforce something the token already enforces, and under
the no-auth provider it would enforce nothing anyway.

**Refusals are 200 with a code, not HTTP errors.** Three reasons, in order:
the closed enum exists precisely so the *body* carries the reason; every
response including a refusal must carry ``result_count`` and
``external_author_count``, because the shipped deny control fails closed on a
missing field; and a knowledge base that is switched off is not a client error
- the turn is supposed to continue, and the model is supposed to say it could
not check.
"""

from __future__ import annotations

from agent_control_models.knowledge_search import (
    KnowledgeRecentRequest,
    KnowledgeSearchRequest,
    KnowledgeSearchResponse,
)
from fastapi import APIRouter, Depends

from ..auth_framework import Operation, Principal, require_operation
from ..services import knowledge_search
from .agent_nudges import session_target_context

router = APIRouter(prefix="/agent-sessions", tags=["company-knowledge"])


@router.post(
    "/{session_key}/knowledge/search",
    response_model=KnowledgeSearchResponse,
    summary="Search the company knowledge base",
    response_description="Ranked snippets, or a stated refusal",
)
async def search_knowledge(
    session_key: str,
    request: KnowledgeSearchRequest,
    principal: Principal = Depends(
        require_operation(
            Operation.COMPANY_KNOWLEDGE_SEARCH,
            context_builder=session_target_context,
        )
    ),
) -> KnowledgeSearchResponse:
    """Machine side. Called by an agent's tool during a turn.

    Ranked search with a hard k, and nothing else. There is no cursor, no
    offset, no wildcard and no list: an agent's whole reach into the corpus is
    one page of ranked results per call, because a list call plus a loop is the
    entire corpus in a transcript in an afternoon.

    The query is model-authored text descended from whatever somebody wrote in
    a task body, so it is treated as such: bounded, parameterized into
    ``websearch_to_tsquery``, visible to pre-stage controls at ``input.query``,
    logged at DEBUG only, and unable to trigger anything but a SELECT.
    """
    return await knowledge_search.search(
        namespace_key=principal.namespace_key,
        session_key=session_key,
        meter_key=principal.target_id,
        query=request.query,
        max_results=request.max_results,
    )


@router.post(
    "/{session_key}/knowledge/recent",
    response_model=KnowledgeSearchResponse,
    summary="List what changed in the company knowledge base",
    response_description="Recently modified documents, or a stated refusal",
)
async def recent_knowledge(
    session_key: str,
    request: KnowledgeRecentRequest,
    principal: Principal = Depends(
        require_operation(
            Operation.COMPANY_KNOWLEDGE_SEARCH,
            context_builder=session_target_context,
        )
    ),
) -> KnowledgeSearchResponse:
    """Machine side. "What changed", bounded so it is not enumeration.

    Same operation and same meter as search, because it is the same reach into
    the same corpus by a different sort. What bounds it is the window ceiling
    and the k-cap: one page of what moved this fortnight, no cursor, and a
    second identical call returns the same page - so a loop gains nothing but
    its own rate limit.
    """
    return await knowledge_search.recent(
        namespace_key=principal.namespace_key,
        session_key=session_key,
        meter_key=principal.target_id,
        days=request.days,
        max_results=request.max_results,
    )
