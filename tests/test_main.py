from dataclasses import dataclass

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from app import main
from app.models import Base, RunStatus, SearchRun
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


def _request(path: str = "/") -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "headers": [],
            "query_string": b"",
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
