from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

DISCOVERY_COLUMNS = {
    "card_type": "VARCHAR(32) NOT NULL DEFAULT 'unknown'",
    "parser_path": "VARCHAR(255) NOT NULL DEFAULT 'unknown'",
    "rank": "INTEGER",
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
