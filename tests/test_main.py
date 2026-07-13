from dataclasses import dataclass
from datetime import date, timedelta
from urllib.parse import urlencode

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from app import main
from app.models import Base, Job, RunStatus, SearchRun
from app.repository import (
    EventSeverity,
    create_search_and_run,
    record_run_event,
    save_job_discovery,
)
from app.seek_session import SessionReadiness


@dataclass
class FakeRun:
    id: int


class FakeRequest:
    def __init__(self, payload: dict[str, str]) -> None:
        self.payload = payload

    async def form(self) -> dict[str, str]:
        return self.payload

    def url_for(self, name: str, **path_params):
        return main.app.url_path_for(name, **path_params)


@pytest.mark.asyncio
async def test_start_form_exact_submission(monkeypatch) -> None:
    captured = {}

    def fake_create_search_and_run(db, keywords, location, date_listed, max_pages):
        captured["keywords"] = keywords
        captured["location"] = location
        captured["date_listed"] = date_listed
        captured["max_pages"] = max_pages
        return FakeRun(id=123)

    def fake_queue(db, collector_input):
        captured["collector_input"] = collector_input

    monkeypatch.setattr(main, "create_search_and_run", fake_create_search_and_run)
    monkeypatch.setattr(main, "_queue_or_wait_for_profile", fake_queue)

    response = await main.start_run(
        FakeRequest(
            {
                "keywords": "strategy analyst",
                "location": "Sydney NSW",
                "date_listed": "last_3_days",
                "maximum_pages": "2",
            }
        ),
        db=object(),
    )

    assert response.status_code == 200
    assert captured["keywords"] == "strategy analyst"
    assert captured["location"] == "Sydney NSW"
    assert captured["max_pages"] == 2
    assert isinstance(captured["collector_input"].keywords, str)
    assert isinstance(captured["collector_input"].location, str)
    assert isinstance(captured["collector_input"].max_pages, int)


def test_successful_session_confirmation_closes_and_releases(monkeypatch) -> None:
    calls = []

    monkeypatch.setattr(
        main.seek_session_manager,
        "readiness",
        lambda: SessionReadiness(
            is_open=True,
            is_signed_in=True,
            message="Signed-in SEEK session detected in the persistent browser profile.",
            current_url="https://www.seek.com.au",
        ),
    )
    monkeypatch.setattr(main.seek_session_manager, "close", lambda: calls.append("close"))
    monkeypatch.setattr(main, "_release_waiting_runs", lambda: calls.append("release"))

    response = main.confirm_seek_session()

    assert response.status_code == 200
    assert calls == ["close", "release"]


def test_profile_busy_resume_sets_queued_message(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(main.seek_session_manager, "is_profile_busy", lambda: True)

    with session_factory() as db:
        run = create_search_and_run(db, "strategy analyst", "Sydney NSW", "last_3_days", 2)
        run.status = RunStatus.AWAITING_USER
        db.commit()
        run_id = run.id

    with session_factory() as db:
        response = main.resume_run(run_id, db=db)

    assert response.status_code == 200
    with session_factory() as db:
        run = db.get(SearchRun, run_id)
        assert run.status == RunStatus.PENDING
        assert run.message.startswith("Waiting for SEEK session/profile to be released")


def _request(path: str = "/", query: dict[str, str] | None = None) -> Request:
    query_string = urlencode(query or {}).encode()
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "headers": [],
            "query_string": query_string,
            "server": ("testserver", 80),
            "scheme": "http",
            "app": main.app,
        }
    )


def _shared_memory_session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _job_payload(
    seek_job_id: str,
    title: str,
    *,
    company: str = "Example Co",
    location: str = "Sydney NSW",
    posting_date: str = "1d ago",
) -> dict[str, str | None]:
    return {
        "seek_job_id": seek_job_id,
        "fallback_key": f"fallback-{seek_job_id}",
        "title": title,
        "company": company,
        "location": location,
        "salary": "$120,000",
        "work_type": "Full time",
        "posting_date": posting_date,
        "url": f"https://www.seek.com.au/job/{seek_job_id}?tracking=abc",
        "description": "Description",
    }


