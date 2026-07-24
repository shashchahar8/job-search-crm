from sqlalchemy import create_engine, inspect, text

from app.migrations import migrate_database


def test_fresh_database_migration_creates_scheduling_schema_idempotently() -> None:
    engine = create_engine("sqlite:///:memory:")

    migrate_database(engine)
    migrate_database(engine)

    inspector = inspect(engine)
    assert {
        "campaign_schedules",
        "campaign_schedule_occurrences",
        "campaign_executions",
    }.issubset(inspector.get_table_names())
    execution_columns = {
        column["name"] for column in inspector.get_columns("campaign_executions")
    }
    assert {
        "origin",
        "schedule_id",
        "schedule_occurrence_id",
        "scheduled_for_at",
    }.issubset(execution_columns)


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
            text("SELECT source, card_type, parser_path, rank FROM job_discoveries WHERE id = 1")
        ).one()
        run = connection.execute(
            text(
                """
                SELECT source, result_cards_observed, unique_jobs_in_run, new_jobs_added,
                       known_jobs_rediscovered, jobs_updated, duplicate_cards_ignored,
                       pages_completed, stop_reason
                FROM search_runs WHERE id = 1
                """
            )
        ).one()
        search = connection.execute(text("SELECT source FROM searches WHERE id = 1")).one()
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
        evaluation_count = connection.execute(
            text("SELECT COUNT(*) FROM job_rule_evaluations")
        ).scalar_one()
        evaluation_columns = {
            row[1]
            for row in connection.execute(text("PRAGMA table_info(job_rule_evaluations)")).all()
        }
        job_count = connection.execute(text("SELECT COUNT(*) FROM jobs")).scalar_one()

    assert discovery.card_type == "unknown"
    assert discovery.source == "seek"
    assert discovery.parser_path == "unknown"
    assert discovery.rank is None
    assert search.source == "seek"
    assert run.source == "seek"
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
    assert evaluation_count == 0
    assert "profile_fingerprint" in evaluation_columns
    assert job_count == 1


