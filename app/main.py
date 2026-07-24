import csv
import io
import logging
import threading
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

from app.campaigns import (
    DATE_WINDOW_OPTIONS,
    CampaignValidationError,
    add_campaign_membership,
    archive_campaign,
    build_campaign_plan,
    campaign_execution_has_child_errors,
    create_campaign,
    create_campaign_execution_plan,
    create_child_run_for_snapshot,
    create_saved_search,
    date_window_label,
    ordered_child_snapshots,
    reconcile_running_campaign_executions,
    record_campaign_event,
    refresh_campaign_execution_aggregates,
    remove_membership,
    sync_child_snapshot_from_run,
    update_campaign,
    update_membership,
    update_saved_search,
)
from app.collectors.base import CollectorInput, UnsupportedSourceError
from app.collectors.registry import (
    SourceIdentifier,
    get_collector,
    get_enabled_source_options,
    get_source_registration,
)
from app.collectors.seek import DATE_LISTED_TO_DAYS
from app.config import get_settings
from app.database import SessionLocal, get_db, init_db
from app.logging_config import configure_logging
from app.models import (
    Campaign,
    CampaignExecution,
    CampaignExecutionChildSnapshot,
    CampaignSavedSearch,
    CRMStatus,
    Job,
    JobDiscovery,
    JobPriority,
    JobRuleEvaluation,
    RuleOutcome,
    RunEvent,
    RunStatus,
    SavedSearch,
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
    ProfileSelectionError,
    RuleProfile,
    effective_recommendation,
    evaluate_and_store_job,
    evaluation_state,
    get_profile_by_key,
    get_profile_or_default,
    latest_evaluation,
    load_profile_registry,
    profile_key,
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
    profile_key=profile_key,
    date_window_label=date_window_label,
)
executor = ThreadPoolExecutor(max_workers=1)
seek_session_manager = SeekSessionManager(settings)
seek_session_status = SessionReadiness(
    is_open=False,
    is_signed_in=False,
    message="SEEK preparation browser has not been opened.",
)
_campaign_worker_guard = threading.Lock()
_active_campaign_execution_ids: set[int] = set()
_source_profile_owner_lock = threading.Lock()
_source_profile_owner: str | None = None

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
    with SessionLocal() as db:
        reconcile_running_campaign_executions(db)
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


def _profile_context(request: Request | None = None) -> dict:
    registry = load_profile_registry()
    query_params = getattr(request, "query_params", {}) if request else {}
    requested_key = query_params.get("profile_key")
    requested_id = query_params.get("profile_id")
    requested_version = query_params.get("profile_version")
    selected_profile = None
    profile_error = None
    try:
        if requested_key:
            selected_profile = get_profile_by_key(requested_key)
        else:
            selected_profile = get_profile_or_default(requested_id, requested_version)
    except ProfileSelectionError as exc:
        profile_error = str(exc)
    return {
        "profile_registry": registry,
        "valid_profiles": registry.valid_profiles,
        "profile_errors": list(registry.errors + registry.duplicate_errors),
        "selected_profile": selected_profile,
        "profile_error": profile_error,
    }


def _profile_query(profile: RuleProfile | None) -> str:
    if profile is None:
        return ""
    return urlencode({"profile_key": profile_key(profile)})


