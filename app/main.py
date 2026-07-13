import csv
import io
import logging
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from math import ceil
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import Select, asc, desc, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.collectors.base import CollectorInput
from app.collectors.seek import DATE_LISTED_TO_DAYS, SeekCollector
from app.config import get_settings
from app.database import SessionLocal, get_db, init_db
from app.logging_config import configure_logging
from app.models import Job, JobDiscovery, RunEvent, RunStatus, SearchRun
from app.presentation import (
    format_datetime,
    format_duration,
    metric_value,
    recently_threshold,
    run_outcome_text,
    status_label,
    status_tone,
    stop_reason_label,
)
from app.repository import (
    EventSeverity,
    build_resume_input,
    canonicalize_url,
    create_search_and_run,
    mark_run,
    record_run_event,
)
from app.seek_session import SeekSessionManager, SessionReadiness

settings = get_settings()
configure_logging(settings.log_level)
LOGGER = logging.getLogger(__name__)
DB_DEP = Depends(get_db)

app = FastAPI(title="Local Job Search CRM")
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")
templates.env.globals.update(
    format_datetime=format_datetime,
    format_duration=format_duration,
    metric_value=metric_value,
    run_outcome_text=run_outcome_text,
    status_label=status_label,
    status_tone=status_tone,
    stop_reason_label=stop_reason_label,
)
executor = ThreadPoolExecutor(max_workers=1)
seek_session_manager = SeekSessionManager(settings)
seek_session_status = SessionReadiness(
    is_open=False,
    is_signed_in=False,
    message="SEEK preparation browser has not been opened.",
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app.router.lifespan_context = lifespan


def _submit_background(description: str, fn, *args) -> None:
    future = executor.submit(fn, *args)

    def _log_exception(done_future) -> None:
        try:
            done_future.result()
        except Exception:
            LOGGER.exception("background_worker_failed description=%s", description)

    future.add_done_callback(_log_exception)


def _run_seek_collector(
    run_id: int, keywords: str, location: str, date_listed: str, max_pages: int
) -> None:
    collector = SeekCollector(settings, SessionLocal)
    collector.collect(
        CollectorInput(
            keywords=keywords,
            location=location,
            date_listed=date_listed,
            max_pages=max_pages,
            run_id=run_id,
        )
    )


def _resume_seek_collector(collector_input: CollectorInput) -> None:
    collector = SeekCollector(settings, SessionLocal)
    collector.collect(collector_input)


def _queue_or_wait_for_profile(db: Session, collector_input: CollectorInput) -> None:
    if seek_session_manager.is_profile_busy():
        record_run_event(
            db,
            collector_input.run_id,
            severity=EventSeverity.INFO,
            code="profile_busy_wait",
            phase="queue",
            message="Waiting for SEEK session/profile to be released",
        )
        mark_run(
            db,
            collector_input.run_id,
            RunStatus.PENDING,
            "Waiting for SEEK session/profile to be released. Click I finished signing in "
            "or close the preparation browser.",
        )
        return
    record_run_event(
        db,
        collector_input.run_id,
        severity=EventSeverity.INFO,
        code="collector_queued",
        phase="queue",
        message=f"Collector queued from page {collector_input.start_page}",
    )
    _submit_background(
        f"collector run {collector_input.run_id}", _resume_seek_collector, collector_input
    )


def _release_waiting_runs() -> None:
    with SessionLocal() as db:
        waiting_runs = db.scalars(
            select(SearchRun)
            .where(SearchRun.status == RunStatus.PENDING)
            .where(SearchRun.message.like("Waiting for SEEK session/profile to be released%"))
            .options(selectinload(SearchRun.search))
        ).all()
        for run in waiting_runs:
            collector_input = build_resume_input(db, run.id)
            mark_run(
                db,
                run.id,
                RunStatus.PENDING,
                f"Resume queued from page {collector_input.start_page}",
            )
            record_run_event(
                db,
                run.id,
                severity=EventSeverity.INFO,
                code="collector_queued",
                phase="queue",
                message=f"Collector queued from page {collector_input.start_page}",
            )
            _submit_background(f"collector run {run.id}", _resume_seek_collector, collector_input)


def _job_filter_options(db: Session) -> dict[str, list[str]]:
    company_query = (
        select(Job.company).where(Job.company.is_not(None)).distinct().order_by(Job.company)
    )
    location_query = (
        select(Job.location).where(Job.location.is_not(None)).distinct().order_by(Job.location)
    )
    work_type_query = (
        select(Job.work_type).where(Job.work_type.is_not(None)).distinct().order_by(Job.work_type)
    )
    source_query = select(Job.source).where(Job.source.is_not(None)).distinct().order_by(Job.source)
    return {
        "companies": [value for value in db.scalars(company_query).all() if value],
        "locations": [value for value in db.scalars(location_query).all() if value],
        "work_types": [value for value in db.scalars(work_type_query).all() if value],
        "sources": [value for value in db.scalars(source_query).all() if value],
    }


def _filtered_jobs_query(request: Request) -> Select[tuple[Job]]:
    params = request.query_params
    query = select(Job)
    text_query = params.get("q", "").strip()
    if text_query:
        like = f"%{text_query}%"
        query = query.where(or_(Job.title.ilike(like), Job.company.ilike(like)))
    for param_name, column in (
        ("company", Job.company),
        ("location", Job.location),
        ("work_type", Job.work_type),
        ("source", Job.source),
    ):
        value = params.get(param_name)
        if value:
            query = query.where(column == value)
    if params.get("salary_present") == "yes":
        query = query.where(Job.salary.is_not(None), Job.salary != "")
    first_discovered = params.get("first_discovered")
    if first_discovered == "last_24h":
        query = query.where(Job.first_seen_at >= recently_threshold())
    elif first_discovered == "last_7d":
        query = query.where(Job.first_seen_at >= datetime.now(UTC) - timedelta(days=7))
    posted_text = params.get("posted_text", "").strip()
    if posted_text:
        query = query.where(Job.posting_date.ilike(f"%{posted_text}%"))

    sort = params.get("sort", "recently_discovered")
    if sort == "oldest_discovered":
        return query.order_by(asc(Job.first_seen_at), asc(Job.id))
    if sort == "company_az":
        return query.order_by(asc(Job.company), asc(Job.title), asc(Job.id))
    if sort == "title_az":
        return query.order_by(asc(Job.title), asc(Job.company), asc(Job.id))
    if sort == "recently_posted":
        return query.order_by(desc(Job.posting_date), desc(Job.first_seen_at), desc(Job.id))
    return query.order_by(desc(Job.first_seen_at), desc(Job.id))


def _query_string_with_page(request: Request, page: int) -> str:
    params = dict(request.query_params)
    params["page"] = str(page)
    return urlencode(params)


def _jobs_csv_response(jobs: list[Job], filename: str) -> StreamingResponse:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "source",
            "source_job_id",
            "canonical_url",
            "source_listing_url",
            "title",
            "company",
            "location",
            "work_type",
            "salary_text",
            "posted_text",
            "first_discovered_at",
            "last_seen_at",
            "description",
        ]
    )
    for job in jobs:
        writer.writerow(
            [
                job.source,
                job.seek_job_id,
                job.canonical_url or canonicalize_url(job.url),
                job.source_listing_url or job.url,
                job.title,
                job.company,
                job.location,
                job.work_type,
                job.salary,
                job.posting_date,
                job.first_seen_at,
                job.last_seen_at,
                job.description,
            ]
        )
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.get("/", response_class=HTMLResponse)
def index(request: Request, db: Session = DB_DEP) -> HTMLResponse:
    latest_run = db.scalar(
        select(SearchRun)
        .options(selectinload(SearchRun.search), selectinload(SearchRun.events))
        .order_by(desc(SearchRun.created_at))
        .limit(1)
    )
    recent_jobs = db.scalars(select(Job).order_by(desc(Job.first_seen_at)).limit(10)).all()
    total_jobs = db.scalar(select(func.count(Job.id))) or 0
    recent_job_count = (
        db.scalar(select(func.count(Job.id)).where(Job.first_seen_at >= recently_threshold())) or 0
    )
    completed_runs = (
        db.scalar(select(func.count(SearchRun.id)).where(SearchRun.status == RunStatus.COMPLETED))
        or 0
    )
    latest_errors: list[RunEvent] = []
    if latest_run:
        latest_errors = [
            event for event in sorted(latest_run.events, key=lambda item: item.created_at)
            if event.severity == EventSeverity.ERROR.value
        ]
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "latest_run": latest_run,
            "latest_errors": latest_errors,
            "recent_jobs": recent_jobs,
            "total_jobs": total_jobs,
            "recent_job_count": recent_job_count,
            "completed_runs": completed_runs,
            "date_options": DATE_LISTED_TO_DAYS.keys(),
            "seek_session_status": seek_session_status,
        },
    )


