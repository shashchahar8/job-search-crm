import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, time, timedelta
from threading import Barrier

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app import scheduling
from app.campaigns import (
    add_campaign_membership,
    create_campaign,
    create_campaign_execution_plan,
    create_saved_search,
)
from app.models import (
    Base,
    CampaignExecution,
    CampaignExecutionChildSnapshot,
    CampaignSchedule,
    CampaignScheduleOccurrence,
    RunStatus,
    SearchRun,
)
from app.scheduling import (
    DuePlanOutcome,
    ScheduleValidationError,
    create_campaign_schedule,
    disable_campaign_schedule,
    enable_campaign_schedule,
    next_occurrence_after,
    normalize_utc,
    plan_all_due_schedules,
    plan_due_schedule,
    resolve_local_occurrence,
    update_campaign_schedule,
)


def _session_factory(url: str = "sqlite:///:memory:"):
    engine = create_engine(url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _campaign_with_search(db, name: str = "Scheduled campaign"):
    campaign = create_campaign(db, name=name)
    saved = create_saved_search(
        db,
        name=f"{name} search",
        source="seek",
        query_text="strategy analyst",
        location="Sydney NSW",
        date_window="last_2_days",
        max_pages=2,
    )
    add_campaign_membership(db, campaign, saved)
    return campaign


def _daily_schedule(db, campaign, *, now: datetime, hour: int = 9):
    return create_campaign_schedule(
        db,
        campaign,
        recurrence_type="daily",
        local_time=time(hour, 0),
        timezone_name="Australia/Sydney",
        now_utc=now,
    )


def test_sqlite_datetime_round_trip_is_normalized_at_service_boundary() -> None:
    session_factory = _session_factory()
    aware = datetime(2026, 7, 20, 1, 2, tzinfo=UTC)

    with session_factory() as db:
        campaign = _campaign_with_search(db)
        schedule = _daily_schedule(db, campaign, now=aware)
        schedule.next_occurrence_at = aware
        db.commit()
        schedule_id = schedule.id

    with session_factory() as db:
        stored = db.get(CampaignSchedule, schedule_id)
        assert stored.next_occurrence_at.tzinfo is None
        result = next_occurrence_after(stored, stored.next_occurrence_at)
        assert result.scheduled_for_at.tzinfo == UTC


def test_daily_schedule_starts_strictly_after_creation() -> None:
    session_factory = _session_factory()

    with session_factory() as db:
        campaign = _campaign_with_search(db)
        before = _daily_schedule(
            db,
            campaign,
            now=datetime(2026, 7, 20, 22, 0, tzinfo=UTC),
        )
        assert before.next_occurrence_at == datetime(2026, 7, 20, 23, 0)

        second_campaign = _campaign_with_search(db, "After local time")
        after = _daily_schedule(
            db,
            second_campaign,
            now=datetime(2026, 7, 20, 23, 30, tzinfo=UTC),
        )
        assert after.next_occurrence_at == datetime(2026, 7, 21, 23, 0)


def test_weekly_schedule_supports_multiple_weekdays_and_week_rollover() -> None:
    session_factory = _session_factory()
    monday_and_friday = (1 << 0) | (1 << 4)

    with session_factory() as db:
        campaign = _campaign_with_search(db)
        schedule = create_campaign_schedule(
            db,
            campaign,
            recurrence_type="weekly",
            local_time=time(9),
            timezone_name="Australia/Sydney",
            weekday_mask=monday_and_friday,
            now_utc=datetime(2026, 7, 20, 0, 0, tzinfo=UTC),
        )
        assert schedule.next_occurrence_at == datetime(2026, 7, 23, 23, 0)

        following = next_occurrence_after(schedule, schedule.next_occurrence_at)
        assert following.scheduled_local_date.isoformat() == "2026-07-27"
        assert following.scheduled_for_at == datetime(2026, 7, 26, 23, 0, tzinfo=UTC)


def test_schedule_validation_rejects_bad_recurrence_weekdays_and_campaign_state() -> None:
    session_factory = _session_factory()

    with session_factory() as db:
        campaign = _campaign_with_search(db)
        with pytest.raises(ScheduleValidationError, match="select weekdays"):
            create_campaign_schedule(
                db,
                campaign,
                recurrence_type="daily",
                local_time=time(9),
                weekday_mask=1,
            )

        campaign.is_active = False
        db.commit()
        with pytest.raises(ScheduleValidationError, match="Inactive"):
            create_campaign_schedule(
                db,
                campaign,
                recurrence_type="weekly",
                local_time=time(9),
                weekday_mask=1,
            )


def test_dst_gap_shifts_forward_and_fold_uses_first_occurrence() -> None:
    gap = resolve_local_occurrence(
        datetime(2026, 10, 4).date(),
        time(2, 30),
        "Australia/Sydney",
    )
    assert gap.resolution == "gap_shifted"
    assert gap.scheduled_for_at == datetime(2026, 10, 3, 16, 30, tzinfo=UTC)
    assert gap.utc_offset_minutes == 660

    fold = resolve_local_occurrence(
        datetime(2026, 4, 5).date(),
        time(2, 30),
        "Australia/Sydney",
    )
    assert fold.resolution == "fold_first"
    assert fold.fold == 0
    assert fold.scheduled_for_at == datetime(2026, 4, 4, 15, 30, tzinfo=UTC)
    assert fold.utc_offset_minutes == 660


def test_weekly_occurrences_cross_dst_using_local_calendar() -> None:
    session_factory = _session_factory()

    with session_factory() as db:
        campaign = _campaign_with_search(db)
        schedule = create_campaign_schedule(
            db,
            campaign,
            recurrence_type="weekly",
            local_time=time(9),
            timezone_name="Australia/Sydney",
            weekday_mask=1 << 0,
            now_utc=datetime(2026, 9, 27, 0, 0, tzinfo=UTC),
        )
        before_dst = next_occurrence_after(
            schedule, datetime(2026, 9, 27, 0, 0, tzinfo=UTC)
        )
        after_dst = next_occurrence_after(schedule, before_dst.scheduled_for_at)
        assert before_dst.utc_offset_minutes == 600
        assert after_dst.utc_offset_minutes == 660
        assert before_dst.scheduled_local_time == after_dst.scheduled_local_time == time(9)


def test_explicit_timezone_is_independent_of_sydney_and_month_boundary() -> None:
    london = resolve_local_occurrence(
        datetime(2026, 7, 31).date(),
        time(9),
        "Europe/London",
    )
    sydney = resolve_local_occurrence(
        datetime(2026, 7, 31).date(),
        time(9),
        "Australia/Sydney",
    )
    assert london.scheduled_for_at == datetime(2026, 7, 31, 8, 0, tzinfo=UTC)
    assert sydney.scheduled_for_at == datetime(2026, 7, 30, 23, 0, tzinfo=UTC)


def test_due_planning_is_frozen_atomic_and_has_no_collection_side_effects() -> None:
    session_factory = _session_factory()
    start = datetime(2026, 7, 20, 0, 0, tzinfo=UTC)

    with session_factory() as db:
        campaign = _campaign_with_search(db)
        schedule = _daily_schedule(db, campaign, now=start)
        due = schedule.next_occurrence_at.replace(tzinfo=UTC)
        result = plan_due_schedule(db, schedule.id, now_utc=due)

        assert result.outcome == DuePlanOutcome.PLANNED
        execution = db.get(CampaignExecution, result.execution_id)
        occurrence = db.get(CampaignScheduleOccurrence, result.occurrence_id)
        snapshots = db.scalars(
            select(CampaignExecutionChildSnapshot).where(
                CampaignExecutionChildSnapshot.campaign_execution_id == execution.id
            )
        ).all()
        assert execution.origin == "scheduled"
        assert execution.status == RunStatus.PENDING
        assert execution.schedule_id == schedule.id
        assert execution.schedule_occurrence_id == occurrence.id
        assert occurrence.disposition == "planned"
        assert len(snapshots) == 1
        assert db.scalar(select(func.count(SearchRun.id))) == 0


def test_latest_missed_occurrence_only_is_planned_with_audit_summary() -> None:
    session_factory = _session_factory()
    start = datetime(2026, 7, 20, 0, 0, tzinfo=UTC)

    with session_factory() as db:
        campaign = _campaign_with_search(db)
        schedule = _daily_schedule(db, campaign, now=start)
        result = plan_due_schedule(
            db,
            schedule.id,
            now_utc=datetime(2026, 7, 23, 1, 0, tzinfo=UTC),
        )
        occurrence = db.get(CampaignScheduleOccurrence, result.occurrence_id)
        assert result.outcome == DuePlanOutcome.PLANNED
        assert occurrence.scheduled_local_date.isoformat() == "2026-07-23"
        assert occurrence.superseded_count == 2
        assert occurrence.superseded_from_at == datetime(2026, 7, 20, 23, 0)
        assert db.scalar(select(func.count(CampaignExecution.id))) == 1
        assert normalize_utc(
            db.get(CampaignSchedule, schedule.id).next_occurrence_at
        ) == datetime(2026, 7, 23, 23, 0, tzinfo=UTC)


def test_multiple_missed_weekly_occurrences_plan_only_latest() -> None:
    session_factory = _session_factory()

    with session_factory() as db:
        campaign = _campaign_with_search(db)
        schedule = create_campaign_schedule(
            db,
            campaign,
            recurrence_type="weekly",
            local_time=time(9),
            timezone_name="Australia/Sydney",
            weekday_mask=(1 << 0) | (1 << 4),
            now_utc=datetime(2026, 7, 20, 0, 0, tzinfo=UTC),
        )
        result = plan_due_schedule(
            db,
            schedule.id,
            now_utc=datetime(2026, 8, 4, 0, 0, tzinfo=UTC),
        )
        occurrence = db.get(CampaignScheduleOccurrence, result.occurrence_id)
        assert result.outcome == DuePlanOutcome.PLANNED
        assert occurrence.scheduled_local_date.isoformat() == "2026-08-03"
        assert occurrence.superseded_count == 3
        assert db.scalar(select(func.count(CampaignExecution.id))) == 1


def test_one_schedule_cannot_accumulate_two_unresolved_executions() -> None:
    session_factory = _session_factory()
    start = datetime(2026, 7, 20, 0, 0, tzinfo=UTC)

    with session_factory() as db:
        campaign = _campaign_with_search(db)
        schedule = _daily_schedule(db, campaign, now=start)
        first_due = schedule.next_occurrence_at.replace(tzinfo=UTC)
        first = plan_due_schedule(db, schedule.id, now_utc=first_due)
        second_due = db.get(CampaignSchedule, schedule.id).next_occurrence_at.replace(tzinfo=UTC)
        second = plan_due_schedule(db, schedule.id, now_utc=second_due)

        assert first.outcome == DuePlanOutcome.PLANNED
        assert second.outcome == DuePlanOutcome.SKIPPED
        assert second.reason_code == "previous_scheduled_execution_unresolved"
        assert second.blocking_execution_id == first.execution_id
        occurrence = db.get(CampaignScheduleOccurrence, second.occurrence_id)
        assert occurrence.disposition == "skipped"
        assert occurrence.blocking_execution_id == first.execution_id
        assert db.scalar(select(func.count(CampaignExecution.id))) == 1

        repeated = plan_due_schedule(db, schedule.id, now_utc=second_due)
        assert repeated.outcome == DuePlanOutcome.ALREADY_CONSIDERED
        assert repeated.occurrence_id == second.occurrence_id
        assert repeated.blocking_execution_id == first.execution_id
        assert db.scalar(select(func.count(CampaignScheduleOccurrence.id))) == 2


@pytest.mark.parametrize("status", [RunStatus.RUNNING, RunStatus.AWAITING_USER])
def test_running_and_awaiting_user_are_unresolved_for_same_schedule(status) -> None:
    session_factory = _session_factory()
    start = datetime(2026, 7, 20, 0, 0, tzinfo=UTC)

    with session_factory() as db:
        campaign = _campaign_with_search(db)
        schedule = _daily_schedule(db, campaign, now=start)
        first = plan_due_schedule(
            db, schedule.id, now_utc=schedule.next_occurrence_at.replace(tzinfo=UTC)
        )
        db.get(CampaignExecution, first.execution_id).status = status
        db.commit()
        next_due = db.get(CampaignSchedule, schedule.id).next_occurrence_at.replace(tzinfo=UTC)
        second = plan_due_schedule(db, schedule.id, now_utc=next_due)
        assert second.outcome == DuePlanOutcome.SKIPPED
        assert second.blocking_execution_id == first.execution_id


def test_different_schedules_can_each_have_one_unresolved_execution() -> None:
    session_factory = _session_factory()
    start = datetime(2026, 7, 20, 0, 0, tzinfo=UTC)

    with session_factory() as db:
        first_campaign = _campaign_with_search(db, "First schedule")
        second_campaign = _campaign_with_search(db, "Second schedule")
        _daily_schedule(db, first_campaign, now=start)
        _daily_schedule(db, second_campaign, now=start)
        results = plan_all_due_schedules(
            db,
            now_utc=datetime(2026, 7, 20, 23, 0, tzinfo=UTC),
        )
        assert [result.outcome for result in results] == [
            DuePlanOutcome.PLANNED,
            DuePlanOutcome.PLANNED,
        ]
        assert db.scalar(select(func.count(CampaignExecution.id))) == 2


def test_terminal_scheduled_execution_allows_next_occurrence() -> None:
    session_factory = _session_factory()
    start = datetime(2026, 7, 20, 0, 0, tzinfo=UTC)

    with session_factory() as db:
        campaign = _campaign_with_search(db)
        schedule = _daily_schedule(db, campaign, now=start)
        first = plan_due_schedule(
            db, schedule.id, now_utc=schedule.next_occurrence_at.replace(tzinfo=UTC)
        )
        execution = db.get(CampaignExecution, first.execution_id)
        execution.status = RunStatus.COMPLETED
        db.commit()
        next_due = db.get(CampaignSchedule, schedule.id).next_occurrence_at.replace(tzinfo=UTC)
        second = plan_due_schedule(db, schedule.id, now_utc=next_due)
        assert second.outcome == DuePlanOutcome.PLANNED
        assert second.execution_id != first.execution_id


def test_manual_pending_execution_does_not_block_scheduled_planning() -> None:
    session_factory = _session_factory()
    start = datetime(2026, 7, 20, 0, 0, tzinfo=UTC)

    with session_factory() as db:
        campaign = _campaign_with_search(db)
        manual = create_campaign_execution_plan(db, campaign)
        schedule = _daily_schedule(db, campaign, now=start)
        result = plan_due_schedule(
            db, schedule.id, now_utc=schedule.next_occurrence_at.replace(tzinfo=UTC)
        )
        assert manual.status == RunStatus.PENDING
        assert manual.origin == "manual"
        assert result.outcome == DuePlanOutcome.PLANNED
        assert db.scalar(select(func.count(CampaignExecution.id))) == 2


def test_inactive_invalid_and_archived_occurrences_are_durable() -> None:
    session_factory = _session_factory()
    start = datetime(2026, 7, 20, 0, 0, tzinfo=UTC)

    with session_factory() as db:
        campaign = _campaign_with_search(db)
        schedule = _daily_schedule(db, campaign, now=start)
        campaign.is_active = False
        db.commit()
        result = plan_due_schedule(
            db, schedule.id, now_utc=schedule.next_occurrence_at.replace(tzinfo=UTC)
        )
        assert result.outcome == DuePlanOutcome.SKIPPED
        assert result.reason_code == "campaign_inactive"
        assert db.get(CampaignScheduleOccurrence, result.occurrence_id).disposition == "skipped"

        campaign.is_active = True
        membership = campaign.memberships[0]
        membership.is_enabled = False
        db.commit()
        next_due = db.get(CampaignSchedule, schedule.id).next_occurrence_at.replace(tzinfo=UTC)
        invalid = plan_due_schedule(db, schedule.id, now_utc=next_due)
        assert invalid.outcome == DuePlanOutcome.INVALID
        assert invalid.reason_code == "no_runnable_searches"


def test_edit_disable_and_reenable_do_not_rewrite_history_or_catch_up() -> None:
    session_factory = _session_factory()
    start = datetime(2026, 7, 20, 0, 0, tzinfo=UTC)

    with session_factory() as db:
        campaign = _campaign_with_search(db)
        schedule = _daily_schedule(db, campaign, now=start)
        old_due = schedule.next_occurrence_at.replace(tzinfo=UTC)
        schedule = update_campaign_schedule(
            db,
            schedule,
            recurrence_type="weekly",
            local_time=time(10),
            timezone_name="Australia/Sydney",
            weekday_mask=1 << 4,
            is_enabled=True,
            now_utc=old_due,
        )
        assert db.scalar(select(func.count(CampaignScheduleOccurrence.id))) == 1
        assert db.scalar(select(func.count(CampaignExecution.id))) == 1
        historical_next = schedule.next_occurrence_at

        schedule = disable_campaign_schedule(
            db,
            schedule,
            now_utc=datetime(2026, 7, 30, 0, 0, tzinfo=UTC),
        )
        assert schedule.next_occurrence_at is None
        occurrence_count = db.scalar(select(func.count(CampaignScheduleOccurrence.id)))

        schedule = enable_campaign_schedule(
            db,
            schedule,
            now_utc=datetime(2026, 8, 10, 0, 0, tzinfo=UTC),
        )
        assert schedule.next_occurrence_at > datetime(2026, 8, 10, 0, 0)
        assert schedule.next_occurrence_at != historical_next
        assert db.scalar(select(func.count(CampaignScheduleOccurrence.id))) == occurrence_count


def test_edit_shortly_before_due_does_not_create_old_occurrence() -> None:
    session_factory = _session_factory()
    start = datetime(2026, 7, 20, 0, 0, tzinfo=UTC)

    with session_factory() as db:
        campaign = _campaign_with_search(db)
        schedule = _daily_schedule(db, campaign, now=start)
        old_due = schedule.next_occurrence_at.replace(tzinfo=UTC)
        schedule = update_campaign_schedule(
            db,
            schedule,
            recurrence_type="daily",
            local_time=time(10),
            timezone_name="Australia/Sydney",
            weekday_mask=None,
            is_enabled=True,
            now_utc=old_due - timedelta(minutes=1),
        )
        assert db.scalar(select(func.count(CampaignScheduleOccurrence.id))) == 0
        assert schedule.next_occurrence_at == datetime(2026, 7, 21, 0, 0)


def test_occurrence_and_execution_uniqueness_constraints_reject_duplicates() -> None:
    session_factory = _session_factory()
    start = datetime(2026, 7, 20, 0, 0, tzinfo=UTC)

    with session_factory() as db:
        campaign = _campaign_with_search(db)
        schedule = _daily_schedule(db, campaign, now=start)
        result = plan_due_schedule(
            db, schedule.id, now_utc=schedule.next_occurrence_at.replace(tzinfo=UTC)
        )
        occurrence = db.get(CampaignScheduleOccurrence, result.occurrence_id)
        db.add(
            CampaignScheduleOccurrence(
                schedule_id=occurrence.schedule_id,
                campaign_id=occurrence.campaign_id,
                scheduled_for_at=occurrence.scheduled_for_at,
                scheduled_local_date=occurrence.scheduled_local_date,
                scheduled_local_time=occurrence.scheduled_local_time,
                timezone_name=occurrence.timezone_name,
                utc_offset_minutes=occurrence.utc_offset_minutes,
                fold=occurrence.fold,
                resolution=occurrence.resolution,
                disposition="skipped",
                considered_at=start,
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()


def test_two_due_checks_racing_create_one_complete_plan(tmp_path, monkeypatch) -> None:
    database_path = tmp_path / "schedule-race.sqlite3"
    engine = create_engine(
        f"sqlite:///{database_path}",
        connect_args={"check_same_thread": False, "timeout": 10},
    )
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text("PRAGMA journal_mode=WAL"))
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    start = datetime(2026, 7, 20, 0, 0, tzinfo=UTC)

    with session_factory() as db:
        campaign = _campaign_with_search(db)
        schedule = _daily_schedule(db, campaign, now=start)
        schedule_id = schedule.id
        due = schedule.next_occurrence_at.replace(tzinfo=UTC)

    barrier = Barrier(2)
    original = scheduling._unresolved_scheduled_execution

    def synchronized_unresolved(db, checked_schedule_id):
        result = original(db, checked_schedule_id)
        barrier.wait(timeout=5)
        return result

    monkeypatch.setattr(
        scheduling,
        "_unresolved_scheduled_execution",
        synchronized_unresolved,
    )

    def check_due():
        with session_factory() as db:
            return plan_due_schedule(db, schedule_id, now_utc=due)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [future.result() for future in [pool.submit(check_due), pool.submit(check_due)]]

    assert {result.outcome for result in results} == {
        DuePlanOutcome.PLANNED,
        DuePlanOutcome.ALREADY_CONSIDERED,
    }
    with session_factory() as db:
        assert db.scalar(select(func.count(CampaignScheduleOccurrence.id))) == 1
        assert db.scalar(select(func.count(CampaignExecution.id))) == 1
        assert db.scalar(select(func.count(CampaignExecutionChildSnapshot.id))) == 1


def test_unrelated_integrity_error_is_reraised_and_transaction_rolls_back(
    monkeypatch,
) -> None:
    session_factory = _session_factory()
    start = datetime(2026, 7, 20, 0, 0, tzinfo=UTC)

    with session_factory() as db:
        campaign = _campaign_with_search(db)
        schedule = _daily_schedule(db, campaign, now=start)
        due = schedule.next_occurrence_at.replace(tzinfo=UTC)

        def unrelated_failure(*_args, **_kwargs):
            raise IntegrityError(
                "INSERT campaign_executions",
                {},
                sqlite3.IntegrityError("FOREIGN KEY constraint failed"),
            )

        monkeypatch.setattr(
            scheduling,
            "create_campaign_execution_plan",
            unrelated_failure,
        )
        with pytest.raises(IntegrityError, match="FOREIGN KEY"):
            plan_due_schedule(db, schedule.id, now_utc=due)

        assert db.scalar(select(func.count(CampaignScheduleOccurrence.id))) == 0
        assert db.scalar(select(func.count(CampaignExecution.id))) == 0
        assert db.scalar(select(func.count(CampaignExecutionChildSnapshot.id))) == 0
