---
id: WF-MAP-001
title: Candidate-driven automatic job-application system
labels: [wayfinder:map]
status: open
created: 2026-07-24
---

# Candidate-driven automatic job-application system

## Destination

Turn `jobhunt-au` into a local, candidate-driven application system with an editable Candidate Profile, a persistent Dashboard, DeepSeek-based screening, resume selection, and automatic application paths for both broad and targeted applications. It must extend the existing web app and use the user's already authenticated ChatGPT Atlas browser session.

## Notes

- The user has now authorised execution through the active goal. `IMPLEMENTATION_PLAN.md` is the operative build plan and acceptance contract; this map remains the decision context.
- The existing project already gathers jobs from LinkedIn, Indeed and SEEK, deduplicates, filters, scores, and has a local `webapp.py`.
- The Candidate Profile is initially extracted from the candidate's existing resume, then becomes editable through the web app. It includes identity/contact details, resume facts, job directions, salary range, availability and visa status.
- The Dashboard is both a permanent history and a daily action surface.
- DeepSeek is the first matching engine. Jobright and Simplify are not part of the initial implementation.
- Roles published within 24 hours take priority; roles within three days are second priority. Default target seniority is entry level and junior.
- Broad and targeted application paths are selected from job match and resume-to-job fit. The exact thresholds remain undecided.
- Browser automation may use the user's pre-authenticated ChatGPT Atlas session. It must not bypass CAPTCHAs or platform security controls; unexpected pages are handed to the user.
- Before any browser-automation work, consult the `computer-use:computer-use` skill. Use the CodeGraph index initialized on 2026-07-24 before structural code changes.

## Decisions so far

- [Define Dashboard ledger and daily action model](../tickets/define-dashboard-ledger-and-daily-action-model.md) — JobHunter owns search and the Dashboard projection; its local-only queue hands selected jobs to `applypilot-au`, which owns platform policy, execution and evidenced outcome write-back.

## Not yet specified

- The first application platforms to support end-to-end and their differing form-field mappings.
- Exact score bands and the rule that distinguishes targeted from broad application.
- The initial resume catalogue, whether one base resume is enough at launch, and how tailored material is approved.
- The manual-takeover flow for CAPTCHAs, screening questions, broken forms and other browser exceptions.
- Follow-up cadence, notifications and how external responses are captured.
- Calibration of DeepSeek prompts, cost limits and how human corrections improve later recommendations.

## Out of scope

- Integrating Jobright or Simplify in the initial system; DeepSeek is the selected first matching engine.
- Bypassing CAPTCHAs, anti-bot protections, authentication controls or platform security checks.
- Unrelated recruiting outreach such as automatically sending LinkedIn messages or email campaigns.
