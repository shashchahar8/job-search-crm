from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.collectors.base import UnsupportedSourceError
from app.collectors.registry import get_source_registration
from app.collectors.seek import DATE_LISTED_TO_DAYS
from app.models import (
    Campaign,
    CampaignExecution,
    CampaignExecutionChildSnapshot,
    CampaignSavedSearch,
    RunStatus,
    SavedSearch,
)

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


class CampaignValidationError(ValueError):
    pass


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
