from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.campaigns import CampaignValidationError, create_campaign_execution_plan
from app.models import (
    Campaign,
    CampaignExecution,
    CampaignSchedule,
    CampaignScheduleOccurrence,
    RunStatus,
)

DEFAULT_TIMEZONE = "Australia/Sydney"
RECURRENCE_TYPES = {"daily", "weekly"}
UNRESOLVED_SCHEDULED_STATUSES = {
    RunStatus.PENDING,
    RunStatus.RUNNING,
    RunStatus.AWAITING_USER,
}
MAX_OCCURRENCES_TO_ADVANCE = 100_000


class ScheduleValidationError(ValueError):
    pass


class DuePlanOutcome(StrEnum):
    PLANNED = "planned"
    ALREADY_CONSIDERED = "already_considered"
    SKIPPED = "skipped"
    INVALID = "invalid"


@dataclass(frozen=True)
class ResolvedOccurrence:
    scheduled_for_at: datetime
    scheduled_local_date: date
    scheduled_local_time: time
    timezone_name: str
    utc_offset_minutes: int
    fold: int
    resolution: str


@dataclass(frozen=True)
class DuePlanResult:
    outcome: DuePlanOutcome
    schedule_id: int
    occurrence_id: int | None = None
    execution_id: int | None = None
    blocking_execution_id: int | None = None
    reason_code: str | None = None


def normalize_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _utc_for_storage(value: datetime) -> datetime:
    return normalize_utc(value)


def validate_timezone(timezone_name: str) -> ZoneInfo:
    normalized = timezone_name.strip()
    if not normalized:
        raise ScheduleValidationError("Timezone is required.")
    try:
        return ZoneInfo(normalized)
    except ZoneInfoNotFoundError as exc:
        raise ScheduleValidationError(f"Unknown IANA timezone '{normalized}'.") from exc


def _normalized_local_time(local_time: time) -> time:
    if local_time.tzinfo is not None:
        raise ScheduleValidationError("Schedule time must be a local wall-clock time.")
    if local_time.second or local_time.microsecond:
        raise ScheduleValidationError("Schedule time must use whole minutes.")
    return local_time.replace(second=0, microsecond=0)


def validate_schedule_values(
    *,
    recurrence_type: str,
    local_time: time,
    timezone_name: str,
    weekday_mask: int | None,
) -> tuple[str, time, str, int | None]:
    recurrence = recurrence_type.strip().lower()
    if recurrence not in RECURRENCE_TYPES:
        raise ScheduleValidationError("Recurrence must be daily or weekly.")
    normalized_time = _normalized_local_time(local_time)
    zone = validate_timezone(timezone_name)
    if recurrence == "daily":
        if weekday_mask is not None:
            raise ScheduleValidationError("Daily schedules cannot select weekdays.")
        normalized_mask = None
    else:
        if weekday_mask is None or weekday_mask < 1 or weekday_mask > 127:
            raise ScheduleValidationError(
                "Weekly schedules require at least one valid selected weekday."
            )
        normalized_mask = weekday_mask
    return recurrence, normalized_time, zone.key, normalized_mask


