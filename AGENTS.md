# JobHunter Agent Instructions

## Read first

Before changing code or operating a job application, read:

1. `IMPLEMENTATION_PLAN.md`
2. `APPLYPILOT_SYSTEM_BUILD_GUIDE.md`
3. `BROWSER_APPLICATION_RUNBOOK.md` for any browser work
4. `profile/candidate_profile.md` only when it exists and the task needs candidate facts

## System facts

- `profile/candidate_profile.md` is the editable candidate fact source. It is initialised from `profile/resume.md` once; never overwrite later user edits by re-parsing the resume.
- `data/application-dashboard.csv` is the persistent application ledger; `data/application-events.csv` is append-only history.
- `data/application-attempts.csv` is the Agent's internal execution ledger. It is not a user-operated queue. Do not create duplicate active attempts for a job.
- DeepSeek API produces job-fit scores from the Skill's stable `references/deepseek-scoring-rules.md`; the current Agent must not invent scores. Resume fit is a separate, explainable selection result from `resume_catalog.py`.
- Default automatic candidates are entry-level/junior roles. Freshness ordering is 24 hours, then 3 days, then older/unknown.

## Truth and privacy

- Never invent candidate identity, work rights, dates, skills, experience, metrics or resume claims.
- Treat `unknown` as a reason to ask or block, never as permission to guess.
- Do not print Candidate Profile contact details, raw resume contents, API keys or other sensitive values in logs.
- Keep `data/`, `profile/candidate_profile.md` and personalised resume files local; they are ignored by Git.

## ApplyPilot applications

- JobHunter owns job discovery, scoring, results, and the local Dashboard. It does not operate recruitment websites.
- For an operational search-and-apply request, use the `applypilot-au` skill configured at `applypilot.skill_path` and immediately run `venv/bin/python run.py --autopilot`.
- Read the Skill's `references/autonomous-jobhunter-run.md`, `references/jobhunter-integration.md`, platform playbook, and safety boundaries before browser work.
- After `--autopilot` finishes, the current Agent must consume `output/agent-run.json` and continue eligible attempts; do not ask the user to click Dashboard buttons, copy prompts, or start another Agent.
- Internal `selected` or legacy `queued` records are not data-transmission consent, browser activity, an attempt, or a submission. Inspect a concrete record with `venv/bin/python run.py --attempt-handoff <attempt-id>`.
- Let ApplyPilot decide `manual_submit` versus `auto_if_allowed`. LinkedIn and Indeed default to manual submission; SEEK and external ATS require current permission plus every auto-submit gate.
- Use the user's existing authenticated browser only through the browser/computer-control skill selected by ApplyPilot.
- Stop for CAPTCHA, unknown mandatory questions, account/permission prompts, legal terms or unsupported forms. Write back `needs_user` with a precise reason.
- Never bypass CAPTCHA, anti-bot controls, security warnings or platform access restrictions.
- Write outcomes through `run.py --attempt-update`; never edit the internal execution CSV directly when that interface is available.
- Mark an attempt/dashboard record `submitted` only after explicit external confirmation evidence appears.

## Verification

After relevant changes, run:

```bash
venv/bin/python test_pipeline.py
venv/bin/python test_seek_parse.py
venv/bin/python test_e2e_offline.py
venv/bin/python test_application_system.py
```
