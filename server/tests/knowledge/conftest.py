"""Corpus fixtures for the tests in this directory, and nothing outside it.

One provisioned database per test module, emptied before every test. The
fixtures live here rather than in ``tests/conftest.py`` so that ``blank_corpus``
can be autouse without provisioning a database for all two and a half thousand
tests in the server suite. Everything under this directory wants a corpus;
nothing above it does.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import pytest
import sqlalchemy as sa
from agent_control_server.knowledge import dispose_knowledge_engine
from sqlalchemy.pool import NullPool

from tests.knowledge_provisioning import Corpus, provision, teardown, unavailable_reason


@pytest.fixture(scope="module")
def corpus() -> Iterator[Corpus]:
    """One provisioned corpus per module, or a skip that says what is missing.

    The skip lives on the fixture rather than in a collection hook: every test
    below reaches it through the autouse fixture, and a hook in a directory
    conftest is handed the whole session's items, which is how a directory-wide
    skip becomes a suite-wide one.
    """
    reason = unavailable_reason()
    if reason:
        pytest.skip(reason)
    provisioned = provision()
    try:
        yield provisioned
    finally:
        teardown(provisioned)


@pytest.fixture(autouse=True)
async def blank_corpus(corpus: Corpus) -> AsyncIterator[None]:
    """Empty the corpus and hand every test a fresh engine.

    Fresh because pytest-asyncio gives each test its own event loop, and a
    pooled async connection outlives neither.
    """
    await dispose_knowledge_engine()
    engine = sa.create_engine(corpus.sync_url, future=True, poolclass=NullPool)
    with engine.begin() as conn:
        conn.execute(sa.text("TRUNCATE chunks, documents, sources RESTART IDENTITY CASCADE"))
    engine.dispose()
    yield
    await dispose_knowledge_engine()