def resolve_local_occurrence(
    local_date: date,
    local_time: time,
    timezone_name: str,
) -> ResolvedOccurrence:
    normalized_time = _normalized_local_time(local_time)
    zone = validate_timezone(timezone_name)
    naive = datetime.combine(local_date, normalized_time)
    valid: list[tuple[int, datetime]] = []
    round_trips: list[datetime] = []
    for fold in (0, 1):
        candidate = naive.replace(tzinfo=zone, fold=fold)
        round_trip = candidate.astimezone(UTC).astimezone(zone)
        round_trips.append(round_trip)
        if round_trip.replace(tzinfo=None) == naive and round_trip.fold == fold:
            valid.append((fold, candidate))

    if valid:
        fold, aware = valid[0]
        resolution = "fold_first" if len(valid) == 2 else "exact"
    else:
        forward = [
            candidate
            for candidate in round_trips
            if candidate.replace(tzinfo=None) > naive
        ]
        if not forward:
            raise ScheduleValidationError(
                f"Could not resolve local occurrence {naive.isoformat()} in {zone.key}."
            )
        aware = min(forward, key=lambda candidate: candidate.astimezone(UTC))
        fold = aware.fold
        resolution = "gap_shifted"

    offset = aware.utcoffset()
    if offset is None:
        raise ScheduleValidationError("Timezone occurrence has no UTC offset.")
    return ResolvedOccurrence(
        scheduled_for_at=aware.astimezone(UTC),
        scheduled_local_date=local_date,
        scheduled_local_time=normalized_time,
        timezone_name=zone.key,
        utc_offset_minutes=int(offset.total_seconds() // 60),
        fold=fold,
        resolution=resolution,
    )


def _weekday_selected(schedule: CampaignSchedule, local_date: date) -> bool:
    if schedule.recurrence_type == "daily":
        return True
    return bool((schedule.weekday_mask or 0) & (1 << local_date.weekday()))


def next_occurrence_after(
    schedule: CampaignSchedule,
    after_utc: datetime,
) -> ResolvedOccurrence:
    after = normalize_utc(after_utc)
    zone = validate_timezone(schedule.timezone_name)
    starting_date = after.astimezone(zone).date()
    for offset in range(0, 370):
        candidate_date = starting_date + timedelta(days=offset)
        if not _weekday_selected(schedule, candidate_date):
            continue
        occurrence = resolve_local_occurrence(
            candidate_date,
            schedule.local_time,
            schedule.timezone_name,
        )
        if occurrence.scheduled_for_at > after:
            return occurrence
    raise ScheduleValidationError("Could not calculate the next schedule occurrence.")


def _occurrence_at_persisted_time(
    schedule: CampaignSchedule,
    scheduled_for_at: datetime,
) -> ResolvedOccurrence:
    scheduled = normalize_utc(scheduled_for_at)
    zone = validate_timezone(schedule.timezone_name)
    local_date = scheduled.astimezone(zone).date()
    occurrence = resolve_local_occurrence(local_date, schedule.local_time, schedule.timezone_name)
    if occurrence.scheduled_for_at != scheduled:
        raise ScheduleValidationError(
            "Persisted next occurrence does not match the current schedule definition."
        )
    return occurrence


def _latest_due_occurrence(
    schedule: CampaignSchedule,
    now_utc: datetime,
) -> tuple[ResolvedOccurrence | None, ResolvedOccurrence, int, datetime | None]:
    now = normalize_utc(now_utc)
    if schedule.next_occurrence_at is None:
        return None, next_occurrence_after(schedule, now), 0, None
    current = _occurrence_at_persisted_time(schedule, schedule.next_occurrence_at)
    if current.scheduled_for_at > now:
        return None, current, 0, None

    first_due = current.scheduled_for_at
    due_count = 1
    for _ in range(MAX_OCCURRENCES_TO_ADVANCE):
        following = next_occurrence_after(schedule, current.scheduled_for_at)
        if following.scheduled_for_at > now:
            return current, following, due_count - 1, first_due if due_count > 1 else None
        current = following
        due_count += 1
    raise ScheduleValidationError("Too many missed schedule occurrences to advance safely.")


def _require_schedulable_campaign(campaign: Campaign) -> None:
    if campaign.is_archived:
        raise ScheduleValidationError("Archived campaigns cannot have an enabled schedule.")
    if not campaign.is_active:
        raise ScheduleValidationError("Inactive campaigns cannot have an enabled schedule.")


def create_campaign_schedule(
    db: Session,
    campaign: Campaign,
    *,
    recurrence_type: str,
    local_time: time,
    timezone_name: str = DEFAULT_TIMEZONE,
    weekday_mask: int | None = None,
    is_enabled: bool = True,
    now_utc: datetime | None = None,
) -> CampaignSchedule:
    if campaign.schedule is not None:
        raise ScheduleValidationError("Campaign already has a schedule.")
    recurrence, wall_time, zone_name, mask = validate_schedule_values(
        recurrence_type=recurrence_type,
        local_time=local_time,
        timezone_name=timezone_name,
        weekday_mask=weekday_mask,
    )
    _require_schedulable_campaign(campaign)
    now = normalize_utc(now_utc or datetime.now(UTC))
    schedule = CampaignSchedule(
        campaign_id=campaign.id,
        recurrence_type=recurrence,
        local_time=wall_time,
        timezone_name=zone_name,
        weekday_mask=mask,
        is_enabled=is_enabled,
        created_at=now,
        updated_at=now,
    )
    db.add(schedule)
    db.flush()
    if is_enabled:
        schedule.next_occurrence_at = next_occurrence_after(schedule, now).scheduled_for_at
    db.commit()
    db.refresh(schedule)
    return schedule


def update_campaign_schedule(
    db: Session,
    schedule: CampaignSchedule,
    *,
    recurrence_type: str,
    local_time: time,
    timezone_name: str,
    weekday_mask: int | None,
    is_enabled: bool,
    now_utc: datetime | None = None,
) -> CampaignSchedule:
    now = normalize_utc(now_utc or datetime.now(UTC))
    if schedule.is_enabled:
        plan_due_schedule(db, schedule.id, now_utc=now)
        schedule = db.get(CampaignSchedule, schedule.id)
    recurrence, wall_time, zone_name, mask = validate_schedule_values(
        recurrence_type=recurrence_type,
        local_time=local_time,
        timezone_name=timezone_name,
        weekday_mask=weekday_mask,
    )
    if is_enabled:
        _require_schedulable_campaign(schedule.campaign)
    schedule.recurrence_type = recurrence
    schedule.local_time = wall_time
    schedule.timezone_name = zone_name
    schedule.weekday_mask = mask
    schedule.is_enabled = is_enabled
    schedule.next_occurrence_at = (
        next_occurrence_after(schedule, now).scheduled_for_at if is_enabled else None
    )
    schedule.updated_at = now
    db.commit()
    db.refresh(schedule)
    return schedule


def disable_campaign_schedule(
    db: Session,
    schedule: CampaignSchedule,
    *,
    now_utc: datetime | None = None,
) -> CampaignSchedule:
    now = normalize_utc(now_utc or datetime.now(UTC))
    if schedule.is_enabled:
        plan_due_schedule(db, schedule.id, now_utc=now)
        schedule = db.get(CampaignSchedule, schedule.id)
    schedule.is_enabled = False
    schedule.next_occurrence_at = None
    schedule.updated_at = now
    db.commit()
    db.refresh(schedule)
    return schedule


def enable_campaign_schedule(
    db: Session,
    schedule: CampaignSchedule,
    *,
    now_utc: datetime | None = None,
) -> CampaignSchedule:
    _require_schedulable_campaign(schedule.campaign)
    now = normalize_utc(now_utc or datetime.now(UTC))
    schedule.is_enabled = True
    schedule.next_occurrence_at = next_occurrence_after(schedule, now).scheduled_for_at
    schedule.updated_at = now
    db.commit()
    db.refresh(schedule)
    return schedule


def _unresolved_scheduled_execution(
    db: Session,
    schedule_id: int,
) -> CampaignExecution | None:
    return db.scalar(
        select(CampaignExecution)
        .where(
            CampaignExecution.origin == "scheduled",
            CampaignExecution.schedule_id == schedule_id,
            CampaignExecution.status.in_(UNRESOLVED_SCHEDULED_STATUSES),
        )
        .order_by(
            CampaignExecution.scheduled_for_at,
            CampaignExecution.id,
        )
    )


def _expected_idempotency_violation(exc: IntegrityError) -> bool:
    message = str(exc.orig).lower()
    expected_fragments = (
        "campaign_schedule_occurrences.schedule_id, "
        "campaign_schedule_occurrences.scheduled_for_at",
        "campaign_executions.schedule_occurrence_id",
        "uq_campaign_schedule_occurrence",
        "ux_campaign_executions_schedule_occurrence",
    )
    return any(fragment in message for fragment in expected_fragments)


def _result_for_existing_occurrence(
    db: Session,
    schedule_id: int,
    scheduled_for_at: datetime,
) -> DuePlanResult:
    occurrence = db.scalar(
        select(CampaignScheduleOccurrence).where(
            CampaignScheduleOccurrence.schedule_id == schedule_id,
            CampaignScheduleOccurrence.scheduled_for_at == _utc_for_storage(scheduled_for_at),
        )
    )
    if occurrence is None:
        raise RuntimeError("Expected concurrent occurrence winner was not found.")
    execution = db.scalar(
        select(CampaignExecution).where(
            CampaignExecution.schedule_occurrence_id == occurrence.id
        )
    )
    return DuePlanResult(
        outcome=DuePlanOutcome.ALREADY_CONSIDERED,
        schedule_id=schedule_id,
        occurrence_id=occurrence.id,
        execution_id=execution.id if execution else None,
        blocking_execution_id=occurrence.blocking_execution_id,
        reason_code=occurrence.reason_code,
    )


def plan_due_schedule(
    db: Session,
    schedule_id: int,
    *,
    now_utc: datetime | None = None,
) -> DuePlanResult:
    if db.new or db.dirty or db.deleted:
        raise ScheduleValidationError(
            "Due planning requires a session with no uncommitted changes."
        )
    if db.in_transaction():
        # End an implicit read-only SQLAlchemy transaction so the complete due
        # decision can begin from a fresh database read and commit atomically.
        db.rollback()
    now = normalize_utc(now_utc or datetime.now(UTC))
    due: ResolvedOccurrence | None = None
    try:
        with db.begin():
            schedule = db.get(CampaignSchedule, schedule_id)
            if schedule is None:
                raise ScheduleValidationError(f"Campaign schedule {schedule_id} does not exist.")
            if not schedule.is_enabled:
                return DuePlanResult(
                    outcome=DuePlanOutcome.ALREADY_CONSIDERED,
                    schedule_id=schedule.id,
                    reason_code="schedule_disabled",
                )

            due, following, superseded_count, superseded_from = _latest_due_occurrence(
                schedule, now
            )
            if due is None:
                if (
                    schedule.last_occurrence_considered_at is not None
                    and normalize_utc(schedule.last_occurrence_considered_at) <= now
                ):
                    return _result_for_existing_occurrence(
                        db,
                        schedule.id,
                        schedule.last_occurrence_considered_at,
                    )
                return DuePlanResult(
                    outcome=DuePlanOutcome.ALREADY_CONSIDERED,
                    schedule_id=schedule.id,
                    reason_code="not_due",
                )

            blocking = _unresolved_scheduled_execution(db, schedule.id)
            campaign = db.get(Campaign, schedule.campaign_id)
            disposition = DuePlanOutcome.PLANNED
            reason_code = None
            message = "Scheduled occurrence planned as a frozen campaign execution."
            if blocking is not None:
                disposition = DuePlanOutcome.SKIPPED
                reason_code = "previous_scheduled_execution_unresolved"
                message = (
                    f"Skipped because scheduled execution {blocking.id} is still unresolved."
                )
            elif campaign is None:
                disposition = DuePlanOutcome.INVALID
                reason_code = "campaign_missing"
                message = "Scheduled occurrence is invalid because the campaign is missing."
            elif campaign.is_archived:
                disposition = DuePlanOutcome.SKIPPED
                reason_code = "campaign_archived"
                message = "Scheduled occurrence skipped because the campaign is archived."
            elif not campaign.is_active:
                disposition = DuePlanOutcome.SKIPPED
                reason_code = "campaign_inactive"
                message = "Scheduled occurrence skipped because the campaign is inactive."

            occurrence = CampaignScheduleOccurrence(
                schedule_id=schedule.id,
                campaign_id=schedule.campaign_id,
                scheduled_for_at=due.scheduled_for_at,
                scheduled_local_date=due.scheduled_local_date,
                scheduled_local_time=due.scheduled_local_time,
                timezone_name=due.timezone_name,
                utc_offset_minutes=due.utc_offset_minutes,
                fold=due.fold,
                resolution=due.resolution,
                disposition=disposition.value,
                reason_code=reason_code,
                message=message,
                superseded_count=superseded_count,
                superseded_from_at=superseded_from,
                blocking_execution_id=blocking.id if blocking else None,
                considered_at=now,
            )
            db.add(occurrence)
            db.flush()

            execution = None
            if disposition == DuePlanOutcome.PLANNED:
                try:
                    execution = create_campaign_execution_plan(
                        db,
                        campaign,
                        commit=False,
                        origin="scheduled",
                        schedule_id=schedule.id,
                        schedule_occurrence_id=occurrence.id,
                        scheduled_for_at=due.scheduled_for_at,
                    )
                except CampaignValidationError as exc:
                    occurrence.disposition = DuePlanOutcome.INVALID.value
                    occurrence.reason_code = (
                        "no_runnable_searches"
                        if "no runnable saved searches" in str(exc).lower()
                        else "plan_validation_failed"
                    )
                    occurrence.message = str(exc)
                    disposition = DuePlanOutcome.INVALID

            schedule.last_occurrence_considered_at = due.scheduled_for_at
            schedule.next_occurrence_at = following.scheduled_for_at
            schedule.updated_at = now
            db.flush()
            result = DuePlanResult(
                outcome=disposition,
                schedule_id=schedule.id,
                occurrence_id=occurrence.id,
                execution_id=execution.id if execution else None,
                blocking_execution_id=blocking.id if blocking else None,
                reason_code=occurrence.reason_code,
            )
        return result
    except IntegrityError as exc:
        db.rollback()
        if not _expected_idempotency_violation(exc) or due is None:
            raise
        return _result_for_existing_occurrence(db, schedule_id, due.scheduled_for_at)


def plan_all_due_schedules(
    db: Session,
    *,
    now_utc: datetime | None = None,
) -> list[DuePlanResult]:
    now = normalize_utc(now_utc or datetime.now(UTC))
    schedule_ids = db.scalars(
        select(CampaignSchedule.id)
        .where(
            CampaignSchedule.is_enabled.is_(True),
            CampaignSchedule.next_occurrence_at <= now,
        )
        .order_by(CampaignSchedule.next_occurrence_at, CampaignSchedule.id)
    ).all()
    db.rollback()
    return [
        plan_due_schedule(db, schedule_id, now_utc=now)
        for schedule_id in schedule_ids
    ]
