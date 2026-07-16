import hashlib
import logging
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote_plus, urljoin

from bs4 import BeautifulSoup
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright
from sqlalchemy.orm import Session

from app.collectors.base import (
    AccessChallengeError,
    Collector,
    CollectorInput,
    LayoutError,
    LoginRequiredError,
)
from app.config import Settings
from app.models import RunStatus, SearchRun
from app.repository import (
    EventSeverity,
    canonicalize_url,
    increment_run_metric,
    mark_run,
    record_run_event,
    save_job_discovery,
    set_run_stop_reason,
)

LOGGER = logging.getLogger(__name__)

SEEK_BASE_URL = "https://www.seek.com.au"
DATE_LISTED_TO_DAYS = {
    "any": None,
    "today": 1,
    "previous_24_hours": 1,
    "last_1_day": 1,
    "last_2_days": 2,
    "last_3_days": 3,
    "last_7_days": 7,
    "last_14_days": 14,
    "last_30_days": 30,
}


@dataclass(frozen=True)
class ListingJob:
    seek_job_id: str | None
    title: str
    company: str | None
    location: str | None
    salary: str | None
    work_type: str | None
    posting_date: str | None
    url: str
    card_type: str = "unknown"
    parser_path: str = "unknown"
    rank: int | None = None


@dataclass(frozen=True)
class ChallengeDetection:
    message: str
    rule: str
    matched: str
    url: str
    title: str | None
    page_kind: str


def _slug(value: str) -> str:
    value = re.sub(r"[^\w\s-]", " ", value.strip(), flags=re.UNICODE)
    value = re.sub(r"\s+", "-", value)
    return quote_plus(value.strip("-"))


def build_seek_search_url(keywords: str, location: str, date_listed: str, page: int = 1) -> str:
    if page < 1:
        raise ValueError("page must be >= 1")
    keyword_slug = _slug(keywords)
    location_slug = _slug(location)
    url = f"{SEEK_BASE_URL}/{keyword_slug}-jobs/in-{location_slug}"
    params: list[str] = []
    days = DATE_LISTED_TO_DAYS.get(date_listed)
    if days:
        params.append(f"daterange={days}")
    if page > 1:
        params.append(f"page={page}")
    if params:
        return f"{url}?{'&'.join(params)}"
    return url


