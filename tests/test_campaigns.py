from dataclasses import dataclass

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import main
from app.campaigns import (
    CampaignValidationError,
    add_campaign_membership,
    archive_campaign,
    build_campaign_plan,
    create_campaign,
    create_campaign_execution_plan,
    create_saved_search,
    reconcile_running_campaign_executions,
    update_saved_search,
)
from app.models import (
    Base,
    Campaign,
    CampaignExecution,
    CampaignExecutionChildSnapshot,
    CampaignSavedSearch,
    JobDiscovery,
    RunStatus,
    SearchRun,
)
from app.repository import mark_run, save_job_discovery, set_run_stop_reason
from tests.test_main import _request


@dataclass
class FakeFormRequest:
    payload: dict[str, str]

    async def form(self) -> dict[str, str]:
        return self.payload

    @property
    def query_params(self) -> dict[str, str]:
        return {}


def _session_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _saved_search(
    db,
    name: str = "Strategy Sydney",
    query: str = '"strategy analyst" OR "commercial analyst"',
):
    return create_saved_search(
        db,
        name=name,
        source="seek",
        query_text=query,
        location="Sydney NSW",
        date_window="last_7_days",
        max_pages=2,
    )


def test_saved_search_validation_preserves_exact_boolean_query() -> None:
    session_factory = _session_factory()
    exact_query = '"strategy analyst" OR ("commercial analyst" AND graduate)'

    with session_factory() as db:
        saved = _saved_search(db, query=exact_query)

        assert saved.query_text == exact_query


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("name", "", "name is required"),
        ("query_text", "", "query is required"),
        ("source", "prosple", "not supported"),
        ("date_window", "today", "Unsupported date window"),
        ("max_pages", "99", "Maximum pages"),
    ],
)
def test_saved_search_validation_rejects_invalid_values(field, value, message) -> None:
    session_factory = _session_factory()
    payload = {
        "name": "Strategy Sydney",
        "source": "seek",
        "query_text": "strategy",
        "location": "Sydney NSW",
        "date_window": "last_7_days",
        "max_pages": 2,
    }
    payload[field] = int(value) if field == "max_pages" else value

    with session_factory() as db, pytest.raises(CampaignValidationError, match=message):
        create_saved_search(db, **payload)


def test_campaign_plan_rejects_empty_archived_and_duplicate_membership() -> None:
    session_factory = _session_factory()

    with session_factory() as db:
        campaign = create_campaign(db, name="Graduate campaign")
        empty_plan = build_campaign_plan(db, campaign)
        assert "no runnable saved searches" in " ".join(empty_plan.errors)

        saved = _saved_search(db)
        add_campaign_membership(db, campaign, saved, position=1)
        with pytest.raises(CampaignValidationError, match="already"):
            add_campaign_membership(db, campaign, saved, position=2)

        archive_campaign(db, campaign)
        archived_plan = build_campaign_plan(db, campaign)
        assert "Archived campaigns" in " ".join(archived_plan.errors)


def test_campaign_plan_ordering_disabled_omission_and_planned_pages() -> None:
    session_factory = _session_factory()

    with session_factory() as db:
        campaign = create_campaign(db, name="Weekly reconciliation")
        first = _saved_search(db, "First")
        second = _saved_search(db, "Second")
        third = _saved_search(db, "Disabled")
        add_campaign_membership(db, campaign, second, position=2)
        add_campaign_membership(db, campaign, first, position=1)
        add_campaign_membership(db, campaign, third, position=3, is_enabled=False)

        plan = build_campaign_plan(db, campaign)

        assert [item.saved_search.name for item in plan.items] == ["First", "Second"]
        assert plan.planned_child_count == 2
        assert plan.planned_pages == 4


def test_campaign_plan_reports_unsupported_enabled_source() -> None:
    session_factory = _session_factory()

    with session_factory() as db:
        campaign = create_campaign(db, name="Unsupported source")
        saved = _saved_search(db)
        saved.source = "linkedin"
        db.commit()
        add_campaign_membership(db, campaign, saved)

        plan = build_campaign_plan(db, campaign)

        assert "not supported" in " ".join(plan.errors)
        assert plan.items == []


