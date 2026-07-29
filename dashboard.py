"""Persistent application Dashboard and append-only event log."""
from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

from application_policy import freshness_bucket
from schema import Job


STATUSES = {
    "new", "review", "ready_to_apply", "applying", "needs_user", "submitted",
    "follow_up", "skipped", "blocked", "interview", "rejected", "withdrawn",
}
REASON_REQUIRED = {"skipped", "blocked", "needs_user"}

DASHBOARD_FIELDS = [
    "job_id", "company", "title", "source", "url", "duplicate_urls", "location",
    "posted_at", "discovered_at", "first_seen_at", "job_fit_score", "job_fit_reason", "job_summary", "match_score",
    "resume_id", "resume_path", "resume_fit_score", "resume_reason", "application_mode",
    "freshness_bucket", "sponsorship_signal", "status", "skip_reason",
    "blocked_reason", "needs_user_reason", "submitted_at", "submission_evidence",
    "next_action", "last_action", "last_updated_at", "notes", "artifact_path",
    "last_run_id", "last_synced_at",
]
EVENT_FIELDS = [
    "event_id", "job_id", "run_id", "timestamp", "actor", "from_status", "to_status",
    "action", "reason", "artifact_path", "details",
]


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Dashboard:
    def __init__(self, path: Path, events_path: Path, freshness_config: dict | None = None):
        self.path = path
        self.events_path = events_path
        # ``None`` keeps the lightweight storage class backwards compatible for callers that
        # only need the persisted bucket. Production callers pass config so queue ordering ages
        # naturally between scoring runs.
        self.freshness_config = freshness_config

    def load_rows(self) -> list[dict[str, str]]:
        if not self.path.exists():
            return []
        with self.path.open(encoding="utf-8", newline="") as f:
            return list(csv.DictReader(f))

    def get(self, job_id: str) -> dict[str, str] | None:
        return next((row for row in self.load_rows() if row.get("job_id") == job_id), None)

    def current_freshness(self, row: dict[str, str], now: datetime | None = None) -> str:
        """Return the bucket for the current clock, not the bucket from an old run."""
        if self.freshness_config is None:
            return row.get("freshness_bucket") or "unknown"
        return freshness_bucket(
            row.get("posted_at"), self.freshness_config, now,
            first_seen_at=row.get("first_seen_at"),
        )

    def sync_existing_jobs(self, jobs: Iterable[Job], run_id: str = "") -> int:
        """Touch only jobs already in the Dashboard, without rescoring or changing status."""
        job_ids = {job.id for job in jobs}
        if not job_ids:
            return 0
        rows = self.load_rows()
        now = utc_now()
        touched = 0
        for row in rows:
            if row.get("job_id") not in job_ids:
                continue
            if not row.get("first_seen_at"):
                row["first_seen_at"] = row.get("discovered_at") or now
            row["last_synced_at"] = now
            if run_id:
                row["last_run_id"] = run_id
            touched += 1
        if touched:
            self._write_rows(rows)
        return touched

    def _write_rows(self, rows: Iterable[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        with temp.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=DASHBOARD_FIELDS, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({field: row.get(field, "") for field in DASHBOARD_FIELDS})
        temp.replace(self.path)

    def _append_event(self, *, job_id: str, actor: str, action: str,
                      from_status: str = "", to_status: str = "", reason: str = "",
                      run_id: str = "", artifact_path: str = "", details: str = "") -> None:
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        new_file = not self.events_path.exists()
        with self.events_path.open("a", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=EVENT_FIELDS, extrasaction="ignore")
            if new_file:
                writer.writeheader()
            writer.writerow({
                "event_id": uuid4().hex[:16], "job_id": job_id, "run_id": run_id,
                "timestamp": utc_now(), "actor": actor, "from_status": from_status,
                "to_status": to_status, "action": action, "reason": reason,
                "artifact_path": artifact_path, "details": details,
            })

    def upsert_recommendation(
        self,
        job: Job,
        *,
        job_fit_score: int,
        job_fit_reason: str,
        sponsorship_signal: str,
        resume_id: str,
        resume_fit_score: int,
        resume_reason: str,
        application_mode: str,
        freshness_bucket: str,
        job_summary: str = "",
        resume_path: str = "",
        run_id: str = "",
        artifact_path: str = "",
    ) -> dict[str, str]:
        rows = self.load_rows()
        by_id = {row.get("job_id"): row for row in rows}
        now = utc_now()
        existing = by_id.get(job.id)
        base = dict(existing or {})
        new_row = not existing
        external = {
            "job_id": job.id, "company": job.company, "title": job.title, "source": job.source,
            "url": job.url, "duplicate_urls": "; ".join(job.duplicate_urls),
            "location": job.location or "", "posted_at": job.posted_date or "",
            "job_fit_score": str(job_fit_score), "job_fit_reason": job_fit_reason,
            "job_summary": job_summary,
            "match_score": str(job_fit_score), "resume_id": resume_id, "resume_path": resume_path,
            "resume_fit_score": str(resume_fit_score), "resume_reason": resume_reason,
            "application_mode": application_mode, "freshness_bucket": freshness_bucket,
            "sponsorship_signal": sponsorship_signal, "last_updated_at": now,
            "last_run_id": run_id, "last_synced_at": now,
        }
        base.update(external)
        if new_row:
            base.update({
                "discovered_at": now, "first_seen_at": now, "status": "review", "skip_reason": "", "blocked_reason": "",
                "needs_user_reason": "", "submitted_at": "", "submission_evidence": "",
                "next_action": "Review recommendation", "last_action": "discovered", "notes": "",
                "artifact_path": artifact_path,
            })
        elif not base.get("first_seen_at"):
            base["first_seen_at"] = base.get("discovered_at") or now
        if not new_row and artifact_path:
            base["artifact_path"] = artifact_path
        by_id[job.id] = base
        self._write_rows(by_id.values())
        if new_row:
            self._append_event(job_id=job.id, actor="system", action="discovered", to_status="review", run_id=run_id)
        return base

    def transition(
        self,
        job_id: str,
        status: str,
        *,
        actor: str,
        reason: str = "",
        next_action: str | None = None,
        notes: str | None = None,
        submission_confirmed: bool = False,
        submission_evidence: str = "",
        run_id: str = "",
        artifact_path: str = "",
    ) -> dict[str, str]:
        if status not in STATUSES:
            raise ValueError(f"未知 Dashboard 状态: {status}")
        if status in REASON_REQUIRED and not reason.strip():
            raise ValueError(f"状态 {status} 必须填写原因")
        if status == "submitted" and (not submission_confirmed or not submission_evidence.strip()):
            raise ValueError("标记 submitted 前必须确认成功，并填写平台成功页或确认文本证据")

        rows = self.load_rows()
        target = next((row for row in rows if row.get("job_id") == job_id), None)
        if target is None:
            raise KeyError(f"Dashboard 中找不到岗位: {job_id}")
        from_status = target.get("status", "")
        target["status"] = status
        target["last_action"] = f"status:{status}"
        target["last_updated_at"] = utc_now()
        if status == "skipped":
            target["skip_reason"] = reason.strip()
        if status == "blocked":
            target["blocked_reason"] = reason.strip()
        if status == "needs_user":
            target["needs_user_reason"] = reason.strip()
        if status == "submitted":
            target["submitted_at"] = target["last_updated_at"]
            target["submission_evidence"] = submission_evidence.strip()
        if next_action is not None:
            target["next_action"] = next_action
        if notes is not None:
            target["notes"] = notes
        if artifact_path:
            target["artifact_path"] = artifact_path
        self._write_rows(rows)
        self._append_event(
            job_id=job_id, actor=actor, action="status_changed", from_status=from_status,
            to_status=status, reason=reason, run_id=run_id, artifact_path=artifact_path,
        )
        return target

    def events_for(self, job_id: str) -> list[dict[str, str]]:
        if not self.events_path.exists():
            return []
        with self.events_path.open(encoding="utf-8", newline="") as f:
            return [row for row in csv.DictReader(f) if row.get("job_id") == job_id]

    def daily_queues(self) -> dict[str, list[dict[str, str]]]:
        rows = []
        for source_row in self.load_rows():
            row = dict(source_row)
            row["freshness_bucket"] = self.current_freshness(row)
            rows.append(row)
        priority = {"within_24h": 0, "within_3d": 1, "older": 2, "unknown": 3}
        active = [r for r in rows if r.get("status") in {"review", "ready_to_apply", "applying"}]
        active.sort(key=lambda r: (priority.get(r.get("freshness_bucket", "unknown"), 9), -_int(r.get("job_fit_score"))))
        return {
            "today": active,
            "review": [r for r in active if r.get("status") == "review"],
            "ready_to_apply": [r for r in active if r.get("status") == "ready_to_apply"],
            "needs_user": [r for r in rows if r.get("status") == "needs_user"],
            "blocked": [r for r in rows if r.get("status") == "blocked"],
            "follow_up": [r for r in rows if r.get("status") == "follow_up"],
        }


def _int(value: Any) -> int:
    try:
        return int(str(value or "0"))
    except ValueError:
        return 0
