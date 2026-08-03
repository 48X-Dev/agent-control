"""Tests for the Linear wire models.

Covers the normalization the ``LinearTeamKey`` annotation promises (case
folding, whitespace stripping, length and character limits), the five-state
milestone status, and the shape of the response a browser receives.
"""

from __future__ import annotations

import datetime as dt

import pytest
from agent_control_models import (
    LINEAR_TEAM_KEY_MAX_LENGTH,
    LinearTeamKey,
    ListMilestoneIssuesResponse,
    ListTeamMilestonesResponse,
    Milestone,
    MilestoneIssue,
    MilestoneIssueCounts,
    MilestonesStatus,
    PatchTeamRequest,
    Team,
    UpsertTeamRequest,
)
from pydantic import TypeAdapter, ValidationError

KEY_ADAPTER: TypeAdapter[str] = TypeAdapter(LinearTeamKey)

NOW = dt.datetime(2026, 8, 1, 12, 0, tzinfo=dt.UTC)


class TestLinearTeamKey:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("ENG", "ENG"),
            ("eng", "ENG"),
            ("Eng", "ENG"),
            ("  eng  ", "ENG"),
            ("\teng\n", "ENG"),
            ("SALES2", "SALES2"),
            ("123", "123"),
            ("a", "A"),
            ("A" * LINEAR_TEAM_KEY_MAX_LENGTH, "A" * LINEAR_TEAM_KEY_MAX_LENGTH),
        ],
    )
    def test_normalizes_to_upper_case(self, value: str, expected: str) -> None:
        assert KEY_ADAPTER.validate_python(value) == expected

    def test_normalization_is_idempotent(self) -> None:
        once = KEY_ADAPTER.validate_python("  eng ")
        assert KEY_ADAPTER.validate_python(once) == once

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "   ",
            "ENG-1",
            "ENG_1",
            "ENG TEAM",
            "ENG!",
            "éng",
            "A" * (LINEAR_TEAM_KEY_MAX_LENGTH + 1),
        ],
    )
    def test_rejects_malformed_keys(self, value: str) -> None:
        with pytest.raises(ValidationError):
            KEY_ADAPTER.validate_python(value)

    def test_team_model_accepts_and_folds_the_key(self) -> None:
        team = Team.model_validate(
            {
                "id": 1,
                "namespace_key": "ns-one",
                "slug": "engineering",
                "display_name": "Engineering",
                "linear_team_key": "eng",
                "created_at": NOW,
                "updated_at": NOW,
            }
        )
        assert team.linear_team_key == "ENG"

    def test_team_key_defaults_to_none(self) -> None:
        team = Team.model_validate(
            {
                "id": 1,
                "namespace_key": "ns-one",
                "slug": "engineering",
                "display_name": "Engineering",
                "created_at": NOW,
                "updated_at": NOW,
            }
        )
        assert team.linear_team_key is None

    def test_upsert_request_folds_the_key(self) -> None:
        request = UpsertTeamRequest.model_validate(
            {"display_name": "Engineering", "linear_team_key": " eng "}
        )
        assert request.linear_team_key == "ENG"

    def test_patch_request_distinguishes_omitted_from_explicit_null(self) -> None:
        omitted = PatchTeamRequest.model_validate({"display_name": "Engineering"})
        cleared = PatchTeamRequest.model_validate({"linear_team_key": None})

        assert "linear_team_key" not in omitted.model_fields_set
        assert "linear_team_key" in cleared.model_fields_set
        assert cleared.linear_team_key is None

    def test_patch_request_rejects_a_malformed_key(self) -> None:
        with pytest.raises(ValidationError):
            PatchTeamRequest.model_validate({"linear_team_key": "ENG-1"})


class TestMilestonesStatus:
    def test_has_exactly_the_five_documented_states(self) -> None:
        assert {status.value for status in MilestonesStatus} == {
            "not_configured",
            "not_linked",
            "error",
            "empty",
            "ok",
        }

    def test_serializes_as_its_string_value(self) -> None:
        response = ListTeamMilestonesResponse(
            status=MilestonesStatus.NOT_LINKED, slug="engineering"
        )
        assert response.model_dump()["status"] == "not_linked"


class TestMilestone:
    def test_optional_fields_default_to_none(self) -> None:
        milestone = Milestone(id="m1", name="Beta")

        assert milestone.description is None
        assert milestone.target_date is None
        assert milestone.status is None
        assert milestone.progress is None
        assert milestone.project_id is None

    def test_accepts_a_fully_populated_row(self) -> None:
        milestone = Milestone(
            id="m1",
            name="Beta",
            description="Ship the beta",
            target_date=dt.date(2026, 9, 1),
            status="unstarted",
            progress=0.25,
            project_id="p1",
            project_name="Platform",
            project_url="https://linear.app/acme/project/platform",
        )
        assert milestone.target_date == dt.date(2026, 9, 1)
        assert milestone.progress == 0.25

    @pytest.mark.parametrize("progress", [-0.01, 1.01, 2.0])
    def test_rejects_progress_outside_zero_to_one(self, progress: float) -> None:
        with pytest.raises(ValidationError):
            Milestone(id="m1", name="Beta", progress=progress)

    def test_passes_through_an_unknown_status_string(self) -> None:
        """Linear may add a status; an unknown one must still render."""
        milestone = Milestone(id="m1", name="Beta", status="something-new")
        assert milestone.status == "something-new"


