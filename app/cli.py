import argparse
import sys
from pathlib import Path

from app.collectors.base import CollectorInput
from app.collectors.seek import SeekCollector
from app.config import get_settings
from app.database import SessionLocal, init_db
from app.logging_config import configure_logging
from app.repository import create_search_and_run
from app.rules import load_profile_registry


def main() -> None:
    parser = argparse.ArgumentParser(description="Local Job Search CRM utilities.")
    subparsers = parser.add_subparsers(dest="command")
    validate_parser = subparsers.add_parser(
        "validate-rules", help="Validate local JSON rule profiles."
    )
    validate_parser.add_argument("--profile-dir", type=Path)
    parser.add_argument("--keywords")
    parser.add_argument("--location")
    parser.add_argument("--date-listed")
    parser.add_argument("--max-pages", type=int)
    args = parser.parse_args()

    if args.command == "validate-rules":
        registry = (
            load_profile_registry(args.profile_dir)
            if args.profile_dir
            else load_profile_registry()
        )
        for profile in registry.valid_profiles:
            default_marker = " default" if profile.is_default else ""
            print(f"VALID {profile.id} {profile.version}{default_marker} {profile.source_path}")
        for error in registry.errors + registry.duplicate_errors:
            print(f"INVALID {error}", file=sys.stderr)
        if registry.errors or registry.duplicate_errors or not registry.valid_profiles:
            raise SystemExit(1)
        return

    if not args.keywords or not args.location or not args.date_listed or not args.max_pages:
        parser.error(
            "--keywords, --location, --date-listed and --max-pages are required for collection"
        )

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
