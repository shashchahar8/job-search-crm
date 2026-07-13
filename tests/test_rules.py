from dataclasses import replace
from datetime import date
from urllib.parse import urlencode

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from app import main
from app.models import Base, Job, JobRuleEvaluation, RuleOutcome
from app.repository import create_search_and_run, save_job_discovery
from app.rules import (
    BASE_SCORE,
    MAX_DESCRIPTION_POSITIVE_CONTRIBUTION,
    MAX_DESCRIPTION_POSITIVE_RULES,
    PROFILE_VERSION,
    content_fingerprint,
    effective_recommendation,
    evaluate_and_store_job,
    evaluate_job_content,
    evaluation_state,
    get_default_profile,
    profile_key,
    set_recommendation_override,
)


class FakeRequest:
    def __init__(self, payload: dict[str, str]) -> None:
        self.payload = payload

    async def form(self) -> dict[str, str]:
        return self.payload

    def url_for(self, name: str, **path_params):
        return main.app.url_path_for(name, **path_params)


def _request(path: str = "/", query: dict[str, str] | None = None) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "headers": [],
            "query_string": urlencode(query or {}).encode(),
            "server": ("testserver", 80),
            "scheme": "http",
            "app": main.app,
        }
    )


def _session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _job_payload(
    seek_job_id: str,
    title: str,
    *,
    description: str = "Support strategy and market research.",
) -> dict[str, str | None]:
    return {
        "seek_job_id": seek_job_id,
        "fallback_key": f"fallback-{seek_job_id}",
        "title": title,
        "company": "Example Co",
        "location": "Sydney NSW",
        "salary": None,
        "work_type": "Full time",
        "posting_date": "1d ago",
        "url": f"https://www.seek.com.au/job/{seek_job_id}",
        "description": description,
    }


def _job(title: str, description: str = "") -> Job:
    return Job(
        seek_job_id="x",
        fallback_key=f"fallback-{title}",
        title=title,
        url="https://www.seek.com.au/job/x",
        description=description,
    )


def test_matching_scores_are_field_aware_and_deterministic() -> None:
    title_match = evaluate_job_content(_job("Strategy Analyst", "General role."))
    description_match = evaluate_job_content(_job("Coordinator", "Strategy analyst support."))
    repeated = evaluate_job_content(_job("Strategy Analyst", "General role."))

    assert title_match["score"] > description_match["score"]
    assert title_match == repeated
    assert title_match["outcome"] == RuleOutcome.STRONG_MATCH.value
    assert title_match["explanation"] == repeated["explanation"]


def test_seniority_and_leadership_context_are_conservative() -> None:
    senior_title = evaluate_job_content(_job("Senior Strategy Analyst"))
    senior_stakeholders = evaluate_job_content(
        _job("Junior Strategy Analyst", "Work with senior stakeholders.")
    )
    leadership = evaluate_job_content(
        _job("Strategy Analyst", "Leadership skills and ability to lead initiatives.")
    )

    assert senior_title["penalty_evidence"]
    assert not senior_stakeholders["penalty_evidence"]
    assert not leadership["penalty_evidence"]
    assert senior_title["score"] < senior_stakeholders["score"]


def test_technical_and_years_matching_are_conservative() -> None:
    technical_title = evaluate_job_content(_job("Technical Business Analyst"))
    incidental = evaluate_job_content(_job("Strategy Analyst", "Partner with SQL teams."))
    five_years = evaluate_job_content(_job("Strategy Analyst", "Bring 5 years experience."))
    six_years = evaluate_job_content(_job("Strategy Analyst", "Bring 6+ years experience."))

    assert technical_title["outcome"] == RuleOutcome.EXCLUDE.value
    assert technical_title["exclusion_evidence"]
    assert incidental["outcome"] != RuleOutcome.EXCLUDE.value
    assert not five_years["penalty_evidence"]
    assert six_years["penalty_evidence"]


def test_word_boundaries_neutral_fallback_and_clamping() -> None:
    neutral = evaluate_job_content(_job("Retail Assistant", "Serve customers."))
    many_signals = evaluate_job_content(
        _job(
            "Corporate Development Analyst",
            "Strategy growth insights transformation market research financial modelling.",
        )
    )

    assert neutral["score"] == 50
    assert neutral["outcome"] == RuleOutcome.REVIEW.value
    assert "No strong positive or exclusion signals" in neutral["explanation"]
    assert not evaluate_job_content(_job("Strategist"))["positive_evidence"]
    assert many_signals["score"] == 100