def test_snapshot_creation_is_immutable_after_saved_search_edit() -> None:
    session_factory = _session_factory()

    with session_factory() as db:
        campaign = create_campaign(db, name="Daily overlap")
        saved = _saved_search(db, query='"strategy analyst" OR "growth analyst"')
        add_campaign_membership(db, campaign, saved, position=1)

        execution = create_campaign_execution_plan(db, campaign)
        update_saved_search(
            db,
            saved,
            name="Renamed",
            source="seek",
            query_text="changed query",
            location="Melbourne VIC",
            date_window="last_3_days",
            max_pages=1,
            is_enabled=True,
            is_archived=False,
        )
        snapshot = db.scalar(
            select(CampaignExecutionChildSnapshot).where(
                CampaignExecutionChildSnapshot.campaign_execution_id == execution.id
            )
        )

        assert execution.status.value == "pending"
        assert execution.planned_child_count == 1
        assert execution.pages_planned == 2
        assert snapshot.saved_search_name_snapshot == "Strategy Sydney"
        assert snapshot.query_text_snapshot == '"strategy analyst" OR "growth analyst"'
        assert snapshot.location_snapshot == "Sydney NSW"
        assert snapshot.date_window_snapshot == "last_7_days"
        assert snapshot.page_limit_snapshot == 2


def test_manual_plan_rejects_partial_or_invalid_schedule_metadata() -> None:
    session_factory = _session_factory()

    with session_factory() as db:
        campaign = create_campaign(db, name="Execution metadata")
        saved = _saved_search(db)
        add_campaign_membership(db, campaign, saved)

        with pytest.raises(CampaignValidationError, match="complete schedule metadata"):
            create_campaign_execution_plan(
                db,
                campaign,
                origin="scheduled",
                schedule_id=1,
            )
        with pytest.raises(CampaignValidationError, match="cannot contain schedule metadata"):
            create_campaign_execution_plan(
                db,
                campaign,
                schedule_id=1,
            )

        execution = create_campaign_execution_plan(db, campaign)
        assert execution.origin == "manual"
        assert execution.schedule_id is None
        assert execution.schedule_occurrence_id is None


def test_migration_creates_campaign_tables_idempotently() -> None:
    from app.migrations import migrate_database

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    migrate_database(engine)
    migrate_database(engine)

    with sessionmaker(bind=engine, expire_on_commit=False)() as db:
        campaign = create_campaign(db, name="Migration smoke")
        saved = _saved_search(db)
        add_campaign_membership(db, campaign, saved)
        execution = create_campaign_execution_plan(db, campaign)

        assert db.scalar(select(Campaign).where(Campaign.id == campaign.id)) is not None
        assert db.scalar(select(CampaignSavedSearch)) is not None
        assert db.scalar(select(CampaignExecution).where(CampaignExecution.id == execution.id))


@pytest.mark.asyncio
async def test_campaign_create_edit_archive_and_detail_routes() -> None:
    session_factory = _session_factory()

    with session_factory() as db:
        response = await main.create_campaign_route(
            FakeFormRequest({"name": "Route campaign", "description": "Daily checks"}),
            db=db,
        )
        assert response.status_code == 303
        campaign = db.scalar(select(Campaign).where(Campaign.name == "Route campaign"))
        saved = _saved_search(db, "Route search")
        add_campaign_membership(db, campaign, saved)

        detail = main.campaign_detail(campaign.id, _request(f"/campaigns/{campaign.id}"), db=db)
        assert "Route campaign" in detail.body.decode()
        assert "mobile-card-list" in detail.body.decode()

        response = await main.update_campaign_route(
            campaign.id,
            FakeFormRequest(
                {"name": "Route campaign edited", "description": "Updated", "is_active": "yes"}
            ),
            db=db,
        )
        assert response.status_code == 303

        archive = main.archive_campaign_route(campaign.id, db=db)
        assert archive.status_code == 303
        assert db.get(Campaign, campaign.id).is_archived is True


