import csv
import io
import logging
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from math import ceil
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import Select, asc, desc, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.collectors.base import CollectorInput
from app.collectors.seek import DATE_LISTED_TO_DAYS, SeekCollector
from app.config import get_settings
from app.database import SessionLocal, get_db, init_db
from app.logging_config import configure_logging
from app.models import (
    CRMStatus,
    Job,
    JobDiscovery,
    JobPriority,
    JobRuleEvaluation,
    RuleOutcome,
    RunEvent,
    RunStatus,
    SearchRun,
)
from app.presentation import (
    best_job_url,
    crm_status_label,
    format_datetime,
    format_duration,
    metric_value,
    priority_label,
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
    update_job_crm,
)
from app.rules import (
    PROFILE_ID,
    PROFILE_VERSION,
    effective_recommendation,
    evaluate_and_store_job,
    evaluation_state,
    latest_evaluation,
    set_recommendation_override,
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
    best_job_url=best_job_url,
    crm_status_label=crm_status_label,
    format_datetime=format_datetime,
    format_duration=format_duration,
    metric_value=metric_value,
    priority_label=priority_label,
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

CRM_STATUS_OPTIONS = [(item.value, crm_status_label(item.value)) for item in CRMStatus]
PRIORITY_OPTIONS = [(item.value, priority_label(item.value)) for item in JobPriority]
RULE_RECOMMENDATION_OPTIONS = [
    ("", "No override"),
    (RuleOutcome.STRONG_MATCH.value, "Strong match"),
    (RuleOutcome.REVIEW.value, "Review"),
    (RuleOutcome.WEAK_MATCH.value, "Weak match"),
    (RuleOutcome.EXCLUDE.value, "Exclude"),
]


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app.router.lifespan_context = lifespan


@app.exception_handler(404)
async def not_found_page(request: Request, _exc: HTTPException) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "404.html",
        {"message": "The requested page or job could not be found."},
        status_code=status.HTTP_404_NOT_FOUND,
    )


def _redirect(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=status.HTTP_303_SEE_OTHER)


def _parse_optional_date(value: str | None, field_label: str) -> tuple[date | None, str | None]:
    if value is None or not value.strip():
        return None, None
    try:
        return date.fromisoformat(value.strip()), None
    except ValueError:
        return None, f"{field_label} must be a valid date in YYYY-MM-DD format."


def _job_detail_context(
    request: Request,
    job: Job,
    db: Session,
    *,
    success: str | None = None,
    error: str | None = None,
) -> dict:
    discoveries = db.scalars(
        select(JobDiscovery)
        .where(JobDiscovery.job_id == job.id)
        .options(selectinload(JobDiscovery.run).selectinload(SearchRun.search))
        .order_by(desc(JobDiscovery.found_at), desc(JobDiscovery.id))
    ).all()
    evaluation = latest_evaluation(db, job.id)
    return {
        "request": request,
        "job": job,
        "discoveries": discoveries,
        "rule_evaluation": evaluation,
        "rule_evaluation_state": evaluation_state(job, evaluation),
        "effective_recommendation": effective_recommendation(evaluation),
        "hard_exclusion_applied": _hard_exclusion_applied(evaluation),
        "score_based_outcome": _score_based_outcome(evaluation),
        "rule_recommendation_options": RULE_RECOMMENDATION_OPTIONS,
        "crm_status_options": CRM_STATUS_OPTIONS,
        "priority_options": PRIORITY_OPTIONS,
        "success": success,
        "error": error,
    }


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
        ("crm_status", Job.crm_status),
        ("priority", Job.priority),
    ):
        value = params.get(param_name)
        if value:
            query = query.where(column == value)
    if params.get("favorites_only") == "yes":
        query = query.where(Job.is_favorite.is_(True))
    if params.get("needs_review") == "yes":
        query = query.where(Job.crm_status == CRMStatus.NEW.value)
    if params.get("applications_in_progress") == "yes":
        query = query.where(
            Job.crm_status.in_(
                [CRMStatus.PREPARING.value, CRMStatus.APPLIED.value, CRMStatus.INTERVIEW.value]
            )
        )
    today = date.today()
    if params.get("follow_up_due") == "yes":
        query = query.where(Job.follow_up_date.is_not(None), Job.follow_up_date <= today)
    deadline_filter = params.get("application_deadline")
    if deadline_filter == "upcoming":
        query = query.where(
            Job.application_deadline.is_not(None), Job.application_deadline >= today
        )
    elif deadline_filter == "due":
        query = query.where(
            Job.application_deadline.is_not(None), Job.application_deadline <= today
        )
    elif deadline_filter == "missing":
        query = query.where(Job.application_deadline.is_(None))
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


