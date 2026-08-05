"""Internal ApplyPilot execution ledger for jobs selected by the current Codex Agent.

JobHunter never drives the browser. During an autonomous Skill run it records which jobs the
Agent selected, then ``applypilot-au`` owns platform policy, readiness, browser execution,
daily limits, blockers, and submission evidence. The user does not operate this ledger.
"""
from __future__ import annotations

import csv
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4
from zoneinfo import ZoneInfo

from candidate_profile import CandidateProfile
from dashboard import Dashboard
from schema import canonical_job_id


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

DEFAULT_ASSISTED_HOSTS = ("linkedin.com", "indeed.com", "indeed.com.au")
DEFAULT_MANUAL_HOSTS = ("seek.com.au", "au.seek.com")
DEFAULT_PLATFORM_LIMITS = {"linkedin": 8, "indeed": 8, "external_ats": 40}


def _job_identity(row: dict[str, str]) -> str:
    """Return a source-independent identity for a Dashboard/attempt row.

    Older CSV rows can carry URL-based IDs.  Selection must still recognise
    them as the same role as a newer canonical row, otherwise a second source
    can create a second active attempt for one advertised job.
    """
    company = (row.get("company") or "").strip()
    title = (row.get("title") or "").strip()
    if company and title:
        return canonical_job_id(company, title)
    url = (row.get("url") or "").strip()
    return f"url:{url}" if url else f"job:{row.get('job_id', '')}"


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _applypilot_config(config: dict | None) -> dict:
    if not config:
        return {}
    nested = config.get("applypilot")
    return nested if isinstance(nested, dict) else config


def _configured_hosts(config: dict | None, key: str, default: tuple[str, ...]) -> tuple[str, ...]:
    value = _applypilot_config(config).get(key, default)
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple, set)):
        return default
    return tuple(
        str(item).strip().lower().removeprefix("www.")
        for item in value if str(item).strip()
    )


def _host_matches(hostname: str, configured_hosts: tuple[str, ...]) -> bool:
    return any(
        hostname == host or hostname.endswith("." + host)
        for host in configured_hosts
    )


def platform_submission_mode(
    platform: str,
    url: str = "",
    config: dict | None = None,
) -> str:
    """Return the ApplyPilot interaction mode for the concrete URL.

    ``assisted`` means the Agent may help fill and review the form, but must stop before the
    final submit control so the user can click it. ``manual_submit`` leaves all platform steps
    to the user. A direct employer/ATS URL is ``auto_if_allowed`` when it is not one of the
    configured assisted/manual hosts; the skill's safety gates and final-submit rule still apply.
    """
    normalised = (platform or "").strip().lower()
    hostname = (urlparse(url).hostname or "").casefold()
    assisted_hosts = _configured_hosts(config, "assisted_hosts", DEFAULT_ASSISTED_HOSTS)
    manual_hosts = _configured_hosts(config, "manual_hosts", DEFAULT_MANUAL_HOSTS)
    if hostname:
        if _host_matches(hostname, assisted_hosts):
            return "assisted"
        if _host_matches(hostname, manual_hosts):
            return "manual_submit"
        return "auto_if_allowed"
    if "linkedin" in normalised or "indeed" in normalised:
        return "assisted"
    if "seek" in normalised:
        return "manual_submit"
    return "auto_if_allowed"


def platform_limit_key(
    platform: str,
    url: str = "",
    config: dict | None = None,
) -> str:
    """Return the daily-limit bucket for the actual submission destination."""
    hostname = (urlparse(url).hostname or "").casefold()
    normalised = (platform or "").strip().lower()
    assisted_hosts = _configured_hosts(config, "assisted_hosts", DEFAULT_ASSISTED_HOSTS)
    manual_hosts = _configured_hosts(config, "manual_hosts", DEFAULT_MANUAL_HOSTS)
    if hostname and _host_matches(hostname, assisted_hosts):
        return "indeed" if "indeed" in hostname else "linkedin"
    if not hostname:
        if "indeed" in normalised:
            return "indeed"
        if "linkedin" in normalised:
            return "linkedin"
    if hostname and _host_matches(hostname, manual_hosts):
        return "external_ats"
    return "external_ats"