@pytest.mark.asyncio
async def test_saved_search_add_preview_and_execution_routes_do_not_collect(monkeypatch) -> None:
    session_factory = _session_factory()
    calls = []
    monkeypatch.setattr(main, "get_collector", lambda *args, **kwargs: calls.append("collector"))

    with session_factory() as db:
        campaign = create_campaign(db, name="Preview campaign")
        response = await main.create_saved_search_for_campaign(
            campaign.id,
            FakeFormRequest(
                {
                    "name": "Boolean search",
                    "source": "seek",
                    "query_text": '"strategy analyst" OR "commercial analyst"',
                    "location": "Sydney NSW",
                    "date_window": "last_2_days",
                    "max_pages": "2",
                    "is_enabled": "yes",
                }
            ),
            db=db,
        )
        assert response.status_code == 303

        preview = main.campaign_preview(
            campaign.id, _request(f"/campaigns/{campaign.id}/preview"), db=db
        )
        body = preview.body.decode()
        assert "Execution preview" in body
        assert "Last 2 days" in body
        assert "starts no browser" in body

        created = main.create_campaign_execution_route(campaign.id, db=db)
        assert created.status_code == 303
        execution = db.scalar(select(CampaignExecution))
        detail = main.campaign_execution_detail(
            execution.id, _request(f"/campaign-executions/{execution.id}"), db=db
        )
        assert "Execution progress" in detail.body.decode()
        assert "&#34;strategy analyst&#34; OR &#34;commercial analyst&#34;" in detail.body.decode()
        assert calls == []


def test_campaign_dashboard_foundation() -> None:
    session_factory = _session_factory()

    with session_factory() as db:
        campaign = create_campaign(db, name="Dashboard campaign")
        saved = _saved_search(db)
        add_campaign_membership(db, campaign, saved)
        create_campaign_execution_plan(db, campaign)

        dashboard = main.index(_request("/"), db=db)
        body = dashboard.body.decode()
        assert "Active campaigns" in body
        assert "Saved searches" in body
        assert "Latest campaign plan" in body


def test_campaign_detail_missing_uses_404() -> None:
    session_factory = _session_factory()

    with session_factory() as db:
        with pytest.raises(HTTPException) as exc_info:
            main.campaign_detail(999, _request("/campaigns/999"), db=db)
        assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_custom_404_renders_safely_for_campaign_area() -> None:
    response = await main.not_found_page(_request("/campaigns/missing"), HTTPException(404))

    assert response.status_code == 404
    assert "could not be found" in response.body.decode()


class FakeCampaignCollector:
    def __init__(self, session_factory, status: RunStatus) -> None:
        self.session_factory = session_factory
        self.status = status

    def collect(self, collector_input) -> None:
        with self.session_factory() as db:
            child_run = db.get(SearchRun, collector_input.run_id)
            assert child_run is not None
            for index in range(child_run.pages_requested):
                save_job_discovery(
                    db,
                    child_run.id,
                    index + 1,
                    {
                        "seek_job_id": f"{child_run.id}-{index}",
                        "fallback_key": f"fallback-{child_run.id}-{index}",
                        "title": f"Fake job {child_run.id}-{index}",
                        "company": "Example Co",
                        "location": "Sydney NSW",
                        "salary": None,
                        "work_type": "Full time",
                        "posting_date": "1d ago",
                        "url": f"https://www.seek.com.au/job/{child_run.id}{index}",
                        "description": "Synthetic campaign execution job.",
                    },
                )
            db.refresh(child_run)
            child_run.pages_attempted = child_run.pages_requested
            child_run.pages_completed = child_run.pages_requested
            child_run.result_cards_observed = child_run.pages_requested * 10
            child_run.unique_jobs_in_run = child_run.pages_requested
            child_run.new_jobs_added = 1
            child_run.known_jobs_rediscovered = max(0, child_run.pages_requested - 1)
            child_run.jobs_updated = 1
            child_run.duplicate_cards_ignored = 2
            if self.status == RunStatus.AWAITING_USER:
                set_run_stop_reason(db, child_run.id, "verification_required")
                mark_run(db, child_run.id, RunStatus.AWAITING_USER, "Manual action required")
            else:
                mark_run(db, child_run.id, self.status, "Fake collector finished")


class ExplodingCampaignCollector:
    def __init__(self, session_factory) -> None:
        self.session_factory = session_factory

    def collect(self, collector_input) -> None:
        with self.session_factory() as db:
            mark_run(db, collector_input.run_id, RunStatus.RUNNING, "Collector started")
        raise RuntimeError("boom")