def _job_detail_context(
    request: Request,
    job: Job,
    db: Session,
    *,
    success: str | None = None,
    error: str | None = None,
) -> dict:
    profile_context = _profile_context(request)
    selected_profile = profile_context["selected_profile"]
    discoveries = db.scalars(
        select(JobDiscovery)
        .where(JobDiscovery.job_id == job.id)
        .options(selectinload(JobDiscovery.run).selectinload(SearchRun.search))
        .order_by(desc(JobDiscovery.found_at), desc(JobDiscovery.id))
    ).all()
    evaluation = latest_evaluation(db, job.id, selected_profile) if selected_profile else None
    return {
        "request": request,
        "job": job,
        "discoveries": discoveries,
        "rule_evaluation": evaluation,
        "rule_evaluation_state": (
            evaluation_state(job, evaluation, selected_profile)
            if selected_profile
            else "unavailable"
        ),
        "effective_recommendation": effective_recommendation(evaluation),
        "hard_exclusion_applied": _hard_exclusion_applied(evaluation),
        "score_based_outcome": _score_based_outcome(evaluation),
        "rule_recommendation_options": RULE_RECOMMENDATION_OPTIONS,
        "profile_query": _profile_query(selected_profile),
        **profile_context,
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


def _campaign_guard_acquire(execution_id: int) -> bool:
    with _campaign_worker_guard:
        if execution_id in _active_campaign_execution_ids:
            return False
        _active_campaign_execution_ids.add(execution_id)
        return True


def _campaign_guard_release(execution_id: int) -> None:
    with _campaign_worker_guard:
        _active_campaign_execution_ids.discard(execution_id)


def _source_profile_owner_matches(owner: str) -> bool:
    with _source_profile_owner_lock:
        return _source_profile_owner == owner


def _try_acquire_source_profile(owner: str) -> bool:
    global _source_profile_owner
    with _source_profile_owner_lock:
        if _source_profile_owner not in {None, owner}:
            return False
        if _source_profile_owner is None and seek_session_manager.is_profile_busy():
            return False
        _source_profile_owner = owner
        return True


def _release_source_profile(owner: str) -> None:
    global _source_profile_owner
    with _source_profile_owner_lock:
        if _source_profile_owner == owner:
            _source_profile_owner = None


def _source_profile_busy_for(owner: str) -> bool:
    with _source_profile_owner_lock:
        if _source_profile_owner not in {None, owner}:
            return True
    return seek_session_manager.is_profile_busy()


def _require_supported_collection(source_identifier: str) -> None:
    registration = get_source_registration(source_identifier)
    if not registration.enabled or not registration.supported:
        raise UnsupportedSourceError(
            f"Source '{source_identifier}' is not supported for collection in this milestone. "
            "Only source 'seek' is enabled."
        )


def _resume_collector(collector_input: CollectorInput) -> None:
    owner = f"run:{collector_input.run_id}"
    try:
        collector = get_collector(collector_input.source_identifier, settings, SessionLocal)
        collector.collect(collector_input)
    finally:
        _release_source_profile(owner)


def _queue_or_wait_for_profile(db: Session, collector_input: CollectorInput) -> None:
    registration = get_source_registration(collector_input.source_identifier)
    if not registration.enabled or not registration.supported:
        message = str(UnsupportedSourceError(
            f"Source '{collector_input.source_identifier}' is not supported for collection "
            "in this milestone. Only source 'seek' is enabled."
        ))
        record_run_event(
            db,
            collector_input.run_id,
            severity=EventSeverity.ERROR,
            code="unsupported_source",
            phase="validation",
            message=message,
            metadata={"source": collector_input.source_identifier},
        )
        mark_run(db, collector_input.run_id, RunStatus.FAILED, message)
        return
    owner = f"run:{collector_input.run_id}"
    if (
        registration.capabilities.requires_persistent_browser_profile
        and not _try_acquire_source_profile(owner)
    ):
        record_run_event(
            db,
            collector_input.run_id,
            severity=EventSeverity.INFO,
            code="profile_busy_wait",
            phase="queue",
            message="Waiting for source session/profile to be released",
        )
        mark_run(
            db,
            collector_input.run_id,
            RunStatus.PENDING,
            "Waiting for source session/profile to be released. Click I finished signing in "
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
        f"collector run {collector_input.run_id}", _resume_collector, collector_input
    )


def _release_waiting_runs() -> None:
    with SessionLocal() as db:
        waiting_runs = db.scalars(
            select(SearchRun)
            .where(SearchRun.status == RunStatus.PENDING)
            .where(SearchRun.message.like("Waiting for source session/profile to be released%"))
            .options(selectinload(SearchRun.search))
        ).all()
        for run in waiting_runs:
            collector_input = build_resume_input(db, run.id)
            registration = get_source_registration(collector_input.source_identifier)
            if not registration.capabilities.supports_resume_after_awaiting_user:
                continue
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
            _submit_background(f"collector run {run.id}", _resume_collector, collector_input)
        waiting_executions = db.scalars(
            select(CampaignExecution)
            .where(CampaignExecution.status == RunStatus.PENDING)
            .where(
                CampaignExecution.message.like(
                    "Waiting for source session/profile to be released%"
                )
            )
        ).all()
        for execution in waiting_executions:
            _queue_campaign_execution(db, execution)


def _campaign_next_collectable_child(
    db: Session, execution: CampaignExecution
) -> CampaignExecutionChildSnapshot | None:
    children = ordered_child_snapshots(db, execution.id)
    for child in children:
        if child.status in {
            RunStatus.COMPLETED,
            RunStatus.COMPLETED_WITH_ERRORS,
            RunStatus.FAILED,
            RunStatus.BLOCKED,
        }:
            continue
        return child
    return None


def _campaign_child_collector_input(
    db: Session,
    child: CampaignExecutionChildSnapshot,
) -> CollectorInput:
    if child.child_run_id is None:
        run = create_child_run_for_snapshot(db, child)
    else:
        run = db.get(SearchRun, child.child_run_id)
        if run is None:
            run = create_child_run_for_snapshot(db, child)
    return build_resume_input(db, run.id)


def _sync_campaign_child_and_parent(
    db: Session,
    execution_id: int,
    child_id: int,
) -> tuple[CampaignExecution, CampaignExecutionChildSnapshot, SearchRun | None]:
    child = db.get(CampaignExecutionChildSnapshot, child_id)
    run = db.get(SearchRun, child.child_run_id) if child and child.child_run_id else None
    if child and run:
        sync_child_snapshot_from_run(db, child, run)
    execution = refresh_campaign_execution_aggregates(db, execution_id)
    return execution, child, run


def _run_campaign_execution(
    execution_id: int,
    session_factory=SessionLocal,
    collector_resolver=get_collector,
) -> None:
    owner = f"campaign:{execution_id}"
    try:
        if not _source_profile_owner_matches(owner) and not _try_acquire_source_profile(owner):
            with session_factory() as db:
                execution = db.get(CampaignExecution, execution_id)
                if execution:
                    execution.status = RunStatus.PENDING
                    execution.message = (
                        "Waiting for source session/profile to be released. Click I finished "
                        "signing in or close the preparation browser."
                    )
                    db.commit()
            return
        with session_factory() as db:
            execution = db.get(CampaignExecution, execution_id)
            if execution is None:
                return
            if execution.status in {
                RunStatus.COMPLETED,
                RunStatus.COMPLETED_WITH_ERRORS,
                RunStatus.FAILED,
                RunStatus.INTERRUPTED,
            }:
                return
            was_awaiting_user = execution.status == RunStatus.AWAITING_USER
            execution.status = RunStatus.RUNNING
            if execution.started_at is None:
                execution.started_at = datetime.now(UTC)
            execution.message = (
                "Campaign execution resumed."
                if was_awaiting_user
                else "Campaign execution started."
            )
            db.commit()
            record_campaign_event(
                db,
                execution.id,
                severity="info",
                code="resumed" if was_awaiting_user else "started",
                phase="execution",
                message=execution.message,
                metadata={"status": RunStatus.RUNNING.value},
            )
            refresh_campaign_execution_aggregates(db, execution.id)

        while True:
            with session_factory() as db:
                execution = db.get(CampaignExecution, execution_id)
                if execution is None:
                    return
                child = _campaign_next_collectable_child(db, execution)
                if child is None:
                    refresh_campaign_execution_aggregates(db, execution.id)
                    execution.status = (
                        RunStatus.COMPLETED_WITH_ERRORS
                        if campaign_execution_has_child_errors(db, execution.id)
                        else RunStatus.COMPLETED
                    )
                    execution.finished_at = datetime.now(UTC)
                    execution.stop_reason = "all_children_completed"
                    execution.message = "Campaign execution finished."
                    db.commit()
                    record_campaign_event(
                        db,
                        execution.id,
                        severity="warning"
                        if execution.status == RunStatus.COMPLETED_WITH_ERRORS
                        else "info",
                        code="completed_with_errors"
                        if execution.status == RunStatus.COMPLETED_WITH_ERRORS
                        else "completed",
                        phase="execution",
                        message=execution.message,
                        metadata={"status": execution.status.value},
                    )
                    return

                try:
                    _require_supported_collection(child.source)
                except UnsupportedSourceError as exc:
                    child.status = RunStatus.FAILED
                    child.stop_reason = "unsupported_source"
                    execution.status = RunStatus.FAILED
                    execution.stop_reason = "unsupported_source"
                    execution.message = str(exc)
                    execution.finished_at = datetime.now(UTC)
                    db.commit()
                    refresh_campaign_execution_aggregates(db, execution.id)
                    record_campaign_event(
                        db,
                        execution.id,
                        severity="error",
                        code="failed",
                        phase="validation",
                        message=str(exc),
                        child_snapshot_id=child.id,
                        metadata={"source": child.source, "stop_reason": "unsupported_source"},
                    )
                    return

                collector_input = _campaign_child_collector_input(db, child)
                child.status = RunStatus.RUNNING
                execution.status = RunStatus.RUNNING
                execution.current_child_id = child.id
                execution.message = (
                    f"Collecting child {child.position}: {child.saved_search_name_snapshot}"
                )
                db.commit()
                record_campaign_event(
                    db,
                    execution.id,
                    severity="info",
                    code="child_started",
                    phase="child",
                    message=execution.message,
                    child_snapshot_id=child.id,
                    metadata={
                        "child_run_id": collector_input.run_id,
                        "source": child.source,
                    },
                )
                child_id = child.id

            collector = collector_resolver(
                collector_input.source_identifier, settings, session_factory
            )
            collector.collect(collector_input)

            with session_factory() as db:
                execution, child, run = _sync_campaign_child_and_parent(
                    db, execution_id, child_id
                )
                if run is None:
                    execution.status = RunStatus.FAILED
                    execution.stop_reason = "missing_child_run"
                    execution.message = "Campaign child run was not found after collection."
                    execution.finished_at = datetime.now(UTC)
                    db.commit()
                    record_campaign_event(
                        db,
                        execution.id,
                        severity="error",
                        code="failed",
                        phase="child",
                        message=execution.message,
                        child_snapshot_id=child.id if child else None,
                        metadata={"stop_reason": "missing_child_run"},
                    )
                    return
                if run.status == RunStatus.AWAITING_USER:
                    execution.status = RunStatus.AWAITING_USER
                    execution.stop_reason = run.stop_reason
                    execution.message = (
                        f"Child {child.position} requires user action. Complete the source "
                        "session challenge, then resume campaign execution."
                    )
                    execution.current_child_id = child.id
                    db.commit()
                    record_campaign_event(
                        db,
                        execution.id,
                        severity="warning",
                        code="awaiting_user",
                        phase="child",
                        message=execution.message,
                        child_snapshot_id=child.id,
                        metadata={"child_run_id": run.id, "stop_reason": run.stop_reason},
                    )
                    return
                if run.status == RunStatus.INTERRUPTED:
                    execution.status = RunStatus.INTERRUPTED
                    execution.stop_reason = run.stop_reason
                    execution.message = run.message
                    execution.finished_at = datetime.now(UTC)
                    db.commit()
                    record_campaign_event(
                        db,
                        execution.id,
                        severity="warning",
                        code="interrupted",
                        phase="child",
                        message=execution.message or "Campaign child interrupted.",
                        child_snapshot_id=child.id,
                        metadata={"child_run_id": run.id, "stop_reason": run.stop_reason},
                    )
                    return
                if run.status in {RunStatus.FAILED, RunStatus.BLOCKED}:
                    record_campaign_event(
                        db,
                        execution.id,
                        severity="error",
                        code="child_failed",
                        phase="child",
                        message=run.message or "Campaign child failed.",
                        child_snapshot_id=child.id,
                        metadata={"child_run_id": run.id, "stop_reason": run.stop_reason},
                    )
                    continue
                record_campaign_event(
                    db,
                    execution.id,
                    severity="warning"
                    if run.status == RunStatus.COMPLETED_WITH_ERRORS
                    else "info",
                    code="child_completed_with_errors"
                    if run.status == RunStatus.COMPLETED_WITH_ERRORS
                    else "child_completed",
                    phase="child",
                    message=run.message or "Campaign child completed.",
                    child_snapshot_id=child.id,
                    metadata={"child_run_id": run.id, "status": run.status.value},
                )
    except Exception as exc:
        LOGGER.exception("campaign_worker_failed execution_id=%s", execution_id)
        with session_factory() as db:
            execution = db.get(CampaignExecution, execution_id)
            if execution:
                failure_message = (
                    f"Campaign collector failed unexpectedly: {type(exc).__name__}: {exc}"
                )
                child = (
                    db.get(CampaignExecutionChildSnapshot, execution.current_child_id)
                    if execution.current_child_id is not None
                    else None
                )
                run = (
                    db.get(SearchRun, child.child_run_id)
                    if child is not None and child.child_run_id is not None
                    else None
                )
                if child is not None and child.status == RunStatus.RUNNING:
                    if run is not None and run.status in {
                        RunStatus.PENDING,
                        RunStatus.RUNNING,
                    }:
                        run.stop_reason = "worker_exception"
                        record_run_event(
                            db,
                            run.id,
                            severity=EventSeverity.ERROR,
                            code="collector_failed",
                            phase="collector",
                            message=failure_message,
                            metadata={"exception_type": type(exc).__name__},
                        )
                        mark_run(db, run.id, RunStatus.FAILED, failure_message)
                        sync_child_snapshot_from_run(db, child, run)
                    else:
                        child.status = RunStatus.FAILED
                        child.stop_reason = "worker_exception"
                        db.commit()
                    record_campaign_event(
                        db,
                        execution.id,
                        severity="error",
                        code="child_failed",
                        phase="child",
                        message=failure_message,
                        child_snapshot_id=child.id,
                        metadata={
                            "child_run_id": run.id if run is not None else None,
                            "exception_type": type(exc).__name__,
                            "stop_reason": "worker_exception",
                        },
                    )
                    refresh_campaign_execution_aggregates(db, execution.id)
                execution.status = RunStatus.FAILED
                execution.stop_reason = "worker_exception"
                execution.message = failure_message
                execution.finished_at = datetime.now(UTC)
                db.commit()
                record_campaign_event(
                    db,
                    execution.id,
                    severity="error",
                    code="failed",
                    phase="worker",
                    message=execution.message,
                    metadata={
                        "exception_type": type(exc).__name__,
                        "stop_reason": "worker_exception",
                    },
                )
    finally:
        with session_factory() as db:
            execution = db.get(CampaignExecution, execution_id)
            if execution:
                record_campaign_event(
                    db,
                    execution.id,
                    severity="info",
                    code="profile_released",
                    phase="lock",
                    message="Campaign source profile ownership released.",
                )
        _release_source_profile(owner)
        _campaign_guard_release(execution_id)


def _queue_campaign_execution(db: Session, execution: CampaignExecution) -> None:
    if execution.status in {
        RunStatus.COMPLETED,
        RunStatus.COMPLETED_WITH_ERRORS,
        RunStatus.FAILED,
        RunStatus.INTERRUPTED,
    }:
        return
    if not _campaign_guard_acquire(execution.id):
        execution.message = "Campaign execution is already queued or running."
        db.commit()
        return
    child = _campaign_next_collectable_child(db, execution)
    if child is None:
        refresh_campaign_execution_aggregates(db, execution.id)
        _campaign_guard_release(execution.id)
        return
    try:
        _require_supported_collection(child.source)
        registration = get_source_registration(child.source)
    except UnsupportedSourceError as exc:
        child.status = RunStatus.FAILED
        child.stop_reason = "unsupported_source"
        execution.status = RunStatus.FAILED
        execution.stop_reason = "unsupported_source"
        execution.message = str(exc)
        execution.finished_at = datetime.now(UTC)
        db.commit()
        refresh_campaign_execution_aggregates(db, execution.id)
        record_campaign_event(
            db,
            execution.id,
            severity="error",
            code="failed",
            phase="validation",
            message=str(exc),
            child_snapshot_id=child.id,
            metadata={"source": child.source, "stop_reason": "unsupported_source"},
        )
        _campaign_guard_release(execution.id)
        return
    owner = f"campaign:{execution.id}"
    if (
        registration.capabilities.requires_persistent_browser_profile
        and not _try_acquire_source_profile(owner)
    ):
        execution.status = RunStatus.PENDING
        execution.current_child_id = child.id
        execution.message = (
            "Waiting for source session/profile to be released. Click I finished signing in "
            "or close the preparation browser."
        )
        db.commit()
        _campaign_guard_release(execution.id)
        return
    execution.status = RunStatus.PENDING
    execution.current_child_id = child.id
    execution.message = "Campaign execution queued."
    db.commit()
    record_campaign_event(
        db,
        execution.id,
        severity="info",
        code="queued",
        phase="queue",
        message=execution.message,
        child_snapshot_id=child.id,
        metadata={"source": child.source},
    )
    _submit_background(
        f"campaign execution {execution.id}",
        _run_campaign_execution,
        execution.id,
    )


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


def _latest_evaluations_for_jobs(
    db: Session, jobs: list[Job], profile: RuleProfile | None = None
) -> dict[int, JobRuleEvaluation]:
    job_ids = [job.id for job in jobs]
    if not job_ids:
        return {}
    query = select(JobRuleEvaluation).where(JobRuleEvaluation.job_id.in_(job_ids))
    if profile is not None:
        query = query.where(
            JobRuleEvaluation.profile_id == profile.id,
            JobRuleEvaluation.profile_version == profile.version,
        )
    evaluations = db.scalars(
        query.order_by(desc(JobRuleEvaluation.evaluated_at), desc(JobRuleEvaluation.id))
    ).all()
    latest: dict[int, JobRuleEvaluation] = {}
    for evaluation in evaluations:
        latest.setdefault(evaluation.job_id, evaluation)
    return latest


def _rule_context(
    job: Job, evaluation: JobRuleEvaluation | None, profile: RuleProfile | None
) -> dict:
    state = evaluation_state(job, evaluation, profile) if profile else "unavailable"
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
    selected_profile = _profile_context(request)["selected_profile"]
    latest = _latest_evaluations_for_jobs(db, jobs, selected_profile)
    params = request.query_params

    rows: list[tuple[Job, dict]] = []
    for job in jobs:
        context = _rule_context(job, latest.get(job.id), selected_profile)
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
            "rule_profile_fingerprint",
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
                evaluation.profile_fingerprint if evaluation else "",
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


def _campaign_execution_csv_response(
    execution: CampaignExecution,
    snapshots: list[CampaignExecutionChildSnapshot],
    db: Session,
) -> StreamingResponse:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "campaign_id",
            "campaign_name",
            "execution_id",
            "child_order",
            "child_saved_search",
            "child_query",
            "child_run_id",
            "source",
            "discovered_at",
            "job_id",
            "job_url",
            "canonical_url",
            "source_listing_url",
            "title",
            "company",
            "location",
            "crm_status",
            "priority",
            "is_favorite",
            "rule_score",
            "rule_outcome",
            "rule_effective_recommendation",
            "rule_profile",
            "rule_version",
            "rule_evaluated_at",
        ]
    )
    snapshots_by_run = {
        snapshot.child_run_id: snapshot for snapshot in snapshots if snapshot.child_run_id
    }
    run_ids = list(snapshots_by_run)
    if run_ids:
        discoveries = db.scalars(
            select(JobDiscovery)
            .where(JobDiscovery.run_id.in_(run_ids))
            .options(selectinload(JobDiscovery.job))
            .order_by(JobDiscovery.run_id, JobDiscovery.page_number, JobDiscovery.rank)
        ).all()
    else:
        discoveries = []
    jobs = [discovery.job for discovery in discoveries if discovery.job]
    profile = get_profile_or_default()
    latest = _latest_evaluations_for_jobs(db, jobs, profile)
    for discovery in discoveries:
        snapshot = snapshots_by_run.get(discovery.run_id)
        job = discovery.job
        if snapshot is None or job is None:
            continue
        evaluation = latest.get(job.id)
        writer.writerow(
            [
                execution.campaign_id,
                execution.campaign_name_snapshot,
                execution.id,
                snapshot.position,
                snapshot.saved_search_name_snapshot,
                snapshot.query_text_snapshot,
                snapshot.child_run_id,
                snapshot.source,
                discovery.found_at,
                job.id,
                best_job_url(job),
                job.canonical_url or canonicalize_url(job.url),
                job.source_listing_url or job.url,
                job.title,
                job.company,
                job.location,
                crm_status_label(job.crm_status),
                priority_label(job.priority),
                "yes" if job.is_favorite else "no",
                evaluation.score if evaluation else "",
                evaluation.outcome if evaluation else "",
                effective_recommendation(evaluation) or "",
                evaluation.profile_id if evaluation else "",
                evaluation.profile_version if evaluation else "",
                evaluation.evaluated_at if evaluation else "",
            ]
        )
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={
            "Content-Disposition": (
                f"attachment; filename=campaign-execution-{execution.id}.csv"
            )
        },
    )