def test_run_detail_event_error_rendering() -> None:
    session_factory = _shared_memory_session_factory()
    with session_factory() as db:
        run = create_search_and_run(db, "strategy analyst", "Sydney NSW", "last_7_days", 2)
        payload = {
            "seek_job_id": "555",
            "fallback_key": "fallback-five",
            "title": "Strategy Analyst",
            "company": "Example Co",
            "location": "Sydney NSW",
            "salary": None,
            "work_type": "Full time",
            "posting_date": "1d ago",
            "url": "https://www.seek.com.au/job/555",
            "description": "Description",
        }
        save_job_discovery(
            db,
            run.id,
            1,
            payload,
            card_type="normal",
            parser_path="seek:data-automation-job-article",
            rank=1,
        )
        record_run_event(
            db,
            run.id,
            severity=EventSeverity.ERROR,
            code="job_detail_error",
            phase="job_detail",
            page_number=1,
            message="Detail error for one job",
        )
        run_id = run.id

    with session_factory() as db:
        response = main.run_detail(run_id, _request(f"/runs/{run_id}"), db=db)

    body = response.body.decode()
    assert "Persistent errors" in body
    assert "Detail error for one job" in body
    assert "seek:data-automation-job-article" in body
    assert "normal" in body


def test_completed_with_errors_dashboard_summary() -> None:
    session_factory = _shared_memory_session_factory()
    with session_factory() as db:
        run = create_search_and_run(db, "strategy analyst", "Sydney NSW", "last_7_days", 2)
        run.status = RunStatus.COMPLETED_WITH_ERRORS
        run.error_count = 1
        run.message = "Collector finished"
        record_run_event(
            db,
            run.id,
            severity=EventSeverity.ERROR,
            code="job_detail_error",
            phase="job_detail",
            message="Detail error survived",
        )
        db.commit()

    with session_factory() as db:
        response = main.index(_request(), db=db)

    body = response.body.decode()
    assert "persistent error event" in body
    assert "Detail error survived" in body


def test_dashboard_limits_latest_jobs_to_ten() -> None:
    session_factory = _shared_memory_session_factory()
    with session_factory() as db:
        run = create_search_and_run(db, "strategy analyst", "Sydney NSW", "last_7_days", 1)
        for index in range(12):
            save_job_discovery(
                db,
                run.id,
                1,
                _job_payload(str(1000 + index), f"Strategy Analyst {index}"),
            )

    with session_factory() as db:
        response = main.index(_request(), db=db)

    body = response.body.decode()
    assert body.count("https://www.seek.com.au/job/") == 10
    assert "job/1011" in body
    assert "job/1000" not in body


def test_jobs_filtering_sorting_and_pagination() -> None:
    session_factory = _shared_memory_session_factory()
    with session_factory() as db:
        run = create_search_and_run(db, "strategy analyst", "Sydney NSW", "last_7_days", 1)
        save_job_discovery(
            db,
            run.id,
            1,
            _job_payload("201", "Strategy Analyst", company="Beta Co"),
        )
        save_job_discovery(
            db,
            run.id,
            1,
            _job_payload("202", "Data Analyst", company="Alpha Co", location="Melbourne VIC"),
        )

    with session_factory() as db:
        response = main.jobs_index(
            _request("/jobs", {"q": "strategy", "page_size": "1", "sort": "company_az"}),
            db=db,
        )

    body = response.body.decode()
    assert "1 job matches the current view" in body
    assert "Strategy Analyst" in body
    assert "Data Analyst" not in body
    assert "page=2" not in body