class DuplicateJobCampaignCollector:
    def __init__(self, session_factory) -> None:
        self.session_factory = session_factory

    def collect(self, collector_input) -> None:
        with self.session_factory() as db:
            child_run = db.get(SearchRun, collector_input.run_id)
            assert child_run is not None
            save_job_discovery(
                db,
                child_run.id,
                1,
                {
                    "seek_job_id": "same-job",
                    "fallback_key": "same-fallback",
                    "title": "Same Job",
                    "company": "Example Co",
                    "location": "Sydney NSW",
                    "salary": None,
                    "work_type": "Full time",
                    "posting_date": "1d ago",
                    "url": "https://www.seek.com.au/job/999",
                    "description": "Same synthetic job.",
                },
            )
            db.refresh(child_run)
            child_run.pages_attempted = child_run.pages_requested
            child_run.pages_completed = child_run.pages_requested
            child_run.result_cards_observed = 1
            child_run.unique_jobs_in_run = child_run.unique_jobs_in_run or 0
            child_run.new_jobs_added = child_run.new_jobs_added or 0
            child_run.known_jobs_rediscovered = child_run.known_jobs_rediscovered or 0
            child_run.jobs_updated = child_run.jobs_updated or 0
            child_run.duplicate_cards_ignored = child_run.duplicate_cards_ignored or 0
            mark_run(db, child_run.id, RunStatus.COMPLETED, "Fake collector finished")


def _fake_resolver(statuses: list[RunStatus], session_factory):
    def resolver(_source_identifier, _settings, _session_factory):
        return FakeCampaignCollector(session_factory, statuses.pop(0))

    return resolver


def test_campaign_execution_runs_children_sequentially_and_aggregates() -> None:
    session_factory = _session_factory()

    with session_factory() as db:
        campaign = create_campaign(db, name="Sequential execution")
        first = _saved_search(db, "First child")
        second = _saved_search(db, "Second child", query="growth analyst")
        second.max_pages = 3
        db.commit()
        add_campaign_membership(db, campaign, first, position=1)
        add_campaign_membership(db, campaign, second, position=2)
        execution = create_campaign_execution_plan(db, campaign)
        execution_id = execution.id

    main._run_campaign_execution(
        execution_id,
        session_factory=session_factory,
        collector_resolver=_fake_resolver(
            [RunStatus.COMPLETED, RunStatus.COMPLETED_WITH_ERRORS], session_factory
        ),
    )

    with session_factory() as db:
        execution = db.get(CampaignExecution, execution_id)
        snapshots = db.scalars(
            select(CampaignExecutionChildSnapshot)
            .where(CampaignExecutionChildSnapshot.campaign_execution_id == execution_id)
            .order_by(CampaignExecutionChildSnapshot.position)
        ).all()

        assert execution.status == RunStatus.COMPLETED_WITH_ERRORS
        assert execution.attempted_child_count == 2
        assert execution.completed_child_count == 2
        assert execution.pages_completed == 5
        assert execution.result_cards_observed == 50
        assert execution.unique_jobs == 5
        assert execution.new_jobs == 2
        assert execution.rediscoveries == 3
        assert execution.updated_jobs == 2
        assert execution.duplicate_cards == 4
        assert [snapshot.child_run_id is not None for snapshot in snapshots] == [True, True]
        assert [snapshot.status for snapshot in snapshots] == [
            RunStatus.COMPLETED,
            RunStatus.COMPLETED_WITH_ERRORS,
        ]


