from sqlalchemy import create_engine, text

from app.migrations import migrate_database


def test_migration_preserves_existing_records() -> None:
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE searches (
                    id INTEGER PRIMARY KEY,
                    keywords TEXT,
                    location TEXT,
                    date_listed TEXT,
                    max_pages INTEGER,
                    created_at DATETIME
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE search_runs (
                    id INTEGER PRIMARY KEY,
                    search_id INTEGER,
                    status TEXT,
                    started_at DATETIME,
                    finished_at DATETIME,
                    pages_requested INTEGER,
                    pages_attempted INTEGER,
                    jobs_found INTEGER,
                    error_count INTEGER,
                    message TEXT,
                    last_url TEXT,
                    created_at DATETIME
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE jobs (
                    id INTEGER PRIMARY KEY,
                    seek_job_id TEXT,
                    fallback_key TEXT,
                    title TEXT,
                    company TEXT,
                    location TEXT,
                    salary TEXT,
                    work_type TEXT,
                    posting_date TEXT,
                    url TEXT,
                    description TEXT,
                    first_seen_at DATETIME,
                    updated_at DATETIME
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE job_discoveries (
                    id INTEGER PRIMARY KEY,
                    job_id INTEGER,
                    search_id INTEGER,
                    run_id INTEGER,
                    page_number INTEGER,
                    found_at DATETIME
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO searches
                VALUES (1, 'strategy analyst', 'Sydney NSW', 'last_7_days', 2, '2026-01-01')
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO search_runs
                VALUES (
                    1, 1, 'completed', '2026-01-01', '2026-01-01',
                    2, 2, 1, 0, 'done', 'https://example.test', '2026-01-01'
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO jobs
                VALUES (
                    1, '123', 'fallback', 'Title', 'Company', 'Sydney', NULL,
                    'Full time', '1d ago', 'https://www.seek.com.au/job/123',
                    'Description', '2026-01-01', '2026-01-01'
                )
                """
            )
        )
        connection.execute(
            text("INSERT INTO job_discoveries VALUES (1, 1, 1, 1, 1, '2026-01-01')")
        )

    migrate_database(engine)
    migrate_database(engine)

    with engine.connect() as connection:
        discovery = connection.execute(
            text("SELECT card_type, parser_path, rank FROM job_discoveries WHERE id = 1")
        ).one()
        run = connection.execute(
            text(
                """
                SELECT result_cards_observed, unique_jobs_in_run, new_jobs_added,
                       known_jobs_rediscovered, jobs_updated, duplicate_cards_ignored,
                       pages_completed, stop_reason
                FROM search_runs WHERE id = 1
                """
            )
        ).one()
        job = connection.execute(
            text(
                """
                SELECT source, source_listing_url, canonical_url, last_seen_at,
                       crm_status, priority, is_favorite, notes,
                       application_deadline, follow_up_date, crm_updated_at
                FROM jobs WHERE id = 1
                """
            )
        ).one()
        event_count = connection.execute(text("SELECT COUNT(*) FROM run_events")).scalar_one()
        job_count = connection.execute(text("SELECT COUNT(*) FROM jobs")).scalar_one()

    assert discovery.card_type == "unknown"
    assert discovery.parser_path == "unknown"
    assert discovery.rank is None
    assert run.result_cards_observed is None
    assert run.unique_jobs_in_run is None
    assert run.stop_reason is None
    assert job.source == "seek"
    assert job.source_listing_url == "https://www.seek.com.au/job/123"
    assert job.canonical_url == "https://www.seek.com.au/job/123"
    assert job.last_seen_at == "2026-01-01"
    assert job.crm_status == "new"
    assert job.priority == "none"
    assert job.is_favorite == 0
    assert job.notes is None
    assert job.application_deadline is None
    assert job.follow_up_date is None
    assert job.crm_updated_at is None
    assert event_count == 0
    assert job_count == 1