@app.get("/", response_class=HTMLResponse)
def index(request: Request, db: Session = DB_DEP) -> HTMLResponse:
    profile_context = _profile_context(request)
    selected_profile = profile_context["selected_profile"]
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
    active_campaign_count = (
        db.scalar(
            select(func.count(Campaign.id)).where(
                Campaign.is_archived.is_(False), Campaign.is_active.is_(True)
            )
        )
        or 0
    )
    saved_search_count = (
        db.scalar(
            select(func.count(SavedSearch.id)).where(SavedSearch.is_archived.is_(False))
        )
        or 0
    )
    latest_campaign_execution = db.scalar(
        select(CampaignExecution)
        .order_by(desc(CampaignExecution.created_at), desc(CampaignExecution.id))
        .limit(1)
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
    latest = _latest_evaluations_for_jobs(db, all_jobs, selected_profile)
    rule_counts = {
        "strong": 0,
        "review": 0,
        "exclude": 0,
        "unevaluated": 0,
        "stale": 0,
    }
    for job in all_jobs:
        context = _rule_context(job, latest.get(job.id), selected_profile)
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
            "active_campaign_count": active_campaign_count,
            "saved_search_count": saved_search_count,
            "latest_campaign_execution": latest_campaign_execution,
            "new_unreviewed_count": new_unreviewed_count,
            "shortlisted_count": shortlisted_count,
            "applications_in_progress_count": applications_in_progress_count,
            "favorites_count": favorites_count,
            "follow_ups_due_count": follow_ups_due_count,
            "rule_counts": rule_counts,
            "profile_query": _profile_query(selected_profile),
            **profile_context,
            "date_options": DATE_LISTED_TO_DAYS.keys(),
            "source_options": get_enabled_source_options(),
            "seek_session_status": seek_session_status,
        },
    )


