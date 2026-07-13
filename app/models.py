from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import (
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
    message: Mapped[str | None] = mapped_column(Text)
    last_url: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    search: Mapped[Search] = relationship(back_populates="runs")
    discoveries: Mapped[list[JobDiscovery]] = relationship(back_populates="run")


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
    url: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
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
    found_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    job: Mapped[Job] = relationship(back_populates="discoveries")
    run: Mapped[SearchRun] = relationship(back_populates="discoveries")
