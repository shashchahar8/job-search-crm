from datetime import date

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.models import Base, Job, JobDiscovery, RunEvent, RunStatus, SearchRun
from app.repository import (
    EventSeverity,
    build_resume_input,
    create_search_and_run,
    increment_run_metric,
    mark_run,
    record_run_event,
    save_job_discovery,
    set_run_stop_reason,
)


def test_save_job_discovery_deduplicates_by_seek_job_id() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    with session_factory() as db:
        run = create_search_and_run(db, "strategy analyst", "Sydney NSW", "last_3_days", 2)
        payload = {
            "seek_job_id": "12345678",
            "fallback_key": "fallback-one",
            "title": "Strategy Analyst",
            "company": "Example Co",
            "location": "Sydney NSW",
            "salary": None,
            "work_type": "Full time",
            "posting_date": "2d ago",
            "url": "https://www.seek.com.au/job/12345678",
            "description": "Description",
        }
        save_job_discovery(db, run.id, 1, payload)
        save_job_discovery(db, run.id, 1, {**payload, "fallback_key": "fallback-two"})

        assert len(db.scalars(select(Job)).all()) == 1
        assert len(db.scalars(select(JobDiscovery)).all()) == 1


def test_errors_survive_final_run_completion() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    with session_factory() as db:
        run = create_search_and_run(db, "strategy analyst", "Sydney NSW", "last_7_days", 2)
        record_run_event(
            db,
            run.id,
            severity=EventSeverity.ERROR,
            code="job_detail_error",
            phase="job_detail",
            message="Detail error for one job",
            metadata={"html": "<secret>", "exception_type": "LayoutError"},
        )
        mark_run(db, run.id, RunStatus.COMPLETED_WITH_ERRORS, "Collector finished")

        events = db.scalars(select(RunEvent).where(RunEvent.run_id == run.id)).all()
        assert len(events) == 1
        assert events[0].code == "job_detail_error"
        assert events[0].metadata_json == {"exception_type": "LayoutError"}


def test_multiple_events_in_chronological_order() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    with session_factory() as db:
        run = create_search_and_run(db, "strategy analyst", "Sydney NSW", "last_7_days", 2)
        first = record_run_event(
            db,
            run.id,
            severity=EventSeverity.INFO,
            code="collector_started",
            phase="collector",
            message="Started",
        )
        second = record_run_event(
            db,
            run.id,
            severity=EventSeverity.ERROR,
            code="job_detail_error",
            phase="job_detail",
            message="Failed",
        )
        ordered = db.scalars(
            select(RunEvent)
            .where(RunEvent.run_id == run.id)
            .order_by(RunEvent.created_at, RunEvent.id)
        ).all()
        assert [event.id for event in ordered] == [first.id, second.id]


def test_resume_partially_completed_run_reuses_incomplete_page_and_dedupes() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    with session_factory() as db:
        run = create_search_and_run(db, "strategy analyst", "Sydney NSW", "last_3_days", 2)
        run.pages_attempted = 1
        db.commit()
        payload = {
            "seek_job_id": "12345678",
            "fallback_key": "fallback-one",
            "title": "Strategy Analyst",
            "company": "Example Co",
            "location": "Sydney NSW",
            "salary": None,
            "work_type": "Full time",
            "posting_date": "2d ago",
            "url": "https://www.seek.com.au/job/12345678",
            "description": "Description",
        }
        save_job_discovery(db, run.id, 1, payload)

        resume_input = build_resume_input(db, run.id)
        assert resume_input.start_page == 1
        assert resume_input.max_pages == 2

        save_job_discovery(db, run.id, 1, payload)
        assert len(db.scalars(select(Job)).all()) == 1
        assert len(db.scalars(select(JobDiscovery)).all()) == 1


def test_source_is_preserved_on_search_run_events_and_discoveries() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    with session_factory() as db:
        run = create_search_and_run(
            db,
            "strategy analyst",
            "Sydney NSW",
            "last_7_days",
            1,
            source_identifier="seek",
        )
        payload = {
            "seek_job_id": "source-1",
            "fallback_key": "fallback-source-1",
            "title": "Strategy Analyst",
            "company": "Example Co",
            "location": "Sydney NSW",
            "salary": None,
            "work_type": "Full time",
            "posting_date": "2d ago",
            "url": "https://www.seek.com.au/job/source-1",
            "description": "Description",
        }
        record_run_event(
            db,
            run.id,
            severity=EventSeverity.INFO,
            code="collector_started",
            phase="collector",
            message="Started",
        )
        save_job_discovery(db, run.id, 1, payload)

        stored_run = db.get(SearchRun, run.id)
        event = db.scalar(select(RunEvent).where(RunEvent.run_id == run.id))
        discovery = db.scalar(select(JobDiscovery).where(JobDiscovery.run_id == run.id))

        assert stored_run.search.source == "seek"
        assert stored_run.source == "seek"
        assert event.source == "seek"
        assert discovery.source == "seek"


