"""The corpus's own engine: small, lazy, and never the control plane's.

Three properties, each with a reason a reviewer can check.

**Its own pool.** ``agent_turns.py``'s docstring guards a pool that agent turns
depend on. If a knowledge search borrowed connections from it, a knowledge
database that stopped answering would drain the pool and take chat down with
it. Two connections, separate pool, ``pool_pre_ping``: the failure mode is a
typed refusal on the search and a turn that proceeds.

**Built lazily.** ``db.py`` builds its engine at import, which is right for a
database the server cannot run without. This one is optional and off by
default, so an import-time engine would mean a missing or malformed DSN
crashed startup for a feature nobody enabled.

**Read-only by credential, not by convention.** The DSN is the
``knowledge_read`` role, which holds SELECT. Nothing in this package writes,
and if something tried, Postgres would refuse it rather than a code review.

The corpus schema is checked on the first query through each pool, not at
startup and not per query. Startup is wrong because the server must boot with
no knowledge database at all; per query is waste, because the sync cannot move
the schema under a pool without the pool noticing when it is next rebuilt.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from agent_control_models.knowledge import KnowledgeRefusalCode
from sqlalchemy.engine.url import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from ..config import KnowledgeSettings, knowledge_settings
from .store import is_supported_schema, read_schema_version

logger = logging.getLogger(__name__)


class KnowledgeUnavailableError(RuntimeError):
    """The corpus cannot be reached or cannot be trusted to parse.

    Carries a code from the refusal enum rather than a driver message, because
    no Postgres error text and no upstream body ever reaches an agent.
    """

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


# Everything about the settings that a built engine bakes in. Keying the cache
# on the DSN alone would hand a caller that tightened the statement ceiling the
# pool built for whoever asked first, and the tightened value would be dropped
# on the floor with nothing to show for it.
EngineKey = tuple[str, int, int, float]

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None
_engine_key: EngineKey | None = None
_schema_checked: EngineKey | None = None
_lock = asyncio.Lock()


def _key_for(url: str, settings: KnowledgeSettings) -> EngineKey:
    return (
        url,
        settings.pool_size,
        settings.connect_timeout_seconds,
        settings.statement_timeout_seconds,
    )


def _connect_args(url: str, settings: KnowledgeSettings) -> dict[str, Any]:
    """Driver-level bounds. Pool timeouts bound waiting; these bound the work."""
    driver = make_url(url).get_driver_name()
    statement_timeout_ms = int(settings.statement_timeout_seconds * 1000)
    if driver == "psycopg":
        args: dict[str, Any] = {"connect_timeout": settings.connect_timeout_seconds}
        if statement_timeout_ms:
            args["options"] = f"-c statement_timeout={statement_timeout_ms}"
        return args
    if driver == "asyncpg":
        args = {"timeout": float(settings.connect_timeout_seconds)}
        if statement_timeout_ms:
            args["server_settings"] = {"statement_timeout": str(statement_timeout_ms)}
        return args
    return {}


async def _get_sessionmaker(
    settings: KnowledgeSettings,
) -> tuple[async_sessionmaker[AsyncSession], EngineKey]:
    global _engine, _sessionmaker, _engine_key

    if not settings.enabled:
        raise KnowledgeUnavailableError(
            KnowledgeRefusalCode.KNOWLEDGE_DISABLED,
            "the knowledge base is switched off on this server",
        )
    url = settings.db_url
    if not url:
        raise KnowledgeUnavailableError(
            KnowledgeRefusalCode.KNOWLEDGE_UNAVAILABLE,
            "no knowledge database is configured on this server",
        )

    key = _key_for(url, settings)
    async with _lock:
        if _sessionmaker is not None and _engine_key == key:
            return _sessionmaker, key
        if _engine is not None:
            await _engine.dispose()
        _engine = create_async_engine(
            url,
            echo=False,
            pool_pre_ping=True,
            pool_size=settings.pool_size,
            max_overflow=0,
            pool_reset_on_return="rollback",
            connect_args=_connect_args(url, settings),
        )
        _engine_key = key
        _sessionmaker = async_sessionmaker(
            bind=_engine,
            autoflush=False,
            expire_on_commit=False,
            class_=AsyncSession,
        )
        return _sessionmaker, key


async def _acquire(
    settings: KnowledgeSettings,
) -> tuple[async_sessionmaker[AsyncSession], EngineKey]:
    """The pool, or a typed refusal. Never a driver exception.

    Broad on purpose, and it can afford to be: no caller code runs in here, so
    the only thing this can hide is a failure to build an engine, which is a
    refusal either way. It has to be broad because a mistyped DSN does not
    raise a SQLAlchemy error - ``postgresql://`` with no driver suffix raises
    ``ModuleNotFoundError`` for a psycopg2 nobody installed, and that escaping
    as a 500 would contradict the one sentence this whole feature rests on.
    """
    try:
        return await _get_sessionmaker(settings)
    except KnowledgeUnavailableError:
        raise
    except Exception as exc:
        logger.warning(
            "Knowledge database engine could not be built: %s", type(exc).__name__
        )
        raise KnowledgeUnavailableError(
            KnowledgeRefusalCode.KNOWLEDGE_UNAVAILABLE,
            "the knowledge base is unreachable right now",
        ) from exc


@asynccontextmanager
async def knowledge_session(
    settings: KnowledgeSettings | None = None,
) -> AsyncIterator[AsyncSession]:
    """One explicit read transaction against the corpus.

    Explicit rather than autocommit-per-statement so a search that runs several
    statements - the rank query and the corpus counters that go beside it in
    every response - sees one consistent snapshot. A connection failure becomes
    ``KnowledgeUnavailableError`` here so no caller has to know what SQLAlchemy
    raises.

    ``OSError`` sits beside ``SQLAlchemyError`` because the asyncpg driver does
    not wrap a refused connection: it lets ``ConnectionRefusedError`` through
    untouched, and a corpus on a host that is down would otherwise be a 500
    rather than the refusal an agent can carry on from. Both are narrower than
    ``Exception`` deliberately - the caller's body is inside this block, and a
    bug in the code that builds a response should keep its own traceback rather
    than be reported to an operator as an unreachable database.
    """
    global _schema_checked

    resolved = settings or knowledge_settings
    factory, key = await _acquire(resolved)
    session = factory()
    try:
        async with session.begin():
            if _schema_checked != key:
                await _assert_schema_is_readable(session)
                _schema_checked = key
            yield session
    except KnowledgeUnavailableError:
        raise
    except (SQLAlchemyError, OSError) as exc:
        logger.warning("Knowledge database query failed: %s", type(exc).__name__)
        raise KnowledgeUnavailableError(
            KnowledgeRefusalCode.KNOWLEDGE_UNAVAILABLE,
            "the knowledge base is unreachable right now",
        ) from exc
    finally:
        await session.close()


async def _assert_schema_is_readable(session: AsyncSession) -> None:
    """Refuse a corpus written in a shape this server does not know.

    Plan 4.1's rule, enforced where it can be: a sync that has moved ahead of
    the reader hands back rows whose columns mean something else, and guessing
    at them is worse than saying so. The version this reads is logged, and the
    refusal that travels carries no version number, because a refusal reaching
    an agent says only that the base could not be consulted.
    """
    version = await read_schema_version(session)
    if is_supported_schema(version):
        return
    logger.warning(
        "Knowledge corpus reports schema version %s, which this server does not read.",
        version,
    )
    raise KnowledgeUnavailableError(
        KnowledgeRefusalCode.KNOWLEDGE_UNAVAILABLE,
        "the knowledge base is unreachable right now",
    )


async def dispose_knowledge_engine() -> None:
    """Close the pool. Shutdown, and any test that repoints the DSN."""
    global _engine, _sessionmaker, _engine_key, _schema_checked
    async with _lock:
        if _engine is not None:
            await _engine.dispose()
        _engine, _sessionmaker, _engine_key, _schema_checked = None, None, None, None
