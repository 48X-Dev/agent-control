"""Tests for shared team models and slug derivation."""

from __future__ import annotations

import datetime as dt

import pytest
from agent_control_models import Team, TeamMember, slugify
from pydantic import ValidationError

NOW = dt.datetime(2026, 8, 1, 12, 0, tzinfo=dt.UTC)


def _team(**overrides: object) -> Team:
    payload: dict[str, object] = {
        "id": 1,
        "namespace_key": "ns-one",
        "slug": "sales-outreach",
        "display_name": "Sales & Outreach",
        "created_at": NOW,
        "updated_at": NOW,
    }
    payload.update(overrides)
    return Team.model_validate(payload)


class TestSlugify:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("Sales & Outreach", "sales-outreach"),
            ("Operations", "operations"),
            ("Marketing", "marketing"),
            ("Engineering", "engineering"),
            ("Café Ops", "cafe-ops"),
            ("  --Ops--  ", "ops"),
            ("R&D / Platform", "r-d-platform"),
            ("team_42", "team-42"),
            ("Sales and  Outreach", "sales-and-outreach"),
            ("Sales   &&&   Outreach", "sales-outreach"),
            ("Sales\t&\nOutreach", "sales-outreach"),
            ("Ops (EMEA) — Tier 1", "ops-emea-tier-1"),
            ("Ürün / Geliştirme", "urun-gelistirme"),
            ("日本 Ops", "ops"),
            ("naïve—café…ops", "naive-cafe-ops"),
        ],
    )
    def test_derives_expected_slug(self, value: str, expected: str) -> None:
        assert slugify(value) == expected

    @pytest.mark.parametrize(
        "value",
        ["Sales & Outreach", "Operations", "Marketing", "Engineering", "Café Ops"],
    )
    def test_is_idempotent(self, value: str) -> None:
        once = slugify(value)
        assert slugify(once) == once

    def test_collapses_variant_spellings_of_the_same_name(self) -> None:
        assert slugify("Sales & Outreach") == slugify("sales   &   outreach")

    @pytest.mark.parametrize(
        "value",
        [
            "Sales—Outreach",  # em dash
            "Sales–Outreach",  # en dash
            "Sales‑Outreach",  # non-breaking hyphen
            "Sales·Outreach",  # middle dot
            "Sales　Outreach",  # ideographic space
            "Sales“and”Outreach".replace("and", ""),
        ],
    )
    def test_unfoldable_separators_do_not_glue_words(self, value: str) -> None:
        # Regression: these characters have no ASCII form. Dropping them instead
        # of treating them as separators produced "salesoutreach", so a display
        # name punctuated with an em dash slugged differently from one using a
        # plain hyphen.
        assert slugify(value) == "sales-outreach"

    def test_characters_without_an_ascii_form_become_separators(self) -> None:
        assert slugify("Bjørn Ops") == "bj-rn-ops"
        assert slugify("Ops日本Team") == "ops-team"

    @pytest.mark.parametrize("value", ["", "!!!", "   ", "---", "日本", "  &&  "])
    def test_returns_empty_string_without_alphanumeric_content(self, value: str) -> None:
        assert slugify(value) == ""

    @pytest.mark.parametrize("value", ["", "!!!", "   ", "日本"])
    def test_empty_result_is_rejected_at_the_model_boundary(self, value: str) -> None:
        # slugify stays a pure helper and returns "" rather than raising, so the
        # TeamSlug constraint is what stops an empty slug being stored.
        with pytest.raises(ValidationError):
            _team(slug=slugify(value))


class TestTeam:
    def test_keeps_display_name_verbatim(self) -> None:
        assert _team().display_name == "Sales & Outreach"

    @pytest.mark.parametrize(
        "slug",
        ["", "Sales", "sales outreach", "sales--outreach", "-sales", "sales-", "sales&"],
    )
    def test_rejects_malformed_slug(self, slug: str) -> None:
        with pytest.raises(ValidationError):
            _team(slug=slug)

    def test_rejects_empty_display_name(self) -> None:
        with pytest.raises(ValidationError):
            _team(display_name="")

    def test_description_is_optional(self) -> None:
        assert _team().description is None


class TestTeamMember:
    def test_normalizes_agent_name(self) -> None:
        member = TeamMember.model_validate(
            {
                "namespace_key": "ns-one",
                "team_id": 1,
                "agent_name": "  Outreach-Bot-One  ",
                "joined_at": NOW,
            }
        )
        assert member.agent_name == "outreach-bot-one"

    @pytest.mark.parametrize("agent_name", ["short", "has spaces here", "UPPER!!!NAME"])
    def test_rejects_invalid_agent_name(self, agent_name: str) -> None:
        with pytest.raises(ValidationError):
            TeamMember.model_validate(
                {
                    "namespace_key": "ns-one",
                    "team_id": 1,
                    "agent_name": agent_name,
                    "joined_at": NOW,
                }
            )
