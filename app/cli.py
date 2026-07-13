import argparse

from app.collectors.base import CollectorInput
from app.collectors.seek import SeekCollector
from app.config import get_settings
from app.database import SessionLocal, init_db
from app.logging_config import configure_logging
from app.repository import create_search_and_run


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a single visible-browser SEEK collection.")
    parser.add_argument("--keywords", required=True)
    parser.add_argument("--location", required=True)
    parser.add_argument("--date-listed", required=True)
    parser.add_argument("--max-pages", type=int, required=True)
    args = parser.parse_args()

    settings = get_settings()
    configure_logging(settings.log_level)
    init_db()
    with SessionLocal() as db:
        run = create_search_and_run(
            db, args.keywords, args.location, args.date_listed, args.max_pages
        )
        run_id = run.id
    collector = SeekCollector(settings, SessionLocal)
    collector.collect(
        CollectorInput(
            keywords=args.keywords,
            location=args.location,
            date_listed=args.date_listed,
            max_pages=args.max_pages,
            run_id=run_id,
        )
    )
    print(f"Run {run_id} finished. Open the web UI to inspect status and jobs.")


if __name__ == "__main__":
    main()

