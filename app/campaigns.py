from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import distinct, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.collectors.base import UnsupportedSourceError
from app.collectors.registry import get_source_registration
from app.collectors.seek import DATE_LISTED_TO_DAYS
from app.models import (
    Campaign,
    CampaignExecution,
    CampaignExecutionChildSnapshot,
    CampaignExecutionEvent,
    CampaignSavedSearch,
    JobDiscovery,
    RunStatus,
    SavedSearch,
    SearchRun,
)
from app.repository import create_search_and_run

DATE_WINDOW_OPTIONS = [
    ("previous_24_hours", "Previous 24 hours"),
    ("last_2_days", "Last 2 days"),
    ("last_3_days", "Last 3 days"),
    ("last_7_days", "Last 7 days"),
    ("last_14_days", "Last 14 days"),
    ("last_30_days", "Last 30 days"),
]
DATE_WINDOW_LABELS = dict(DATE_WINDOW_OPTIONS)
SUPPORTED_DATE_WINDOWS = {value for value, _label in DATE_WINDOW_OPTIONS}
MAX_QUERY_LENGTH = 500
MAX_NAME_LENGTH = 160
MAX_LOCATION_LENGTH = 255
MAX_PAGES = 10
MAX_SEARCHES_PER_CAMPAIGN = 30
TERMINAL_RUN_STATUSES = {
    RunStatus.COMPLETED,
    RunStatus.COMPLETED_WITH_ERRORS,
    RunStatus.FAILED,
    RunStatus.BLOCKED,
    RunStatus.INTERRUPTED,
}
SAFE_CAMPAIGN_EVENT_METADATA_KEYS = {
    "status",
    "source",
    "child_run_id",
    "child_snapshot_id",
    "exception_type",
    "stop_reason",
}


class CampaignValidationError(ValueError):
    pass


def _safe_campaign_metadata(metadata: dict[str, Any] | None) -> dict[str, Any] | None:
    if not metadata:
        return None
    safe: dict[str, Any] = {}
    for key, value in metadata.items():
        if key not in SAFE_CAMPAIGN_EVENT_METADATA_KEYS:
            continue
        if value is None or isinstance(value, str | int | float | bool):
            safe[key] = value
    return safe or None