def test_campaign_execution_awaiting_user_stops_and_resume_continues() -> None:
    session_factory = _session_factory()

    with session_factory() as db:
        campaign = create_campaign(db, name="Awaiting campaign")
        first = _saved_search(db, "Challenge child")
        second = _saved_search(db, "Follow-up child", query="operations analyst")
        add_campaign_membership(db, campaign, first, position=1)
        add_campaign_membership(db, campaign, second, position=2)
        execution = create_campaign_execution_plan(db, campaign)
        execution_id = execution.id

    main._run_campaign_execution(
        execution_id,
        session_factory=session_factory,
        collector_resolver=_fake_resolver([RunStatus.AWAITING_USER], session_factory),
    )

    with session_factory() as db:
        execution = db.get(CampaignExecution, execution_id)
        assert execution.status == RunStatus.AWAITING_USER
        assert execution.awaiting_user_child_count == 1
        assert execution.current_child_id is not None
        awaiting_child = db.get(CampaignExecutionChildSnapshot, execution.current_child_id)
        assert awaiting_child is not None
        awaiting_child_run_id = awaiting_child.child_run_id
        assert awaiting_child_run_id is not None
        assert (
            db.scalars(
                select(JobDiscovery).where(JobDiscovery.run_id == awaiting_child_run_id)
            ).first()
            is not None
        )

    main._run_campaign_execution(
        execution_id,
        session_factory=session_factory,
        collector_resolver=_fake_resolver(
            [RunStatus.COMPLETED, RunStatus.COMPLETED], session_factory
        ),
    )

    with session_factory() as db:
        execution = db.get(CampaignExecution, execution_id)
        assert execution.status == RunStatus.COMPLETED
        assert execution.completed_child_count == 2
        assert execution.awaiting_user_child_count == 0
        snapshots = db.scalars(
            select(CampaignExecutionChildSnapshot)
            .where(CampaignExecutionChildSnapshot.campaign_execution_id == execution_id)
            .order_by(CampaignExecutionChildSnapshot.position)
        ).all()
        assert snapshots[0].child_run_id == awaiting_child_run_id


def test_campaign_start_respects_existing_source_session_lock(monkeypatch) -> None:
    session_factory = _session_factory()
    submitted = []
    monkeypatch.setattr(main.seek_session_manager, "is_profile_busy", lambda: True)
    monkeypatch.setattr(main, "_submit_background", lambda *args: submitted.append(args))

    with session_factory() as db:
        campaign = create_campaign(db, name="Busy profile")
        saved = _saved_search(db)
        add_campaign_membership(db, campaign, saved)
        execution = create_campaign_execution_plan(db, campaign)

        response = main.start_campaign_execution_route(execution.id, db=db)

        assert response.status_code == 303
        stored = db.get(CampaignExecution, execution.id)
        assert stored.status == RunStatus.PENDING
        assert stored.message.startswith("Waiting for source session/profile")
        assert submitted == []


def test_campaign_start_rejects_unsupported_snapshot_without_worker(monkeypatch) -> None:
    session_factory = _session_factory()
    submitted = []
    monkeypatch.setattr(main, "_submit_background", lambda *args: submitted.append(args))

    with session_factory() as db:
        campaign = create_campaign(db, name="Unsupported execution")
        saved = _saved_search(db)
        add_campaign_membership(db, campaign, saved)
        execution = create_campaign_execution_plan(db, campaign)
        snapshot = db.scalar(
            select(CampaignExecutionChildSnapshot).where(
                CampaignExecutionChildSnapshot.campaign_execution_id == execution.id
            )
        )
        snapshot.source = "linkedin"
        db.commit()

        response = main.start_campaign_execution_route(execution.id, db=db)

        assert response.status_code == 303
        stored = db.get(CampaignExecution, execution.id)
        assert stored.status == RunStatus.FAILED
        assert stored.stop_reason == "unsupported_source"
        assert submitted == []


def test_repeated_start_is_idempotent_and_does_not_submit_duplicate_worker(monkeypatch) -> None:
    session_factory = _session_factory()
    submitted = []
    monkeypatch.setattr(main, "_submit_background", lambda *args: submitted.append(args))

    with session_factory() as db:
        campaign = create_campaign(db, name="Duplicate start")
        saved = _saved_search(db)
        add_campaign_membership(db, campaign, saved)
        execution = create_campaign_execution_plan(db, campaign)
        execution_id = execution.id

        first = main.start_campaign_execution_route(execution.id, db=db)
        second = main.start_campaign_execution_route(execution.id, db=db)

        assert first.status_code == 303
        assert second.status_code == 303
        assert len(submitted) == 1
        assert db.get(CampaignExecution, execution.id).message == (
            "Campaign execution is already queued or running."
        )

    main._release_source_profile(f"campaign:{execution_id}")
    main._campaign_guard_release(execution_id)


