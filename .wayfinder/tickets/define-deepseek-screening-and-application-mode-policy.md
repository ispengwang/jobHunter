---
id: WF-003
title: Define DeepSeek screening and application-mode policy
parent: WF-MAP-001
labels: [wayfinder:grilling]
status: open
assignee: unassigned
blocked_by: [WF-001]
mode: HITL
---

# Define DeepSeek screening and application-mode policy

## Question

How should DeepSeek produce an explainable job-fit score and a separate resume-to-job fit score, apply freshness and seniority rules, and decide whether a role follows the broad or targeted application path?

## Known context

- DeepSeek is the initial match engine.
- Jobs posted within 24 hours take first priority; jobs posted within three days take second priority.
- Default target seniority is entry level and junior; other levels are not yet in the default path.
- The user wants broad and targeted application modes, decided mainly by match score and resume-to-job fit.
