# Project Handover

This document is for a fresh Codex thread taking over the local Job Search CRM.
It summarizes the repository architecture and accepted milestones without
including credentials, cookies, browser-profile contents, collected job
descriptions, exports, or ignored SQLite contents.

## Product Objective

Build a Windows-first, local-only job-search CRM for collecting, reviewing,
evaluating, and managing job opportunities from source-aware collectors. SEEK is
currently the only enabled and supported collector.

The intended workflow is:

1. Start the local FastAPI app.
2. Prepare the SEEK browser session manually if needed.
3. Run a narrow SEEK search with explicit keywords, location, date-listed, and
   page-limit inputs.
4. Save jobs incrementally to SQLite with discovery provenance.
5. Review jobs in the CRM, including deterministic rule recommendations.
6. Use CSV export for local review when needed.
7. Configure reusable saved searches and campaign execution plans for future
   sequential campaign collection.

Correctness, auditability, and restartability matter more than pretending a run
or evaluation succeeded.

## Current Stack

- Python 3.14 using the existing `.venv`
- FastAPI and Uvicorn
- SQLite with SQLAlchemy ORM
- Playwright Chromium for visible SEEK collection
- Beautiful Soup for SEEK HTML parsing
- Jinja templates with local CSS
- pytest and Ruff

Explicit exclusions remain: no React, Node frontend, Docker, Selenium, cloud
services, AI API, stealth tooling, CAPTCHA bypass, proxy rotation, or automated
application submission.

## Main Source Layout

- `app/main.py`: FastAPI routes, profile selection, background queue, campaigns,
  execution-plan preview, and CSV export.
- `app/campaigns.py`: saved-search/campaign validation and pending execution
  snapshot planning.
- `app/models.py`: SQLAlchemy entities for runs, jobs, CRM fields, evaluations,
  campaigns, snapshots, and events.
- `app/migrations.py`: additive SQLite migrations.
- `app/repository.py`: persistence helpers, deduplication, CRM preservation,
  events, and metrics.
- `app/collectors/seek.py`: SEEK URL construction, challenge detection, parsing,
  and collection.
- `app/collectors/registry.py`: source identifiers, collector registry, and
  declared source capabilities.
- `app/seek_session.py`: visible persistent-session preparation lifecycle.
- `app/rules.py`: deterministic JSON-profile rule engine and validation.
- `config/rule_profiles/`: registered JSON evaluation profiles.
- `app/templates/`: Jinja UI.
- `tests/`: unit, route-rendering, migration, rule, and profile tests.

## Completed Milestones

Committed history includes Milestones 1-5, with the latest accepted milestone at
commit `d11fe8e` (`Milestone 5 completion`).

- Milestone 1: local SEEK collection MVP with incremental SQLite persistence.
- Milestone 2: persistent run events, errors, and discovery provenance.
- Milestone 3: job explorer, filtering, pagination, run reporting, and export.
- Milestone 4: manual CRM workflow with status, priority, favorites, notes,
  deadlines, follow-ups, detail page, filters, exports, and additive migration
  coverage.
- Milestone 5: deterministic evaluation engine with history, overrides, CSV/UI
  audit fields, calibrated JSON profiles, profile fingerprints, and validation.

Milestone 5.1 is complete as a narrow usability/safety correction. It keeps the
accepted engine architecture and scoring behavior, while tightening profile
selection and profile-regex validation.

Milestone 6A source-aware collector registry foundation is present in the
working branch: `seek` is enabled and supported; `prosple`, `seek_grad`, and
`linkedin` are known future identifiers but are rejected and not implemented.

Milestone 6B adds saved-search and campaign configuration plus pending
execution-plan snapshots.

Milestone 6C adds safe sequential campaign execution. Campaign executions use
the same single `ThreadPoolExecutor`, source registry, child `SearchRun` rows,
collector interface, SEEK persistent profile lock, run statuses, run events, and
metrics as one-off collection. There is not a second worker/session subsystem.
Campaign orchestration only sequences child snapshots and aggregates progress.
Start and Resume requests are guarded per execution so rapid repeated posts do
not create duplicate workers. Scheduling and Prosple, SEEK Grad, or LinkedIn
collection remain deferred.