def _campaign_form_context(
    request: Request,
    *,
    campaign: Campaign | None = None,
    error: str | None = None,
) -> dict:
    return {
        "request": request,
        "campaign": campaign,
        "error": error,
    }


def _campaign_detail_context(
    request: Request,
    db: Session,
    campaign: Campaign,
    *,
    error: str | None = None,
    success: str | None = None,
) -> dict:
    campaign = db.scalar(
        select(Campaign)
        .where(Campaign.id == campaign.id)
        .options(
            selectinload(Campaign.memberships).selectinload(CampaignSavedSearch.saved_search),
            selectinload(Campaign.executions),
        )
    ) or campaign
    memberships = sorted(campaign.memberships, key=lambda item: item.position)
    member_ids = {membership.saved_search_id for membership in memberships}
    available_saved_searches = db.scalars(
        select(SavedSearch)
        .where(SavedSearch.is_archived.is_(False))
        .where(SavedSearch.id.not_in(member_ids) if member_ids else SavedSearch.id.is_not(None))
        .order_by(SavedSearch.name)
    ).all()
    executions = db.scalars(
        select(CampaignExecution)
        .where(CampaignExecution.campaign_id == campaign.id)
        .order_by(desc(CampaignExecution.created_at), desc(CampaignExecution.id))
    ).all()
    return {
        "request": request,
        "campaign": campaign,
        "memberships": memberships,
        "available_saved_searches": available_saved_searches,
        "executions": executions,
        "plan": build_campaign_plan(db, campaign),
        "source_options": get_enabled_source_options(),
        "date_window_options": DATE_WINDOW_OPTIONS,
        "error": error,
        "success": success,
    }


