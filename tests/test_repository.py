from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.models import Base, Job, JobDiscovery
from app.repository import build_resume_input, create_search_and_run, save_job_discovery


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
