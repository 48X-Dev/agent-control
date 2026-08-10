"""Sample documents and small helpers the two end-to-end run modules share.

Plain functions and constants, deliberately not fixtures: they take the corpus as an argument.
"""

from __future__ import annotations

from typing import Any

from agent_control_knowledge_sync.config import SyncConfig
from agent_control_knowledge_sync.drive_auth import DriveCredentials

from tests.conftest import query
from tests.fakes.drive import FakeDrive

ROOT_ID = "root-folder-1"
NESTED_ID = "nested-folder-1"

CREDENTIALS = DriveCredentials(
    client_id="123456789012-abcdefghijklmnop.apps.googleusercontent.com",
    client_secret="GOCSPX-not-a-real-secret",
    refresh_token="1//0e-not-a-real-refresh-token",
)

LAPTOPS = """# Onboarding

## Laptops

Laptops are reimbursed up to 1500 GBP. Submit the receipt within thirty days of
purchase and the finance team pays it in the next run. A replacement machine is
approved by the hiring manager, never by the agent, and the asset register is
updated by IT the same week the machine arrives on somebody's desk.
"""

LAPTOPS_REVISED = (
    LAPTOPS
    + """
## Loaners

A loaner is signed out at the front desk for up to two weeks. Anything longer
is a replacement and goes through the hiring manager like any other machine.
"""
)

PHONES = """# Onboarding

## Phones

Company phones are issued to staff who are on call. The monthly allowance is
forty pounds and it is claimed on the same expense form as travel. Handsets are
returned when somebody leaves the on-call rotation, and the finance team closes
the line in the same week rather than at the end of the quarter.
"""

PHONES_REVISED = PHONES.replace("forty pounds", "fifty pounds")

RELEASES = """# Release process

Releases ship on Thursdays. The release manager freezes the branch on Wednesday
afternoon, runs the full suite, and posts the changelog to the engineering
channel before anybody merges anything else into the release branch. A hotfix
is the one exception and it is announced in the same channel before it lands.
"""

EXPENSES = """# Expenses

## Travel

Trains are booked in advance and standard class. Anything over three hundred
pounds is approved by a manager before it is booked, and the receipt is filed
within thirty days like every other claim in this handbook.
"""


async def _no_wait(seconds: float) -> None:
    """The retry backoff, without the wall clock."""


def populate(drive: FakeDrive) -> None:
    """The three-file subtree every run in here walks."""
    drive.folder(ROOT_ID, "Company Knowledge")
    drive.folder(NESTED_ID, "Onboarding", ROOT_ID)
    drive.markdown("file-laptops", "laptops.md", LAPTOPS, ROOT_ID)
    drive.markdown("file-phones", "phones.md", PHONES, ROOT_ID)
    drive.markdown("file-releases", "releases.md", RELEASES, NESTED_ID)


def config_for(corpus: Any) -> SyncConfig:
    """The sync config a run in here is given, pointed at this test's corpus."""
    return SyncConfig(
        credentials=CREDENTIALS,
        root_folder_id=ROOT_ID,
        database_url=corpus.sync_url,
        max_file_bytes=1_000_000,
        max_documents_per_run=50,
        request_timeout_seconds=5.0,
    )


def source_row(corpus: Any) -> dict[str, Any]:
    rows = query(corpus, "SELECT * FROM sources WHERE ref = :ref", ref=ROOT_ID)
    assert rows, "the run registered no source for the root folder"
    return dict(rows[0])


def document(corpus: Any, external_id: str) -> dict[str, Any]:
    rows = query(corpus, "SELECT * FROM documents WHERE external_id = :id", id=external_id)
    assert rows, f"no document row for {external_id}"
    return dict(rows[0])