def extract_seek_job_id(url: str, attrs: dict[str, Any] | None = None) -> str | None:
    attrs = attrs or {}
    for key in ("data-job-id", "data-jobid", "data-automation-job-id"):
        value = attrs.get(key)
        if value:
            return str(value)
    patterns = [
        r"/job/(\d+)",
        r"[?&]jobId=(\d+)",
        r"[?&]jobid=(\d+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


def fallback_key_for_job(title: str, company: str | None, location: str | None, url: str) -> str:
    seed = "|".join(
        [
            title.strip().lower(),
            (company or "").strip().lower(),
            (location or "").strip().lower(),
            re.sub(r"[?#].*$", "", url).strip().lower(),
        ]
    )
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = re.sub(r"\s+", " ", value).strip()
    return cleaned or None


def _first_text(node, selectors: list[str]) -> str | None:
    for selector in selectors:
        found = node.select_one(selector)
        if found:
            text = _clean_text(found.get_text(" "))
            if text:
                return text
    return None


def _card_type_from_attrs(attrs: dict[str, Any]) -> str:
    evidence = " ".join(str(value).lower() for value in attrs.values())
    if "normaljob" in evidence:
        return "normal"
    if "sponsored" in evidence or "promoted" in evidence:
        return "sponsored"
    if "recommended" in evidence:
        return "recommended"
    if "related" in evidence:
        return "related"
    return "unknown"


def parse_seek_listing_page(html: str) -> list[ListingJob]:
    soup = BeautifulSoup(html, "lxml")
    cards = soup.select('[data-automation="normalJob"], article[data-automation*="job"]')
    parser_path = "seek:data-automation-job-article"
    if not cards:
        cards = [
            link.find_parent(["article", "div"]) or link
            for link in soup.select('a[href*="/job/"]')
        ]
        parser_path = "seek:fallback-job-link-parent"

    jobs: list[ListingJob] = []
    seen_urls: set[str] = set()
    for rank, card in enumerate(cards, start=1):
        link = card.select_one('a[data-automation="jobTitle"][href], a[href*="/job/"][href]')
        if not link:
            continue
        url = urljoin(SEEK_BASE_URL, link.get("href", ""))
        if url in seen_urls:
            continue
        seen_urls.add(url)
        title = _clean_text(link.get_text(" ")) or _first_text(
            card, ['[data-automation="jobTitle"]']
        )
        if not title:
            continue

        text_chunks = [_clean_text(item.get_text(" ")) for item in card.select("span, div")]
        text_chunks = [item for item in text_chunks if item]
        salary = _first_text(card, ['[data-automation="jobSalary"]'])
        work_type = _first_text(card, ['[data-automation="jobWorkType"]'])
        posting_date = _first_text(card, ['[data-automation="jobListingDate"]', "time"])

        jobs.append(
            ListingJob(
                seek_job_id=extract_seek_job_id(url, dict(card.attrs)),
                title=title,
                company=_first_text(card, ['[data-automation="jobCompany"]']),
                location=_first_text(card, ['[data-automation="jobLocation"]']),
                salary=salary,
                work_type=work_type or next((t for t in text_chunks if "time" in t.lower()), None),
                posting_date=posting_date,
                url=url,
                card_type=_card_type_from_attrs(dict(card.attrs)),
                parser_path=parser_path,
                rank=rank,
            )
        )
    return jobs


def page_has_search_results(html: str) -> bool:
    return bool(parse_seek_listing_page(html))


def page_has_job_detail(html: str) -> bool:
    soup = BeautifulSoup(html, "lxml")
    return bool(
        soup.select_one('[data-automation="jobAdDetails"], [data-automation="jobDescription"]')
    )


def parse_seek_detail_page(html: str) -> dict[str, str | None]:
    soup = BeautifulSoup(html, "lxml")
    title = _first_text(soup, ["h1", '[data-automation="job-detail-title"]'])
    company = _first_text(
        soup, ['[data-automation="advertiser-name"]', '[data-automation="company"]']
    )
    location = _first_text(soup, ['[data-automation="job-detail-location"]'])
    salary = _first_text(soup, ['[data-automation="job-detail-salary"]'])
    work_type = _first_text(soup, ['[data-automation="job-detail-work-type"]'])
    posting_date = _first_text(soup, ['[data-automation="job-detail-date"]', "time"])
    description_node = soup.select_one(
        '[data-automation="jobAdDetails"], [data-automation="jobDescription"]'
    )
    description = _clean_text(description_node.get_text("\n")) if description_node else None
    if not description:
        raise LayoutError("Could not locate SEEK job description on detail page")
    return {
        "title": title,
        "company": company,
        "location": location,
        "salary": salary,
        "work_type": work_type,
        "posting_date": posting_date,
        "description": description,
    }


def detect_signed_in_session(html: str) -> bool:
    soup = BeautifulSoup(html, "lxml")
    selectors = [
        '[data-automation="profile-menu"]',
        '[data-automation="user-profile"]',
        '[data-automation="account-menu"]',
        'a[href*="/profile"]',
        'a[href*="/my-activity"]',
        'a[href*="/saved-jobs"]',
    ]
    if any(soup.select_one(selector) for selector in selectors):
        return True
    text = soup.get_text(" ", strip=True).lower()
    signed_in_markers = [
        "my activity",
        "saved jobs",
        "profile",
        "sign out",
        "logout",
    ]
    return any(marker in text for marker in signed_in_markers)


def detect_optional_signin_modal(html: str) -> bool:
    soup = BeautifulSoup(html, "lxml")
    selectors = [
        '[role="dialog"]',
        '[aria-modal="true"]',
        '[data-automation*="sign-in"]',
        '[data-automation*="signin"]',
        '[data-automation*="login"]',
    ]
    dialog_text = " ".join(
        node.get_text(" ", strip=True).lower() for node in soup.select(",".join(selectors))
    )
    return any(
        marker in dialog_text
        for marker in (
            "sign in",
            "sign-in",
            "log in",
            "login",
            "continue with",
            "create account",
        )
    )


def classify_page_kind(html: str) -> str:
    if page_has_search_results(html):
        return "results"
    if page_has_job_detail(html):
        return "job_detail"
    return "unknown"


def inspect_genuine_access_challenge(
    html: str, url: str, title: str | None = None
) -> ChallengeDetection | None:
    lower_url = url.lower()
    page_kind = classify_page_kind(html)
    soup = BeautifulSoup(html, "lxml")
    text = BeautifulSoup(html, "lxml").get_text(" ", strip=True).lower()
    headings = " ".join(
        node.get_text(" ", strip=True).lower() for node in soup.select("title,h1,h2")
    )
    url_markers = {
        "access-denied": "url:access-denied",
        "captcha": "url:captcha",
    }
    for marker, rule in url_markers.items():
        if marker in lower_url:
            return ChallengeDetection(
                message="SEEK displayed an access-denied or CAPTCHA page",
                rule=rule,
                matched=marker,
                url=url,
                title=title,
                page_kind=page_kind,
            )

    challenge_markers = {
        "captcha": "phrase:captcha",
        "verify you are human": "phrase:verify-you-are-human",
        "access denied": "phrase:access-denied",
        "unusual traffic": "phrase:unusual-traffic",
        "security check": "phrase:security-check",
        "are you a robot": "phrase:are-you-a-robot",
        "temporarily blocked": "phrase:temporarily-blocked",
        "too many requests": "phrase:too-many-requests",
    }
    for marker, rule in challenge_markers.items():
        if marker in headings or (page_kind == "unknown" and marker in text):
            return ChallengeDetection(
                message="SEEK displayed a verification or access challenge",
                rule=rule,
                matched=marker,
                url=url,
                title=title,
                page_kind=page_kind,
            )
    return None


def detect_genuine_access_challenge(html: str, url: str) -> str | None:
    detection = inspect_genuine_access_challenge(html, url)
    return detection.message if detection else None


def _format_challenge(detection: ChallengeDetection, phase: str) -> str:
    return (
        f"{detection.message} "
        f"(rule={detection.rule}; matched={detection.matched}; "
        f"phase={phase}; page_kind={detection.page_kind}; "
        f"title={detection.title or 'unknown'}; url={detection.url})"
    )


def is_browser_closed_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "target page, context or browser has been closed" in text or "targetclosed" in text


def detect_login_required(html: str, url: str) -> str | None:
    lower_url = url.lower()
    text = BeautifulSoup(html, "lxml").get_text(" ", strip=True).lower()
    if page_has_search_results(html):
        return None
    if "signin" in lower_url or "login" in lower_url:
        return "SEEK requires sign-in before this page can be collected"
    if (
        detect_optional_signin_modal(html)
        and not page_has_search_results(html)
        and ("sign in" in text or "log in" in text)
    ):
        return "SEEK is showing a sign-in prompt before results are available"
    return None


def validate_collectable_page(
    html: str, url: str, title: str | None = None, phase: str = "unknown"
) -> None:
    challenge = inspect_genuine_access_challenge(html, url, title)
    if challenge:
        raise AccessChallengeError(
            _format_challenge(challenge, phase),
            challenge_rule=challenge.rule,
            page_title=challenge.title,
            url=challenge.url,
            page_kind=challenge.page_kind,
        )
    login_required = detect_login_required(html, url)
    if login_required:
        raise LoginRequiredError(login_required)


class SeekCollector(Collector):
    def __init__(self, settings: Settings, session_factory: type[Session]) -> None:
        self.settings = settings
        self.session_factory = session_factory

    def collect(self, collector_input: CollectorInput) -> None:
        self.settings.playwright_profile_dir.mkdir(parents=True, exist_ok=True)
        with self.session_factory() as db:
            mark_run(db, collector_input.run_id, RunStatus.RUNNING, "Collector started")
            record_run_event(
                db,
                collector_input.run_id,
                severity=EventSeverity.INFO,
                code="collector_started",
                phase="collector",
                message="Collector started",
                metadata={"status": RunStatus.RUNNING.value},
            )

        had_errors = False
        try:
            with sync_playwright() as playwright:
                context = playwright.chromium.launch_persistent_context(
                    user_data_dir=str(self.settings.playwright_profile_dir),
                    headless=False,
                    args=[
                        "--disable-save-password-bubble",
                        "--disable-features=PasswordManagerOnboarding",
                    ],
                )
                page = context.new_page()
                try:
                    try:
                        for page_number in range(
                            collector_input.start_page, collector_input.max_pages + 1
                        ):
                            had_errors = (
                                self._collect_page(page, collector_input, page_number)
                                or had_errors
                            )
                    except AccessChallengeError as exc:
                        message = (
                            f"{exc}. Complete the visible browser challenge, then use Resume run."
                        )
                        with self.session_factory() as db:
                            set_run_stop_reason(
                                db, collector_input.run_id, "verification_required"
                            )
                            record_run_event(
                                db,
                                collector_input.run_id,
                                severity=EventSeverity.WARNING,
                                code="access_challenge",
                                phase="challenge",
                                message=str(exc),
                                url=exc.url,
                                page_title=exc.page_title,
                                challenge_rule=exc.challenge_rule,
                                metadata={"page_kind": exc.page_kind},
                            )
                            mark_run(
                                db,
                                collector_input.run_id,
                                RunStatus.AWAITING_USER,
                                message,
                            )
                        try:
                            page.wait_for_timeout(300_000)
                        except PlaywrightError as wait_exc:
                            if not is_browser_closed_error(wait_exc):
                                raise
                            LOGGER.info(
                                "seek_challenge_browser_closed run_id=%s message=%s",
                                collector_input.run_id,
                                wait_exc,
                            )
                            with self.session_factory() as db:
                                record_run_event(
                                    db,
                                    collector_input.run_id,
                                    severity=EventSeverity.INFO,
                                    code="manual_browser_closed",
                                    phase="challenge_wait",
                                    message=(
                                        "Visible browser was closed during manual challenge wait"
                                    ),
                                    metadata={
                                        "exception_type": type(wait_exc).__name__,
                                    },
                                )
                        return
                    except LoginRequiredError as exc:
                        with self.session_factory() as db:
                            set_run_stop_reason(db, collector_input.run_id, "login_required")
                            record_run_event(
                                db,
                                collector_input.run_id,
                                severity=EventSeverity.WARNING,
                                code="login_required",
                                phase="authentication",
                                message=str(exc),
                            )
                            mark_run(
                                db,
                                collector_input.run_id,
                                RunStatus.AWAITING_USER,
                                f"{exc}. Use Open/Prepare SEEK session, sign in manually, "
                                "then resume.",
                            )
                        return
                finally:
                    context.close()
        except KeyboardInterrupt:
            with self.session_factory() as db:
                set_run_stop_reason(db, collector_input.run_id, "cancelled")
                record_run_event(
                    db,
                    collector_input.run_id,
                    severity=EventSeverity.WARNING,
                    code="collector_interrupted",
                    phase="collector",
                    message="Collector interrupted",
                )
                mark_run(db, collector_input.run_id, RunStatus.INTERRUPTED, "Collector interrupted")
            raise
        except LayoutError as exc:
            with self.session_factory() as db:
                set_run_stop_reason(db, collector_input.run_id, "failed")
                record_run_event(
                    db,
                    collector_input.run_id,
                    severity=EventSeverity.ERROR,
                    code="layout_error",
                    phase="parsing",
                    message=str(exc),
                    metadata={"exception_type": type(exc).__name__},
                )
                mark_run(db, collector_input.run_id, RunStatus.BLOCKED, str(exc))
            return
        except (PlaywrightError, PlaywrightTimeoutError) as exc:
            with self.session_factory() as db:
                status = RunStatus.INTERRUPTED if is_browser_closed_error(exc) else RunStatus.FAILED
                set_run_stop_reason(
                    db,
                    collector_input.run_id,
                    "browser_closed" if status == RunStatus.INTERRUPTED else "failed",
                )
                message = (
                    "Visible browser was closed before collection completed"
                    if status == RunStatus.INTERRUPTED
                    else str(exc)
                )
                record_run_event(
                    db,
                    collector_input.run_id,
                    severity=EventSeverity.WARNING
                    if status == RunStatus.INTERRUPTED
                    else EventSeverity.ERROR,
                    code="manual_browser_closed"
                    if status == RunStatus.INTERRUPTED
                    else "browser_error",
                    phase="browser",
                    message=message,
                    metadata={"exception_type": type(exc).__name__},
                )
                mark_run(db, collector_input.run_id, status, message)
            return

        with self.session_factory() as db:
            final_status = RunStatus.COMPLETED_WITH_ERRORS if had_errors else RunStatus.COMPLETED
            run = db.get(SearchRun, collector_input.run_id)
            if run and not run.stop_reason:
                run.stop_reason = "requested_page_limit_reached"
            record_run_event(
                db,
                collector_input.run_id,
                severity=EventSeverity.INFO,
                code="collector_finished",
                phase="collector",
                message="Collector finished",
                metadata={"status": final_status.value},
            )
            mark_run(db, collector_input.run_id, final_status, "Collector finished")

    def _collect_page(self, page, collector_input: CollectorInput, page_number: int) -> bool:
        had_errors = False
        search_url = build_seek_search_url(
            collector_input.keywords,
            collector_input.location,
            collector_input.date_listed,
            page_number,
        )
        LOGGER.info(
            "collecting_seek_page run_id=%s page=%s url=%s",
            collector_input.run_id,
            page_number,
            search_url,
        )
        with self.session_factory() as db:
            record_run_event(
                db,
                collector_input.run_id,
                severity=EventSeverity.INFO,
                code="page_started",
                phase="results",
                page_number=page_number,
                url=search_url,
                message="Started results page collection",
            )
        with self.session_factory() as db:
            run = db.get(SearchRun, collector_input.run_id)
            if run:
                run.pages_attempted = page_number
                run.last_url = search_url
                db.commit()
        page.goto(search_url, wait_until="domcontentloaded", timeout=45_000)
        page.wait_for_timeout(1500)
        html = page.content()
        validate_collectable_page(html, page.url, page.title(), "results")
        listing_jobs = parse_seek_listing_page(html)
        with self.session_factory() as db:
            increment_run_metric(
                db, collector_input.run_id, "result_cards_observed", len(listing_jobs)
            )
        if not listing_jobs:
            with self.session_factory() as db:
                set_run_stop_reason(db, collector_input.run_id, "no_results")
            
            with self.session_factory() as db:
                record_run_event(
                    db,
                    collector_input.run_id,
                    severity=EventSeverity.ERROR,
                    code="no_result_cards",
                    phase="parsing",
                    page_number=page_number,
                    url=page.url,
                    page_title=page.title(),
                    message="No job result cards were found",
                )
            raise LayoutError(
                "No job result cards were found; treating as layout failure, not zero results"
            )
        with self.session_factory() as db:
            record_run_event(
                db,
                collector_input.run_id,
                severity=EventSeverity.INFO,
                code="page_parsed",
                phase="results",
                page_number=page_number,
                url=page.url,
                page_title=page.title(),
                message=f"Parsed {len(listing_jobs)} listing cards",
                metadata={"card_count": len(listing_jobs)},
            )
            run = db.get(SearchRun, collector_input.run_id)
            if run:
                run.pages_completed = max(run.pages_completed or 0, page_number)
                db.commit()
        for listing_job in listing_jobs:
            try:
                detail = self._fetch_detail(page, listing_job)
                job_payload = self._merge_payload(listing_job, detail)
                with self.session_factory() as db:
                    save_job_discovery(
                        db,
                        collector_input.run_id,
                        page_number,
                        job_payload,
                        card_type=listing_job.card_type,
                        parser_path=listing_job.parser_path,
                        rank=listing_job.rank,
                    )
            except (LayoutError, PlaywrightError, PlaywrightTimeoutError) as exc:
                had_errors = True
                LOGGER.exception(
                    "seek_job_detail_failed run_id=%s url=%s",
                    collector_input.run_id,
                    listing_job.url,
                )
                with self.session_factory() as db:
                    record_run_event(
                        db,
                        collector_input.run_id,
                        severity=EventSeverity.ERROR,
                        code="job_detail_error",
                        phase="job_detail",
                        page_number=page_number,
                        url=listing_job.url,
                        message=f"Detail error for {listing_job.url}: {exc}",
                        metadata={
                            "exception_type": type(exc).__name__,
                            "seek_job_id": listing_job.seek_job_id,
                            "card_type": listing_job.card_type,
                            "parser_path": listing_job.parser_path,
                        },
                    )
                    run = db.get(SearchRun, collector_input.run_id)
                    if run:
                        run.error_count += 1
                        run.message = f"Detail error for {listing_job.url}: {exc}"
                        db.commit()
        return had_errors

    def _fetch_detail(self, page, listing_job: ListingJob) -> dict[str, str | None]:
        page.goto(listing_job.url, wait_until="domcontentloaded", timeout=45_000)
        page.wait_for_timeout(1000)
        html = page.content()
        validate_collectable_page(html, page.url, page.title(), "job_detail")
        return parse_seek_detail_page(html)

    def _merge_payload(
        self, listing_job: ListingJob, detail: dict[str, str | None]
    ) -> dict[str, str | None]:
        title = detail.get("title") or listing_job.title
        company = detail.get("company") or listing_job.company
        location = detail.get("location") or listing_job.location
        payload = {
            "seek_job_id": listing_job.seek_job_id,
            "fallback_key": fallback_key_for_job(title, company, location, listing_job.url),
            "source": "seek",
            "source_listing_url": listing_job.url,
            "canonical_url": canonicalize_url(listing_job.url),
            "title": title,
            "company": company,
            "location": location,
            "salary": detail.get("salary") or listing_job.salary,
            "work_type": detail.get("work_type") or listing_job.work_type,
            "posting_date": detail.get("posting_date") or listing_job.posting_date,
            "url": listing_job.url,
            "description": detail.get("description"),
        }
        return payload