def _latest_evaluations_for_jobs(db: Session, jobs: list[Job]) -> dict[int, JobRuleEvaluation]:
    job_ids = [job.id for job in jobs]
    if not job_ids:
        return {}
    evaluations = db.scalars(
        select(JobRuleEvaluation)
        .where(JobRuleEvaluation.job_id.in_(job_ids))
        .order_by(desc(JobRuleEvaluation.evaluated_at), desc(JobRuleEvaluation.id))
    ).all()
    latest: dict[int, JobRuleEvaluation] = {}
    for evaluation in evaluations:
        latest.setdefault(evaluation.job_id, evaluation)
    return latest


def _rule_context(job: Job, evaluation: JobRuleEvaluation | None) -> dict:
    state = evaluation_state(job, evaluation)
    return {
        "evaluation": evaluation,
        "state": state,
        "effective": effective_recommendation(evaluation),
        "hard_exclusion_applied": _hard_exclusion_applied(evaluation),
        "score_based_outcome": _score_based_outcome(evaluation),
    }


def _hard_exclusion_applied(evaluation: JobRuleEvaluation | None) -> bool:
    if evaluation is None:
        return False
    return any(item.get("hard_exclusion_applied") for item in evaluation.exclusion_evidence)


def _score_based_outcome(evaluation: JobRuleEvaluation | None) -> str | None:
    if evaluation is None:
        return None
    if evaluation.score >= 75:
        return RuleOutcome.STRONG_MATCH.value
    if evaluation.score >= 50:
        return RuleOutcome.REVIEW.value
    if evaluation.score >= 30:
        return RuleOutcome.WEAK_MATCH.value
    return RuleOutcome.EXCLUDE.value