def test_description_positive_cap_preserves_all_evidence_and_reproduces_score() -> None:
    result = evaluate_job_content(
        _job(
            "General Analyst",
            (
                "Strategy commercial analysis business performance market research insights "
                "financial modelling growth transformation operational improvement stakeholder "
                "management executive reporting graduate."
            ),
        )
    )
    description_positives = [
        item for item in result["positive_evidence"] if item["field"] == "description"
    ]
    contributing_description_positives = [
        item for item in description_positives if item["effective_weight"] > 0
    ]
    all_evidence = (
        result["positive_evidence"]
        + result["penalty_evidence"]
        + result["exclusion_evidence"]
    )
    effective_total = sum(item["effective_weight"] for item in all_evidence)

    assert len(description_positives) > MAX_DESCRIPTION_POSITIVE_RULES
    assert len(contributing_description_positives) == MAX_DESCRIPTION_POSITIVE_RULES
    assert (
        sum(item["effective_weight"] for item in description_positives)
        == MAX_DESCRIPTION_POSITIVE_CONTRIBUTION
    )
    assert any(item["matched_weight"] > item["effective_weight"] for item in description_positives)
    assert result["score"] == BASE_SCORE + effective_total


def test_title_seniority_outcome_ceilings_keep_roles_reviewable() -> None:
    senior_strategy = evaluate_job_content(
        _job("Senior Strategy Analyst", "Strategy growth insights transformation.")
    )
    senior_commercial = evaluate_job_content(
        _job("Senior Commercial Analyst", "Commercial analysis strategy growth insights.")
    )
    commercial_lead = evaluate_job_content(
        _job("Commercial Insights Lead", "Commercial analysis strategy growth insights.")
    )

    assert senior_strategy["score"] >= 75
    assert senior_strategy["outcome"] == RuleOutcome.REVIEW.value
    assert senior_commercial["outcome"] == RuleOutcome.REVIEW.value
    assert commercial_lead["outcome"] == RuleOutcome.REVIEW.value
    assert "limited the recommendation to review" in senior_strategy["explanation"]
    assert not senior_strategy["exclusion_evidence"]


@pytest.mark.parametrize(
    ("title", "expected_outcome"),
    [
        ("Strategy Manager", RuleOutcome.WEAK_MATCH.value),
        ("Principal Strategy Analyst", RuleOutcome.WEAK_MATCH.value),
        ("Head of Strategy", RuleOutcome.WEAK_MATCH.value),
        ("Strategy Director", RuleOutcome.WEAK_MATCH.value),
    ],
)
def test_manager_principal_head_and_director_ceilings(title: str, expected_outcome: str) -> None:
    result = evaluate_job_content(
        _job(title, "Strategy commercial analysis growth insights transformation.")
    )

    assert result["score_based_outcome"] in {
        RuleOutcome.REVIEW.value,
        RuleOutcome.STRONG_MATCH.value,
    }
    assert result["outcome"] == expected_outcome
    assert not result["exclusion_evidence"]


def test_hard_exclusion_preserves_score_and_overrides_score_based_outcome() -> None:
    result = evaluate_job_content(
        _job(
            "Technical Business Analyst",
            "Strategy commercial analysis growth insights transformation market research.",
        )
    )

    assert result["score"] >= 50
    assert result["score_based_outcome"] == RuleOutcome.REVIEW.value
    assert result["outcome"] == RuleOutcome.EXCLUDE.value
    assert result["hard_exclusion_applied"] is True
    assert result["exclusion_evidence"][0]["hard_exclusion_applied"] is True
    assert "hard-exclusion rule set the final outcome to exclude" in result["explanation"]


def test_explanation_is_deterministic_and_backed_by_effective_evidence() -> None:
    first = evaluate_job_content(
        _job("Senior Commercial Analyst", "Commercial analysis strategy growth insights.")
    )
    second = evaluate_job_content(
        _job("Senior Commercial Analyst", "Commercial analysis strategy growth insights.")
    )
    evidence_labels = {
        item["evidence_label"]
        for item in (
            first["positive_evidence"]
            + first["penalty_evidence"]
            + first["exclusion_evidence"]
        )
    }

    assert first["explanation"] == second["explanation"]
    assert "title contains a seniority signal" in evidence_labels
    assert "title contains a seniority signal" in first["explanation"]


