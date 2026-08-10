"""The human side of the company-knowledge mirror: the console's two reads.

The agents' routes hang off ``/agent-sessions/{session_key}/`` because a
runtime token *is* a session and the verifier compares the path against it. A
browser has no session key and never will, so the console cannot use those
routes at all - not as a matter of taste, but because
``require_operation(COMPANY_KNOWLEDGE_SEARCH, context_builder=...)`` refuses a
credential with no target binding. Hence a second surface, on the operation
that was declared for exactly this and had no route until now.

**Same policy, different door.** Every bound, clamp, refusal code and
neutralization here comes from ``services.knowledge_search``, untouched. That
module refuses to import FastAPI for this reason: the day a second surface
appeared, the alternative was two retrieval policies drifting apart.

**Metered too, and that is not paranoia.** A console read is a read of the same
corpus, and an unmetered surface beside a metered one is a bypass of the
ceiling rather than a convenience. Human traffic gets its own bucket rather
than sharing the agents': a person typing in a search box must not spend the
fleet's allowance, and the fleet must not lock a person out of the panel they
would use to find out why.

**The tier is ``company_knowledge.status``**, which plan 16's Phase 3b names in
as many words. It is deliberately not the agents' operation: handing a console
``company_knowledge.search`` would hand a browser the machine-side credential
and every ceiling attached to it, which is the thing declaring two operations
was meant to prevent. What that widens is real and worth stating - an
authenticated console user can now read snippet text, where before the tier
covered only counters - and it is the same reach section 9 already gives every
agent in the namespace, on a corpus whose security boundary is the allowlist
and not the read path.
"""

from __future__ import annotations

from agent_control_models.knowledge_search import (
    KnowledgeRecentRequest,
    KnowledgeSearchRequest,
    KnowledgeSearchResponse,
)
from agent_control_models.knowledge_status import (
    KnowledgeSourceStatus,
    KnowledgeStatus,
)
from fastapi import APIRouter, Depends

from ..auth_framework import Operation, Principal, require_operation
from ..config import knowledge_settings
from ..knowledge.status import CorpusStatus, SourceStatus, read_status
from ..services import knowledge_search
from ..services.caller_identity import hash_caller_id

router = APIRouter(prefix="/company-knowledge", tags=["company-knowledge"])

CONSOLE_METER_PREFIX = "console:"
"""What keeps the console's window and the fleet's window apart.

Composed here from a hashed caller, never from anything a request carries, so
no caller can put itself in the other population's bucket or mint a fresh one
by asking differently. Under a provider that resolves nobody the suffix is
empty and every console read shares one bucket, which is the same fallback the
agents' path takes and for the same reason."""


def _meter_key(principal: Principal) -> str:
    return f"{CONSOLE_METER_PREFIX}{hash_caller_id(principal.caller_id) or ''}"


@router.post(
    "/search",
    response_model=KnowledgeSearchResponse,
    summary="Search the company knowledge base from the console",
    response_description="Ranked snippets, or a stated refusal",
)
async def search_company_knowledge(
    request: KnowledgeSearchRequest,
    principal: Principal = Depends(
        require_operation(Operation.COMPANY_KNOWLEDGE_STATUS)
    ),
) -> KnowledgeSearchResponse:
    """Human side. What a person asks the corpus from the Knowledge panel.

    Returns exactly what an agent's tool receives, including the neutralization:
    a snippet is corpus text either way, and a filename that could forge a fence
    header is a filename that could carry markup into a browser. Defusing it
    once on the way out is what makes both renderings safe from one rule.
    """
    return await knowledge_search.search(
        namespace_key=principal.namespace_key,
        session_key="console",
        meter_key=_meter_key(principal),
        query=request.query,
        max_results=request.max_results,
    )


@router.post(
    "/recent",
    response_model=KnowledgeSearchResponse,
    summary="List what changed in the company knowledge base",
    response_description="Recently modified documents, or a stated refusal",
)
async def recent_company_knowledge(
    request: KnowledgeRecentRequest,
    principal: Principal = Depends(
        require_operation(Operation.COMPANY_KNOWLEDGE_STATUS)
    ),
) -> KnowledgeSearchResponse:
    """Human side. "What changed", and the panel's opening view.

    It opens here rather than on an empty search box because the ``corpus``
    block every response carries is the freshness strip, so the page can say
    how many documents there are and how far behind the mirror is without a
    person having thought of a query first. The dedicated status endpoint with
    per-source cursors and run outcomes is Phase 4 and is not this.
    """
    return await knowledge_search.recent(
        namespace_key=principal.namespace_key,
        session_key="console",
        meter_key=_meter_key(principal),
        days=request.days,
        max_results=request.max_results,
    )


@router.get(
    "/status",
    response_model=KnowledgeStatus,
    summary="Report the knowledge mirror's freshness and per-source health",
    response_description="Per-source freshness, or a stated inability to read the corpus",
    dependencies=[Depends(require_operation(Operation.COMPANY_KNOWLEDGE_STATUS))],
)
async def company_knowledge_status() -> KnowledgeStatus:
    """Oversight. Whether the mirror is current, and which source is not.

    A GET where its two neighbours are POSTs, because it carries no query.
    Never an error status either: an unreachable or unreadable corpus answers
    200 with ``schema_supported`` false, so the panel built to show a broken
    sync keeps working when there is one.
    """
    return _status_model(await read_status(), knowledge_settings.staleness_warn_seconds)


def _status_model(status: CorpusStatus, staleness_warn_seconds: int) -> KnowledgeStatus:
    """The domain reading, plus the one number that is configuration."""
    return KnowledgeStatus(
        schema_version=status.schema_version,
        schema_supported=status.schema_supported,
        document_count=status.document_count,
        chunk_count=status.chunk_count,
        stale_seconds=status.stale_seconds,
        staleness_warn_seconds=staleness_warn_seconds,
        sources_failing=status.sources_failing,
        sources=[_source_model(source) for source in status.sources],
    )


def _source_model(source: SourceStatus) -> KnowledgeSourceStatus:
    return KnowledgeSourceStatus(
        source_id=source.source_id,
        kind=source.kind,
        enabled=source.enabled,
        last_verified_at=source.last_verified_at,
        cursor_advanced_at=source.cursor_advanced_at,
        stale_seconds=source.stale_seconds,
        document_count=source.document_count,
        failing=source.failing,
        last_failure_code=source.last_failure_code,
        refusals_by_code=source.refusals_by_code,
    )
