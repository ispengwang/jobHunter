# Local Markdown Wayfinder Tracker

This repository has no configured issue tracker, so Wayfinder uses this local Markdown tracker.

- Maps live in `maps/` and have the `wayfinder:map` label.
- Tickets live in `tickets/`, declare their parent map in front matter, and use exactly one `wayfinder:<type>` label.
- A ticket is claimed by setting `assignee` from `unassigned` to the active developer.
- A ticket is on the frontier when it is `open`, `unassigned`, and every ticket in `blocked_by` is `closed`.
- Resolutions are appended to the ticket under `## Resolution`; closing a ticket adds a one-line link to its parent map's `## Decisions so far`.

This tracker is for Wayfinder decisions, not implementation tasks or bug reports.