def configured_platform_limits(config: dict | None = None) -> dict[str, int]:
    """Return positive per-destination limits, merged with safe defaults."""
    limits = dict(DEFAULT_PLATFORM_LIMITS)
    configured = _applypilot_config(config).get("platform_limits", {})
    if isinstance(configured, dict):
        for key, value in configured.items():
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                continue
            if parsed >= 0:
                limits[str(key)] = parsed
    return limits


def _submission_local_date(row: dict[str, str], zone: ZoneInfo) -> date | None:
    """Return the local calendar date for a row with evidenced submission history."""
    if not row.get("submission_evidence", "").strip():
        return None
    raw = row.get("submitted_at", "").strip().replace("Z", "+00:00")
    try:
        submitted_at = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if submitted_at.tzinfo is None:
        submitted_at = submitted_at.replace(tzinfo=zone)
    return submitted_at.astimezone(zone).date()


def submitted_today_job_ids(
    rows: list[dict[str, str]],
    timezone_name: str,
    *,
    today: date | None = None,
) -> set[str]:
    """Return Dashboard job IDs with evidenced submissions on the local date."""
    zone = ZoneInfo(timezone_name)
    target_date = today or datetime.now(zone).date()
    return {
        row.get("job_id", "")
        for row in rows
        if row.get("job_id", "") and _submission_local_date(row, zone) == target_date
    }


def submitted_today_by_platform(
    rows: list[dict[str, str]],
    timezone_name: str,
    config: dict | None = None,
) -> dict[str, int]:
    """Count evidenced submissions for today by their actual destination bucket."""
    zone = ZoneInfo(timezone_name)
    today = datetime.now(zone).date()
    counts = {key: 0 for key in configured_platform_limits(config)}
    for row in rows:
        if _submission_local_date(row, zone) != today:
            continue
        key = platform_limit_key(row.get("source", ""), row.get("url", ""), config)
        counts[key] = counts.get(key, 0) + 1
    return counts