## Deterministic Rule Engine

`app/rules.py` evaluates saved jobs with a registered `RuleProfile`.

Accepted behavior:

- The default profile is
  `early_career_strategy_commercial_growth_ops` version `2026-07-13.2`.
- The engine is deterministic and evidence-backed.
- Each evaluation stores score, outcome, positive evidence, penalty evidence,
  hard-exclusion evidence, explanation, profile ID/version/fingerprint,
  content fingerprint, and evaluation timestamp.
- Latest evaluation state is one of evaluated, unevaluated, or stale, based on
  profile ID/version/fingerprint and job content fingerprint.
- Hard exclusions override the final outcome but preserve the numeric score and
  score-based outcome for audit.
- Manual recommendation overrides are stored on evaluations and do not alter CRM
  status, priority, notes, or other manual CRM fields.

Calibrated `.2` profile behavior:

- Description-positive contribution is capped at +20.
- At most 3 description-positive rules contribute score, while all matched
  evidence remains stored.
- Structured evidence distinguishes `matched_weight` from `effective_weight`.
- Senior and Lead title signals cap the recommendation at `review`.
- Manager, Principal, Head, and Director title signals cap the recommendation at
  `weak_match`.
- These seniority title signals are not hard exclusions.
- Technical/accounting specialist hard exclusions can produce `exclude` even
  when the numeric score is 50 or higher.

## JSON Profiles And Validation

Profiles live in `config/rule_profiles/*.json`.

The current registered profiles are:

- `default.json`: default early-career profile
  `early_career_strategy_commercial_growth_ops` / `2026-07-13.2`.
- `example-copy.json`: non-default example profile
  `example_custom_strategy_profile` / `2026-07-13.1`, named
  `EXAMPLE TO COPY - custom strategy profile`.

Each profile has a content fingerprint derived from canonical JSON. Reusing the
same ID/version with different profile content is rejected for evaluation when
stored non-legacy evaluations already exist.

Validation command:

```powershell
.\.venv\Scripts\python.exe -m app.cli validate-rules
```

Milestone 5.1 regex safety:

- General user-defined regex is not open-ended.
- Regex is limited to the controlled existing rule IDs:
  `seniority_title`, `seniority_description`, `years_experience`, and
  `technical_mandatory`.
- Other custom rules should use phrase matching.
- Validation enforces maximum pattern length, maximum total patterns per rule,
  maximum regex patterns per rule, and rejects backreferences, unsupported
  lookarounds, nested/repeated quantifiers, unbounded dot-star/dot-plus, and
  representative catastrophic-backtracking structures.

## Profile Selection

Milestone 5.1 uses one combined profile selector everywhere a user chooses an
evaluation profile.

Selector design:

- Normal rendered forms submit `profile_key`.
- A key is the exact registered ID/version pair:
  `<profile_id>::<profile_version>`.
- Option labels display readable profile name and version.
- The backend parses the key and still validates the exact pair against the
  registry.
- The UI never builds separate profile ID and version selectors, so a user
  cannot submit a mismatched pair through normal rendered options.
- Malformed, unknown, or mismatched explicit keys are rejected; they are not
  inferred or silently substituted.

Profile-aware workflows include:

- Jobs page profile filter.
- Bulk evaluate unevaluated jobs.
- Re-evaluate stale jobs.
- Single-job evaluation.
- Manual recommendation override on a selected profile evaluation.
- Filtered CSV export and dashboard rule counts.

## Database Entities And Migration Strategy

Primary entities:

- `Search`: one-off search inputs with source provenance.
- `SearchRun`: one-off run status, timing, page counts, metrics, stop reason,
  source, and message.
- `SavedSearch`: reusable saved-search definition with unique readable name,
  source, exact query text, location, stable date window, maximum pages,
  enabled/archived flags, and timestamps.
- `Campaign`: readable campaign name, description, active/archived state, and
  timestamps.
- `CampaignSavedSearch`: explicit ordered campaign membership with enabled flag.
- `CampaignExecution`: parent campaign execution-plan snapshot and aggregate
  metrics for Milestone 6C.