def test_repeated_resume_is_idempotent_and_does_not_submit_duplicate_worker(monkeypatch) -> None:
    session_factory = _session_factory()
    submitted = []
    monkeypatch.setattr(main, "_submit_background", lambda *args: submitted.append(args))

    with session_factory() as db:
        campaign = create_campaign(db, name="Duplicate resume")
        saved = _saved_search(db)
        add_campaign_membership(db, campaign, saved)
        execution = create_campaign_execution_plan(db, campaign)
        execution.status = RunStatus.AWAITING_USER
        execution_id = execution.id
        db.commit()

        first = main.resume_campaign_execution_route(execution.id, db=db)
        second = main.resume_campaign_execution_route(execution.id, db=db)

        assert first.status_code == 303
        assert second.status_code == 303
        assert len(submitted) == 1
        assert db.get(CampaignExecution, execution.id).message == (
            "Campaign execution is already queued or running."
        )

    main._release_source_profile(f"campaign:{execution_id}")
    main._campaign_guard_release(execution_id)


def test_recoverable_child_failure_continues_and_finishes_with_errors() -> None:
    session_factory = _session_factory()

    with session_factory() as db:
        campaign = create_campaign(db, name="Recoverable child")
        first = _saved_search(db, "Failing child")
        second = _saved_search(db, "Successful child", query="ops analyst")
        add_campaign_membership(db, campaign, first, position=1)
        add_campaign_membership(db, campaign, second, position=2)
        execution = create_campaign_execution_plan(db, campaign)
        execution_id = execution.id

    main._run_campaign_execution(
        execution_id,
        session_factory=session_factory,
        collector_resolver=_fake_resolver([RunStatus.FAILED, RunStatus.COMPLETED], session_factory),
    )

    with session_factory() as db:
        execution = db.get(CampaignExecution, execution_id)
        assert execution.status == RunStatus.COMPLETED_WITH_ERRORS
        assert execution.failed_child_count == 1
        assert execution.completed_child_count == 1
        assert "child_failed" in [event.code for event in execution.events]


def test_campaign_browser_interruption_stops_later_children() -> None:
    session_factory = _session_factory()

    with session_factory() as db:
        campaign = create_campaign(db, name="Interrupted child")
        first = _saved_search(db, "Interrupted child")
        second = _saved_search(db, "Skipped child", query="ops analyst")
        add_campaign_membership(db, campaign, first, position=1)
        add_campaign_membership(db, campaign, second, position=2)
        execution = create_campaign_execution_plan(db, campaign)
        execution_id = execution.id

    main._run_campaign_execution(
        execution_id,
        session_factory=session_factory,
        collector_resolver=_fake_resolver([RunStatus.INTERRUPTED], session_factory),
    )

    with session_factory() as db:
        execution = db.get(CampaignExecution, execution_id)
        snapshots = db.scalars(
            select(CampaignExecutionChildSnapshot)
            .where(CampaignExecutionChildSnapshot.campaign_execution_id == execution_id)
            .order_by(CampaignExecutionChildSnapshot.position)
        ).all()
        assert execution.status == RunStatus.INTERRUPTED
        assert snapshots[0].status == RunStatus.INTERRUPTED
        assert snapshots[1].status == RunStatus.PENDING
        assert "interrupted" in [event.code for event in execution.events]


