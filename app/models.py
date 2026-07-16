from __future__ import annotations

from datetime import UTC, date, datetime
from enum import StrEnum

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    COMPLETED_WITH_ERRORS = "completed_with_errors"
    AWAITING_USER = "awaiting_user"
    INTERRUPTED = "interrupted"
    BLOCKED = "blocked"
    FAILED = "failed"


class CRMStatus(StrEnum):
    NEW = "new"
    REVIEWING = "reviewing"
    SHORTLISTED = "shortlisted"
    PREPARING = "preparing"
    APPLIED = "applied"
    INTERVIEW = "interview"
    OFFER = "offer"
    REJECTED = "rejected"
    EXCLUDED = "excluded"
    ARCHIVED = "archived"


class JobPriority(StrEnum):
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class RuleOutcome(StrEnum):
    STRONG_MATCH = "strong_match"
    REVIEW = "review"
    WEAK_MATCH = "weak_match"
    EXCLUDE = "exclude"


def utc_now() -> datetime:
    return datetime.now(UTC)


class Search(Base):
    __tablename__ = "searches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(50), default="seek", nullable=False)
    keywords: Mapped[str] = mapped_column(String(255), nullable=False)
    location: Mapped[str] = mapped_column(String(255), nullable=False)
    date_listed: Mapped[str] = mapped_column(String(50), nullable=False)
    max_pages: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    runs: Mapped[list[SearchRun]] = relationship(back_populates="search")