def _get_campaign_or_404(db: Session, campaign_id: int) -> Campaign:
    campaign = db.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return campaign


def _get_membership_or_404(db: Session, membership_id: int) -> CampaignSavedSearch:
    membership = db.scalar(
        select(CampaignSavedSearch)
        .where(CampaignSavedSearch.id == membership_id)
        .options(selectinload(CampaignSavedSearch.campaign))
    )
    if membership is None:
        raise HTTPException(status_code=404, detail="Campaign membership not found")
    return membership


@app.get("/campaigns", response_class=HTMLResponse)
def campaigns_index(request: Request, db: Session = DB_DEP) -> HTMLResponse:
    campaigns = db.scalars(
        select(Campaign).order_by(Campaign.is_archived, asc(Campaign.name))
    ).all()
    return templates.TemplateResponse(
        request,
        "campaigns.html",
        {
            "campaigns": campaigns,
            "saved_search_count": db.scalar(select(func.count(SavedSearch.id))) or 0,
        },
    )


@app.get("/campaigns/new", response_class=HTMLResponse)
def new_campaign(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "campaign_form.html", _campaign_form_context(request)
    )


@app.post("/campaigns")
async def create_campaign_route(request: Request, db: Session = DB_DEP):
    form = await request.form()
    try:
        campaign = create_campaign(
            db,
            name=str(form.get("name", "")),
            description=str(form.get("description", "")),
        )
    except CampaignValidationError as exc:
        return templates.TemplateResponse(
            request,
            "campaign_form.html",
            _campaign_form_context(request, error=str(exc)),
            status_code=400,
        )
    return _redirect(f"/campaigns/{campaign.id}")


