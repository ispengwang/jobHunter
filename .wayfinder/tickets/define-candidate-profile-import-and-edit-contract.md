---
id: WF-001
title: Define Candidate Profile import and edit contract
parent: WF-MAP-001
labels: [wayfinder:grilling]
status: open
assignee: unassigned
blocked_by: []
mode: HITL
---

# Define Candidate Profile import and edit contract

## Question

What is the canonical Candidate Profile schema, which fields are extracted from the existing resume, which fields must be supplied or confirmed by the user, and how do profile edits become the durable source of truth?

## Known context

- The initial profile is extracted from the user's resume and then edited in the web app.
- It must support name, contact details, resume facts, desired directions, salary range, start date and visa/work-right status.
- Missing facts must remain unknown rather than being inferred by DeepSeek or any other model.
