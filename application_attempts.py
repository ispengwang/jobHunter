"""Internal ApplyPilot execution ledger for jobs selected by the current Codex Agent.

JobHunter never drives the browser. During an autonomous Skill run it records which jobs the
Agent selected, then ``applypilot-au`` owns platform policy, readiness, browser execution,
daily limits, blockers, and submission evidence. The user does not operate this ledger.
"""
from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from candidate_profile import CandidateProfile
from dashboard import Dashboard


ATTEMPT_STATUSES = {
    "selected", "queued", "browser_opened", "filling", "needs_user", "ready_to_submit",
    "submitted", "failed", "cancelled",
}
ATTEMPT_FIELDS = [
    "attempt_id", "job_id", "platform", "url", "company", "title", "mode",
    "platform_mode", "status", "readiness", "created_at", "updated_at",
    "profile_path", "resume_path", "skill_name", "skill_path", "reason",
    "last_action", "data_transmission_confirmed", "submission_confirmed",
    "submission_evidence",
]


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def platform_submission_mode(platform: str, url: str = "") -> str:
    """Return the conservative ApplyPilot default for the concrete URL.

    A direct employer/ATS URL can be checked for ``auto_if_allowed`` even when the lead originated
    on a job board. On-platform LinkedIn, Indeed and SEEK flows remain manual by default.
    """
    normalised = (platform or "").strip().lower()
    hostname = (urlparse(url).hostname or "").casefold()
    manual_hosts = ("linkedin.com", "indeed.com", "indeed.com.au", "seek.com.au", "au.seek.com")
    if hostname:
        if any(hostname == host or hostname.endswith("." + host) for host in manual_hosts):
            return "manual_submit"
        return "auto_if_allowed"
    if any(name in normalised for name in ("linkedin", "indeed", "seek")):
        return "manual_submit"
    return "auto_if_allowed"