@app.get("/campaigns/{campaign_id}", response_class=HTMLResponse)
def campaign_detail(campaign_id: int, request: Request, db: Session = DB_DEP) -> HTMLResponse:
    campaign = _get_campaign_or_404(db, campaign_id)
    return templates.TemplateResponse(
        request,
        "campaign_detail.html",
        _campaign_detail_context(
            request,
            db,
            campaign,
            success=request.query_params.get("success"),
            error=request.query_params.get("error"),
        ),
    )


@app.get("/campaigns/{campaign_id}/edit", response_class=HTMLResponse)
def edit_campaign(campaign_id: int, request: Request, db: Session = DB_DEP) -> HTMLResponse:
    campaign = _get_campaign_or_404(db, campaign_id)
    return templates.TemplateResponse(
        request,
        "campaign_form.html",
        _campaign_form_context(request, campaign=campaign),
    )


@app.post("/campaigns/{campaign_id}/edit")
async def update_campaign_route(campaign_id: int, request: Request, db: Session = DB_DEP):
    campaign = _get_campaign_or_404(db, campaign_id)
    form = await request.form()
    try:
        update_campaign(
            db,
            campaign,
            name=str(form.get("name", "")),
            description=str(form.get("description", "")),
            is_active=form.get("is_active") == "yes",
        )
    except CampaignValidationError as exc:
        return templates.TemplateResponse(
            request,
            "campaign_form.html",
            _campaign_form_context(request, campaign=campaign, error=str(exc)),
            status_code=400,
        )
    return _redirect(f"/campaigns/{campaign.id}?success=Campaign+updated")


@app.post("/campaigns/{campaign_id}/archive")
def archive_campaign_route(campaign_id: int, db: Session = DB_DEP):
    campaign = _get_campaign_or_404(db, campaign_id)
    archive_campaign(db, campaign)
    return _redirect("/campaigns?success=Campaign+archived")


@app.post("/campaigns/{campaign_id}/saved-searches")
async def create_saved_search_for_campaign(
    campaign_id: int, request: Request, db: Session = DB_DEP
):
    campaign = _get_campaign_or_404(db, campaign_id)
    form = await request.form()
    try:
        max_pages = int(str(form.get("max_pages", "1")))
        saved_search = create_saved_search(
            db,
            name=str(form.get("name", "")),
            source=str(form.get("source", "seek")),
            query_text=str(form.get("query_text", "")),
            location=str(form.get("location", "")),
            date_window=str(form.get("date_window", "")),
            max_pages=max_pages,
            is_enabled=form.get("is_enabled", "yes") == "yes",
        )
        add_campaign_membership(db, campaign, saved_search)
    except (CampaignValidationError, ValueError) as exc:
        return templates.TemplateResponse(
            request,
            "campaign_detail.html",
            _campaign_detail_context(request, db, campaign, error=str(exc)),
            status_code=400,
        )
    return _redirect(f"/campaigns/{campaign.id}?success=Saved+search+added")


@app.post("/campaigns/{campaign_id}/memberships")
async def add_membership_route(campaign_id: int, request: Request, db: Session = DB_DEP):
    campaign = _get_campaign_or_404(db, campaign_id)
    form = await request.form()
    saved_search = db.get(SavedSearch, int(str(form.get("saved_search_id", "0")) or 0))
    if saved_search is None:
        raise HTTPException(status_code=400, detail="Saved search not found")
    try:
        add_campaign_membership(db, campaign, saved_search)
    except CampaignValidationError as exc:
        return templates.TemplateResponse(
            request,
            "campaign_detail.html",
            _campaign_detail_context(request, db, campaign, error=str(exc)),
            status_code=400,
        )
    return _redirect(f"/campaigns/{campaign.id}?success=Saved+search+linked")


@app.post("/campaign-memberships/{membership_id}/update")
async def update_membership_route(membership_id: int, request: Request, db: Session = DB_DEP):
    membership = _get_membership_or_404(db, membership_id)
    form = await request.form()
    try:
        update_membership(
            db,
            membership,
            position=int(str(form.get("position", membership.position))),
            is_enabled=form.get("is_enabled") == "yes",
        )
    except (CampaignValidationError, ValueError) as exc:
        return _redirect(f"/campaigns/{membership.campaign_id}?error={str(exc)}")
    return _redirect(f"/campaigns/{membership.campaign_id}?success=Membership+updated")


@app.post("/campaign-memberships/{membership_id}/remove")
def remove_membership_route(membership_id: int, db: Session = DB_DEP):
    membership = _get_membership_or_404(db, membership_id)
    campaign_id = membership.campaign_id
    remove_membership(db, membership)
    return _redirect(f"/campaigns/{campaign_id}?success=Membership+removed")