class TestListTeamMilestonesResponse:
    def test_defaults_are_the_empty_non_error_shape(self) -> None:
        response = ListTeamMilestonesResponse(
            status=MilestonesStatus.NOT_CONFIGURED, slug="engineering"
        )

        assert response.milestones == []
        assert response.error is None
        assert response.retry_after_seconds is None
        assert response.cached is False
        assert response.fetched_at is None
        assert response.linear_team_key is None

    def test_has_no_field_that_could_carry_a_credential(self) -> None:
        """The browser sees this model; nothing in it is named like a secret."""
        suspicious = {"api_key", "key", "token", "secret", "authorization", "password"}
        assert suspicious.isdisjoint(ListTeamMilestonesResponse.model_fields)
        assert suspicious.isdisjoint(Milestone.model_fields)

    def test_error_response_carries_a_reason_and_no_milestones(self) -> None:
        response = ListTeamMilestonesResponse(
            status=MilestonesStatus.ERROR,
            slug="engineering",
            linear_team_key="ENG",
            error="Linear could not be reached.",
            retry_after_seconds=30,
        )

        assert response.milestones == []
        assert response.error == "Linear could not be reached."
        assert response.retry_after_seconds == 30

    def test_rejects_a_negative_retry_after(self) -> None:
        with pytest.raises(ValidationError):
            ListTeamMilestonesResponse(
                status=MilestonesStatus.ERROR, slug="engineering", retry_after_seconds=-1
            )


class TestMilestoneIssueModels:
    """The scope-read response, which a browser and a dispatcher both see."""

    def test_the_new_models_are_exported_from_the_package(self) -> None:
        import agent_control_models as models

        for name in (
            "MilestoneIssue",
            "MilestoneIssueSkipCounts",
            "MilestoneIssueCounts",
            "ListMilestoneIssuesResponse",
        ):
            assert name in models.__all__
            assert getattr(models, name) is not None

    def test_defaults_are_the_empty_non_error_shape(self) -> None:
        response = ListMilestoneIssuesResponse(
            status=MilestonesStatus.EMPTY,
            slug="operations",
            linear_team_key="OPS",
            milestone_id="m-1",
        )

        assert response.issues == []
        assert response.counts.fetched == 0
        assert response.counts.eligible == 0
        assert response.counts.beyond_page_cap is False
        assert response.counts.skipped.started == 0
        assert response.counts.skipped.assigned == 0
        assert response.counts.skipped.other_team == 0
        assert response.error is None
        assert response.cached is False

    def test_an_issue_carries_no_state_assignee_or_label(self) -> None:
        """Those three are the eligibility inputs and the later-phase filter.

        None of them is rendered: state and assignee are counts, and a label is
        never a selector because anyone who can file an issue can attach one.
        """

        assert set(MilestoneIssue.model_fields) == {
            "ref",
            "identifier",
            "title",
            "description",
            "url",
            "created_at",
            "updated_at",
            "creator_id",
            "creator_display_name",
        }

    def test_has_no_field_that_could_carry_a_credential(self) -> None:
        suspicious = {"api_key", "key", "token", "secret", "authorization", "password"}

        assert suspicious.isdisjoint(ListMilestoneIssuesResponse.model_fields)
        assert suspicious.isdisjoint(MilestoneIssue.model_fields)
        assert suspicious.isdisjoint(MilestoneIssueCounts.model_fields)

    @pytest.mark.parametrize(
        "counts",
        [
            {"fetched": -1},
            {"eligible": -1},
            {"skipped": {"started": -1}},
            {"skipped": {"assigned": -1}},
            {"skipped": {"other_team": -1}},
        ],
    )
    def test_a_negative_count_is_refused(self, counts: dict) -> None:
        with pytest.raises(ValidationError):
            ListMilestoneIssuesResponse(
                status=MilestonesStatus.OK,
                slug="operations",
                linear_team_key="OPS",
                milestone_id="m-1",
                counts=counts,
            )

    def test_a_negative_retry_after_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            ListMilestoneIssuesResponse(
                status=MilestonesStatus.ERROR,
                slug="operations",
                linear_team_key="OPS",
                milestone_id="m-1",
                retry_after_seconds=-1,
            )

    def test_the_scope_of_the_read_is_required_on_every_response(self) -> None:
        """A response that could not say which team and milestone it read is a
        response nobody can act on."""

        with pytest.raises(ValidationError):
            ListMilestoneIssuesResponse(status=MilestonesStatus.OK, slug="operations")

    def test_the_error_code_for_an_unlinked_team_exists_and_is_its_own(self) -> None:
        from agent_control_models.errors import ErrorCode

        assert ErrorCode.TEAM_NOT_LINKED == "TEAM_NOT_LINKED"
        assert ErrorCode.TEAM_NOT_LINKED is not ErrorCode.TEAM_HAS_MEMBERS