def test_unexpected_campaign_worker_exception_marks_failed_and_releases_lock() -> None:
    session_factory = _session_factory()

    with session_factory() as db:
        campaign = create_campaign(db, name="Worker exception")
        saved = _saved_search(db)
        add_campaign_membership(db, campaign, saved)
        execution = create_campaign_execution_plan(db, campaign)
        execution_id = execution.id

    main._run_campaign_execution(
        execution_id,
        session_factory=session_factory,
        collector_resolver=lambda *_args: ExplodingCampaignCollector(session_factory),
    )

    with session_factory() as db:
        execution = db.get(CampaignExecution, execution_id)
        snapshots = db.scalars(
            select(CampaignExecutionChildSnapshot).where(
                CampaignExecutionChildSnapshot.campaign_execution_id == execution_id
            )
        ).all()
        child_runs = db.scalars(
            select(SearchRun).where(
                SearchRun.id.in_(
                    [snapshot.child_run_id for snapshot in snapshots if snapshot.child_run_id]
                )
            )
        ).all()
        assert execution.status == RunStatus.FAILED
        assert execution.stop_reason == "worker_exception"
        assert execution.finished_at is not None
        assert "RuntimeError: boom" in execution.message
        assert len(snapshots) == 1
        assert snapshots[0].status == RunStatus.FAILED
        assert snapshots[0].stop_reason == "worker_exception"
        assert len(child_runs) == 1
        assert child_runs[0].status == RunStatus.FAILED
        assert child_runs[0].stop_reason == "worker_exception"
        assert child_runs[0].finished_at is not None
        assert "RuntimeError: boom" in child_runs[0].message
        assert all(snapshot.status != RunStatus.RUNNING for snapshot in snapshots)
        assert all(run.status != RunStatus.RUNNING for run in child_runs)
        assert [event.code for event in execution.events].count("child_failed") == 1
        assert "failed" in [event.code for event in execution.events]
        assert [event.code for event in child_runs[0].events].count("collector_failed") == 1
    assert main._try_acquire_source_profile(f"campaign:{execution_id}") is True
    main._release_source_profile(f"campaign:{execution_id}")


def test_startup_reconciliation_interrupts_running_campaign_only() -> None:
    session_factory = _session_factory()

    with session_factory() as db:
        running_campaign = create_campaign(db, name="Running at shutdown")
        running_saved = _saved_search(db, "Running saved")
        add_campaign_membership(db, running_campaign, running_saved)
        running_execution = create_campaign_execution_plan(db, running_campaign)
        running_execution.status = RunStatus.RUNNING

        awaiting_campaign = create_campaign(db, name="Awaiting at shutdown")
        awaiting_saved = _saved_search(db, "Awaiting saved")
        add_campaign_membership(db, awaiting_campaign, awaiting_saved)
        awaiting_execution = create_campaign_execution_plan(db, awaiting_campaign)
        awaiting_execution.status = RunStatus.AWAITING_USER
        db.commit()

        changed = reconcile_running_campaign_executions(db)

        assert changed == 1
        assert db.get(CampaignExecution, running_execution.id).status == RunStatus.INTERRUPTED
        assert db.get(CampaignExecution, awaiting_execution.id).status == RunStatus.AWAITING_USER


def test_campaign_unique_jobs_deduplicates_same_job_across_children() -> None:
    session_factory = _session_factory()

    with session_factory() as db:
        campaign = create_campaign(db, name="Dedup campaign")
        first = _saved_search(db, "First duplicate")
        second = _saved_search(db, "Second duplicate", query="duplicate analyst")
        add_campaign_membership(db, campaign, first, position=1)
        add_campaign_membership(db, campaign, second, position=2)
        execution = create_campaign_execution_plan(db, campaign)
        execution_id = execution.id

    main._run_campaign_execution(
        execution_id,
        session_factory=session_factory,
        collector_resolver=lambda *_args: DuplicateJobCampaignCollector(session_factory),
    )

    with session_factory() as db:
        execution = db.get(CampaignExecution, execution_id)
        assert execution.unique_jobs == 1
        assert execution.new_jobs == 1
        assert execution.rediscoveries == 1


@pytest.mark.asyncio
async def test_campaign_execution_csv_export_has_plain_text_child_provenance() -> None:
    session_factory = _session_factory()

    with session_factory() as db:
        campaign = create_campaign(db, name="Export campaign")
        saved = _saved_search(db)
        add_campaign_membership(db, campaign, saved)
        execution = create_campaign_execution_plan(db, campaign)
        execution_id = execution.id

    main._run_campaign_execution(
        execution_id,
        session_factory=session_factory,
        collector_resolver=_fake_resolver([RunStatus.COMPLETED], session_factory),
    )

    with session_factory() as db:
        response = main.export_campaign_execution(execution_id, db=db)
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk)
        body = b"".join(
            chunk if isinstance(chunk, bytes) else chunk.encode() for chunk in chunks
        ).decode()

    assert "campaign_id,campaign_name,execution_id,child_order" in body
    assert "https://www.seek.com.au/job/" in body
    assert "<a href" not in body