def test_evaluation_lifecycle_stale_override_and_upsert_preservation() -> None:
    session_factory = _session_factory()
    with session_factory() as db:
        run = create_search_and_run(db, "strategy analyst", "Sydney NSW", "last_7_days", 1)
        job = save_job_discovery(db, run.id, 1, _job_payload("901", "Strategy Analyst"))
        first = evaluate_and_store_job(db, job)
        second = evaluate_and_store_job(db, job)
        set_recommendation_override(db, job, RuleOutcome.REVIEW.value)
        save_job_discovery(
            db,
            run.id,
            1,
            {**_job_payload("901", "Strategy Analyst"), "description": "Changed content."},
        )
        updated = db.scalar(select(Job).where(Job.seek_job_id == "901"))
        latest = db.scalar(select(JobRuleEvaluation))

        assert first.id == second.id
        assert latest.recommendation_override == RuleOutcome.REVIEW.value
        assert effective_recommendation(latest) == RuleOutcome.REVIEW.value
        assert latest.score == first.score
        assert evaluation_state(updated, latest) == "stale"
        assert latest.content_fingerprint != content_fingerprint(updated)


def test_rule_version_change_supports_reevaluation() -> None:
    session_factory = _session_factory()
    with session_factory() as db:
        run = create_search_and_run(db, "strategy analyst", "Sydney NSW", "last_7_days", 1)
        job = save_job_discovery(db, run.id, 1, _job_payload("902", "Strategy Analyst"))
        first = evaluate_and_store_job(db, job)
        next_profile = replace(get_default_profile(), version=f"{PROFILE_VERSION}.next")
        assert evaluation_state(job, first, next_profile) == "stale"
        second = evaluate_and_store_job(db, job, next_profile)

        assert second.id != first.id
        assert second.profile_version.endswith(".next")


@pytest.mark.asyncio
async def test_routes_ui_filters_and_csv_rule_fields() -> None:
    session_factory = _session_factory()
    default_profile_key = profile_key(get_default_profile())
    with session_factory() as db:
        run = create_search_and_run(db, "strategy analyst", "Sydney NSW", "last_7_days", 1)
        strong = save_job_discovery(db, run.id, 1, _job_payload("903", "Strategy Analyst"))
        review = save_job_discovery(db, run.id, 1, _job_payload("904", "Retail Assistant"))
        strong_id = strong.id
        review_id = review.id
        response = await main.evaluate_job_route(
            strong_id, FakeRequest({"profile_key": default_profile_key}), db=db
        )
        assert response.status_code == 303
        await main.update_recommendation_override_route(
            strong_id,
            FakeRequest(
                {
                    "recommendation_override": RuleOutcome.WEAK_MATCH.value,
                    "profile_key": default_profile_key,
                }
            ),
            db=db,
        )
        bulk = await main.evaluate_bulk_jobs(
            FakeRequest({"mode": "unevaluated", "profile_key": default_profile_key}), db=db
        )
        assert bulk.status_code == 303

    with session_factory() as db:
        jobs_response = main.jobs_index(
            _request("/jobs", {"overridden": "yes", "review_queue": "yes"}),
            db=db,
        )
        body = jobs_response.body.decode()
        assert "Rule recommendation" in body
        assert "Override: Weak Match" in body
        assert "AI assessment" not in body

        detail = main.job_detail(strong_id, _request(f"/jobs/{strong_id}"), db=db)
        detail_body = detail.body.decode()
        assert "Rule assessment" in detail_body
        assert "Matched positive signals" in detail_body
        assert "Manual recommendation override" in detail_body

        invalid = await main.update_recommendation_override_route(
            strong_id,
            FakeRequest(
                {"recommendation_override": "not-real", "profile_key": default_profile_key}
            ),
            db=db,
        )
        assert invalid.status_code == 400
        clear = await main.update_recommendation_override_route(
            strong_id,
            FakeRequest({"recommendation_override": "", "profile_key": default_profile_key}),
            db=db,
        )
        assert clear.status_code == 303

        export_response = main.export_filtered_jobs(_request("/jobs"), db=db)
        chunks = []
        async for chunk in export_response.body_iterator:
            chunks.append(chunk)
        csv_body = "".join(chunks)
        assert "rule_score,rule_outcome,effective_recommendation" in csv_body
        assert "rule_explanation" in csv_body
        assert "<a" not in csv_body
        assert review_id is not None


def test_dashboard_rule_links_render() -> None:
    session_factory = _session_factory()
    with session_factory() as db:
        run = create_search_and_run(db, "strategy analyst", "Sydney NSW", "last_7_days", 1)
        job = save_job_discovery(db, run.id, 1, _job_payload("905", "Strategy Analyst"))
        evaluate_and_store_job(db, job)

    with session_factory() as db:
        response = main.index(_request(), db=db)

    body = response.body.decode()
    assert 'href="/jobs?effective_recommendation=strong_match' in body
    assert 'href="/jobs?rule_state=unevaluated' in body