@app.post("/saved-searches/{saved_search_id}/update")
async def update_saved_search_route(
    saved_search_id: int, request: Request, db: Session = DB_DEP
):
    saved_search = db.get(SavedSearch, saved_search_id)
    if saved_search is None:
        raise HTTPException(status_code=404, detail="Saved search not found")
    form = await request.form()
    campaign_id = str(form.get("campaign_id", "")).strip()
    redirect_to = f"/campaigns/{campaign_id}" if campaign_id else "/campaigns"
    try:
        update_saved_search(
            db,
            saved_search,
            name=str(form.get("name", "")),
            source=str(form.get("source", "seek")),
            query_text=str(form.get("query_text", "")),
            location=str(form.get("location", "")),
            date_window=str(form.get("date_window", "")),
            max_pages=int(str(form.get("max_pages", saved_search.max_pages))),
            is_enabled=form.get("is_enabled") == "yes",
            is_archived=form.get("is_archived") == "yes",
        )
    except (CampaignValidationError, ValueError) as exc:
        return _redirect(f"{redirect_to}?error={str(exc)}")
    return _redirect(f"{redirect_to}?success=Saved+search+updated")


@app.get("/campaigns/{campaign_id}/preview", response_class=HTMLResponse)
def campaign_preview(campaign_id: int, request: Request, db: Session = DB_DEP) -> HTMLResponse:
    campaign = _get_campaign_or_404(db, campaign_id)
    plan = build_campaign_plan(db, campaign)
    return templates.TemplateResponse(
        request,
        "campaign_preview.html",
        {"request": request, "campaign": campaign, "plan": plan},
    )


@app.post("/campaigns/{campaign_id}/executions")
def create_campaign_execution_route(campaign_id: int, db: Session = DB_DEP):
    campaign = _get_campaign_or_404(db, campaign_id)
    try:
        execution = create_campaign_execution_plan(db, campaign)
    except CampaignValidationError as exc:
        return _redirect(f"/campaigns/{campaign.id}/preview?error={str(exc)}")
    return _redirect(f"/campaign-executions/{execution.id}")


@app.post("/campaign-executions/{execution_id}/start")
def start_campaign_execution_route(execution_id: int, db: Session = DB_DEP):
    execution = db.get(CampaignExecution, execution_id)
    if execution is None:
        raise HTTPException(status_code=404, detail="Campaign execution not found")
    if execution.status not in {RunStatus.PENDING, RunStatus.AWAITING_USER}:
        execution.message = "Only pending or awaiting-user campaign executions can be started."
        db.commit()
        return _redirect(f"/campaign-executions/{execution.id}?error={execution.message}")
    _queue_campaign_execution(db, execution)
    return _redirect(f"/campaign-executions/{execution.id}")


@app.post("/campaign-executions/{execution_id}/resume")
def resume_campaign_execution_route(execution_id: int, db: Session = DB_DEP):
    execution = db.get(CampaignExecution, execution_id)
    if execution is None:
        raise HTTPException(status_code=404, detail="Campaign execution not found")
    if execution.status in {RunStatus.PENDING, RunStatus.RUNNING}:
        execution.message = "Campaign execution is already queued or running."
        db.commit()
        return _redirect(f"/campaign-executions/{execution.id}?error={execution.message}")
    if execution.status != RunStatus.AWAITING_USER:
        execution.message = "Only awaiting-user campaign executions can be resumed."
        db.commit()
        return _redirect(f"/campaign-executions/{execution.id}?error={execution.message}")
    record_campaign_event(
        db,
        execution.id,
        severity="info",
        code="resume_queued",
        phase="queue",
        message="Campaign resume queued.",
        child_snapshot_id=execution.current_child_id,
    )
    _queue_campaign_execution(db, execution)
    return _redirect(f"/campaign-executions/{execution.id}")


@app.get("/campaign-executions/{execution_id}", response_class=HTMLResponse)
def campaign_execution_detail(
    execution_id: int, request: Request, db: Session = DB_DEP
) -> HTMLResponse:
    execution = db.scalar(
        select(CampaignExecution)
        .where(CampaignExecution.id == execution_id)
        .options(
            selectinload(CampaignExecution.child_snapshots),
            selectinload(CampaignExecution.events),
        )
    )
    if execution is None:
        raise HTTPException(status_code=404, detail="Campaign execution not found")
    snapshots = db.scalars(
        select(CampaignExecutionChildSnapshot)
        .where(CampaignExecutionChildSnapshot.campaign_execution_id == execution.id)
        .order_by(CampaignExecutionChildSnapshot.position)
    ).all()
    return templates.TemplateResponse(
        request,
        "campaign_execution.html",
        {
            "request": request,
            "execution": execution,
            "snapshots": snapshots,
            "events": sorted(execution.events, key=lambda item: (item.created_at, item.id)),
            "error": request.query_params.get("error"),
        },
    )


@app.get("/campaign-executions/{execution_id}/export.csv")
def export_campaign_execution(execution_id: int, db: Session = DB_DEP) -> StreamingResponse:
    execution = db.get(CampaignExecution, execution_id)
    if execution is None:
        raise HTTPException(status_code=404, detail="Campaign execution not found")
    snapshots = ordered_child_snapshots(db, execution.id)
    return _campaign_execution_csv_response(execution, snapshots, db)


