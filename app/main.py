import csv
import io
import logging
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, select
from sqlalchemy.orm import Session, selectinload

from app.collectors.base import CollectorInput
from app.collectors.seek import DATE_LISTED_TO_DAYS, SeekCollector
from app.config import get_settings
from app.database import SessionLocal, get_db, init_db
from app.logging_config import configure_logging
from app.models import Job, JobDiscovery, RunEvent, RunStatus, SearchRun
from app.repository import (
    EventSeverity,
    build_resume_input,
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


@app.get("/", response_class=HTMLResponse)
def index(request: Request, db: Session = DB_DEP) -> HTMLResponse:
    runs = db.scalars(
        select(SearchRun)
        .options(selectinload(SearchRun.search), selectinload(SearchRun.events))
        .order_by(desc(SearchRun.created_at))
        .limit(20)
    ).all()
    jobs = db.scalars(select(Job).order_by(desc(Job.first_seen_at)).limit(50)).all()
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "runs": runs,
            "jobs": jobs,
            "date_options": DATE_LISTED_TO_DAYS.keys(),
            "seek_session_status": seek_session_status,
        },
    )


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
    return templates.TemplateResponse(
        request,
        "run.html",
        {"run": run, "discoveries": discoveries, "events": events, "errors": errors},
    )


@app.get("/export/jobs.csv")
def export_jobs(db: Session = DB_DEP) -> StreamingResponse:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "seek_job_id",
            "title",
            "company",
            "location",
            "salary",
            "work_type",
            "posting_date",
            "url",
            "description",
            "first_seen_at",
            "updated_at",
        ]
    )
    for job in db.scalars(select(Job).order_by(Job.first_seen_at)).all():
        writer.writerow(
            [
                job.seek_job_id,
                job.title,
                job.company,
                job.location,
                job.salary,
                job.work_type,
                job.posting_date,
                job.url,
                job.description,
                job.first_seen_at,
                job.updated_at,
            ]
        )
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=jobs.csv"},
    )