- `CampaignExecutionChildSnapshot`: ordered frozen child search snapshots for
  future execution.
- `CampaignExecutionEvent`: parent-level campaign execution events such as
  queued, child started/completed/failed, awaiting user, resumed, interrupted,
  completed, failed, and profile released.
- `Job`: collected job fields plus manual CRM fields.
- `JobDiscovery`: job/run provenance, source, page, rank, parser path, and card
  type.
- `RunEvent`: persistent chronological run events, source, and errors.
- `JobRuleEvaluation`: deterministic rule history, evidence, fingerprints, and
  recommendation override.

Migration strategy:

- `app.database.init_db()` calls `migrate_database(engine)` before
  `Base.metadata.create_all`.
- Migrations are additive SQLite migrations only.
- Existing user data must not be deleted, recreated, truncated, reset, or
  overwritten.
- Legacy evaluations without profile fingerprints remain readable.

## Routes And Workflows

Current routes:

- `GET /`: dashboard, run form, CRM/rule counts, latest run, SEEK preparation.
- `GET /campaigns`: campaign list and campaign-planning summary.
- `GET /campaigns/new` / `POST /campaigns`: create campaigns.
- `GET /campaigns/{id}`: campaign detail, ordered saved searches, memberships,
  execution history foundation, and saved-search creation.
- `GET /campaigns/{id}/edit` / `POST /campaigns/{id}/edit`: edit campaign
  name, description, and active state.
- `POST /campaigns/{id}/archive`: archive campaigns without deleting history.
- `POST /campaigns/{id}/saved-searches`: create a saved search and add it to
  the campaign.
- `POST /campaigns/{id}/memberships`: add an existing saved search to a
  campaign.
- `POST /campaign-memberships/{id}/update`: update order and enabled state.
- `POST /campaign-memberships/{id}/remove`: remove membership.
- `POST /saved-searches/{id}/update`: edit saved-search definitions.
- `GET /campaigns/{id}/preview`: validate and preview an ordered execution plan
  without source/network/browser access.
- `POST /campaigns/{id}/executions`: store a pending execution plan snapshot
  only; no collector starts.
- `POST /campaign-executions/{id}/start`: queue a pending campaign execution.
- `POST /campaign-executions/{id}/resume`: resume an awaiting-user campaign
  execution.
- `GET /campaign-executions/{id}`: show stored parent and child snapshots,
  progress, aggregate metrics, and start/resume actions.
- `GET /campaign-executions/{id}/export.csv`: export discoveries with explicit
  campaign, child, run, source, CRM, and current-rule fields.
- `POST /runs`: starts a SEEK collection run.
- `GET /jobs`: job explorer, CRM filters, rule filters, profile selector.
- `GET /jobs/export.csv`: exports the current filtered job view with rule fields.
- `POST /jobs/evaluate-bulk`: evaluates unevaluated, stale, or all jobs for the
  selected profile.
- `GET /jobs/{job_id}`: job detail, CRM controls, rule assessment, provenance.
- `POST /jobs/{job_id}/evaluate`: evaluates one job for the selected profile.
- `POST /jobs/{job_id}/recommendation-override`: saves a manual recommendation
  override for the selected profile evaluation.
- `POST /jobs/{job_id}/crm`: saves manual CRM fields.
- `GET /runs`: run history.
- `GET /runs/{run_id}`: run details, events, warnings, errors, discoveries.
- `POST /runs/{run_id}/resume`: resumes only `awaiting_user` runs.
- `POST /seek-session/open`: opens the persistent visible SEEK preparation
  browser.
- `POST /seek-session/confirm`: checks readiness, releases the profile when safe,
  and releases waiting runs.
- `GET /export/jobs.csv`: exports all jobs with default-profile rule fields.

Do not run live SEEK collection, inspect the browser profile, or modify the real
database unless the user explicitly asks.

## Standard Commands

Start the app:

```powershell
.\run.ps1
```

Run the complete test suite:

```powershell
.\.venv\Scripts\python.exe -m pytest
```

Run Ruff:

```powershell
.\.venv\Scripts\python.exe -m ruff check .
```

Validate rule profiles:

```powershell
.\.venv\Scripts\python.exe -m app.cli validate-rules
```

Current collected test count after Milestone 5.1 verification: 67 tests.
Current collected test count after Milestone 6B verification: 88 tests.
Current collected test count after Milestone 6C verification: 100 tests.

## Campaign Planning Notes

Saved searches preserve quoted and Boolean query text exactly. Do not rewrite
OR queries or split them into multiple searches.

Campaign date-window labels:

- Previous 24 hours
- Last 2 days
- Last 3 days
- Last 7 days
- Last 14 days
- Last 30 days

Stable internal values are used for snapshots. The legacy one-off SEEK values
remain readable for old runs, but campaign UI should not label rolling 24 hours
as "Today".

Recommended operating patterns:

- Daily campaign: use a two-day overlap to reduce missed listings.
- Weekly reconciliation: use Last 7 days to catch jobs missed by daily passes.

Campaign preview warns that source results are non-exhaustive and can change.
Execution snapshots freeze the effective plan so later edits to a campaign or
saved search do not alter stored child snapshots.

Milestone 6C execution behavior:

- Starting a campaign queues the parent execution on the existing single
  background executor.
- Each child snapshot creates or resumes a normal `SearchRun`; request-scoped
  database sessions are never passed into worker threads.
- The collector is resolved through the source registry and called through the
  generic collector interface.
- SEEK-specific URL construction, challenge detection, parsing, and browser
  lifecycle remain inside `SeekCollector`.
- The campaign owns the source profile for the whole parent execution. The
  current SEEK collector opens and closes one persistent browser context per
  child, not one reused context for the full campaign; exclusive ownership is
  retained across children so one-off runs, another campaign, or the SEEK
  preparation browser cannot interleave.
- Duplicate Start and Resume posts are idempotent at the execution guard and
  show readable messages for invalid or already-queued transitions.
- If the SEEK preparation browser/profile is busy, the campaign stays pending
  with a readable source-session wait message.
- If a child run becomes `awaiting_user`, the parent campaign execution becomes
  `awaiting_user`, keeps the current child pointer, and resumes the same child
  after manual source-session preparation.
- Recoverable child failures mark that child failed and continue to later
  children; the final parent becomes `completed_with_errors`. Browser/context
  interruption stops later children as `interrupted`. Unsupported sources fail
  before browser/profile work starts.
- Startup reconciliation marks persisted `running` campaign executions as
  `interrupted` with a readable `server_restarted` reason rather than silently
  restarting them.
- Child run metrics are copied into child snapshots and aggregated onto the
  parent execution. Campaign-unique jobs are recomputed from stored
  `JobDiscovery` rows with distinct job IDs, so the same job found by multiple
  children counts once for the parent.

## Safety And Privacy Rules

Never store or commit:

- Credentials, passwords, cookies, or browser-session files.
- Browser-profile contents.
- CAPTCHA or verification bypass data.
- Captured page HTML.
- Private collected job descriptions in docs or code comments.
- Exported CSV files.
- SQLite databases.

Ignored user-owned state includes the real database, browser profiles, exports,
captured HTML, and logs. Treat those as private local data.

## Known Limitations

- SEEK selectors and markup can change.
- Posting age and salary are stored as raw display text.
- The background worker is a single-process `ThreadPoolExecutor`.
- The UI is local and pragmatic, not an authenticated multi-user product.
- CSV export includes job descriptions because they are part of the local data
  model; do not commit exports.
- Profile regex customization is intentionally constrained to preserve local
  safety and deterministic validation.

## Deferred Features

Deferred unless explicitly requested:

- Broad or scheduled scraping.
- Additional job boards.
- Stealth, CAPTCHA bypass, proxy rotation, or evasion.
- Credential storage.
- Cloud sync, hosted database, or deployment.
- Multi-user authentication.
- React or Node frontend.
- Dockerization or Selenium.
- AI-based ranking or summarization.
- Automated application submission.
- Contact enrichment or external email/calendar integrations.
- Deleting or archiving collected data.
