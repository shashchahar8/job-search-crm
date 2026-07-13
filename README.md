# Local Job Search CRM

Phase 1 is a Windows-first, local-only FastAPI CRM with a manually prompted visible-browser SEEK collector.

## Stack

- Python 3.14 from the existing `.venv`
- FastAPI and Uvicorn
- SQLite and SQLAlchemy
- Playwright Chromium
- Beautiful Soup parser fixtures
- Jinja templates and local CSS
- pytest and Ruff

No React, Node, Docker, Selenium, cloud service, proxy, stealth tooling, or AI API is used.

## Quick start on Windows

```powershell
.\run.ps1
```

Open `http://127.0.0.1:8000`.

The app stores local data under `data/`. The persistent Playwright profile is `data/browser-profiles/seek`.

## Run a narrow SEEK collection from the CLI

```powershell
.\.venv\Scripts\python.exe -m app.cli --keywords "strategy analyst" --location "Sydney NSW" --date-listed last_3_days --max-pages 2
```

A visible Chromium window opens. Do not save passwords in the browser profile. If SEEK shows login, CAPTCHA, verification, unusual-traffic, or another access challenge, the collector marks the run as `awaiting_user` and stops instead of bypassing it.

## Run statuses

- `completed`: all requested pages were processed without job-detail errors.
- `completed_with_errors`: listing pages were processed, but one or more job detail pages failed.
- `awaiting_user`: SEEK requested login, CAPTCHA, verification, or similar manual action.
- `interrupted`: the collector was interrupted.
- `blocked`: the page layout did not match expected SEEK structures, so correctness is unknown.
- `failed`: browser or navigation infrastructure failed.

Missing selectors, unexpected page layout, and navigation failures are never treated as successful zero-result runs.

## CSV export

Use the `Export CSV` link in the web UI or open:

```text
http://127.0.0.1:8000/export/jobs.csv
```

## Development checks

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
```

## Configuration

Copy `.env.example` to `.env` if you need to override defaults:

```powershell
Copy-Item .env.example .env
```

Important defaults:

- `DATABASE_URL=sqlite:///data/job_search_crm.sqlite3`
- `PLAYWRIGHT_PROFILE_DIR=data/browser-profiles/seek`
- `HOST=127.0.0.1`
- `PORT=8000`