@pytest.mark.asyncio
async def test_invalid_bulk_mode_and_missing_job_errors() -> None:
    default_profile_key = profile_key(get_default_profile())
    session_factory = _session_factory()
    with session_factory() as db, pytest.raises(HTTPException):
        await main.evaluate_job_route(
            999, FakeRequest({"profile_key": default_profile_key}), db=db
        )

    with session_factory() as db, pytest.raises(HTTPException):
        await main.evaluate_bulk_jobs(FakeRequest({"mode": "bad"}), db)


def test_collector_upsert_preserves_crm_and_rule_state() -> None:
    session_factory = _session_factory()
    with session_factory() as db:
        run = create_search_and_run(db, "strategy analyst", "Sydney NSW", "last_7_days", 1)
        job = save_job_discovery(db, run.id, 1, _job_payload("906", "Strategy Analyst"))
        job.crm_status = "shortlisted"
        job.priority = "high"
        job.is_favorite = True
        job.notes = "Manual note"
        job.application_deadline = date(2026, 7, 20)
        job.follow_up_date = date(2026, 7, 21)
        evaluation = evaluate_and_store_job(db, job)
        set_recommendation_override(db, job, RuleOutcome.REVIEW.value)

        save_job_discovery(
            db,
            run.id,
            1,
            {**_job_payload("906", "Senior Strategy Analyst"), "salary": "$140,000"},
        )
        updated = db.scalar(select(Job).where(Job.seek_job_id == "906"))
        evaluations = db.scalars(select(JobRuleEvaluation)).all()

        assert updated.crm_status == "shortlisted"
        assert updated.priority == "high"
        assert updated.is_favorite is True
        assert updated.notes == "Manual note"
        assert updated.application_deadline == date(2026, 7, 20)
        assert updated.follow_up_date == date(2026, 7, 21)
        assert len(evaluations) == 1
        assert evaluations[0].id == evaluation.id
        assert evaluations[0].recommendation_override == RuleOutcome.REVIEW.value


@pytest.mark.asyncio
async def test_hard_exclusion_override_renders_and_exports() -> None:
    session_factory = _session_factory()
    with session_factory() as db:
        run = create_search_and_run(db, "business analyst", "Sydney NSW", "last_7_days", 1)
        job = save_job_discovery(
            db,
            run.id,
            1,
            _job_payload(
                "907",
                "Technical Business Analyst",
                description=(
                    "Strategy commercial analysis growth insights transformation market research."
                ),
            ),
        )
        evaluate_and_store_job(db, job)
        job_id = job.id

    with session_factory() as db:
        detail = main.job_detail(job_id, _request(f"/jobs/{job_id}"), db=db)
        detail_body = detail.body.decode()
        assert "Hard rule override applied" in detail_body
        assert "Score-based outcome" in detail_body

        jobs_response = main.jobs_index(_request("/jobs"), db=db)
        assert "Hard rule override" in jobs_response.body.decode()

        export_response = main.export_filtered_jobs(_request("/jobs"), db=db)
        chunks = []
        async for chunk in export_response.body_iterator:
            chunks.append(chunk)
        csv_body = "".join(chunks)
        assert "rule_score_based_outcome,rule_hard_exclusion_applied" in csv_body
        assert ",review,yes," in csv_body


@pytest.mark.asyncio
async def test_profile_selection_forms_submit_only_registered_profile_keys() -> None:
    registry = main.load_profile_registry()
    profiles = list(registry.valid_profiles)
    assert len(profiles) >= 2
    default = get_default_profile()
    mismatched_key = f"{default.id}::example-only-version"

    session_factory = _session_factory()
    with session_factory() as db:
        run = create_search_and_run(db, "strategy analyst", "Sydney NSW", "last_7_days", 1)
        job = save_job_discovery(db, run.id, 1, _job_payload("908", "Strategy Analyst"))
        job_id = job.id

    with session_factory() as db:
        jobs_body = main.jobs_index(_request("/jobs"), db=db).body.decode()
        detail_body = main.job_detail(job_id, _request(f"/jobs/{job_id}"), db=db).body.decode()

    combined_body = jobs_body + detail_body
    assert 'name="profile_key"' in combined_body
    assert 'name="profile_id"' not in combined_body
    assert 'name="profile_version"' not in combined_body
    for profile in profiles:
        assert f'value="{profile_key(profile)}"' in jobs_body
        assert f"{profile.name} - {profile.version}" in jobs_body
    assert mismatched_key not in combined_body

    with session_factory() as db, pytest.raises(HTTPException) as exc:
        await main.evaluate_bulk_jobs(
            FakeRequest({"mode": "unevaluated", "profile_key": mismatched_key}), db=db
        )
    assert exc.value.status_code == 400
