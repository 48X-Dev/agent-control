"""Where an attachment ceiling is configured twice, and the two must agree.

Every number here exists in two places: a constant in ``agent_control_models``
that a wire model validates against, and a setting an operator can move. The
failure that pattern produces is not a crash but a 422 on a request that names
no setting and offers no remedy, so each pair is bound rather than merely
defaulted to the same value, and each binding is asserted here. A refusal at
construction is a refusal to boot, which is where a misconfiguration should
land.
"""

from __future__ import annotations

import pytest
from agent_control_models.attachments import (
    ATTACHMENT_HARD_MAX_BYTES,
    ATTACHMENT_MAX_PER_TURN,
)
from pydantic import ValidationError

from agent_control_server.config import ExecutorSettings


def test_the_per_turn_default_is_the_models_constant() -> None:
    """``StartTurnRequest.attachment_keys`` is bounded by the constant, so a
    default that disagreed would refuse a key the server would have accepted."""
    assert ExecutorSettings().attachment_max_per_turn == ATTACHMENT_MAX_PER_TURN


def test_a_per_turn_ceiling_above_the_constant_is_refused() -> None:
    with pytest.raises(ValidationError):
        ExecutorSettings(attachment_max_per_turn=ATTACHMENT_MAX_PER_TURN + 1)


def test_a_per_turn_ceiling_below_the_constant_is_allowed() -> None:
    """Lowering it is a real operational choice - a deployment on a small box
    delivering one file per turn - and it is safe, because the wire model then
    accepts keys the server refuses with a message that names the setting."""
    assert ExecutorSettings(attachment_max_per_turn=1).attachment_max_per_turn == 1


def test_a_byte_cap_above_the_database_check_is_refused() -> None:
    """The ``CHECK`` on both tables carries this number, so a setting above it
    would accept an upload the insert then rejects."""
    with pytest.raises(ValidationError):
        ExecutorSettings(attachment_max_bytes=ATTACHMENT_HARD_MAX_BYTES + 1)


def test_a_turn_total_below_a_single_file_is_refused() -> None:
    """Set that way, every file the size cap allows is accepted at 201 and then
    never delivered, by a different ceiling, with nothing saying so."""
    with pytest.raises(ValidationError) as caught:
        ExecutorSettings(
            attachment_max_bytes=1024, attachment_turn_total_bytes=512
        )

    assert "ATTACHMENT_TURN_TOTAL_BYTES" in str(caught.value)


def test_the_page_ceilings_exist_and_do_nothing_yet() -> None:
    """Counting pages means opening the file, and no code path here opens one.

    These three are kept at their documented defaults so a converter phase
    wires up the limits this plan chose rather than re-inventing them under
    new names. Asserting the defaults is the anchor that makes a silent
    redefinition visible.
    """
    settings = ExecutorSettings()

    assert settings.attachment_max_pages == 1000
    assert settings.attachment_warn_pages == 100
    assert settings.attachment_session_total_pages == 400
