# Project Handover

This document is for a fresh Codex thread taking over the local Job Search CRM.
It summarizes the verified repository state without including credentials, cookies,
browser-profile contents, collected descriptions, or other private data.

## Product Objective And Target Workflow

Build a Windows-first, local-only job-search CRM for collecting, reviewing, and
managing job opportunities from SEEK.

Target workflow:

1. Start the local FastAPI app.
2. Prepare the SEEK browser session if needed using the persistent visible
   Playwright profile.
3. Run a narrow SEEK search with keywords, location, date-listed, and page-limit
   inputs.
4. Let the collector save each job incrementally into SQLite.
5. Review dashboard status, run outcomes, persistent errors, discovery
   provenance, and job records in the local UI.
6. Export jobs to CSV for external review when needed.

Correctness and restartability matter more than pretending a collection
succeeded. Unexpected layouts, missing selectors, navigation failures, and
genuine verification challenges must be surfaced explicitly.

## Current Architecture And Stack

Repository stack:

- Python 3.14 using the existing `.venv`
- FastAPI and Uvicorn
- SQLite with SQLAlchemy ORM
- Playwright Chromium for visible browser collection
- Beautiful Soup for SEEK HTML parsing
- Jinja templates with local CSS
- pytest for tests
- Ruff for linting

Explicit exclusions:

- No React
- No Node
- No Docker
- No Selenium
- No cloud services
- No AI API
- No stealth tooling, CAPTCHA bypass, proxy rotation, or evasion

Main source layout:

- `app/main.py`: FastAPI routes, background collection queue, CSV export.
- `app/models.py`: SQLAlchemy entities and run status enum.
- `app/migrations.py`: additive SQLite migration helper.
- `app/database.py`: engine/session setup and deterministic initialization.
- `app/repository.py`: persistence helpers, deduplication, events, metrics.
- `app/collectors/base.py`: reusable collector interface and collector errors.
- `app/collectors/seek.py`: SEEK URL construction, parsing, detection, collection.
- `app/seek_session.py`: visible persistent-session preparation lifecycle.
- `app/presentation.py`: status labels, stop-reason labels, time/metric formatting.
- `app/templates/`: Jinja pages.
- `app/static/styles.css`: local UI styling.
- `tests/`: unit and route-rendering tests.

## Git Commit History And Completed Milestones

Current verified commit history:

- `0a38687 feat: build local SEEK job collection MVP`
- `4793183 feat: add collection observability and provenance`
- `52e61da Update models.py`
- `be1c406 feat: add job explorer and clear run reporting`

Completed milestones:

- Milestone 1: local SEEK collector MVP.
  - Visible Playwright Chromium collection.
  - SEEK URL construction from user inputs.
  - Incremental SQLite persistence.
  - Job deduplication by SEEK job ID with fallback key.
  - Search/run attribution.
  - Safe restart/resume behavior.
  - Local web UI and CSV export.

- Milestone 2: persistent observability and card provenance.
  - Persistent run events.
  - Persistent errors that survive final run status changes.
  - Safe event metadata filtering.
  - Challenge-rule logging.
  - Discovery provenance: card type, parser path, page, rank, first discovery.
  - Safe additive migration strategy.

- Milestone 3: job explorer and clear run reporting.
  - Dashboard summary and latest-run outcome.
  - `/jobs` explorer with filters, sorting, pagination, mobile cards, and CSV export.
  - `/runs` history with readable statuses, metrics, durations, stop reasons.
  - Improved run-detail page with summary, persistent issues, discovery provenance,
    and collapsed technical details.

## Database Entities And Migration Strategy

Primary entities:

- `Search`
  - Stores keywords, location, date-listed value, maximum pages, and creation time.

- `SearchRun`
  - Stores run status, timing, requested/attempted/completed pages, counts,
    stop reason, message, last URL, and relationship to search/discoveries/events.

- `Job`
  - Stores SEEK job ID when available, fallback key, title, company, location,
    salary text, work type, posting text, source, source listing URL, canonical URL,
    outbound URL, description, first seen, last seen, and updated timestamp.

- `JobDiscovery`
  - Links a job to a search/run.
  - Stores page number, card type, parser path, rank, and first discovery timestamp.
  - Has a uniqueness constraint on `(job_id, run_id)` to prevent duplicate
    discoveries within a run.

- `RunEvent`
  - Stores persistent chronological events/errors: run ID, timestamp, severity,
    stable code, phase, page number, URL, page title, message, challenge rule, and
    safe structured metadata.

Migration strategy:

- `app.database.init_db()` calls `migrate_database(engine)` before
  `Base.metadata.create_all`.