@pytest.mark.asyncio
async def test_jobs_filter_query_and_csv_use_canonical_urls() -> None:
    session_factory = _shared_memory_session_factory()
    with session_factory() as db:
        run = create_search_and_run(db, "strategy analyst", "Sydney NSW", "last_7_days", 1)
        save_job_discovery(db, run.id, 1, _job_payload("301", "Strategy Analyst"))
        jobs = db.scalars(main._filtered_jobs_query(_request("/jobs", {"q": "strategy"}))).all()
        response = main._jobs_csv_response(jobs, "jobs-filtered.csv")

    chunks: list[str] = []
    async for chunk in response.body_iterator:
        chunks.append(chunk)
    body = "".join(chunks)
    assert "canonical_url" in body
    assert "https://www.seek.com.au/job/301," in body
    assert "tracking=abc" in body


def test_run_detail_legacy_error_explanation() -> None:
    session_factory = _shared_memory_session_factory()
    with session_factory() as db:
        run = create_search_and_run(db, "strategy analyst", "Sydney NSW", "last_7_days", 1)
        run.error_count = 1
        db.commit()
        run_id = run.id

    with session_factory() as db:
        response = main.run_detail(run_id, _request(f"/runs/{run_id}"), db=db)

    body = response.body.decode()
    assert "before persistent error tracking was introduced" in body


def test_runs_index_uses_readable_status_and_mobile_cards() -> None:
    session_factory = _shared_memory_session_factory()
    with session_factory() as db:
        run = create_search_and_run(db, "strategy analyst", "Sydney NSW", "last_7_days", 1)
        run.status = RunStatus.COMPLETED
        db.commit()

    with session_factory() as db:
        response = main.runs_index(_request("/runs"), db=db)

    body = response.body.decode()
    assert "Completed" in body
    assert "mobile-card-list" in body


def test_job_detail_renders_crm_controls_and_copyable_url() -> None:
    session_factory = _shared_memory_session_factory()
    with session_factory() as db:
        run = create_search_and_run(db, "strategy analyst", "Sydney NSW", "last_7_days", 1)
        job = save_job_discovery(db, run.id, 1, _job_payload("401", "Strategy Analyst"))
        job_id = job.id

    with session_factory() as db:
        response = main.job_detail(job_id, _request(f"/jobs/{job_id}"), db=db)

    body = response.body.decode()
    assert "CRM controls" in body
    assert "Copy URL" in body
    assert 'id="job-url"' in body
    assert "https://www.seek.com.au/job/401" in body
    assert "Search and run provenance" in body


@pytest.mark.asyncio
async def test_crm_update_route_accepts_valid_values_and_redirects() -> None:
    session_factory = _shared_memory_session_factory()
    with session_factory() as db:
        run = create_search_and_run(db, "strategy analyst", "Sydney NSW", "last_7_days", 1)
        job = save_job_discovery(db, run.id, 1, _job_payload("501", "Strategy Analyst"))
        job_id = job.id

    with session_factory() as db:
        response = await main.update_job_crm_route(
            job_id,
            FakeRequest(
                {
                    "crm_status": "shortlisted",
                    "priority": "high",
                    "is_favorite": "yes",
                    "application_deadline": "2026-07-20",
                    "follow_up_date": "2026-07-15",
                    "notes": "Prepare examples",
                }
            ),
            db=db,
        )

    assert response.status_code == 303
    with session_factory() as db:
        job = db.get(Job, job_id)
        assert job.crm_status == "shortlisted"
        assert job.priority == "high"
        assert job.is_favorite is True
        assert job.application_deadline == date(2026, 7, 20)
        assert job.follow_up_date == date(2026, 7, 15)
        assert job.notes == "Prepare examples"