def test_scheduling_migration_preserves_campaign_history_and_schema_invariants() -> None:
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE campaigns (
                    id INTEGER PRIMARY KEY,
                    name VARCHAR(160) NOT NULL UNIQUE,
                    description TEXT,
                    is_active BOOLEAN NOT NULL,
                    is_archived BOOLEAN NOT NULL,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE campaign_executions (
                    id INTEGER PRIMARY KEY,
                    campaign_id INTEGER NOT NULL,
                    campaign_name_snapshot VARCHAR(160) NOT NULL,
                    status VARCHAR(21) NOT NULL,
                    stop_reason VARCHAR(80),
                    message TEXT,
                    started_at DATETIME,
                    finished_at DATETIME,
                    created_at DATETIME NOT NULL,
                    planned_child_count INTEGER NOT NULL DEFAULT 0,
                    attempted_child_count INTEGER NOT NULL DEFAULT 0,
                    completed_child_count INTEGER NOT NULL DEFAULT 0,
                    failed_child_count INTEGER NOT NULL DEFAULT 0,
                    awaiting_user_child_count INTEGER NOT NULL DEFAULT 0,
                    pages_planned INTEGER NOT NULL DEFAULT 0,
                    pages_completed INTEGER NOT NULL DEFAULT 0,
                    result_cards_observed INTEGER NOT NULL DEFAULT 0,
                    unique_jobs INTEGER NOT NULL DEFAULT 0,
                    new_jobs INTEGER NOT NULL DEFAULT 0,
                    rediscoveries INTEGER NOT NULL DEFAULT 0,
                    updated_jobs INTEGER NOT NULL DEFAULT 0,
                    duplicate_cards INTEGER NOT NULL DEFAULT 0,
                    error_count INTEGER NOT NULL DEFAULT 0,
                    current_child_id INTEGER
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE campaign_execution_child_snapshots (
                    id INTEGER PRIMARY KEY,
                    campaign_execution_id INTEGER NOT NULL,
                    position INTEGER NOT NULL,
                    source VARCHAR(50) NOT NULL,
                    saved_search_id INTEGER NOT NULL,
                    saved_search_name_snapshot VARCHAR(160) NOT NULL,
                    query_text_snapshot VARCHAR(500) NOT NULL,
                    location_snapshot VARCHAR(255) NOT NULL,
                    date_window_snapshot VARCHAR(50) NOT NULL,
                    page_limit_snapshot INTEGER NOT NULL,
                    status VARCHAR(21) NOT NULL,
                    child_run_id INTEGER,
                    stop_reason VARCHAR(80),
                    pages_planned INTEGER NOT NULL DEFAULT 0,
                    pages_completed INTEGER NOT NULL DEFAULT 0,
                    result_cards_observed INTEGER NOT NULL DEFAULT 0,
                    unique_jobs INTEGER NOT NULL DEFAULT 0,
                    new_jobs INTEGER NOT NULL DEFAULT 0,
                    rediscoveries INTEGER NOT NULL DEFAULT 0,
                    updated_jobs INTEGER NOT NULL DEFAULT 0,
                    duplicate_cards INTEGER NOT NULL DEFAULT 0,
                    error_count INTEGER NOT NULL DEFAULT 0,
                    created_at DATETIME NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE campaign_execution_events (
                    id INTEGER PRIMARY KEY,
                    campaign_execution_id INTEGER NOT NULL,
                    child_snapshot_id INTEGER,
                    created_at DATETIME NOT NULL,
                    severity VARCHAR(20) NOT NULL,
                    code VARCHAR(80) NOT NULL,
                    phase VARCHAR(80) NOT NULL,
                    message TEXT NOT NULL,
                    metadata_json JSON
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO campaigns VALUES
                    (1, 'History', 'Preserve me', 1, 0, '2026-07-01', '2026-07-01')
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO campaign_executions VALUES (
                    1, 1, 'History', 'completed', 'all_children_completed', 'done',
                    '2026-07-01', '2026-07-01', '2026-07-01',
                    1, 1, 1, 0, 0, 2, 2, 20, 3, 2, 1, 1, 0, 0, NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO campaign_execution_child_snapshots VALUES (
                    1, 1, 1, 'seek', 7, 'Frozen', 'query', 'Sydney NSW',
                    'last_2_days', 2, 'completed', 9, 'done',
                    2, 2, 20, 3, 2, 1, 1, 0, 0, '2026-07-01'
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO campaign_execution_events VALUES (
                    1, 1, 1, '2026-07-01', 'info', 'completed', 'execution',
                    'Campaign execution finished.', NULL
                )
                """
            )
        )

    before = {}
    with engine.connect() as connection:
        for table in (
            "campaign_executions",
            "campaign_execution_child_snapshots",
            "campaign_execution_events",
        ):
            before[table] = connection.execute(text(f"SELECT * FROM {table}")).all()

    migrate_database(engine)
    migrate_database(engine)

    inspector = inspect(engine)
    schedule_unique = {
        constraint["name"] for constraint in inspector.get_unique_constraints("campaign_schedules")
    }
    occurrence_indexes = {
        index["name"]: index
        for index in inspector.get_indexes("campaign_schedule_occurrences")
    }
    schedule_checks = {
        constraint["name"] for constraint in inspector.get_check_constraints("campaign_schedules")
    }
    occurrence_checks = {
        constraint["name"]
        for constraint in inspector.get_check_constraints("campaign_schedule_occurrences")
    }
    execution_indexes = {
        index["name"]: index for index in inspector.get_indexes("campaign_executions")
    }
    schedule_foreign_keys = {
        foreign_key["constrained_columns"][0]: foreign_key
        for foreign_key in inspector.get_foreign_keys("campaign_schedules")
    }
    occurrence_foreign_keys = {
        foreign_key["constrained_columns"][0]: foreign_key
        for foreign_key in inspector.get_foreign_keys("campaign_schedule_occurrences")
    }
    execution_foreign_keys = {
        foreign_key["constrained_columns"][0]: foreign_key
        for foreign_key in inspector.get_foreign_keys("campaign_executions")
    }
    with engine.connect() as connection:
        schedule_delete_actions = {
            row[3]: row[6]
            for row in connection.execute(
                text("PRAGMA foreign_key_list(campaign_schedules)")
            ).all()
        }
        occurrence_delete_actions = {
            row[3]: row[6]
            for row in connection.execute(
                text("PRAGMA foreign_key_list(campaign_schedule_occurrences)")
            ).all()
        }
        execution_delete_actions = {
            row[3]: row[6]
            for row in connection.execute(
                text("PRAGMA foreign_key_list(campaign_executions)")
            ).all()
        }

    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT origin FROM campaign_executions WHERE id = 1")
        ).scalar_one() == "manual"
        scheduling_columns = {
            "origin",
            "schedule_id",
            "schedule_occurrence_id",
            "scheduled_for_at",
        }
        for table, expected in before.items():
            columns = [
                row[1]
                for row in connection.execute(text(f"PRAGMA table_info({table})")).all()
                if row[1] not in scheduling_columns
            ]
            current = connection.execute(
                text(f"SELECT {', '.join(columns)} FROM {table}")
            ).all()
            assert current == expected

    assert "uq_campaign_schedules_campaign" in schedule_unique
    assert occurrence_indexes["uq_campaign_schedule_occurrence"]["unique"] == 1
    assert schedule_checks == {
        "ck_campaign_schedules_recurrence_type",
        "ck_campaign_schedules_weekday_mask",
    }
    assert occurrence_checks == {
        "ck_campaign_schedule_occurrences_disposition",
        "ck_campaign_schedule_occurrences_fold",
        "ck_campaign_schedule_occurrences_resolution",
        "ck_campaign_schedule_occurrences_superseded_count",
        "ck_campaign_schedule_occurrences_utc_offset",
    }
    partial = execution_indexes["ux_campaign_executions_schedule_occurrence"]
    assert partial["unique"] == 1
    assert "schedule_occurrence_id IS NOT NULL" in str(
        partial["dialect_options"]["sqlite_where"]
    )
    assert schedule_foreign_keys["campaign_id"]["referred_table"] == "campaigns"
    assert schedule_delete_actions["campaign_id"] == "RESTRICT"
    assert occurrence_foreign_keys["schedule_id"]["referred_table"] == "campaign_schedules"
    assert occurrence_foreign_keys["campaign_id"]["referred_table"] == "campaigns"
    assert occurrence_foreign_keys["blocking_execution_id"]["referred_table"] == (
        "campaign_executions"
    )
    assert set(occurrence_delete_actions.values()) == {"RESTRICT"}
    assert execution_foreign_keys["schedule_id"]["referred_table"] == "campaign_schedules"
    assert execution_foreign_keys["schedule_occurrence_id"]["referred_table"] == (
        "campaign_schedule_occurrences"
    )
    assert execution_delete_actions["schedule_id"] == "RESTRICT"
    assert execution_delete_actions["schedule_occurrence_id"] == "RESTRICT"