def _jobs_with_rule_context(db: Session, request: Request) -> list[tuple[Job, dict]]:
    jobs = db.scalars(_filtered_jobs_query(request)).all()
    latest = _latest_evaluations_for_jobs(db, jobs)
    params = request.query_params

    rows: list[tuple[Job, dict]] = []
    for job in jobs:
        context = _rule_context(job, latest.get(job.id))
        evaluation = context["evaluation"]
        score = evaluation.score if evaluation else None
        requested_outcome = params.get("rule_outcome")
        if requested_outcome and (evaluation is None or evaluation.outcome != requested_outcome):
            continue
        requested_effective = params.get("effective_recommendation")
        if requested_effective and context["effective"] != requested_effective:
            continue
        if params.get("rule_state") and context["state"] != params.get("rule_state"):
            continue
        if params.get("unevaluated") == "yes" and context["state"] != "unevaluated":
            continue
        if params.get("stale") == "yes" and context["state"] != "stale":
            continue
        has_override = evaluation and evaluation.recommendation_override
        if params.get("overridden") == "yes" and not has_override:
            continue
        min_score = params.get("min_score", "").strip()
        if min_score and (score is None or score < int(min_score)):
            continue
        max_score = params.get("max_score", "").strip()
        if max_score and (score is None or score > int(max_score)):
            continue
        in_review_queue = (
            context["state"] in {"unevaluated", "stale"}
            or context["effective"] == RuleOutcome.REVIEW.value
            or (
                context["effective"] == RuleOutcome.STRONG_MATCH.value
                and job.crm_status == CRMStatus.NEW.value
            )
            or bool(has_override)
        )
        if params.get("review_queue") == "yes" and not in_review_queue:
            continue
        rows.append((job, context))

    sort = params.get("sort", "recently_discovered")
    if sort == "rule_score_high":
        rows.sort(
            key=lambda row: row[1]["evaluation"].score if row[1]["evaluation"] else -1,
            reverse=True,
        )
    elif sort == "rule_score_low":
        rows.sort(key=lambda row: row[1]["evaluation"].score if row[1]["evaluation"] else 101)
    elif sort == "rule_evaluated_recent":
        rows.sort(
            key=lambda row: (
                row[1]["evaluation"].evaluated_at.timestamp() if row[1]["evaluation"] else 0
            ),
            reverse=True,
        )
    return rows


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
            "job_url",
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
            "CRM status",
            "Manual priority",
            "Favorite",
            "Application deadline",
            "Follow-up date",
            "Notes",
            "rule_score",
            "rule_outcome",
            "effective_recommendation",
            "recommendation_override",
            "rule_explanation",
            "rule_profile",
            "rule_version",
            "rule_evaluated_at",
            "rule_evaluation_state",
            "rule_score_based_outcome",
            "rule_hard_exclusion_applied",
            "description",
        ]
    )
    for job in jobs:
        job_url = best_job_url(job)
        evaluation = getattr(job, "_rule_evaluation", None)
        state = getattr(job, "_rule_evaluation_state", "unevaluated")
        hard_exclusion_applied = _hard_exclusion_applied(evaluation)
        score_based_outcome = _score_based_outcome(evaluation)
        writer.writerow(
            [
                job.source,
                job.seek_job_id,
                job_url,
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
                crm_status_label(job.crm_status),
                priority_label(job.priority),
                "yes" if job.is_favorite else "no",
                job.application_deadline,
                job.follow_up_date,
                job.notes,
                evaluation.score if evaluation else "",
                evaluation.outcome if evaluation else "",
                effective_recommendation(evaluation) or "",
                evaluation.recommendation_override if evaluation else "",
                evaluation.explanation if evaluation else "",
                evaluation.profile_id if evaluation else "",
                evaluation.profile_version if evaluation else "",
                evaluation.evaluated_at if evaluation else "",
                state,
                score_based_outcome or "",
                "yes" if hard_exclusion_applied else "no",
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
    today = date.today()
    new_unreviewed_count = (
        db.scalar(select(func.count(Job.id)).where(Job.crm_status == CRMStatus.NEW.value)) or 0
    )
    shortlisted_count = (
        db.scalar(select(func.count(Job.id)).where(Job.crm_status == CRMStatus.SHORTLISTED.value))
        or 0
    )
    applications_in_progress_count = (
        db.scalar(
            select(func.count(Job.id)).where(
                Job.crm_status.in_(
                    [
                        CRMStatus.PREPARING.value,
                        CRMStatus.APPLIED.value,
                        CRMStatus.INTERVIEW.value,
                    ]
                )
            )
        )
        or 0
    )
    favorites_count = db.scalar(select(func.count(Job.id)).where(Job.is_favorite.is_(True))) or 0
    follow_ups_due_count = (
        db.scalar(
            select(func.count(Job.id)).where(
                Job.follow_up_date.is_not(None), Job.follow_up_date <= today
            )
        )
        or 0
    )
    all_jobs = db.scalars(select(Job)).all()
    latest = _latest_evaluations_for_jobs(db, all_jobs)
    rule_counts = {
        "strong": 0,
        "review": 0,
        "exclude": 0,
        "unevaluated": 0,
        "stale": 0,
    }
    for job in all_jobs:
        context = _rule_context(job, latest.get(job.id))
        if context["state"] == "unevaluated":
            rule_counts["unevaluated"] += 1
        elif context["state"] == "stale":
            rule_counts["stale"] += 1
        if context["effective"] == RuleOutcome.STRONG_MATCH.value:
            rule_counts["strong"] += 1
        elif context["effective"] == RuleOutcome.REVIEW.value:
            rule_counts["review"] += 1
        elif context["effective"] == RuleOutcome.EXCLUDE.value:
            rule_counts["exclude"] += 1
    latest_errors: list[RunEvent] = []
    if latest_run:
        latest_errors = [
            event
            for event in sorted(latest_run.events, key=lambda item: item.created_at)
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
            "new_unreviewed_count": new_unreviewed_count,
            "shortlisted_count": shortlisted_count,
            "applications_in_progress_count": applications_in_progress_count,
            "favorites_count": favorites_count,
            "follow_ups_due_count": follow_ups_due_count,
            "rule_counts": rule_counts,
            "date_options": DATE_LISTED_TO_DAYS.keys(),
            "seek_session_status": seek_session_status,
        },
    )


@app.get("/jobs", response_class=HTMLResponse)
def jobs_index(request: Request, db: Session = DB_DEP) -> HTMLResponse:
    page = max(1, int(request.query_params.get("page", "1") or 1))
    page_size = min(100, max(1, int(request.query_params.get("page_size", "25") or 25)))
    rows = _jobs_with_rule_context(db, request)
    total = len(rows)
    paged_rows = rows[(page - 1) * page_size : page * page_size]
    total_pages = max(1, ceil(total / page_size)) if total else 1
    return templates.TemplateResponse(
        request,
        "jobs.html",
        {
            "job_rows": paged_rows,
            "jobs": [job for job, _context in paged_rows],
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
            "filters": request.query_params,
            "previous_page_query": _query_string_with_page(request, page - 1),
            "next_page_query": _query_string_with_page(request, page + 1),
            "crm_status_options": CRM_STATUS_OPTIONS,
            "priority_options": PRIORITY_OPTIONS,
            "rule_recommendation_options": RULE_RECOMMENDATION_OPTIONS[1:],
            "rule_profile_id": PROFILE_ID,
            "rule_profile_version": PROFILE_VERSION,
            **_job_filter_options(db),
        },
    )


@app.get("/jobs/export.csv")
def export_filtered_jobs(request: Request, db: Session = DB_DEP) -> StreamingResponse:
    rows = _jobs_with_rule_context(db, request)
    jobs = []
    for job, context in rows:
        job._rule_evaluation = context["evaluation"]
        job._rule_evaluation_state = context["state"]
        jobs.append(job)
    return _jobs_csv_response(jobs, "jobs-filtered.csv")


@app.post("/jobs/evaluate-bulk")
async def evaluate_bulk_jobs(request: Request, db: Session = DB_DEP):
    form = await request.form()
    mode = str(form.get("mode", "unevaluated"))
    if mode not in {"unevaluated", "stale", "all"}:
        raise HTTPException(status_code=400, detail="Unsupported evaluation mode")
    jobs = db.scalars(select(Job).order_by(desc(Job.first_seen_at), desc(Job.id))).all()
    latest = _latest_evaluations_for_jobs(db, jobs)
    evaluated = 0
    for job in jobs:
        state = evaluation_state(job, latest.get(job.id))
        if mode == "unevaluated" and state != "unevaluated":
            continue
        if mode == "stale" and state != "stale":
            continue
        before = latest_evaluation(db, job.id)
        after = evaluate_and_store_job(db, job)
        if before is None or before.id != after.id or mode == "all":
            evaluated += 1
    return _redirect(f"/jobs?success={evaluated}+job+rule+assessment(s)+updated")


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_detail(job_id: int, request: Request, db: Session = DB_DEP) -> HTMLResponse:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    success = request.query_params.get("success")
    return templates.TemplateResponse(
        request,
        "job.html",
        _job_detail_context(request, job, db, success=success),
    )


@app.post("/jobs/{job_id}/evaluate", response_class=HTMLResponse)
def evaluate_job_route(job_id: int, db: Session = DB_DEP):
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    evaluate_and_store_job(db, job)
    return _redirect(f"/jobs/{job.id}?success=Rule+assessment+updated")


@app.post("/jobs/{job_id}/recommendation-override", response_class=HTMLResponse)
async def update_recommendation_override_route(job_id: int, request: Request, db: Session = DB_DEP):
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    form = await request.form()
    raw_value = str(form.get("recommendation_override", "")).strip()
    override = raw_value or None
    try:
        set_recommendation_override(db, job, override)
    except ValueError:
        return templates.TemplateResponse(
            request,
            "job.html",
            _job_detail_context(request, job, db, error="Invalid recommendation override."),
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    return _redirect(f"/jobs/{job.id}?success=Manual+recommendation+override+saved")


@app.post("/jobs/{job_id}/crm", response_class=HTMLResponse)
async def update_job_crm_route(job_id: int, request: Request, db: Session = DB_DEP):
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    form = await request.form()
    status_value = str(form.get("crm_status", "")).strip()
    priority_value = str(form.get("priority", "")).strip()
    try:
        crm_status = CRMStatus(status_value)
    except ValueError:
        return templates.TemplateResponse(
            request,
            "job.html",
            _job_detail_context(request, job, db, error="Invalid CRM status."),
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    try:
        priority = JobPriority(priority_value)
    except ValueError:
        return templates.TemplateResponse(
            request,
            "job.html",
            _job_detail_context(request, job, db, error="Invalid manual priority."),
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    application_deadline, deadline_error = _parse_optional_date(
        form.get("application_deadline"), "Application deadline"
    )
    follow_up_date, follow_up_error = _parse_optional_date(
        form.get("follow_up_date"), "Follow-up date"
    )
    if deadline_error or follow_up_error:
        return templates.TemplateResponse(
            request,
            "job.html",
            _job_detail_context(request, job, db, error=deadline_error or follow_up_error),
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    notes = str(form.get("notes", "")).strip() or None
    update_job_crm(
        db,
        job,
        crm_status=crm_status,
        priority=priority,
        is_favorite=form.get("is_favorite") == "yes",
        notes=notes,
        application_deadline=application_deadline,
        follow_up_date=follow_up_date,
    )
    return _redirect(f"/jobs/{job.id}?success=CRM+details+saved")


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
        "<meta http-equiv='refresh' content='0; url=/'><p>SEEK preparation browser opened.</p>"
    )


@app.post("/seek-session/confirm")
def confirm_seek_session():
    global seek_session_status
    seek_session_status = seek_session_manager.readiness()
    if "complete it in the visible browser" not in seek_session_status.message.lower():
        seek_session_manager.close()
        _release_waiting_runs()
    return HTMLResponse(
        "<meta http-equiv='refresh' content='0; url=/'><p>SEEK preparation status checked.</p>"
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
        f"<meta http-equiv='refresh' content='0; url=/'><p>Run {run_id} resume queued.</p>"
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
        select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.created_at, RunEvent.id)
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
    latest = _latest_evaluations_for_jobs(db, jobs)
    for job in jobs:
        evaluation = latest.get(job.id)
        job._rule_evaluation = evaluation
        job._rule_evaluation_state = evaluation_state(job, evaluation)
    return _jobs_csv_response(jobs, "jobs.csv")