class ApplicationAttempts:
    def __init__(
        self,
        path: Path,
        dashboard: Dashboard,
        *,
        browser_enabled: bool = True,
        skill_path: Path | None = None,
        project_root: Path | None = None,
    ):
        self.path = path
        self.dashboard = dashboard
        self.browser_enabled = browser_enabled
        self.skill_path = skill_path
        self.project_root = project_root or path.parent.parent

    def load_rows(self) -> list[dict[str, str]]:
        if not self.path.exists():
            return []
        with self.path.open(encoding="utf-8", newline="") as f:
            return list(csv.DictReader(f))

    def get(self, attempt_id: str) -> dict[str, str] | None:
        return next((row for row in self.load_rows() if row.get("attempt_id") == attempt_id), None)

    def _write_rows(self, rows: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=ATTEMPT_FIELDS, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({field: row.get(field, "") for field in ATTEMPT_FIELDS})
        temporary.replace(self.path)

    def create(
        self,
        job_id: str,
        profile: CandidateProfile,
        profile_path: Path,
        *,
        data_transmission_confirmed: bool | None = None,
        actor: str = "user",
    ) -> dict[str, str]:
        """Create one internal execution record without opening or submitting a form.

        ``data_transmission_confirmed`` is accepted only for backward compatibility. Selection
        does not transmit candidate data, so the value is deliberately not persisted as consent.
        """
        row = self.dashboard.get(job_id)
        if row is None:
            raise KeyError(f"Dashboard 中找不到岗位: {job_id}")
        if not self.browser_enabled:
            raise ValueError("config.yaml 已关闭 ApplyPilot Agent 执行")

        active = {"selected", "queued", "browser_opened", "filling", "needs_user", "ready_to_submit"}
        for existing in self.load_rows():
            if existing.get("job_id") == job_id and existing.get("status") in active:
                return existing

        if row.get("status") not in {"review", "ready_to_apply"}:
            raise ValueError(f"当前状态为 {row.get('status') or 'unknown'}，不能创建 Agent 执行记录")
        if row.get("application_mode") not in {"broad", "targeted"}:
            raise ValueError("该岗位不符合 broad 或 targeted 自动选择规则")
        missing = profile.validation_items()

        now = _now()
        artifact_path = Path(row.get("artifact_path", "")) if row.get("artifact_path") else None
        tailored_resume = artifact_path / "resume.md" if artifact_path else None
        resume_path = str(tailored_resume) if tailored_resume and tailored_resume.exists() else row.get("resume_path", "")
        platform_mode = platform_submission_mode(row.get("source", ""), row.get("url", ""))
        attempt = {
            "attempt_id": uuid4().hex[:16], "job_id": job_id, "platform": row.get("source", ""),
            "url": row.get("url", ""), "company": row.get("company", ""),
            "title": row.get("title", ""), "mode": row.get("application_mode", ""),
            "platform_mode": platform_mode, "status": "selected",
            "readiness": "needs_profile" if missing else "ready",
            "created_at": now, "updated_at": now,
            "profile_path": str(profile_path), "resume_path": resume_path,
            "skill_name": "applypilot-au",
            "skill_path": str(self.skill_path or ""),
            "reason": "Candidate Profile 待补全：" + "、".join(missing) if missing else "",
            "last_action": "selected_by_agent", "data_transmission_confirmed": "",
            "submission_confirmed": "", "submission_evidence": "",
        }
        rows = self.load_rows()
        rows.append(attempt)
        self._write_rows(rows)
        if row.get("status") == "review":
            self.dashboard.transition(
                job_id, "ready_to_apply", actor=actor,
                next_action="Current Codex Agent will continue with applypilot-au",
            )
        return attempt

    def select_eligible(
        self,
        profile: CandidateProfile,
        profile_path: Path,
        *,
        max_new: int,
        soft_max_new: int | None = None,
        actor: str = "agent",
    ) -> dict[str, list[dict[str, str]]]:
        """Select eligible jobs for the current Agent, prioritising postings from the last 24h.

        ``max_new`` is the remaining hard-cap allowance. ``soft_max_new`` limits older jobs to
        the normal daily target, while fresh jobs may use the remaining buffer up to the hard cap.
        """
        rows = self.dashboard.load_rows()
        attempts = self.load_rows()
        active_statuses = {
            "selected", "queued", "browser_opened", "filling", "needs_user", "ready_to_submit",
        }
        terminal_statuses = {"submitted"}
        existing_job_ids = {
            row.get("job_id", "")
            for row in attempts
            if row.get("status") in active_statuses | terminal_statuses
        }

        candidates = [
            row for row in rows
            if row.get("status") in {"review", "ready_to_apply"}
            and row.get("application_mode") in {"broad", "targeted"}
            and row.get("job_id", "") not in existing_job_ids
        ]
        freshness_order = {"within_24h": 0, "within_3d": 1, "older": 2, "unknown": 3}
        current_freshness = {
            row.get("job_id", ""): self.dashboard.current_freshness(row)
            for row in rows
        }
        candidates.sort(key=lambda row: (
            freshness_order.get(current_freshness.get(row.get("job_id", ""), "unknown"), 9),
            -_score(row.get("job_fit_score")),
            -_score(row.get("resume_fit_score")),
            row.get("title", "").casefold(),
        ))

        automatic = [
            row for row in candidates
            if platform_submission_mode(row.get("source", ""), row.get("url", "")) == "auto_if_allowed"
        ]
        manual_only = [
            row for row in candidates
            if platform_submission_mode(row.get("source", ""), row.get("url", "")) == "manual_submit"
        ]
        hard_slots = max(0, max_new)
        soft_slots = hard_slots if soft_max_new is None else min(hard_slots, max(0, soft_max_new))
        priority_automatic = [
            row for row in automatic
            if current_freshness.get(row.get("job_id", ""), "unknown") == "within_24h"
        ]
        standard_automatic = [
            row for row in automatic
            if current_freshness.get(row.get("job_id", ""), "unknown") != "within_24h"
        ]
        selected_rows = priority_automatic[:hard_slots]
        standard_slots = max(0, soft_slots - len(selected_rows))
        selected_rows.extend(standard_automatic[:standard_slots])
        selected = [
            self.create(row["job_id"], profile, profile_path, actor=actor)
            for row in selected_rows
        ]

        runnable_statuses = {"selected", "queued", "browser_opened", "filling", "ready_to_submit"}
        dashboard_rows = {
            row.get("job_id", ""): row for row in self.dashboard.load_rows()
        }
        current_dashboard_freshness = {
            job_id: self.dashboard.current_freshness(row)
            for job_id, row in dashboard_rows.items()
        }
        runnable = [
            attempt for attempt in self.load_rows()
            if attempt.get("status") in runnable_statuses
        ]
        runnable.sort(key=lambda attempt: (
            freshness_order.get(
                current_dashboard_freshness.get(attempt.get("job_id", ""), "unknown"),
                9,
            ),
            -_score(
                dashboard_rows.get(attempt.get("job_id", ""), {}).get("job_fit_score")
            ),
            -_score(
                dashboard_rows.get(attempt.get("job_id", ""), {}).get("resume_fit_score")
            ),
            attempt.get("created_at", ""),
        ))
        return {
            "selected": selected,
            "runnable": runnable,
            "manual_only": manual_only,
            "priority_selected": [
                row for row in selected
                if self.dashboard.current_freshness(
                    self.dashboard.get(row["job_id"]) or {}
                ) == "within_24h"
            ],
        }

    def authorize_execution(self, attempt_id: str, *, actor: str = "user") -> dict[str, str]:
        """Record the user's explicit per-job start action separately from internal selection."""
        rows = self.load_rows()
        target = next((row for row in rows if row.get("attempt_id") == attempt_id), None)
        if target is None:
            raise KeyError(f"找不到申请尝试: {attempt_id}")
        if target.get("status") in {"submitted", "cancelled"}:
            raise ValueError(f"当前申请状态为 {target.get('status')}，不能再次发起")
        target.update({
            "data_transmission_confirmed": "yes",
            "updated_at": _now(),
            "last_action": f"application_started_by_{actor}",
        })
        self._write_rows(rows)
        return target

    def advance(
        self,
        attempt_id: str,
        status: str,
        *,
        actor: str,
        reason: str = "",
        submission_confirmed: bool = False,
        submission_evidence: str = "",
        next_action: str | None = None,
        notes: str | None = None,
    ) -> dict[str, str]:
        if status not in ATTEMPT_STATUSES:
            raise ValueError(f"未知申请尝试状态: {status}")
        if status in {"needs_user", "failed"} and not reason.strip():
            raise ValueError(f"状态 {status} 必须填写原因")
        if status == "submitted" and (not submission_confirmed or not submission_evidence.strip()):
            raise ValueError("记录提交成功前必须确认结果，并填写平台成功页或确认文本证据")

        rows = self.load_rows()
        target = next((row for row in rows if row.get("attempt_id") == attempt_id), None)
        if target is None:
            raise KeyError(f"找不到申请尝试: {attempt_id}")
        target.update({"status": status, "reason": reason.strip(), "updated_at": _now(), "last_action": status})
        if submission_confirmed:
            target["submission_confirmed"] = "yes"
        if submission_evidence.strip():
            target["submission_evidence"] = submission_evidence.strip()
        self._write_rows(rows)

        if status == "needs_user":
            self.dashboard.transition(
                target["job_id"], "needs_user", actor=actor, reason=reason.strip(),
                next_action="Resolve the ApplyPilot blocker, then resume this attempt",
            )
        elif status == "failed":
            self.dashboard.transition(target["job_id"], "blocked", actor=actor, reason=reason.strip())
        elif status in {"browser_opened", "filling", "ready_to_submit"}:
            self.dashboard.transition(
                target["job_id"], "applying", actor=actor,
                next_action="Continue the active browser application attempt",
            )
        elif status == "submitted":
            self.dashboard.transition(
                target["job_id"], "submitted", actor=actor,
                reason=reason.strip() or "browser attempt succeeded",
                next_action=next_action, notes=notes,
                submission_confirmed=True, submission_evidence=submission_evidence.strip(),
            )
        elif status == "cancelled":
            self.dashboard.transition(
                target["job_id"], "review", actor=actor,
                next_action="Review before adding the job to ApplyPilot again",
            )
        return target

    def handoff_payload(self, attempt_id: str) -> dict[str, str]:
        """Return local paths and job metadata; never duplicate sensitive profile values in logs."""
        attempt = self.get(attempt_id)
        if attempt is None:
            raise KeyError(f"找不到申请尝试: {attempt_id}")
        dashboard_row = self.dashboard.get(attempt["job_id"]) or {}
        return {
            "integration": "jobhunter",
            "executor": "applypilot-au",
            "attempt_id": attempt["attempt_id"], "platform": attempt["platform"],
            "company": attempt["company"], "title": attempt["title"], "url": attempt["url"],
            "mode": attempt["mode"],
            "platform_mode": platform_submission_mode(attempt["platform"], attempt["url"]),
            "readiness": attempt.get("readiness", "unknown"),
            "job_fit_score": dashboard_row.get("job_fit_score", ""),
            "resume_fit_score": dashboard_row.get("resume_fit_score", ""),
            "freshness_bucket": self.dashboard.current_freshness(dashboard_row),
            "data_transmission_confirmed": attempt.get("data_transmission_confirmed", ""),
            "artifact_path": dashboard_row.get("artifact_path", ""),
            "project_root": str(self.project_root),
            "skill_path": attempt.get("skill_path") or str(self.skill_path or ""),
            "profile_path": attempt["profile_path"],
            "resume_path": attempt["resume_path"],
            "instructions": (
                "Continue this attempt now in the current Codex Agent. Use applypilot-au as the sole "
                "submission policy and executor, validate readiness, and load its platform playbook "
                "and safety gates. This internal selection record is not browser activity or a "
                "submission. LinkedIn "
                "and Indeed default to manual_submit. SEEK and external ATS may auto-submit only when "
                "the skill's current permission and safety gates pass. Record explicit submission "
                "evidence before marking submitted."
            ),
        }

    def handoff_list(self) -> list[dict[str, str]]:
        """Return selected/queued attempts for the current Agent session.

        This is a local handoff manifest, not a consent record. Keep it limited to the
        fields the Agent needs to order and execute attempts; candidate contact details,
        profile contents and API credentials never enter the result.
        """
        dashboard_rows = {
            row.get("job_id", ""): row for row in self.dashboard.load_rows()
        }
        freshness_order = {"within_24h": 0, "within_3d": 1, "older": 2, "unknown": 3}
        result: list[dict[str, str]] = []
        for attempt in self.load_rows():
            if attempt.get("status") not in {"selected", "queued"}:
                continue
            row = dashboard_rows.get(attempt.get("job_id", ""), {})
            freshness = self.dashboard.current_freshness(row)
            platform = attempt.get("platform", "")
            url = attempt.get("url", "")
            result.append({
                "attempt_id": attempt.get("attempt_id", ""),
                "company": attempt.get("company", ""),
                "title": attempt.get("title", ""),
                "url": url,
                "platform": platform,
                "platform_mode": platform_submission_mode(platform, url),
                "job_fit_score": row.get("job_fit_score", ""),
                "resume_fit_score": row.get("resume_fit_score", ""),
                "freshness": freshness,
                "resume_path": attempt.get("resume_path", ""),
                "readiness": attempt.get("readiness", "unknown"),
            })
        result.sort(key=lambda item: (
            freshness_order.get(item["freshness"], 9),
            -_score(item["job_fit_score"]),
            -_score(item["resume_fit_score"]),
            item["company"].casefold(),
            item["title"].casefold(),
        ))
        return result

    def launch_prompt(self, attempt_id: str) -> str:
        """Build a legacy prompt for old bookmarks; autonomous runs do not require copy/paste."""
        payload = self.handoff_payload(attempt_id)
        skill_file = Path(payload["skill_path"]) / "SKILL.md" if payload["skill_path"] else None
        skill_ref = f"[$applypilot-au]({skill_file})" if skill_file else "$applypilot-au"
        return (
            f"使用 {skill_ref} 继续 JobHunter 的内部执行记录 {attempt_id}。\n\n"
            f"项目目录：{payload['project_root']}\n"
            f"先运行：venv/bin/python run.py --attempt-handoff {attempt_id}\n\n"
            "JobHunter 只负责搜岗、评分和展示；以 applypilot-au 的平台规则、"
            "自动投递门槛、每日限额和证据要求为准。执行后使用 run.py 的 "
            "--attempt-update 接口写回状态。不要把 selected 当成已开始或已提交。"
        )


def _score(value: Any) -> int:
    try:
        return int(str(value or "-1"))
    except ValueError:
        return -1
