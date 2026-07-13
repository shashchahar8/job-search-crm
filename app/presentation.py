from datetime import UTC, datetime, timedelta

from app.models import RunStatus, SearchRun

STATUS_LABELS = {
    RunStatus.PENDING: ("Pending", "pending"),
    RunStatus.RUNNING: ("Running", "running"),
    RunStatus.COMPLETED: ("Completed", "success"),
    RunStatus.COMPLETED_WITH_ERRORS: ("Completed with warnings", "warning"),
    RunStatus.AWAITING_USER: ("Action required", "action-required"),
    RunStatus.INTERRUPTED: ("Interrupted", "warning"),
    RunStatus.BLOCKED: ("Blocked", "failed"),
    RunStatus.FAILED: ("Failed", "failed"),
}

STOP_REASON_LABELS = {
    "requested_page_limit_reached": "Stopped because the requested page limit was reached.",
    "no_next_page": "Stopped because no next page was available.",
    "no_results": "Stopped because no results were found.",
    "no_new_ids": "Stopped because no new job identifiers were found.",
    "verification_required": "Stopped because SEEK requested verification.",
    "login_required": "Stopped because SEEK required login.",
    "browser_closed": "Stopped because the browser was closed.",
    "cancelled": "Stopped because the run was cancelled.",
    "failed": "Stopped because collection failed.",
}


def status_label(status: RunStatus | str) -> str:
    if isinstance(status, str):
        status = RunStatus(status)
    return STATUS_LABELS[status][0]


def status_tone(status: RunStatus | str) -> str:
    if isinstance(status, str):
        status = RunStatus(status)
    return STATUS_LABELS[status][1]


def stop_reason_label(stop_reason: str | None) -> str:
    if stop_reason is None:
        return "Not recorded for this legacy run."
    return STOP_REASON_LABELS.get(stop_reason, stop_reason.replace("_", " ").capitalize())


def format_datetime(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.replace(microsecond=0).isoformat(sep=" ")


def format_duration(started_at: datetime | None, finished_at: datetime | None) -> str:
    if started_at is None or finished_at is None:
        return ""
    seconds = max(0, int((finished_at - started_at).total_seconds()))
    minutes, remaining_seconds = divmod(seconds, 60)
    if minutes:
        return f"{minutes}m {remaining_seconds}s"
    return f"{remaining_seconds}s"


def metric_value(value: int | None) -> str:
    return str(value) if value is not None else "Not recorded for this legacy run"


def run_outcome_text(run: SearchRun) -> str:
    if run.result_cards_observed is None and run.unique_jobs_in_run is None:
        pages_completed = (
            run.pages_completed if run.pages_completed is not None else run.pages_attempted
        )
        return (
            "Detailed card metrics were not recorded for this legacy run. "
            f"It saved {run.jobs_found} job records across {pages_completed} pages."
        )
    cards = metric_value(run.result_cards_observed)
    unique = metric_value(run.unique_jobs_in_run)
    new = metric_value(run.new_jobs_added)
    known = metric_value(run.known_jobs_rediscovered)
    duplicates = metric_value(run.duplicate_cards_ignored)
    pages_completed = (
        run.pages_completed if run.pages_completed is not None else run.pages_attempted
    )
    summary = (
        f"{cards} result cards processed across {pages_completed} pages. "
        f"{unique} unique jobs found. {new} were new to your CRM. "
        f"{known} were already known. {duplicates} duplicate results were ignored."
    )
    if run.stop_reason == "requested_page_limit_reached":
        return f"{summary} More SEEK results may be available."
    return summary


def recently_threshold() -> datetime:
    return datetime.now(UTC) - timedelta(hours=24)
