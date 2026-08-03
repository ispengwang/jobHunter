# Candidate-Driven Application System — Implementation Plan and Acceptance Criteria

## Objective

Extend the existing `jobhunt-au` project into a local application system that:

- extracts an initial Candidate Profile from the current resume and lets the candidate edit it in the web app;
- stores an auditable Dashboard of jobs, application state, selected resume, reasons and events;
- ranks jobs with DeepSeek using entry/junior eligibility, 24-hour and 3-day freshness priorities, and separate job-fit and resume-fit decisions;
- supports broad and targeted application preparation paths;
- hands selected jobs to the separate `applypilot-au` Skill, which owns platform policy, permitted auto-submit, daily limits, browser execution, blockers and submission evidence.

The system remains local-first. It does not bypass platform security controls, invent facts, or mark an application as submitted without a recorded successful submission event.

## Scope decisions used for implementation

| Area | Initial implementation decision |
| --- | --- |
| Candidate Profile | Parse `profile/resume.md` for safe initial values, persist an editable profile locally, and surface fields missing from the source resume for user completion. |
| Dashboard persistence | CSV files in `data/`: one stable job/application row per canonical job ID plus an append-only event log. |
| Default seniority | Only new grad, entry-level and junior roles are eligible by default; other levels are retained for manual review. |
| Freshness | Jobs posted within 24 hours are highest priority, those within 3 days are next, and unknown/older dates remain visible but do not receive the priority boost. |
| Matching | DeepSeek's existing score remains the job-fit score. A deterministic resume catalogue selector produces an explicit resume-fit score and reason; the UI keeps both visible. |
| Application paths | Targeted: high job-fit plus high resume-fit; Broad: eligible but lower-confidence roles. Exact thresholds are configurable in `config.yaml`. |
| Resumes | `profile/resume.md` is the initial default variant. A manifest supports more variants without copying or fabricating resume facts. |
| Application execution | A full `applypilot-au` invocation can run search through execution, while each Dashboard card can also start one existing handoff immediately. Neither path exposes an attempt queue or copied prompt. |

## Delivery plan

### 1. Foundation — profile, resume catalogue and dashboard storage

- Add profile parsing, validation and persistence.
- Add a default resume manifest and a reusable resume catalogue loader.
- Add dashboard and append-only event storage with idempotent upsert behaviour.
- Extend configuration paths and policy values.

### 2. Decision pipeline — screening, resume selection and material preparation

- Add freshness, seniority and application-mode classification to scored jobs.
- Separate job-fit from resume-fit, include reasons, and persist both to the Dashboard.
- Generate material in a stable job-ID directory using the selected resume variant.
- Do not send jobs that are already submitted, skipped or currently blocked back to the active queue.

### 3. Local web app — profile settings and action Dashboard

- Add Candidate Profile import, display, edit and validation routes.
- Add Dashboard filters, daily action queues, job detail, status updates and event history.
- Add clear actions for `review`, `ready_to_apply`, `submitted`, `skipped`, `unavailable` and `blocked`; require a reason where the policy requires one.

### 4. ApplyPilot integration — autonomous Agent execution and outcomes

- Keep an internal execution ledger with explicit states, artifact paths and failure reasons for idempotency and audit.
- Generate one machine-readable `output/agent-run.json` manifest automatically after search and scoring.
- Let the current Codex Agent consume the manifest immediately; do not expose a user-operated queue or copyable launch prompt.
- Let a per-job Dashboard button record job-level authorization and invoke a serial background Codex executor for permitted external ATS flows; manual platforms open for user operation.
- Keep browser and platform decisions out of JobHunter; consume the configured `applypilot-au` Skill instead.
- Add a deterministic CLI write-back interface for progress, `needs_user`, failures and evidenced submissions.
- Stop and record `needs_user` for user-resolvable exceptions and `blocked` for unrecoverable failures. Never bypass controls.

### 5. Verification and documentation

- Add unit and offline end-to-end tests for every new persistence and policy invariant.
- Run the original tests plus new tests.
- Update README, HOWTO and the existing architecture guide so their stated submission policy matches the autonomous Agent model.

## Acceptance criteria and evidence

| Requirement | Acceptance criterion | Evidence |
| --- | --- | --- |
| Candidate Profile | A profile can be initialised from `profile/resume.md`, edited, persisted and reloaded without silently replacing user edits. Missing required values are reported as `unknown`/validation items. | Profile unit tests and web-route test. |
| Resume variants | A manifest supports the default resume plus additional paths. Selection returns `resume_id`, fit score and reason without adding unsupported claims. | Resume catalogue tests. |
| Dashboard memory | Reprocessing the same job updates one canonical row, preserves user status/notes, and creates an event for meaningful state changes. | Dashboard idempotency and event-log tests. |
| Dashboard state safety | `skipped`/`blocked` require a reason; `unavailable` requires a typed availability reason; `submitted` requires an explicit recorded submission outcome and timestamp. | State-transition tests. |
| Screening policy | Entry/junior jobs are eligible by default; senior roles are review/skip candidates; freshness priority is ordered `<24h`, `<=3d`, older/unknown. | Policy tests using fixed dates. |
| Broad vs targeted | Every eligible job has separate job-fit and resume-fit values, an explicit mode and a reason. Thresholds are configurable. | Pipeline test with fixture jobs. |
| Generated materials | Material uses the selected resume source, stable job-ID output path and summary metadata. | Generation test. |
| Web app | The existing local app exposes profile editing, daily queues, dashboard rows, job details, status mutation and event history. | Flask route tests and local smoke check. |
| ApplyPilot workflow | `run.py --autopilot` uses the Skill's user-auditable scoring rules, calls DeepSeek, updates Dashboard, and creates an internal manifest ordered by 24-hour freshness then score. Light automation targets 30 and hard-stops at 40 confirmed daily submissions. `submitted` still requires external confirmation evidence. | Execution-ledger/state tests; manifest/CLI tests; optional live session smoke test. |
| Privacy and truthfulness | No profile secrets are logged, and material generation receives only resume/profile facts. | Log redaction and prompt-context tests. |
| Regression protection | Existing scraper, dedupe, visa, scoring and offline E2E tests remain green. | Commands listed below. |

## Required verification commands

```bash
python test_pipeline.py
python test_seek_parse.py
python test_e2e_offline.py
python test_application_system.py
```

For the live browser criterion, an already authenticated user session and a specific job application are required. That final external action cannot be proven by offline tests and must be confirmed at the time of submission.