def test_card_provenance_is_stored_on_discovery() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    with session_factory() as db:
        run = create_search_and_run(db, "strategy analyst", "Sydney NSW", "last_7_days", 2)
        payload = {
            "seek_job_id": "222",
            "fallback_key": "fallback-two",
            "title": "Strategy Analyst",
            "company": "Example Co",
            "location": "Sydney NSW",
            "salary": None,
            "work_type": "Full time",
            "posting_date": "2d ago",
            "url": "https://www.seek.com.au/job/222",
            "description": "Description",
        }
        save_job_discovery(
            db,
            run.id,
            2,
            payload,
            card_type="normal",
            parser_path="seek:data-automation-job-article",
            rank=7,
        )
        discovery = db.scalar(select(JobDiscovery))
        assert discovery.card_type == "normal"
        assert discovery.parser_path == "seek:data-automation-job-article"
        assert discovery.rank == 7


def test_run_metrics_track_new_known_duplicate_and_stop_reason() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    with session_factory() as db:
        first_run = create_search_and_run(db, "strategy analyst", "Sydney NSW", "last_7_days", 1)
        second_run = create_search_and_run(
            db, "strategy analyst", "Sydney NSW", "last_7_days", 1
        )
        payload = {
            "seek_job_id": "777",
            "fallback_key": "fallback-seven",
            "title": "Strategy Analyst",
            "company": "Example Co",
            "location": "Sydney NSW",
            "salary": None,
            "work_type": "Full time",
            "posting_date": "2d ago",
            "url": "https://www.seek.com.au/job/777?ref=search",
            "description": "Description",
        }

        increment_run_metric(db, first_run.id, "result_cards_observed", 2)
        save_job_discovery(db, first_run.id, 1, payload)
        save_job_discovery(db, first_run.id, 1, payload)
        save_job_discovery(db, second_run.id, 1, payload)
        set_run_stop_reason(db, first_run.id, "requested_page_limit_reached")

        first = db.get(type(first_run), first_run.id)
        second = db.get(type(second_run), second_run.id)
        assert first.result_cards_observed == 2
        assert first.unique_jobs_in_run == 1
        assert first.new_jobs_added == 1
        assert first.duplicate_cards_ignored == 1
        assert first.stop_reason == "requested_page_limit_reached"
        assert second.unique_jobs_in_run == 1
        assert second.known_jobs_rediscovered == 1


def test_collector_update_preserves_manual_crm_fields() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    with session_factory() as db:
        run = create_search_and_run(db, "strategy analyst", "Sydney NSW", "last_7_days", 1)
        payload = {
            "seek_job_id": "909",
            "fallback_key": "fallback-909",
            "title": "Strategy Analyst",
            "company": "Example Co",
            "location": "Sydney NSW",
            "salary": None,
            "work_type": "Full time",
            "posting_date": "2d ago",
            "url": "https://www.seek.com.au/job/909",
            "description": "Original description",
        }
        job = save_job_discovery(db, run.id, 1, payload)
        job.crm_status = "shortlisted"
        job.priority = "high"
        job.is_favorite = True
        job.notes = "Follow up with hiring manager"
        job.application_deadline = date(2026, 7, 20)
        job.follow_up_date = date(2026, 7, 15)
        db.commit()

        save_job_discovery(
            db,
            run.id,
            1,
            {
                **payload,
                "title": "Senior Strategy Analyst",
                "salary": "$140,000",
                "description": "Updated description",
            },
        )

        updated = db.scalar(select(Job).where(Job.seek_job_id == "909"))
        assert updated.title == "Senior Strategy Analyst"
        assert updated.salary == "$140,000"
        assert updated.description == "Updated description"
        assert updated.crm_status == "shortlisted"
        assert updated.priority == "high"
        assert updated.is_favorite is True
        assert updated.notes == "Follow up with hiring manager"
        assert updated.application_deadline == date(2026, 7, 20)
        assert updated.follow_up_date == date(2026, 7, 15)