@app.get("/jobs", response_class=HTMLResponse)
def jobs_index(request: Request, db: Session = DB_DEP) -> HTMLResponse:
    profile_context = _profile_context(request)
    selected_profile = profile_context["selected_profile"]
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
            "rule_profile_id": selected_profile.id if selected_profile else PROFILE_ID,
            "rule_profile_version": selected_profile.version
            if selected_profile
            else PROFILE_VERSION,
            "profile_query": _profile_query(selected_profile),
            **profile_context,
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
    try:
        profile = get_profile_by_key(str(form.get("profile_key", "")).strip() or None)
    except ProfileSelectionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    jobs = db.scalars(select(Job).order_by(desc(Job.first_seen_at), desc(Job.id))).all()
    latest = _latest_evaluations_for_jobs(db, jobs, profile)
    evaluated = 0
    for job in jobs:
        state = evaluation_state(job, latest.get(job.id), profile)
        if mode == "unevaluated" and state != "unevaluated":
            continue
        if mode == "stale" and state != "stale":
            continue
        before = latest_evaluation(db, job.id, profile)
        try:
            after = evaluate_and_store_job(db, job, profile)
        except ProfileSelectionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if before is None or before.id != after.id or mode == "all":
            evaluated += 1
    query = _profile_query(profile)
    suffix = f"&{query}" if query else ""
    return _redirect(f"/jobs?success={evaluated}+job+rule+assessment(s)+updated{suffix}")


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
async def evaluate_job_route(job_id: int, request: Request, db: Session = DB_DEP):
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    form = await request.form()
    try:
        profile = get_profile_by_key(str(form.get("profile_key", "")).strip() or None)
        evaluate_and_store_job(db, job, profile)
    except ProfileSelectionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    query = _profile_query(profile)
    suffix = f"&{query}" if query else ""
    return _redirect(f"/jobs/{job.id}?success=Rule+assessment+updated{suffix}")


@app.post("/jobs/{job_id}/recommendation-override", response_class=HTMLResponse)
async def update_recommendation_override_route(job_id: int, request: Request, db: Session = DB_DEP):
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    form = await request.form()
    raw_value = str(form.get("recommendation_override", "")).strip()
    override = raw_value or None
    try:
        profile = get_profile_by_key(str(form.get("profile_key", "")).strip() or None)
        set_recommendation_override(db, job, override, profile)
    except ValueError:
        return templates.TemplateResponse(
            request,
            "job.html",
            _job_detail_context(request, job, db, error="Invalid recommendation override."),
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    except ProfileSelectionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    query = _profile_query(profile)
    suffix = f"&{query}" if query else ""
    return _redirect(f"/jobs/{job.id}?success=Manual+recommendation+override+saved{suffix}")


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
    child_snapshots = (
        db.scalars(
            select(CampaignExecutionChildSnapshot).where(
                CampaignExecutionChildSnapshot.child_run_id.in_([run.id for run in runs])
            )
        ).all()
        if runs
        else []
    )
    campaign_child_by_run = {
        snapshot.child_run_id: snapshot for snapshot in child_snapshots if snapshot.child_run_id
    }
    return templates.TemplateResponse(
        request,
        "runs.html",
        {"runs": runs, "campaign_child_by_run": campaign_child_by_run},
    )


@app.post("/runs")
async def start_run(request: Request, db: Session = DB_DEP):
    form = await request.form()
    source_identifier = str(form.get("source", SourceIdentifier.SEEK.value)).strip()
    try:
        registration = get_source_registration(source_identifier)
        _require_supported_collection(source_identifier)
    except UnsupportedSourceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    keywords = str(form.get("keywords", "")).strip()
    location = str(form.get("location", "")).strip()
    date_listed = str(form.get("date_listed", "")).strip()
    maximum_pages_raw = form.get("maximum_pages", form.get("max_pages"))
    if registration.capabilities.supports_keyword_query and not keywords:
        raise HTTPException(status_code=400, detail="Keywords are required")
    if registration.capabilities.supports_location and not location:
        raise HTTPException(status_code=400, detail="Location is required")
    if (
        registration.source_identifier == SourceIdentifier.SEEK
        and date_listed not in DATE_LISTED_TO_DAYS
    ):
        raise HTTPException(status_code=400, detail="Unsupported date-listed value")
    try:
        maximum_pages = int(str(maximum_pages_raw))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Maximum pages must be an integer") from exc
    if maximum_pages < 1 or maximum_pages > 10:
        raise HTTPException(status_code=400, detail="Maximum pages must be between 1 and 10")
    run = create_search_and_run(
        db,
        keywords,
        location,
        date_listed,
        maximum_pages,
        source_identifier=registration.source_identifier.value,
    )
    _queue_or_wait_for_profile(
        db,
        CollectorInput(
            keywords=keywords,
            location=location,
            date_listed=date_listed,
            max_pages=maximum_pages,
            run_id=run.id,
            source_identifier=registration.source_identifier.value,
        ),
    )
    return HTMLResponse(
        "<meta http-equiv='refresh' content='0; url=/'><p>Collector started. Return to /.</p>"
    )


@app.post("/seek-session/open")
def open_seek_session():
    global seek_session_status
    if _source_profile_busy_for("seek-session"):
        seek_session_status = SessionReadiness(
            is_open=False,
            is_signed_in=False,
            message=(
                "A source collection owns the persistent profile. Wait for it to finish "
                "or pause before opening the SEEK preparation browser."
            ),
        )
        return HTMLResponse(
            "<meta http-equiv='refresh' content='0; url=/'><p>Source session is busy.</p>"
        )
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
    try:
        _require_supported_collection(collector_input.source_identifier)
    except UnsupportedSourceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
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
    campaign_child = db.scalar(
        select(CampaignExecutionChildSnapshot).where(
            CampaignExecutionChildSnapshot.child_run_id == run.id
        )
    )
    return templates.TemplateResponse(
        request,
        "run.html",
        {
            "run": run,
            "campaign_child": campaign_child,
            "discoveries": discoveries,
            "events": events,
            "errors": errors,
            "warnings": warnings,
        },
    )


@app.get("/export/jobs.csv")
def export_jobs(db: Session = DB_DEP) -> StreamingResponse:
    jobs = db.scalars(select(Job).order_by(desc(Job.first_seen_at), desc(Job.id))).all()
    profile = get_profile_or_default()
    latest = _latest_evaluations_for_jobs(db, jobs, profile)
    for job in jobs:
        evaluation = latest.get(job.id)
        job._rule_evaluation = evaluation
        job._rule_evaluation_state = evaluation_state(job, evaluation, profile)
    return _jobs_csv_response(jobs, "jobs.csv")