@app.get("/jobs", response_class=HTMLResponse)
def jobs_index(request: Request, db: Session = DB_DEP) -> HTMLResponse:
    page = max(1, int(request.query_params.get("page", "1") or 1))
    page_size = min(100, max(1, int(request.query_params.get("page_size", "25") or 25)))
    query = _filtered_jobs_query(request)
    total = db.scalar(select(func.count()).select_from(query.order_by(None).subquery())) or 0
    jobs = db.scalars(query.offset((page - 1) * page_size).limit(page_size)).all()
    total_pages = max(1, ceil(total / page_size)) if total else 1
    return templates.TemplateResponse(
        request,
        "jobs.html",
        {
            "jobs": jobs,
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
            "filters": request.query_params,
            "previous_page_query": _query_string_with_page(request, page - 1),
            "next_page_query": _query_string_with_page(request, page + 1),
            **_job_filter_options(db),
        },
    )


@app.get("/jobs/export.csv")
def export_filtered_jobs(request: Request, db: Session = DB_DEP) -> StreamingResponse:
    jobs = db.scalars(_filtered_jobs_query(request)).all()
    return _jobs_csv_response(jobs, "jobs-filtered.csv")


@app.get("/runs", response_class=HTMLResponse)
def runs_index(request: Request, db: Session = DB_DEP) -> HTMLResponse:
    runs = db.scalars(
        select(SearchRun)
        .options(selectinload(SearchRun.search), selectinload(SearchRun.events))
        .order_by(desc(SearchRun.created_at))
    ).all()
    return templates.TemplateResponse(request, "runs.html", {"runs": runs})


