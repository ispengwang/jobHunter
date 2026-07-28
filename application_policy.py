"""Explainable eligibility, freshness and broad/targeted application decisions."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from resume_catalog import ResumeSelection, choose_resume
from score import Scored


@dataclass(frozen=True)
class ApplicationDecision:
    freshness_bucket: str  # within_24h | within_3d | older | unknown
    eligible: bool
    mode: str  # targeted | broad | manual_review
    reason: str


def freshness_bucket(posted_at: str | None, cfg: dict, now: datetime | None = None) -> str:
    if not posted_at:
        return "unknown"
    try:
        raw = str(posted_at).strip().replace("Z", "+00:00")
        when = datetime.fromisoformat(raw)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
    except ValueError:
        return "unknown"
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    age_hours = max(0.0, (now - when.astimezone(timezone.utc)).total_seconds() / 3600)
    app_cfg = cfg.get("application", {})
    if age_hours <= int(app_cfg.get("priority_within_hours", 24)):
        return "within_24h"
    if age_hours <= int(app_cfg.get("recent_within_hours", 72)):
        return "within_3d"
    return "older"


def decide_application_mode(scored: Scored, resume: ResumeSelection, cfg: dict,
                            now: datetime | None = None) -> ApplicationDecision:
    app_cfg = cfg.get("application", {})
    title = (scored.job.title or "").casefold()
    excluded = [str(x).casefold() for x in app_cfg.get("excluded_seniority_keywords", [])]
    eligible = [str(x).casefold() for x in app_cfg.get("eligible_seniority_keywords", [])]
    freshness = freshness_bucket(scored.job.posted_date, cfg, now)

    if any(word and word in title for word in excluded):
        return ApplicationDecision(freshness, False, "manual_review", "职位级别高于默认 entry/junior 范围")
    if eligible and not any(word and word in title for word in eligible):
        return ApplicationDecision(freshness, False, "manual_review", "职位级别未明确属于 entry/junior，需人工判断")
    if scored.score < int(app_cfg.get("broad_job_fit_threshold", 70)):
        return ApplicationDecision(freshness, False, "manual_review", "岗位匹配分低于海投阈值")
    if (
        scored.score >= int(app_cfg.get("targeted_job_fit_threshold", 80))
        and resume.fit_score >= int(app_cfg.get("targeted_resume_fit_threshold", 72))
    ):
        return ApplicationDecision(freshness, True, "targeted", "岗位与简历匹配均达到精投阈值")
    return ApplicationDecision(freshness, True, "broad", "符合 entry/junior 海投阈值，使用匹配简历版本")


def enrich_scored_jobs(scored_jobs: list[Scored], variants, root, cfg: dict,
                       now: datetime | None = None) -> list[Scored]:
    """Attach resume selection and workflow policy to already LLM-scored jobs."""
    for scored in scored_jobs:
        if scored.score < 0:
            scored.application_mode = "manual_review"
            scored.eligibility_reason = "DeepSeek 打分失败，需人工判断"
            scored.freshness_bucket = freshness_bucket(scored.job.posted_date, cfg, now)
            continue
        selection = choose_resume(scored.job, variants, root)
        decision = decide_application_mode(scored, selection, cfg, now)
        scored.resume_id = selection.resume_id
        scored.resume_path = str(selection.path)
        scored.resume_fit_score = selection.fit_score
        scored.resume_reason = selection.reason
        scored.application_mode = decision.mode
        scored.eligibility_reason = decision.reason
        scored.freshness_bucket = decision.freshness_bucket
    return scored_jobs