def record_campaign_event(
    db: Session,
    execution_id: int,
    *,
    severity: str,
    code: str,
    phase: str,
    message: str,
    child_snapshot_id: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> CampaignExecutionEvent:
    event = CampaignExecutionEvent(
        campaign_execution_id=execution_id,
        child_snapshot_id=child_snapshot_id,
        severity=severity,
        code=code,
        phase=phase,
        message=message,
        metadata_json=_safe_campaign_metadata(metadata),
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


@dataclass(frozen=True)
class CampaignPlanItem:
    membership: CampaignSavedSearch
    saved_search: SavedSearch
    position: int


@dataclass(frozen=True)
class CampaignPlan:
    campaign: Campaign
    items: list[CampaignPlanItem]
    errors: list[str]
    warnings: list[str]

    @property
    def planned_child_count(self) -> int:
        return len(self.items)

    @property
    def planned_pages(self) -> int:
        return sum(item.saved_search.max_pages for item in self.items)

    @property
    def can_create_execution(self) -> bool:
        return not self.errors and bool(self.items)


def date_window_label(value: str | None) -> str:
    if value in DATE_WINDOW_LABELS:
        return DATE_WINDOW_LABELS[value]
    if value == "today" or value == "last_1_day":
        return "Previous 24 hours"
    return (value or "").replace("_", " ")


def _touch(entity) -> None:
    entity.updated_at = datetime.now(UTC)


def _validate_source(source: str) -> str:
    source = source.strip().lower()
    try:
        registration = get_source_registration(source)
    except UnsupportedSourceError as exc:
        raise CampaignValidationError(str(exc)) from exc
    if not registration.enabled or not registration.supported:
        raise CampaignValidationError(
            f"Source '{source}' is not supported for campaign collection in this milestone."
        )
    return registration.source_identifier.value


def _validate_saved_search_values(
    *,
    name: str,
    source: str,
    query_text: str,
    location: str,
    date_window: str,
    max_pages: int,
) -> str:
    if not name.strip():
        raise CampaignValidationError("Saved search name is required.")
    if len(name.strip()) > MAX_NAME_LENGTH:
        raise CampaignValidationError("Saved search name is too long.")
    if not query_text:
        raise CampaignValidationError("Saved search query is required.")
    if len(query_text) > MAX_QUERY_LENGTH:
        raise CampaignValidationError("Saved search query is too long.")
    if not location.strip():
        raise CampaignValidationError("Location is required.")
    if len(location.strip()) > MAX_LOCATION_LENGTH:
        raise CampaignValidationError("Location is too long.")
    if date_window not in SUPPORTED_DATE_WINDOWS or date_window not in DATE_LISTED_TO_DAYS:
        raise CampaignValidationError("Unsupported date window.")
    if max_pages < 1 or max_pages > MAX_PAGES:
        raise CampaignValidationError(f"Maximum pages must be between 1 and {MAX_PAGES}.")
    return _validate_source(source)


def create_saved_search(
    db: Session,
    *,
    name: str,
    source: str,
    query_text: str,
    location: str,
    date_window: str,
    max_pages: int,
    is_enabled: bool = True,
) -> SavedSearch:
    normalized_source = _validate_saved_search_values(
        name=name,
        source=source,
        query_text=query_text,
        location=location,
        date_window=date_window,
        max_pages=max_pages,
    )
    saved_search = SavedSearch(
        name=name.strip(),
        source=normalized_source,
        query_text=query_text,
        location=location.strip(),
        date_window=date_window,
        max_pages=max_pages,
        is_enabled=is_enabled,
        is_archived=False,
    )
    db.add(saved_search)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise CampaignValidationError("Saved search name must be unique.") from exc
    db.refresh(saved_search)
    return saved_search


def update_saved_search(
    db: Session,
    saved_search: SavedSearch,
    *,
    name: str,
    source: str,
    query_text: str,
    location: str,
    date_window: str,
    max_pages: int,
    is_enabled: bool,
    is_archived: bool,
) -> SavedSearch:
    normalized_source = _validate_saved_search_values(
        name=name,
        source=source,
        query_text=query_text,
        location=location,
        date_window=date_window,
        max_pages=max_pages,
    )
    saved_search.name = name.strip()
    saved_search.source = normalized_source
    saved_search.query_text = query_text
    saved_search.location = location.strip()
    saved_search.date_window = date_window
    saved_search.max_pages = max_pages
    saved_search.is_enabled = is_enabled
    saved_search.is_archived = is_archived
    _touch(saved_search)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise CampaignValidationError("Saved search name must be unique.") from exc
    db.refresh(saved_search)
    return saved_search


def create_campaign(db: Session, *, name: str, description: str | None = None) -> Campaign:
    if not name.strip():
        raise CampaignValidationError("Campaign name is required.")
    if len(name.strip()) > MAX_NAME_LENGTH:
        raise CampaignValidationError("Campaign name is too long.")
    campaign = Campaign(name=name.strip(), description=(description or "").strip() or None)
    db.add(campaign)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise CampaignValidationError("Campaign name must be unique.") from exc
    db.refresh(campaign)
    return campaign


def update_campaign(
    db: Session,
    campaign: Campaign,
    *,
    name: str,
    description: str | None,
    is_active: bool,
) -> Campaign:
    if not name.strip():
        raise CampaignValidationError("Campaign name is required.")
    if len(name.strip()) > MAX_NAME_LENGTH:
        raise CampaignValidationError("Campaign name is too long.")
    campaign.name = name.strip()
    campaign.description = (description or "").strip() or None
    campaign.is_active = is_active
    _touch(campaign)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise CampaignValidationError("Campaign name must be unique.") from exc
    db.refresh(campaign)
    return campaign


def archive_campaign(db: Session, campaign: Campaign) -> Campaign:
    campaign.is_archived = True
    campaign.is_active = False
    _touch(campaign)
    db.commit()
    db.refresh(campaign)
    return campaign


def _next_position(db: Session, campaign_id: int) -> int:
    highest = db.scalar(
        select(func.max(CampaignSavedSearch.position)).where(
            CampaignSavedSearch.campaign_id == campaign_id
        )
    )
    return (highest or 0) + 1


def add_campaign_membership(
    db: Session,
    campaign: Campaign,
    saved_search: SavedSearch,
    *,
    position: int | None = None,
    is_enabled: bool = True,
) -> CampaignSavedSearch:
    if campaign.is_archived:
        raise CampaignValidationError("Archived campaigns cannot be edited.")
    if saved_search.is_archived:
        raise CampaignValidationError("Archived saved searches cannot be added.")
    count = db.scalar(
        select(func.count(CampaignSavedSearch.id)).where(
            CampaignSavedSearch.campaign_id == campaign.id
        )
    )
    if count >= MAX_SEARCHES_PER_CAMPAIGN:
        raise CampaignValidationError("Campaign has too many saved searches.")
    membership = CampaignSavedSearch(
        campaign_id=campaign.id,
        saved_search_id=saved_search.id,
        position=position or _next_position(db, campaign.id),
        is_enabled=is_enabled,
    )
    db.add(membership)
    _touch(campaign)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise CampaignValidationError(
            "Saved search is already in this campaign or position is already used."
        ) from exc
    db.refresh(membership)
    return membership


def update_membership(
    db: Session,
    membership: CampaignSavedSearch,
    *,
    position: int,
    is_enabled: bool,
) -> CampaignSavedSearch:
    if position < 1:
        raise CampaignValidationError("Execution order must be at least 1.")
    membership.position = position
    membership.is_enabled = is_enabled
    _touch(membership)
    _touch(membership.campaign)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise CampaignValidationError("Execution order positions must be unique.") from exc
    db.refresh(membership)
    return membership


def remove_membership(db: Session, membership: CampaignSavedSearch) -> None:
    campaign = membership.campaign
    db.delete(membership)
    _touch(campaign)
    db.commit()


def build_campaign_plan(db: Session, campaign: Campaign) -> CampaignPlan:
    campaign = db.scalar(
        select(Campaign)
        .where(Campaign.id == campaign.id)
        .options(
            selectinload(Campaign.memberships).selectinload(CampaignSavedSearch.saved_search)
        )
    ) or campaign
    errors: list[str] = []
    warnings = [
        "Campaign collection is planned only; Milestone 6B does not start collectors.",
        "Visible browser preparation may be required for SEEK source sessions.",
        "Job-board result pages are non-exhaustive and can change between runs.",
    ]
    if campaign.is_archived:
        errors.append("Archived campaigns cannot be planned.")
    memberships = sorted(campaign.memberships, key=lambda item: item.position)
    enabled_memberships = [item for item in memberships if item.is_enabled]
    items: list[CampaignPlanItem] = []
    seen_positions: set[int] = set()
    for membership in enabled_memberships:
        if membership.position in seen_positions:
            errors.append(f"Duplicate execution position {membership.position}.")
        seen_positions.add(membership.position)
        saved_search = membership.saved_search
        if saved_search is None:
            errors.append(f"Membership {membership.id} is missing its saved search.")
            continue
        if saved_search.is_archived or not saved_search.is_enabled:
            continue
        try:
            _validate_saved_search_values(
                name=saved_search.name,
                source=saved_search.source,
                query_text=saved_search.query_text,
                location=saved_search.location,
                date_window=saved_search.date_window,
                max_pages=saved_search.max_pages,
            )
        except CampaignValidationError as exc:
            errors.append(f"{saved_search.name}: {exc}")
            continue
        items.append(
            CampaignPlanItem(
                membership=membership,
                saved_search=saved_search,
                position=membership.position,
            )
        )
    if not items:
        errors.append("Campaign has no runnable saved searches.")
    if len(items) > MAX_SEARCHES_PER_CAMPAIGN:
        errors.append("Campaign has too many runnable saved searches.")
    return CampaignPlan(campaign=campaign, items=items, errors=errors, warnings=warnings)


def create_campaign_execution_plan(db: Session, campaign: Campaign) -> CampaignExecution:
    plan = build_campaign_plan(db, campaign)
    if plan.errors:
        raise CampaignValidationError(" ".join(plan.errors))
    execution = CampaignExecution(
        campaign_id=campaign.id,
        campaign_name_snapshot=campaign.name,
        status=RunStatus.PENDING,
        message="Execution plan created. Campaign collection is not enabled until Milestone 6C.",
        planned_child_count=plan.planned_child_count,
        pages_planned=plan.planned_pages,
    )
    db.add(execution)
    db.flush()
    for item in plan.items:
        saved_search = item.saved_search
        db.add(
            CampaignExecutionChildSnapshot(
                campaign_execution_id=execution.id,
                position=item.position,
                source=saved_search.source,
                saved_search_id=saved_search.id,
                saved_search_name_snapshot=saved_search.name,
                query_text_snapshot=saved_search.query_text,
                location_snapshot=saved_search.location,
                date_window_snapshot=saved_search.date_window,
                page_limit_snapshot=saved_search.max_pages,
                status=RunStatus.PENDING,
                pages_planned=saved_search.max_pages,
            )
        )
    db.commit()
    db.refresh(execution)
    return execution


def ordered_child_snapshots(
    db: Session, execution_id: int
) -> list[CampaignExecutionChildSnapshot]:
    return db.scalars(
        select(CampaignExecutionChildSnapshot)
        .where(CampaignExecutionChildSnapshot.campaign_execution_id == execution_id)
        .order_by(CampaignExecutionChildSnapshot.position)
    ).all()


def create_child_run_for_snapshot(
    db: Session, snapshot: CampaignExecutionChildSnapshot
) -> SearchRun:
    run = create_search_and_run(
        db,
        snapshot.query_text_snapshot,
        snapshot.location_snapshot,
        snapshot.date_window_snapshot,
        snapshot.page_limit_snapshot,
        source_identifier=snapshot.source,
    )
    snapshot.child_run_id = run.id
    snapshot.status = RunStatus.PENDING
    snapshot.pages_planned = snapshot.page_limit_snapshot
    db.commit()
    db.refresh(snapshot)
    return run


def sync_child_snapshot_from_run(
    db: Session,
    snapshot: CampaignExecutionChildSnapshot,
    run: SearchRun,
) -> None:
    snapshot.status = run.status
    snapshot.stop_reason = run.stop_reason
    snapshot.pages_planned = run.pages_requested
    snapshot.pages_completed = run.pages_completed or 0
    snapshot.result_cards_observed = run.result_cards_observed or 0
    snapshot.unique_jobs = run.unique_jobs_in_run or 0
    snapshot.new_jobs = run.new_jobs_added or 0
    snapshot.rediscoveries = run.known_jobs_rediscovered or 0
    snapshot.updated_jobs = run.jobs_updated or 0
    snapshot.duplicate_cards = run.duplicate_cards_ignored or 0
    snapshot.error_count = run.error_count or 0
    db.commit()


def refresh_campaign_execution_aggregates(db: Session, execution_id: int) -> CampaignExecution:
    execution = db.get(CampaignExecution, execution_id)
    if execution is None:
        raise CampaignValidationError(f"Campaign execution {execution_id} does not exist.")
    children = ordered_child_snapshots(db, execution_id)
    execution.planned_child_count = len(children)
    execution.attempted_child_count = sum(1 for child in children if child.child_run_id)
    execution.completed_child_count = sum(
        1
        for child in children
        if child.status in {RunStatus.COMPLETED, RunStatus.COMPLETED_WITH_ERRORS}
    )
    execution.failed_child_count = sum(
        1 for child in children if child.status in {RunStatus.FAILED, RunStatus.BLOCKED}
    )
    execution.awaiting_user_child_count = sum(
        1 for child in children if child.status == RunStatus.AWAITING_USER
    )
    execution.pages_planned = sum(child.pages_planned for child in children)
    execution.pages_completed = sum(child.pages_completed for child in children)
    execution.result_cards_observed = sum(child.result_cards_observed for child in children)
    child_run_ids = [child.child_run_id for child in children if child.child_run_id]
    if child_run_ids:
        execution.unique_jobs = (
            db.scalar(
                select(func.count(distinct(JobDiscovery.job_id))).where(
                    JobDiscovery.run_id.in_(child_run_ids)
                )
            )
            or 0
        )
    else:
        execution.unique_jobs = 0
    execution.new_jobs = sum(child.new_jobs for child in children)
    execution.rediscoveries = sum(child.rediscoveries for child in children)
    execution.updated_jobs = sum(child.updated_jobs for child in children)
    execution.duplicate_cards = sum(child.duplicate_cards for child in children)
    execution.error_count = sum(child.error_count for child in children)
    active_child = next(
        (
            child
            for child in children
            if child.status in {RunStatus.RUNNING, RunStatus.AWAITING_USER}
        ),
        None,
    )
    next_child = next((child for child in children if child.status == RunStatus.PENDING), None)
    current_child = active_child or next_child
    execution.current_child_id = current_child.id if current_child else None
    db.commit()
    db.refresh(execution)
    return execution


def campaign_execution_has_child_errors(db: Session, execution_id: int) -> bool:
    return any(
        child.status in {RunStatus.COMPLETED_WITH_ERRORS, RunStatus.FAILED, RunStatus.BLOCKED}
        or child.error_count > 0
        for child in ordered_child_snapshots(db, execution_id)
    )


def reconcile_running_campaign_executions(db: Session) -> int:
    interrupted_count = 0
    running_executions = db.scalars(
        select(CampaignExecution).where(CampaignExecution.status == RunStatus.RUNNING)
    ).all()
    for execution in running_executions:
        execution.status = RunStatus.INTERRUPTED
        execution.stop_reason = "server_restarted"
        execution.message = (
            "Campaign execution was marked interrupted during startup because no live "
            "worker exists after server restart."
        )
        execution.finished_at = datetime.now(UTC)
        running_children = db.scalars(
            select(CampaignExecutionChildSnapshot).where(
                CampaignExecutionChildSnapshot.campaign_execution_id == execution.id,
                CampaignExecutionChildSnapshot.status == RunStatus.RUNNING,
            )
        ).all()
        for child in running_children:
            child.status = RunStatus.INTERRUPTED
            child.stop_reason = "server_restarted"
        db.commit()
        record_campaign_event(
            db,
            execution.id,
            severity="warning",
            code="interrupted",
            phase="startup",
            message="Campaign execution interrupted during startup reconciliation.",
            metadata={"stop_reason": "server_restarted"},
        )
        refresh_campaign_execution_aggregates(db, execution.id)
        interrupted_count += 1
    return interrupted_count
