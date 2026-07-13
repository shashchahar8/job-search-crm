from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.collectors.base import CollectorInput
from app.models import Job, JobDiscovery, RunEvent, RunStatus, Search, SearchRun


class EventSeverity(StrEnum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


SAFE_METADATA_KEYS = {
    "job_url",
    "job_id",
    "seek_job_id",
    "page_kind",
    "matched",
    "status",
    "exception_type",
    "card_count",
    "card_type",
    "parser_path",
}


def _safe_metadata(metadata: dict[str, Any] | None) -> dict[str, Any] | None:
    if not metadata:
        return None
    safe: dict[str, Any] = {}
    for key, value in metadata.items():
        if key not in SAFE_METADATA_KEYS:
            continue
        if value is None or isinstance(value, str | int | float | bool):
            safe[key] = value
    return safe or None


def create_search_and_run(
    db: Session, keywords: str, location: str, date_listed: str, max_pages: int
) -> SearchRun:
    search = Search(
        keywords=keywords.strip(),
        location=location.strip(),
        date_listed=date_listed,
        max_pages=max_pages,
    )
    run = SearchRun(search=search, pages_requested=max_pages, status=RunStatus.PENDING)
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def mark_run(
    db: Session,
    run_id: int,
    status: RunStatus,
    message: str | None = None,
    *,
    last_url: str | None = None,
) -> None:
    run = db.get(SearchRun, run_id)
    if run is None:
        return
    run.status = status
    if status == RunStatus.RUNNING and run.started_at is None:
        run.started_at = datetime.now(UTC)
    if status not in {RunStatus.PENDING, RunStatus.RUNNING, RunStatus.AWAITING_USER}:
        run.finished_at = datetime.now(UTC)
    if message:
        run.message = message
    if last_url:
        run.last_url = last_url
    db.commit()


def record_run_event(
    db: Session,
    run_id: int,
    *,
    severity: EventSeverity | str,
    code: str,
    phase: str,
    message: str,
    page_number: int | None = None,
    url: str | None = None,
    page_title: str | None = None,
    challenge_rule: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> RunEvent:
    event = RunEvent(
        run_id=run_id,
        severity=str(severity),
        code=code,
        phase=phase,
        page_number=page_number,
        url=url,
        page_title=page_title,
        message=message,
        challenge_rule=challenge_rule,
        metadata_json=_safe_metadata(metadata),
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


def build_resume_input(db: Session, run_id: int) -> CollectorInput:
    run = db.get(SearchRun, run_id)
    if run is None:
        raise ValueError(f"SearchRun {run_id} does not exist")
    if run.search is None:
        raise ValueError(f"SearchRun {run_id} is missing its search")
    start_page = max(1, run.pages_attempted or 1)
    return CollectorInput(
        keywords=run.search.keywords,
        location=run.search.location,
        date_listed=run.search.date_listed,
        max_pages=run.pages_requested,
        run_id=run.id,
        start_page=start_page,
    )


def save_job_discovery(
    db: Session,
    run_id: int,
    page_number: int,
    job_data: dict[str, str | None],
    *,
    card_type: str = "unknown",
    parser_path: str = "unknown",
    rank: int | None = None,
) -> Job:
    run = db.get(SearchRun, run_id)
    if run is None:
        raise ValueError(f"SearchRun {run_id} does not exist")

    existing = None
    seek_job_id = job_data.get("seek_job_id")
    fallback_key = job_data["fallback_key"]
    if seek_job_id:
        existing = db.scalar(select(Job).where(Job.seek_job_id == seek_job_id))
    if existing is None:
        existing = db.scalar(select(Job).where(Job.fallback_key == fallback_key))

    if existing is None:
        existing = Job(**job_data)
        db.add(existing)
        db.flush()
    else:
        for field in (
            "title",
            "company",
            "location",
            "salary",
            "work_type",
            "posting_date",
            "url",
            "description",
        ):
            value = job_data.get(field)
            if value:
                setattr(existing, field, value)
        existing.updated_at = datetime.now(UTC)

    already = db.scalar(
        select(JobDiscovery).where(
            JobDiscovery.job_id == existing.id,
            JobDiscovery.run_id == run_id,
        )
    )
    if already is None:
        db.add(
            JobDiscovery(
                job=existing,
                search_id=run.search_id,
                run_id=run_id,
                page_number=page_number,
                card_type=card_type,
                parser_path=parser_path,
                rank=rank,
            )
        )
        run.jobs_found += 1
    db.commit()
    db.refresh(existing)
    return existing