@app.post("/runs")
async def start_run(request: Request, db: Session = DB_DEP):
    form = await request.form()
    keywords = str(form.get("keywords", "")).strip()
    location = str(form.get("location", "")).strip()
    date_listed = str(form.get("date_listed", "")).strip()
    maximum_pages_raw = form.get("maximum_pages", form.get("max_pages"))
    if not keywords or not location:
        raise HTTPException(status_code=400, detail="Keywords and location are required")
    if date_listed not in DATE_LISTED_TO_DAYS:
        raise HTTPException(status_code=400, detail="Unsupported date-listed value")
    try:
        maximum_pages = int(str(maximum_pages_raw))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Maximum pages must be an integer") from exc
    if maximum_pages < 1 or maximum_pages > 10:
        raise HTTPException(status_code=400, detail="Maximum pages must be between 1 and 10")
    run = create_search_and_run(db, keywords, location, date_listed, maximum_pages)
    _queue_or_wait_for_profile(
        db,
        CollectorInput(
            keywords=keywords,
            location=location,
            date_listed=date_listed,
            max_pages=maximum_pages,
            run_id=run.id,
        ),
    )
    return HTMLResponse(
        "<meta http-equiv='refresh' content='0; url=/'><p>Collector started. Return to /.</p>"
    )


@app.post("/seek-session/open")
def open_seek_session():
    global seek_session_status
    seek_session_status = seek_session_manager.open_prepare_browser()
    return HTMLResponse(
        "<meta http-equiv='refresh' content='0; url=/'>"
        "<p>SEEK preparation browser opened.</p>"
    )


@app.post("/seek-session/confirm")
def confirm_seek_session():
    global seek_session_status
    seek_session_status = seek_session_manager.readiness()
    if "complete it in the visible browser" not in seek_session_status.message.lower():
        seek_session_manager.close()
        _release_waiting_runs()
    return HTMLResponse(
        "<meta http-equiv='refresh' content='0; url=/'>"
        "<p>SEEK preparation status checked.</p>"
    )


@app.post("/runs/{run_id}/resume")
def resume_run(run_id: int, db: Session = DB_DEP):
    run = db.get(SearchRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.status != RunStatus.AWAITING_USER:
        raise HTTPException(status_code=400, detail="Only awaiting_user runs can be resumed")
    collector_input = build_resume_input(db, run_id)
    mark_run(
        db,
        run_id,
        RunStatus.PENDING,
        f"Resume queued from page {collector_input.start_page}",
    )
    _queue_or_wait_for_profile(db, collector_input)
    return HTMLResponse(
        "<meta http-equiv='refresh' content='0; url=/'>"
        f"<p>Run {run_id} resume queued.</p>"
    )


@app.get("/runs/{run_id}", response_class=HTMLResponse)
def run_detail(run_id: int, request: Request, db: Session = DB_DEP) -> HTMLResponse:
    run = db.scalar(
        select(SearchRun)
        .where(SearchRun.id == run_id)
        .options(selectinload(SearchRun.search), selectinload(SearchRun.discoveries))
    )
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    discoveries = db.scalars(
        select(JobDiscovery)
        .where(JobDiscovery.run_id == run_id)
        .options(selectinload(JobDiscovery.job))
        .order_by(JobDiscovery.found_at)
    ).all()
    events = db.scalars(
        select(RunEvent)
        .where(RunEvent.run_id == run_id)
        .order_by(RunEvent.created_at, RunEvent.id)
    ).all()
    errors = [event for event in events if event.severity == EventSeverity.ERROR.value]
    warnings = [event for event in events if event.severity == EventSeverity.WARNING.value]
    return templates.TemplateResponse(
        request,
        "run.html",
        {
            "run": run,
            "discoveries": discoveries,
            "events": events,
            "errors": errors,
            "warnings": warnings,
        },
    )


@app.get("/export/jobs.csv")
def export_jobs(db: Session = DB_DEP) -> StreamingResponse:
    jobs = db.scalars(select(Job).order_by(desc(Job.first_seen_at), desc(Job.id))).all()
    return _jobs_csv_response(jobs, "jobs.csv")