class SearchRun(Base):
    __tablename__ = "search_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    search_id: Mapped[int] = mapped_column(ForeignKey("searches.id"), nullable=False)
    source: Mapped[str] = mapped_column(String(50), default="seek", nullable=False)
    status: Mapped[RunStatus] = mapped_column(
        Enum(RunStatus, values_callable=lambda enum_cls: [item.value for item in enum_cls]),
        default=RunStatus.PENDING,
        nullable=False,
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pages_requested: Mapped[int] = mapped_column(Integer, nullable=False)
    pages_attempted: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    jobs_found: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    result_cards_observed: Mapped[int | None] = mapped_column(Integer)
    unique_jobs_in_run: Mapped[int | None] = mapped_column(Integer)
    new_jobs_added: Mapped[int | None] = mapped_column(Integer)
    known_jobs_rediscovered: Mapped[int | None] = mapped_column(Integer)
    jobs_updated: Mapped[int | None] = mapped_column(Integer)
    duplicate_cards_ignored: Mapped[int | None] = mapped_column(Integer)
    pages_completed: Mapped[int | None] = mapped_column(Integer)
    stop_reason: Mapped[str | None] = mapped_column(String(80))
    message: Mapped[str | None] = mapped_column(Text)
    last_url: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    search: Mapped[Search] = relationship(back_populates="runs")
    discoveries: Mapped[list[JobDiscovery]] = relationship(back_populates="run")
    events: Mapped[list[RunEvent]] = relationship(back_populates="run")


class SavedSearch(Base):
    __tablename__ = "saved_searches"
    __table_args__ = (UniqueConstraint("name", name="uq_saved_searches_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    query_text: Mapped[str] = mapped_column(String(500), nullable=False)
    location: Mapped[str] = mapped_column(String(255), nullable=False)
    date_window: Mapped[str] = mapped_column(String(50), nullable=False)
    max_pages: Mapped[int] = mapped_column(Integer, nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    memberships: Mapped[list[CampaignSavedSearch]] = relationship(back_populates="saved_search")


class Campaign(Base):
    __tablename__ = "campaigns"
    __table_args__ = (UniqueConstraint("name", name="uq_campaigns_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    memberships: Mapped[list[CampaignSavedSearch]] = relationship(back_populates="campaign")
    executions: Mapped[list[CampaignExecution]] = relationship(back_populates="campaign")


class CampaignSavedSearch(Base):
    __tablename__ = "campaign_saved_searches"
    __table_args__ = (
        UniqueConstraint("campaign_id", "saved_search_id", name="uq_campaign_saved_search"),
        UniqueConstraint("campaign_id", "position", name="uq_campaign_saved_search_position"),
        Index("ix_campaign_saved_searches_order", "campaign_id", "position"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"), nullable=False)
    saved_search_id: Mapped[int] = mapped_column(ForeignKey("saved_searches.id"), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    campaign: Mapped[Campaign] = relationship(back_populates="memberships")
    saved_search: Mapped[SavedSearch] = relationship(back_populates="memberships")


class CampaignExecution(Base):
    __tablename__ = "campaign_executions"
    __table_args__ = (
        Index("ix_campaign_executions_campaign_created", "campaign_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"), nullable=False)
    campaign_name_snapshot: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[RunStatus] = mapped_column(
        Enum(RunStatus, values_callable=lambda enum_cls: [item.value for item in enum_cls]),
        default=RunStatus.PENDING,
        nullable=False,
    )
    stop_reason: Mapped[str | None] = mapped_column(String(80))
    message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    planned_child_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    attempted_child_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completed_child_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed_child_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    awaiting_user_child_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    pages_planned: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    pages_completed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    result_cards_observed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    unique_jobs: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    new_jobs: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rediscoveries: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated_jobs: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    duplicate_cards: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    current_child_id: Mapped[int | None] = mapped_column(Integer)

    campaign: Mapped[Campaign] = relationship(back_populates="executions")
    child_snapshots: Mapped[list[CampaignExecutionChildSnapshot]] = relationship(
        back_populates="execution"
    )


class CampaignExecutionChildSnapshot(Base):
    __tablename__ = "campaign_execution_child_snapshots"
    __table_args__ = (
        UniqueConstraint("campaign_execution_id", "position", name="uq_campaign_child_position"),
        Index("ix_campaign_child_snapshots_execution_order", "campaign_execution_id", "position"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_execution_id: Mapped[int] = mapped_column(
        ForeignKey("campaign_executions.id"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    saved_search_id: Mapped[int] = mapped_column(ForeignKey("saved_searches.id"), nullable=False)
    saved_search_name_snapshot: Mapped[str] = mapped_column(String(160), nullable=False)
    query_text_snapshot: Mapped[str] = mapped_column(String(500), nullable=False)
    location_snapshot: Mapped[str] = mapped_column(String(255), nullable=False)
    date_window_snapshot: Mapped[str] = mapped_column(String(50), nullable=False)
    page_limit_snapshot: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[RunStatus] = mapped_column(
        Enum(RunStatus, values_callable=lambda enum_cls: [item.value for item in enum_cls]),
        default=RunStatus.PENDING,
        nullable=False,
    )
    child_run_id: Mapped[int | None] = mapped_column(ForeignKey("search_runs.id"))
    stop_reason: Mapped[str | None] = mapped_column(String(80))
    pages_planned: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    pages_completed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    result_cards_observed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    unique_jobs: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    new_jobs: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rediscoveries: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated_jobs: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    duplicate_cards: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    execution: Mapped[CampaignExecution] = relationship(back_populates="child_snapshots")


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    seek_job_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    fallback_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    company: Mapped[str | None] = mapped_column(String(255))
    location: Mapped[str | None] = mapped_column(String(255))
    salary: Mapped[str | None] = mapped_column(String(255))
    work_type: Mapped[str | None] = mapped_column(String(255))
    posting_date: Mapped[str | None] = mapped_column(String(255))
    source: Mapped[str] = mapped_column(String(50), default="seek", nullable=False)
    source_listing_url: Mapped[str | None] = mapped_column(Text)
    canonical_url: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    crm_status: Mapped[str] = mapped_column(String(32), default=CRMStatus.NEW.value, nullable=False)
    priority: Mapped[str] = mapped_column(
        String(16), default=JobPriority.NONE.value, nullable=False
    )
    is_favorite: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    application_deadline: Mapped[date | None] = mapped_column(Date)
    follow_up_date: Mapped[date | None] = mapped_column(Date)
    crm_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    discoveries: Mapped[list[JobDiscovery]] = relationship(back_populates="job")
    rule_evaluations: Mapped[list[JobRuleEvaluation]] = relationship(back_populates="job")


class JobRuleEvaluation(Base):
    __tablename__ = "job_rule_evaluations"
    __table_args__ = (
        Index("ix_job_rule_evaluations_job_evaluated", "job_id", "evaluated_at"),
        Index("ix_job_rule_evaluations_profile", "profile_id", "profile_version"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), nullable=False)
    score: Mapped[int] = mapped_column(Integer, nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    positive_evidence: Mapped[list[dict]] = mapped_column(JSON, nullable=False)
    penalty_evidence: Mapped[list[dict]] = mapped_column(JSON, nullable=False)
    exclusion_evidence: Mapped[list[dict]] = mapped_column(JSON, nullable=False)
    explanation: Mapped[str] = mapped_column(Text, nullable=False)
    profile_id: Mapped[str] = mapped_column(String(80), nullable=False)
    profile_version: Mapped[str] = mapped_column(String(40), nullable=False)
    profile_fingerprint: Mapped[str | None] = mapped_column(String(64))
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    recommendation_override: Mapped[str | None] = mapped_column(String(32))
    content_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)

    job: Mapped[Job] = relationship(back_populates="rule_evaluations")


class JobDiscovery(Base):
    __tablename__ = "job_discoveries"
    __table_args__ = (
        UniqueConstraint("job_id", "run_id", name="uq_job_discovered_in_run"),
        Index("ix_job_discoveries_search_run", "search_id", "run_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), nullable=False)
    search_id: Mapped[int] = mapped_column(ForeignKey("searches.id"), nullable=False)
    run_id: Mapped[int] = mapped_column(ForeignKey("search_runs.id"), nullable=False)
    source: Mapped[str] = mapped_column(String(50), default="seek", nullable=False)
    page_number: Mapped[int | None] = mapped_column(Integer)
    card_type: Mapped[str] = mapped_column(String(32), default="unknown", nullable=False)
    parser_path: Mapped[str] = mapped_column(String(255), default="unknown", nullable=False)
    rank: Mapped[int | None] = mapped_column(Integer)
    found_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    job: Mapped[Job] = relationship(back_populates="discoveries")
    run: Mapped[SearchRun] = relationship(back_populates="discoveries")


class RunEvent(Base):
    __tablename__ = "run_events"
    __table_args__ = (
        Index("ix_run_events_run_created", "run_id", "created_at"),
        Index("ix_run_events_run_severity", "run_id", "severity"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("search_runs.id"), nullable=False)
    source: Mapped[str] = mapped_column(String(50), default="seek", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    severity: Mapped[str] = mapped_column(String(20), nullable=False)
    code: Mapped[str] = mapped_column(String(80), nullable=False)
    phase: Mapped[str] = mapped_column(String(80), nullable=False)
    page_number: Mapped[int | None] = mapped_column(Integer)
    url: Mapped[str | None] = mapped_column(Text)
    page_title: Mapped[str | None] = mapped_column(String(500))
    message: Mapped[str] = mapped_column(Text, nullable=False)
    challenge_rule: Mapped[str | None] = mapped_column(String(120))
    metadata_json: Mapped[dict | None] = mapped_column(JSON)

    run: Mapped[SearchRun] = relationship(back_populates="events")