class ApplicationAttempts:
    def __init__(
        self,
        path: Path,
        dashboard: Dashboard,
        *,
        browser_enabled: bool = True,
        skill_path: Path | None = None,
        project_root: Path | None = None,
        platform_config: dict | None = None,
    ):
        self.path = path
        self.dashboard = dashboard
        self.browser_enabled = browser_enabled
        self.skill_path = skill_path
        self.project_root = project_root or path.parent.parent
        self.platform_config = platform_config or {}

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
        target_identity = _job_identity(row)
        suppressed_dashboard_statuses = {
            "submitted", "follow_up", "rejected", "interview", "phone_interview",
            "formal_interview", "offer", "withdrawn",
            "skipped", "unavailable", "blocked", "needs_user", "applying",
        }
        for sibling in self.dashboard.load_rows():
            if (
                sibling.get("job_id") != job_id
                and _job_identity(sibling) == target_identity
                and sibling.get("status") in suppressed_dashboard_statuses
            ):
                raise ValueError("同一公司同一岗位已有已处理或已归档记录，不能重复发起")
        for existing in self.load_rows():
            if (
                existing.get("status") in active
                and (
                    existing.get("job_id") == job_id
                    or _job_identity(existing) == target_identity
                )
            ):
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
        platform_mode = platform_submission_mode(
            row.get("source", ""), row.get("url", ""), self.platform_config,
        )
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
        platform_remaining: dict[str, int] | None = None,
    ) -> dict[str, list[dict[str, str]]]:
        """Select eligible jobs for the current Agent, prioritising postings from the last 24h.

        ``max_new`` is the remaining total allowance. ``platform_remaining`` optionally applies
        per-destination limits after already evidenced submissions and active attempts are
        accounted for. ``soft_max_new`` limits older jobs to the normal daily target, while fresh
        jobs may use the remaining buffer up to the hard limit.
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
        existing_identities = {
            _job_identity(row)
            for row in attempts
            if row.get("status") in active_statuses | terminal_statuses
        }
        suppressed_dashboard_statuses = {
            "submitted", "follow_up", "rejected", "interview", "phone_interview",
            "formal_interview", "offer", "withdrawn",
            "skipped", "unavailable", "blocked", "needs_user", "applying",
        }
        suppressed_identities = {
            _job_identity(row)
            for row in rows
            if row.get("status") in suppressed_dashboard_statuses
        }

        candidates = [
            row for row in rows
            if row.get("status") in {"review", "ready_to_apply"}
            and row.get("application_mode") in {"broad", "targeted"}
            and row.get("job_id", "") not in existing_job_ids
            and _job_identity(row) not in existing_identities
            and _job_identity(row) not in suppressed_identities
        ]
        # A legacy Dashboard can contain two rows with different IDs but the
        # same company/title.  Keep the more actionable row in this run; the
        # explicit migration command can later consolidate the CSV and history
        # without making selection depend on that maintenance step.
        unique_candidates: dict[str, dict[str, str]] = {}
        for row in candidates:
            identity = _job_identity(row)
            previous = unique_candidates.get(identity)
            if previous is None or (
                row.get("status") == "ready_to_apply"
                and previous.get("status") != "ready_to_apply"
            ) or (
                row.get("status") == previous.get("status")
                and (
                    _score(row.get("job_fit_score")),
                    _score(row.get("resume_fit_score")),
                ) > (
                    _score(previous.get("job_fit_score")),
                    _score(previous.get("resume_fit_score")),
                )
            ):
                unique_candidates[identity] = row
        candidates = list(unique_candidates.values())
        freshness_order = {"within_24h": 0, "within_3d": 1, "older": 2, "unknown": 3}
        current_freshness = {
            row.get("job_id", ""): self.dashboard.current_freshness(row)
            for row in rows
        }
        def mode_priority(row: dict[str, str]) -> int:
            mode = platform_submission_mode(
                row.get("source", ""), row.get("url", ""), self.platform_config,
            )
            return 0 if mode == "auto_if_allowed" else 1 if mode == "assisted" else 2

        candidates.sort(key=lambda row: (
            freshness_order.get(current_freshness.get(row.get("job_id", ""), "unknown"), 9),
            mode_priority(row),
            -_score(row.get("job_fit_score")),
            -_score(row.get("resume_fit_score")),
            row.get("title", "").casefold(),
        ))

        agent_candidates = [
            row for row in candidates
            if platform_submission_mode(
                row.get("source", ""), row.get("url", ""), self.platform_config,
            ) in {"auto_if_allowed", "assisted"}
        ]
        manual_only = [
            row for row in candidates
            if platform_submission_mode(
                row.get("source", ""), row.get("url", ""), self.platform_config,
            ) == "manual_submit"
        ]
        hard_slots = max(0, max_new)
        soft_slots = hard_slots if soft_max_new is None else min(hard_slots, max(0, soft_max_new))
        remaining = dict(platform_remaining) if platform_remaining is not None else None

        def take(candidates_to_take: list[dict[str, str]], slots: int) -> list[dict[str, str]]:
            selected_rows: list[dict[str, str]] = []
            for row in candidates_to_take:
                if len(selected_rows) >= slots:
                    break
                if remaining is not None:
                    key = platform_limit_key(
                        row.get("source", ""), row.get("url", ""), self.platform_config,
                    )
                    if remaining.get(key, 0) <= 0:
                        continue
                    remaining[key] -= 1
                selected_rows.append(row)
            return selected_rows

        priority_agent = [
            row for row in agent_candidates
            if current_freshness.get(row.get("job_id", ""), "unknown") == "within_24h"
        ]
        standard_agent = [
            row for row in agent_candidates
            if current_freshness.get(row.get("job_id", ""), "unknown") != "within_24h"
        ]
        selected_rows = take(priority_agent, hard_slots)
        standard_slots = max(0, soft_slots - len(selected_rows))
        selected_rows.extend(take(standard_agent, standard_slots))
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
            "platform_mode": platform_submission_mode(
                attempt["platform"], attempt["url"], self.platform_config,
            ),
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
                "submission. LinkedIn and Indeed use assisted mode: fill and review, then stop "
                "before the final submit control so the user can click it. SEEK remains manual; "
                "external ATS still requires the skill's current permission and safety gates, "
                "and the user must click the final submit control. Record explicit submission "
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
                "platform_mode": platform_submission_mode(
                    platform, url, self.platform_config,
                ),
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
