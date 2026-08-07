"""Sample documents and small helpers the corpus tests share.

Plain functions and constants, deliberately not fixtures: they take the corpus
as an argument, so a test that wants two differently-configured sources can
call them twice.
"""

from __future__ import annotations

from agent_control_server.config import KnowledgeSettings
from agent_control_server.knowledge.seed import SeedDocument, SeedResult, seed_corpus

from tests.knowledge_provisioning import Corpus

LAPTOPS = """# Onboarding

Welcome to the operations handbook.

## Laptops

Laptops are reimbursed up to 1500 GBP. Submit the receipt within thirty days of
purchase and the finance team pays it in the next run. A replacement machine is
approved by the hiring manager, never by the agent, and the asset register is
updated by IT the same week the machine arrives on somebody's desk.
"""

PHONES = """# Onboarding

## Phones

Company phones are issued to staff who are on call. The monthly allowance is
forty pounds and it is claimed on the same expense form as travel. Handsets are
returned when somebody leaves the on-call rotation.
"""

RELEASES = """# Release process

Releases ship on Thursdays. The release manager freezes the branch on Wednesday
afternoon, runs the full suite, and posts the changelog to the engineering
channel before anybody merges anything else into the release branch.
"""


def settings_for(corpus: Corpus, **overrides: object) -> KnowledgeSettings:
    values: dict[str, object] = {
        "enabled": True,
        "db_url": corpus.read_url,
        "search_max_results": 5,
        "snippet_max_chars": 1200,
    }
    values.update(overrides)
    return KnowledgeSettings(**values)  # type: ignore[arg-type]


def seed(corpus: Corpus, **kwargs: object) -> SeedResult:
    return seed_corpus(corpus.sync_url, **kwargs)  # type: ignore[arg-type]


def handbook(**kwargs: object) -> dict[str, object]:
    """The two-document Ops Handbook most cases start from."""
    return {
        "source_ref": "ops-handbook",
        "source_name": "Ops Handbook",
        "docs": [
            SeedDocument(path="Ops Handbook/Onboarding/laptops.md", body=LAPTOPS),
            SeedDocument(path="Ops Handbook/Onboarding/phones.md", body=PHONES),
        ],
        **kwargs,
    }
