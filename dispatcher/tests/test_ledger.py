"""The local claim ledger. Resumability, not safety."""

from __future__ import annotations

from pathlib import Path

from agent_control_dispatcher.ledger import ClaimLedger, ClaimStatus


def _ledger(tmp_path: Path) -> ClaimLedger:
    return ClaimLedger(tmp_path / "nested" / "claims.sqlite3")


def test_one_ref_is_claimed_once(tmp_path: Path) -> None:
    with _ledger(tmp_path) as ledger:
        assert ledger.claim(source_kind="file", ref="t1", agent_name="a", dry_run=True)
        assert not ledger.claim(source_kind="file", ref="t1", agent_name="a", dry_run=True)


def test_a_terminal_ref_is_not_reclaimed_by_a_rerun(tmp_path: Path) -> None:
    path = tmp_path / "claims.sqlite3"
    with ClaimLedger(path) as ledger:
        ledger.claim(source_kind="file", ref="t1", agent_name="a", dry_run=True)
        ledger.finish(source_kind="file", ref="t1", status=ClaimStatus.COMPLETED)
    with ClaimLedger(path) as ledger:
        assert not ledger.claim(source_kind="file", ref="t1", agent_name="a", dry_run=True)
        claim = ledger.get(source_kind="file", ref="t1")
        assert claim is not None and claim.status is ClaimStatus.COMPLETED


def test_paused_quota_is_the_one_status_that_resumes(tmp_path: Path) -> None:
    with _ledger(tmp_path) as ledger:
        ledger.claim(source_kind="file", ref="t1", agent_name="a", dry_run=True)
        ledger.finish(
            source_kind="file",
            ref="t1",
            status=ClaimStatus.PAUSED_QUOTA,
            outcome_code="QUOTA_EXCEEDED",
        )
        assert ledger.claim(source_kind="file", ref="t1", agent_name="a", dry_run=True)
        claim = ledger.get(source_kind="file", ref="t1")
        assert claim is not None and claim.status is ClaimStatus.CLAIMED
        assert claim.outcome_code is None


def test_the_same_ref_in_two_sources_is_two_claims(tmp_path: Path) -> None:
    with _ledger(tmp_path) as ledger:
        assert ledger.claim(source_kind="file", ref="t1", agent_name="a", dry_run=True)
        assert ledger.claim(source_kind="other", ref="t1", agent_name="a", dry_run=True)


def test_the_transcript_and_trace_are_recorded_for_the_operator(tmp_path: Path) -> None:
    with _ledger(tmp_path) as ledger:
        ledger.claim(source_kind="file", ref="t1", agent_name="a", dry_run=False)
        ledger.record_session(source_kind="file", ref="t1", session_key="sk")
        ledger.finish(
            source_kind="file", ref="t1", status=ClaimStatus.BLOCKED, turn_trace_id="tr"
        )
        claim = ledger.get(source_kind="file", ref="t1")
        assert claim is not None
        assert (claim.session_key, claim.turn_trace_id, claim.dry_run) == ("sk", "tr", False)
