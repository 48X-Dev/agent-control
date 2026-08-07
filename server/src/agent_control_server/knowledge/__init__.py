"""The control plane's read-only view of the company-knowledge corpus.

Everything in this package talks to ``agent_knowledge`` through the
``knowledge_read`` role, which holds SELECT and nothing else, over an engine
that shares nothing with the control plane's. No FastAPI, no request objects,
no Pydantic in the query path: this is the domain layer the endpoint will call,
and it stays callable from a script, a test, or the MCP surface.

The corpus itself is written by a separate process with separate credentials.
Nothing here can start a sync, fetch a document, or write a row.
"""

from .engine import (
    KnowledgeUnavailableError,
    dispose_knowledge_engine,
    knowledge_session,
)
from .schema import (
    KNOWLEDGE_METADATA,
    SUPPORTED_SCHEMA_VERSIONS,
    chunks,
    documents,
    schema_meta,
    sources,
    sync_lease,
    sync_runs,
    synonyms,
)
from .store import (
    CorpusStats,
    SnippetRow,
    corpus_stats,
    is_supported_schema,
    read_schema_version,
    recent_documents,
    search_chunks,
    search_chunks_trigram,
)

__all__ = [
    "CorpusStats",
    "KNOWLEDGE_METADATA",
    "KnowledgeUnavailableError",
    "SUPPORTED_SCHEMA_VERSIONS",
    "SnippetRow",
    "chunks",
    "corpus_stats",
    "dispose_knowledge_engine",
    "documents",
    "is_supported_schema",
    "knowledge_session",
    "read_schema_version",
    "recent_documents",
    "schema_meta",
    "search_chunks",
    "search_chunks_trigram",
    "sources",
    "sync_lease",
    "sync_runs",
    "synonyms",
]
