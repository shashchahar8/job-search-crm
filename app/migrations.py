from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

DISCOVERY_COLUMNS = {
    "card_type": "VARCHAR(32) NOT NULL DEFAULT 'unknown'",
    "parser_path": "VARCHAR(255) NOT NULL DEFAULT 'unknown'",
    "rank": "INTEGER",
}

RUN_COLUMNS = {
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


def migrate_database(engine: Engine) -> None:
    """Apply small additive SQLite migrations without deleting existing data."""
    if not engine.url.get_backend_name().startswith("sqlite"):
        return

    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    with engine.begin() as connection:
        if "job_discoveries" in table_names:
            existing_columns = {
                column["name"] for column in inspector.get_columns("job_discoveries")
            }
            for column_name, definition in DISCOVERY_COLUMNS.items():
                if column_name not in existing_columns:
                    connection.execute(
                        text(f"ALTER TABLE job_discoveries ADD COLUMN {column_name} {definition}")
                    )

        if "search_runs" in table_names:
            existing_columns = {column["name"] for column in inspector.get_columns("search_runs")}
            for column_name, definition in RUN_COLUMNS.items():
                if column_name not in existing_columns:
                    connection.execute(
                        text(f"ALTER TABLE search_runs ADD COLUMN {column_name} {definition}")
                    )

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

        if "run_events" not in table_names:
            connection.execute(
                text(
                    """
                    CREATE TABLE run_events (
                        id INTEGER NOT NULL PRIMARY KEY,
                        run_id INTEGER NOT NULL,
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
