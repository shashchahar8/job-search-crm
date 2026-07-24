from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

DISCOVERY_COLUMNS = {
    "source": "VARCHAR(50) NOT NULL DEFAULT 'seek'",
    "card_type": "VARCHAR(32) NOT NULL DEFAULT 'unknown'",
    "parser_path": "VARCHAR(255) NOT NULL DEFAULT 'unknown'",
    "rank": "INTEGER",
}

SEARCH_COLUMNS = {
    "source": "VARCHAR(50) NOT NULL DEFAULT 'seek'",
}

RUN_COLUMNS = {
    "source": "VARCHAR(50) NOT NULL DEFAULT 'seek'",
    "result_cards_observed": "INTEGER",
    "unique_jobs_in_run": "INTEGER",
    "new_jobs_added": "INTEGER",
    "known_jobs_rediscovered": "INTEGER",
    "jobs_updated": "INTEGER",
    "duplicate_cards_ignored": "INTEGER",
    "pages_completed": "INTEGER",
    "stop_reason": "VARCHAR(80)",
}

JOB_COLUMNS = {
    "source": "VARCHAR(50) NOT NULL DEFAULT 'seek'",
    "source_listing_url": "TEXT",
    "canonical_url": "TEXT",
    "last_seen_at": "DATETIME",
}

JOB_CRM_COLUMNS = {
    "crm_status": "VARCHAR(32) NOT NULL DEFAULT 'new'",
    "priority": "VARCHAR(16) NOT NULL DEFAULT 'none'",
    "is_favorite": "BOOLEAN NOT NULL DEFAULT 0",
    "notes": "TEXT",
    "application_deadline": "DATE",
    "follow_up_date": "DATE",
    "crm_updated_at": "DATETIME",
}

CAMPAIGN_EXECUTION_SCHEDULE_COLUMNS = {
    "origin": "VARCHAR(16) NOT NULL DEFAULT 'manual'",
    "schedule_id": (
        "INTEGER REFERENCES campaign_schedules(id) ON DELETE RESTRICT"
    ),
    "schedule_occurrence_id": (
        "INTEGER REFERENCES campaign_schedule_occurrences(id) ON DELETE RESTRICT"
    ),
    "scheduled_for_at": "DATETIME",
}


