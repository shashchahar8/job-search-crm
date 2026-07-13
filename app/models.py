from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import (
    JSON,
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


def utc_now() -> datetime:
    return datetime.now(UTC)


class Search(Base):
    __tablename__ = "searches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
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

    discoveries: Mapped[list[JobDiscovery]] = relationship(back_populates="job")


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