@pytest.mark.asyncio
async def test_crm_update_accepts_all_valid_statuses_and_priorities() -> None:
    session_factory = _shared_memory_session_factory()
    with session_factory() as db:
        run = create_search_and_run(db, "strategy analyst", "Sydney NSW", "last_7_days", 1)
        job = save_job_discovery(db, run.id, 1, _job_payload("504", "Strategy Analyst"))
        job_id = job.id

    for crm_status in [
        "new",
        "reviewing",
        "shortlisted",
        "preparing",
        "applied",
        "interview",
        "offer",
        "rejected",
        "excluded",
        "archived",
    ]:
        with session_factory() as db:
            response = await main.update_job_crm_route(
                job_id,
                FakeRequest({"crm_status": crm_status, "priority": "none"}),
                db=db,
            )
        assert response.status_code == 303

    for priority in ["none", "low", "medium", "high"]:
        with session_factory() as db:
            response = await main.update_job_crm_route(
                job_id,
                FakeRequest({"crm_status": "reviewing", "priority": priority}),
                db=db,
            )
        assert response.status_code == 303


@pytest.mark.asyncio
async def test_crm_update_rejects_invalid_status_and_priority_without_persisting() -> None:
    session_factory = _shared_memory_session_factory()
    with session_factory() as db:
        run = create_search_and_run(db, "strategy analyst", "Sydney NSW", "last_7_days", 1)
        job = save_job_discovery(db, run.id, 1, _job_payload("502", "Strategy Analyst"))
        job.crm_status = "reviewing"
        job.priority = "medium"
        db.commit()
        job_id = job.id

    with session_factory() as db:
        response = await main.update_job_crm_route(
            job_id,
            FakeRequest({"crm_status": "not-real", "priority": "high"}),
            db=db,
        )
    assert response.status_code == 400
    assert "Invalid CRM status" in response.body.decode()

    with session_factory() as db:
        response = await main.update_job_crm_route(
            job_id,
            FakeRequest({"crm_status": "shortlisted", "priority": "urgent"}),
            db=db,
        )
    assert response.status_code == 400
    assert "Invalid manual priority" in response.body.decode()

    with session_factory() as db:
        job = db.get(Job, job_id)
        assert job.crm_status == "reviewing"
        assert job.priority == "medium"


@pytest.mark.asyncio
async def test_crm_update_empty_dates_persist_null_and_invalid_date_does_not_corrupt() -> None:
    session_factory = _shared_memory_session_factory()
    with session_factory() as db:
        run = create_search_and_run(db, "strategy analyst", "Sydney NSW", "last_7_days", 1)
        job = save_job_discovery(db, run.id, 1, _job_payload("503", "Strategy Analyst"))
        job.application_deadline = date(2026, 7, 20)
        job.follow_up_date = date(2026, 7, 15)
        db.commit()
        job_id = job.id

    with session_factory() as db:
        response = await main.update_job_crm_route(
            job_id,
            FakeRequest(
                {
                    "crm_status": "reviewing",
                    "priority": "low",
                    "application_deadline": "",
                    "follow_up_date": "",
                }
            ),
            db=db,
        )
    assert response.status_code == 303
    with session_factory() as db:
        job = db.get(Job, job_id)
        assert job.application_deadline is None
        assert job.follow_up_date is None
        job.application_deadline = date(2026, 8, 1)
        job.follow_up_date = date(2026, 8, 3)
        db.commit()

    with session_factory() as db:
        response = await main.update_job_crm_route(
            job_id,
            FakeRequest(
                {
                    "crm_status": "reviewing",
                    "priority": "low",
                    "application_deadline": "not-a-date",
                    "follow_up_date": "",
                }
            ),
            db=db,
        )
    assert response.status_code == 400
    assert "Application deadline must be a valid date" in response.body.decode()
    with session_factory() as db:
        job = db.get(Job, job_id)
        assert job.application_deadline == date(2026, 8, 1)
        assert job.follow_up_date == date(2026, 8, 3)


