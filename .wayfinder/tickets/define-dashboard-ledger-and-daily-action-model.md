---
id: WF-002
title: Define Dashboard ledger and daily action model
parent: WF-MAP-001
labels: [wayfinder:grilling]
status: closed
assignee: codex
blocked_by: []
mode: HITL
---

# Define Dashboard ledger and daily action model

## Question

What durable record and status lifecycle should a job application have, and how should the existing web app turn that history into daily priority queues, review queues, follow-ups and exception handling?

## Known context

- The Dashboard records company, job, URL, application status, resume used, skip reason and other actions.
- It is both a permanent history and the user's daily action panel.
- The implementation should extend the existing `webapp.py` rather than introduce a separate product.

## Resolution

JobHunter is the discovery and presentation layer. It owns scraping, deduplication, scoring, resume suggestions, `data/application-dashboard.csv`, the append-only event log, and the `/dashboard` UI. It does not operate recruitment websites or duplicate platform policy.

The Dashboard action is **“加入 ApplyPilot 投递队列”**:

- it creates one idempotent local handoff in `data/application-attempts.csv`;
- it does not start Codex, open a browser, transmit candidate data, count an attempt, or count a submission;
- it moves a `review` job to `ready_to_apply`, not `applying`;
- `/applypilot` explains the three-step flow and provides a machine-readable handoff plus an exact Codex prompt.

The configured `applypilot-au` Skill is the sole application policy and executor. It decides `manual_submit` versus `auto_if_allowed`, checks profile/material readiness, platform rules, safety gates and daily limits, and writes outcomes back through `run.py --attempt-update`.

Status projection is:

- `queued` → `ready_to_apply`;
- `browser_opened` / `filling` / `ready_to_submit` → `applying`;
- `needs_user` → `needs_user`;
- `failed` → `blocked`;
- evidenced `submitted` → `submitted`;
- `cancelled` → `review`.

`submitted` requires both confirmation and explicit platform evidence. Queueing, page opening, autofill, file upload and an unconfirmed Submit click are never submission evidence.

Implementation pointers:

- [Dashboard UI and routes](../../webapp.py)
- [ApplyPilot handoff queue](../../application_attempts.py)
- [Dashboard state model](../../dashboard.py)
- [ApplyPilot runbook](../../BROWSER_APPLICATION_RUNBOOK.md)