def migrate_database(engine: Engine) -> None:
    """Apply small additive SQLite migrations without deleting existing data."""
    if not engine.url.get_backend_name().startswith("sqlite"):
        return

    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    with engine.begin() as connection:
        if "searches" in table_names:
            existing_columns = {column["name"] for column in inspector.get_columns("searches")}
            for column_name, definition in SEARCH_COLUMNS.items():
                if column_name not in existing_columns:
                    connection.execute(
                        text(f"ALTER TABLE searches ADD COLUMN {column_name} {definition}")
                    )
            connection.execute(text("UPDATE searches SET source = 'seek' WHERE source IS NULL"))

        if "job_discoveries" in table_names:
            existing_columns = {
                column["name"] for column in inspector.get_columns("job_discoveries")
            }
            for column_name, definition in DISCOVERY_COLUMNS.items():
                if column_name not in existing_columns:
                    connection.execute(
                        text(f"ALTER TABLE job_discoveries ADD COLUMN {column_name} {definition}")
                    )
            connection.execute(
                text("UPDATE job_discoveries SET source = 'seek' WHERE source IS NULL")
            )

        if "search_runs" in table_names:
            existing_columns = {column["name"] for column in inspector.get_columns("search_runs")}
            for column_name, definition in RUN_COLUMNS.items():
                if column_name not in existing_columns:
                    connection.execute(
                        text(f"ALTER TABLE search_runs ADD COLUMN {column_name} {definition}")
                    )
            connection.execute(text("UPDATE search_runs SET source = 'seek' WHERE source IS NULL"))

        if "jobs" in table_names:
            existing_columns = {column["name"] for column in inspector.get_columns("jobs")}
            for column_name, definition in (JOB_COLUMNS | JOB_CRM_COLUMNS).items():
                if column_name not in existing_columns:
                    connection.execute(
                        text(f"ALTER TABLE jobs ADD COLUMN {column_name} {definition}")
                    )
            connection.execute(text("UPDATE jobs SET source = 'seek' WHERE source IS NULL"))
            connection.execute(
                text("UPDATE jobs SET source_listing_url = url WHERE source_listing_url IS NULL")
            )
            connection.execute(
                text("UPDATE jobs SET canonical_url = url WHERE canonical_url IS NULL")
            )
            connection.execute(
                text("UPDATE jobs SET last_seen_at = updated_at WHERE last_seen_at IS NULL")
            )
            connection.execute(text("UPDATE jobs SET crm_status = 'new' WHERE crm_status IS NULL"))
            connection.execute(text("UPDATE jobs SET priority = 'none' WHERE priority IS NULL"))
            connection.execute(text("UPDATE jobs SET is_favorite = 0 WHERE is_favorite IS NULL"))

        if "saved_searches" not in table_names:
            connection.execute(
                text(
                    """
                    CREATE TABLE saved_searches (
                        id INTEGER NOT NULL PRIMARY KEY,
                        name VARCHAR(160) NOT NULL,
                        source VARCHAR(50) NOT NULL,
                        query_text VARCHAR(500) NOT NULL,
                        location VARCHAR(255) NOT NULL,
                        date_window VARCHAR(50) NOT NULL,
                        max_pages INTEGER NOT NULL,
                        is_enabled BOOLEAN NOT NULL DEFAULT 1,
                        is_archived BOOLEAN NOT NULL DEFAULT 0,
                        created_at DATETIME NOT NULL,
                        updated_at DATETIME NOT NULL,
                        CONSTRAINT uq_saved_searches_name UNIQUE (name)
                    )
                    """
                )
            )

        if "campaigns" not in table_names:
            connection.execute(
                text(
                    """
                    CREATE TABLE campaigns (
                        id INTEGER NOT NULL PRIMARY KEY,
                        name VARCHAR(160) NOT NULL,
                        description TEXT,
                        is_active BOOLEAN NOT NULL DEFAULT 1,
                        is_archived BOOLEAN NOT NULL DEFAULT 0,
                        created_at DATETIME NOT NULL,
                        updated_at DATETIME NOT NULL,
                        CONSTRAINT uq_campaigns_name UNIQUE (name)
                    )
                    """
                )
            )

        if "campaign_saved_searches" not in table_names:
            connection.execute(
                text(
                    """
                    CREATE TABLE campaign_saved_searches (
                        id INTEGER NOT NULL PRIMARY KEY,
                        campaign_id INTEGER NOT NULL,
                        saved_search_id INTEGER NOT NULL,
                        position INTEGER NOT NULL,
                        is_enabled BOOLEAN NOT NULL DEFAULT 1,
                        created_at DATETIME NOT NULL,
                        updated_at DATETIME NOT NULL,
                        CONSTRAINT uq_campaign_saved_search UNIQUE
                            (campaign_id, saved_search_id),
                        CONSTRAINT uq_campaign_saved_search_position UNIQUE
                            (campaign_id, position),
                        FOREIGN KEY(campaign_id) REFERENCES campaigns (id),
                        FOREIGN KEY(saved_search_id) REFERENCES saved_searches (id)
                    )
                    """
                )
            )
            connection.execute(
                text(
                    "CREATE INDEX ix_campaign_saved_searches_order "
                    "ON campaign_saved_searches (campaign_id, position)"
                )
            )

        if "campaign_schedules" not in table_names:
            connection.execute(
                text(
                    """
                    CREATE TABLE campaign_schedules (
                        id INTEGER NOT NULL PRIMARY KEY,
                        campaign_id INTEGER NOT NULL,
                        recurrence_type VARCHAR(16) NOT NULL,
                        local_time TIME NOT NULL,
                        timezone_name VARCHAR(64) NOT NULL DEFAULT 'Australia/Sydney',
                        weekday_mask INTEGER,
                        is_enabled BOOLEAN NOT NULL DEFAULT 1,
                        last_occurrence_considered_at DATETIME,
                        next_occurrence_at DATETIME,
                        created_at DATETIME NOT NULL,
                        updated_at DATETIME NOT NULL,
                        CONSTRAINT uq_campaign_schedules_campaign UNIQUE (campaign_id),
                        CONSTRAINT ck_campaign_schedules_recurrence_type
                            CHECK (recurrence_type IN ('daily', 'weekly')),
                        CONSTRAINT ck_campaign_schedules_weekday_mask CHECK (
                            (recurrence_type = 'daily' AND weekday_mask IS NULL)
                            OR
                            (recurrence_type = 'weekly'
                             AND weekday_mask BETWEEN 1 AND 127)
                        ),
                        FOREIGN KEY(campaign_id) REFERENCES campaigns (id)
                            ON DELETE RESTRICT
                    )
                    """
                )
            )
            connection.execute(
                text(
                    "CREATE INDEX ix_campaign_schedules_enabled_next "
                    "ON campaign_schedules (is_enabled, next_occurrence_at)"
                )
            )

        if "campaign_schedule_occurrences" not in table_names:
            connection.execute(
                text(
                    """
                    CREATE TABLE campaign_schedule_occurrences (
                        id INTEGER NOT NULL PRIMARY KEY,
                        schedule_id INTEGER NOT NULL,
                        campaign_id INTEGER NOT NULL,
                        scheduled_for_at DATETIME NOT NULL,
                        scheduled_local_date DATE NOT NULL,
                        scheduled_local_time TIME NOT NULL,
                        timezone_name VARCHAR(64) NOT NULL,
                        utc_offset_minutes INTEGER NOT NULL,
                        fold INTEGER NOT NULL DEFAULT 0,
                        resolution VARCHAR(24) NOT NULL,
                        disposition VARCHAR(16) NOT NULL,
                        reason_code VARCHAR(80),
                        message TEXT,
                        superseded_count INTEGER NOT NULL DEFAULT 0,
                        superseded_from_at DATETIME,
                        blocking_execution_id INTEGER,
                        considered_at DATETIME NOT NULL,
                        CONSTRAINT ck_campaign_schedule_occurrences_fold
                            CHECK (fold IN (0, 1)),
                        CONSTRAINT ck_campaign_schedule_occurrences_resolution
                            CHECK (resolution IN ('exact', 'gap_shifted', 'fold_first')),
                        CONSTRAINT ck_campaign_schedule_occurrences_disposition
                            CHECK (disposition IN ('planned', 'skipped', 'invalid')),
                        CONSTRAINT ck_campaign_schedule_occurrences_superseded_count
                            CHECK (superseded_count >= 0),
                        CONSTRAINT ck_campaign_schedule_occurrences_utc_offset
                            CHECK (utc_offset_minutes BETWEEN -840 AND 840),
                        FOREIGN KEY(schedule_id) REFERENCES campaign_schedules (id)
                            ON DELETE RESTRICT,
                        FOREIGN KEY(campaign_id) REFERENCES campaigns (id)
                            ON DELETE RESTRICT,
                        FOREIGN KEY(blocking_execution_id) REFERENCES campaign_executions (id)
                            ON DELETE RESTRICT
                    )
                    """
                )
            )
            connection.execute(
                text(
                    "CREATE UNIQUE INDEX uq_campaign_schedule_occurrence "
                    "ON campaign_schedule_occurrences (schedule_id, scheduled_for_at)"
                )
            )
            connection.execute(
                text(
                    "CREATE INDEX ix_campaign_schedule_occurrences_schedule_time "
                    "ON campaign_schedule_occurrences (schedule_id, scheduled_for_at)"
                )
            )
            connection.execute(
                text(
                    "CREATE INDEX ix_campaign_schedule_occurrences_campaign_considered "
                    "ON campaign_schedule_occurrences (campaign_id, considered_at)"
                )
            )
            connection.execute(
                text(
                    "CREATE INDEX ix_campaign_schedule_occurrences_disposition_considered "
                    "ON campaign_schedule_occurrences (disposition, considered_at)"
                )
            )

        if "campaign_executions" not in table_names:
            connection.execute(
                text(
                    """
                    CREATE TABLE campaign_executions (
                        id INTEGER NOT NULL PRIMARY KEY,
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
                        current_child_id INTEGER,
                        origin VARCHAR(16) NOT NULL DEFAULT 'manual',
                        schedule_id INTEGER,
                        schedule_occurrence_id INTEGER,
                        scheduled_for_at DATETIME,
                        FOREIGN KEY(campaign_id) REFERENCES campaigns (id),
                        FOREIGN KEY(schedule_id) REFERENCES campaign_schedules (id)
                            ON DELETE RESTRICT,
                        FOREIGN KEY(schedule_occurrence_id)
                            REFERENCES campaign_schedule_occurrences (id)
                            ON DELETE RESTRICT
                    )
                    """
                )
            )
            connection.execute(
                text(
                    "CREATE INDEX ix_campaign_executions_campaign_created "
                    "ON campaign_executions (campaign_id, created_at)"
                )
            )
            connection.execute(
                text(
                    "CREATE INDEX ix_campaign_executions_schedule_created "
                    "ON campaign_executions (schedule_id, created_at)"
                )
            )
            connection.execute(
                text(
                    "CREATE UNIQUE INDEX ux_campaign_executions_schedule_occurrence "
                    "ON campaign_executions (schedule_occurrence_id) "
                    "WHERE schedule_occurrence_id IS NOT NULL"
                )
            )
        else:
            existing_columns = {
                column["name"] for column in inspector.get_columns("campaign_executions")
            }
            for column_name, definition in CAMPAIGN_EXECUTION_SCHEDULE_COLUMNS.items():
                if column_name not in existing_columns:
                    connection.execute(
                        text(
                            f"ALTER TABLE campaign_executions "
                            f"ADD COLUMN {column_name} {definition}"
                        )
                    )
            connection.execute(
                text(
                    "UPDATE campaign_executions SET origin = 'manual' "
                    "WHERE origin IS NULL"
                )
            )
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_campaign_executions_schedule_created "
                    "ON campaign_executions (schedule_id, created_at)"
                )
            )
            connection.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS "
                    "ux_campaign_executions_schedule_occurrence "
                    "ON campaign_executions (schedule_occurrence_id) "
                    "WHERE schedule_occurrence_id IS NOT NULL"
                )
            )

        if "campaign_execution_child_snapshots" not in table_names:
            connection.execute(
                text(
                    """
                    CREATE TABLE campaign_execution_child_snapshots (
                        id INTEGER NOT NULL PRIMARY KEY,
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
                        created_at DATETIME NOT NULL,
                        CONSTRAINT uq_campaign_child_position UNIQUE
                            (campaign_execution_id, position),
                        FOREIGN KEY(campaign_execution_id) REFERENCES campaign_executions (id),
                        FOREIGN KEY(saved_search_id) REFERENCES saved_searches (id),
                        FOREIGN KEY(child_run_id) REFERENCES search_runs (id)
                    )
                    """
                )
            )
            connection.execute(
                text(
                    "CREATE INDEX ix_campaign_child_snapshots_execution_order "
                    "ON campaign_execution_child_snapshots (campaign_execution_id, position)"
                )
            )

        if "campaign_execution_events" not in table_names:
            connection.execute(
                text(
                    """
                    CREATE TABLE campaign_execution_events (
                        id INTEGER NOT NULL PRIMARY KEY,
                        campaign_execution_id INTEGER NOT NULL,
                        child_snapshot_id INTEGER,
                        created_at DATETIME NOT NULL,
                        severity VARCHAR(20) NOT NULL,
                        code VARCHAR(80) NOT NULL,
                        phase VARCHAR(80) NOT NULL,
                        message TEXT NOT NULL,
                        metadata_json JSON,
                        FOREIGN KEY(campaign_execution_id) REFERENCES campaign_executions (id),
                        FOREIGN KEY(child_snapshot_id)
                            REFERENCES campaign_execution_child_snapshots (id)
                    )
                    """
                )
            )
            connection.execute(
                text(
                    "CREATE INDEX ix_campaign_execution_events_execution_created "
                    "ON campaign_execution_events (campaign_execution_id, created_at)"
                )
            )

        if "run_events" not in table_names:
            connection.execute(
                text(
                    """
                    CREATE TABLE run_events (
                        id INTEGER NOT NULL PRIMARY KEY,
                        run_id INTEGER NOT NULL,
                        source VARCHAR(50) NOT NULL DEFAULT 'seek',
                        created_at DATETIME NOT NULL,
                        severity VARCHAR(20) NOT NULL,
                        code VARCHAR(80) NOT NULL,
                        phase VARCHAR(80) NOT NULL,
                        page_number INTEGER,
                        url TEXT,
                        page_title VARCHAR(500),
                        message TEXT NOT NULL,
                        challenge_rule VARCHAR(120),
                        metadata_json JSON,
                        FOREIGN KEY(run_id) REFERENCES search_runs (id)
                    )
                    """
                )
            )
            connection.execute(
                text("CREATE INDEX ix_run_events_run_created ON run_events (run_id, created_at)")
            )
            connection.execute(
                text("CREATE INDEX ix_run_events_run_severity ON run_events (run_id, severity)")
            )
        else:
            existing_columns = {column["name"] for column in inspector.get_columns("run_events")}
            if "source" not in existing_columns:
                connection.execute(
                    text(
                        "ALTER TABLE run_events "
                        "ADD COLUMN source VARCHAR(50) NOT NULL DEFAULT 'seek'"
                    )
                )
            connection.execute(text("UPDATE run_events SET source = 'seek' WHERE source IS NULL"))

        if "job_rule_evaluations" not in table_names:
            connection.execute(
                text(
                    """
                    CREATE TABLE job_rule_evaluations (
                        id INTEGER NOT NULL PRIMARY KEY,
                        job_id INTEGER NOT NULL,
                        score INTEGER NOT NULL,
                        outcome VARCHAR(32) NOT NULL,
                        positive_evidence JSON NOT NULL,
                        penalty_evidence JSON NOT NULL,
                        exclusion_evidence JSON NOT NULL,
                        explanation TEXT NOT NULL,
                        profile_id VARCHAR(80) NOT NULL,
                        profile_version VARCHAR(40) NOT NULL,
                        profile_fingerprint VARCHAR(64),
                        evaluated_at DATETIME NOT NULL,
                        recommendation_override VARCHAR(32),
                        content_fingerprint VARCHAR(64) NOT NULL,
                        FOREIGN KEY(job_id) REFERENCES jobs (id)
                    )
                    """
                )
            )
            connection.execute(
                text(
                    "CREATE INDEX ix_job_rule_evaluations_job_evaluated "
                    "ON job_rule_evaluations (job_id, evaluated_at)"
                )
            )
            connection.execute(
                text(
                    "CREATE INDEX ix_job_rule_evaluations_profile "
                    "ON job_rule_evaluations (profile_id, profile_version)"
                )
            )
        else:
            existing_columns = {
                column["name"] for column in inspector.get_columns("job_rule_evaluations")
            }
            if "profile_fingerprint" not in existing_columns:
                connection.execute(
                    text(
                        "ALTER TABLE job_rule_evaluations "
                        "ADD COLUMN profile_fingerprint VARCHAR(64)"
                    )
                )