@pytest.mark.asyncio
async def test_jobs_csv_exports_plain_text_urls_and_crm_fields() -> None:
    session_factory = _shared_memory_session_factory()
    with session_factory() as db:
        run = create_search_and_run(db, "strategy analyst", "Sydney NSW", "last_7_days", 1)
        job = save_job_discovery(db, run.id, 1, _job_payload("601", "Strategy Analyst"))
        job.crm_status = "applied"
        job.priority = "medium"
        job.is_favorite = True
        job.notes = "Submitted application"
        job.application_deadline = date(2026, 7, 20)
        job.follow_up_date = date(2026, 7, 22)
        db.commit()
        jobs = db.scalars(main._filtered_jobs_query(_request("/jobs"))).all()
        response = main._jobs_csv_response(jobs, "jobs.csv")

    chunks: list[str] = []
    async for chunk in response.body_iterator:
        chunks.append(chunk)
    body = "".join(chunks)
    assert "job_url,canonical_url,source_listing_url" in body
    assert "https://www.seek.com.au/job/601" in body
    assert "tracking=abc" in body
    assert "Applied,Medium,yes,2026-07-20,2026-07-22,Submitted application" in body
    assert "<a" not in body


def test_jobs_crm_filters() -> None:
    session_factory = _shared_memory_session_factory()
    with session_factory() as db:
        run = create_search_and_run(db, "strategy analyst", "Sydney NSW", "last_7_days", 1)
        first = save_job_discovery(db, run.id, 1, _job_payload("701", "First Job"))
        first.crm_status = "new"
        first.priority = "high"
        first.is_favorite = True
        first.follow_up_date = date.today() - timedelta(days=1)
        first.application_deadline = date.today()
        second = save_job_discovery(db, run.id, 1, _job_payload("702", "Second Job"))
        second.crm_status = "archived"
        second.priority = "low"
        db.commit()

    with session_factory() as db:
        response = main.jobs_index(
            _request(
                "/jobs",
                {
                    "needs_review": "yes",
                    "priority": "high",
                    "favorites_only": "yes",
                    "follow_up_due": "yes",
                    "application_deadline": "due",
                },
            ),
            db=db,
        )

    body = response.body.decode()
    assert "First Job" in body
    assert "Second Job" not in body
    assert "New" in body
    assert "High" in body
    assert "Favorite" in body


def test_dashboard_crm_counts_are_linked() -> None:
    session_factory = _shared_memory_session_factory()
    with session_factory() as db:
        run = create_search_and_run(db, "strategy analyst", "Sydney NSW", "last_7_days", 1)
        new_job = save_job_discovery(db, run.id, 1, _job_payload("801", "New Job"))
        shortlisted = save_job_discovery(db, run.id, 1, _job_payload("802", "Shortlisted Job"))
        shortlisted.crm_status = "shortlisted"
        preparing = save_job_discovery(db, run.id, 1, _job_payload("803", "Preparing Job"))
        preparing.crm_status = "preparing"
        favorite = save_job_discovery(db, run.id, 1, _job_payload("804", "Favorite Job"))
        favorite.is_favorite = True
        due = save_job_discovery(db, run.id, 1, _job_payload("805", "Due Job"))
        due.follow_up_date = date.today()
        db.commit()
        assert new_job.crm_status == "new"

    with session_factory() as db:
        response = main.index(_request(), db=db)

    body = response.body.decode()
    assert 'href="/jobs?needs_review=yes"' in body
    assert 'href="/jobs?crm_status=shortlisted"' in body
    assert 'href="/jobs?applications_in_progress=yes"' in body
    assert 'href="/jobs?favorites_only=yes"' in body
    assert 'href="/jobs?follow_up_due=yes"' in body


def test_missing_job_returns_404() -> None:
    session_factory = _shared_memory_session_factory()
    with session_factory() as db, pytest.raises(HTTPException) as exc:
        main.job_detail(999, _request("/jobs/999"), db=db)

    assert exc.value.status_code == 404