- `app/migrations.py` performs additive SQLite migrations only.
- Existing data must not be deleted or recreated.
- Missing columns are added with `ALTER TABLE`.
- `run_events` is created if absent.
- Existing jobs are backfilled with safe defaults for `source`,
  `source_listing_url`, `canonical_url`, and `last_seen_at`.
- Migration tests exercise preservation of pre-migration searches, runs, jobs,
  and discoveries.

## SEEK Collector And Persistent-Session Lifecycle

Collector behavior:

- `build_seek_search_url()` creates SEEK search URLs from keywords, location,
  date-listed, and page number.
- `SeekCollector.collect()` launches visible Chromium using the persistent
  profile at `data/browser-profiles/seek`.
- Results pages are parsed first; each job detail page is then visited.
- Jobs are saved incrementally via `save_job_discovery()` rather than buffered
  until the end.
- Collection records events throughout queueing, page parsing, detail errors,
  challenges, browser closure, and completion.

Session preparation:

- The dashboard exposes an Open/Prepare SEEK session action.
- `SeekSessionManager.open_prepare_browser()` opens visible Chromium with the
  same persistent profile and navigates to SEEK.
- The user signs in manually if desired.
- The user then clicks "I finished signing in".
- `SeekSessionManager.readiness()` checks signed-in indicators and genuine
  challenge conditions.
- Confirmation closes/releases the preparation browser unless a genuine
  challenge still needs manual completion.

Profile lifecycle rules:

- Never open two persistent contexts for the same profile simultaneously.
- If the preparation profile is busy, a collection run is queued as pending with
  a message explaining it is waiting for the profile to be released.
- Resume continues from the first incomplete page using `build_resume_input()`.

Challenge/login boundaries:

- Ordinary optional sign-in modals are not treated as CAPTCHA or access-denied
  challenges when normal search results are available.
- Genuine CAPTCHA, access-denied, unusual traffic, security check, robot, too
  many requests, and similar verification signals are handled separately.
- No bypass, stealth, proxy rotation, credential storage, or evasion is allowed.

## Current Routes And UI Structure

Routes:

- `GET /`
  - Dashboard with total jobs, recent jobs, completed runs, latest-run summary,
    SEEK session preparation, and run form.

- `POST /runs`
  - Starts a run from form fields: `keywords`, `location`, `date_listed`,
    `maximum_pages`.

- `GET /jobs`
  - Job explorer with filters for text, company, location, work type, first
    discovered window, posted text, salary presence, source, sort, and page size.

- `GET /jobs/export.csv`
  - Exports the current filtered job view.

- `GET /runs`
  - Run history with status, timing, pages, metrics, errors, stop reason, and
    links to details.

- `GET /runs/{run_id}`
  - Run detail with outcome summary, persistent errors/warnings, discovery
    provenance, and collapsed technical details.

- `POST /runs/{run_id}/resume`
  - Resumes only `awaiting_user` runs.

- `POST /seek-session/open`
  - Opens the persistent visible SEEK preparation browser.

- `POST /seek-session/confirm`
  - Checks session readiness, closes/releases the preparation browser when safe,
    and releases waiting runs.

- `GET /export/jobs.csv`
  - Exports all jobs.

Templates:

- `base.html`: shared page shell and navigation.
- `index.html`: dashboard.
- `jobs.html`: job explorer.
- `runs.html`: run list.
- `run.html`: run detail.

## Run Metrics And Stop Reasons

Run statuses:

- `pending`
- `running`
- `completed`
- `completed_with_errors`
- `awaiting_user`
- `interrupted`
- `blocked`
- `failed`

Run metrics:

- `result_cards_observed`
- `unique_jobs_in_run`
- `new_jobs_added`
- `known_jobs_rediscovered`
- `jobs_updated`
- `duplicate_cards_ignored`
- `pages_completed`
- legacy `jobs_found`
- legacy `error_count`

Stop reasons currently represented in presentation helpers:

- `requested_page_limit_reached`
- `no_next_page`
- `no_results`
- `no_new_ids`
- `verification_required`
- `login_required`
- `browser_closed`
- `cancelled`
- `failed`

Important nuance:

- `completed_with_errors` means listing pages were processed, but one or more
  job-detail pages failed.
- Collector completion records a final event but must not overwrite or erase
  earlier persistent errors.
- Legacy runs can have missing detailed metrics; the UI labels this explicitly.

## Security And Privacy Rules

Never store or commit:

- Credentials
- Passwords
- Cookies
- Browser-session files
- Browser-profile contents
- CAPTCHA or verification bypass data
- Captured page HTML
- Private collected job descriptions in docs or code comments
- Exported CSV files
- SQLite databases

Ignore rules already cover:

- `.venv/`
- `.env` and `.env.*`, except `.env.example`
- SQLite database files
- `data/browser-profiles/`
- `data/exports/`
- `data/captured-html/`
- `data/logs/`
- Python, pytest, Ruff, coverage, and OS/editor caches

Run events intentionally store safe structured metadata only. The allowlist is in
`SAFE_METADATA_KEYS` in `app/repository.py`.

## Standard Commands

Setup:

```powershell
Copy-Item .env.example .env
```

The `.env` file is optional unless overriding defaults.

Start the app:

```powershell
.\run.ps1
```

Open:

```text
http://127.0.0.1:8000
```

Run tests:

```powershell
.\.venv\Scripts\python.exe -m pytest
```

Run Ruff:

```powershell
.\.venv\Scripts\python.exe -m ruff check .
```

Optional CLI collection command:

```powershell
.\.venv\Scripts\python.exe -m app.cli --keywords "strategy analyst" --location "Sydney NSW" --date-listed last_3_days --max-pages 2
```

Do not run broad or repeated live SEEK collection without explicit user approval.

## Known Limitations

- SEEK selectors and markup can change; missing result cards or detail sections
  are treated as layout failures rather than zero results.
- Posting age is stored as raw SEEK text, not normalized dates.
- Salary is stored as raw display text.
- Canonical URL handling removes query and fragment for new records, but older
  migrated rows may retain raw URL text as their initial canonical value.
- Card type is `unknown` unless SEEK markup provides evidence.
- The background worker is a single-process `ThreadPoolExecutor`, not a durable
  external queue.
- The UI is local and pragmatic; it is not an authenticated multi-user product.
- CSV export includes job descriptions because that was part of the local data
  model; do not commit exports.
- Session readiness depends on observable SEEK UI indicators and can require
  manual confirmation.

## Existing Data That Must Be Preserved

Verified local database counts at handover time:

- `jobs`: 112
- `searches`: 5
- `search_runs`: 5
- `job_discoveries`: 214
- `run_events`: 19

Verified run status distribution:

- `completed`: 3
- `completed_with_errors`: 1
- `failed`: 1

The real database is `data/job_search_crm.sqlite3` and is ignored. Treat it as
user data. Do not delete, recreate, truncate, reset, or overwrite it.

The persistent browser profile is `data/browser-profiles/seek` and is ignored.
Treat it as user-owned session state. Do not inspect or commit it.

## Features Explicitly Deferred

Deferred unless the user explicitly asks:

- Broad or scheduled scraping.
- Additional job boards.
- Any stealth, CAPTCHA bypass, proxy rotation, or evasion.
- Credential storage.
- Cloud sync, hosted database, or deployment.
- Multi-user authentication.
- React or Node frontend.
- Dockerization.
- Selenium.
- AI-based ranking or summarization.
- Automated application submission.
- Contact enrichment or external email/calendar integrations.
- Deleting or archiving collected data.

## Approved Roadmap

Completed roadmap:

1. Milestone 1: local SEEK collection MVP.
2. Milestone 2: persistent observability and provenance.
3. Milestone 3: job explorer and clear run reporting.

Next product direction should build on the local CRM workflow without changing
the collector safety model or risking the preserved SQLite data/profile.

Recommended next stages:

1. Milestone 4: manual CRM workflow for reviewing and managing saved jobs.
2. Milestone 5: saved views, lightweight reporting, and export refinements.
3. Milestone 6: optional additional local-only collectors, only after SEEK
   workflow and data quality remain stable.

## Milestone 4 Goal And Boundaries

Goal:

Add a local manual CRM workflow on top of the collected jobs so the user can
triage, annotate, and track opportunities after collection.

Suggested Milestone 4 scope:

- Add job pipeline/status fields such as `new`, `reviewing`, `interested`,
  `applied`, `interviewing`, `rejected`, and `closed`.
- Add user notes for jobs.
- Add priority or fit rating.
- Add favorite/watchlist marker.
- Add application deadline or follow-up date if useful.
- Add job detail page for one saved job.
- Add filters for CRM status, priority, favorites, and follow-up due.
- Preserve existing collection fields and provenance.
- Add additive migrations and migration tests.
- Add route/template tests for the CRM workflow.

Milestone 4 boundaries:

- Do not run live SEEK collection unless explicitly requested.
- Do not modify the collector unless needed for compatibility with new fields.
- Do not delete or rewrite existing jobs, discoveries, runs, or events.
- Do not inspect browser-profile contents.
- Do not store credentials, cookies, or private session data.
- Do not add cloud services, AI APIs, React, Node, Docker, Selenium, stealth, or
  proxy features.
- Keep all schema changes additive and covered by tests.
