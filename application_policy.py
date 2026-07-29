"""Explainable eligibility, freshness and broad/targeted application decisions."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re

from resume_catalog import ResumeSelection, choose_resume
from score import Scored


@dataclass(frozen=True)
class ApplicationDecision:
    freshness_bucket: str  # within_24h | within_3d | older | unknown
    eligible: bool
    mode: str  # targeted | broad | manual_review
    reason: str


# Keep this parser deliberately small and explainable.  It extracts the first
# number in a requirement such as "5+ years", "minimum 3 years experience",
# or "at least 4 years"; for a range such as "1-3 years" the lower bound is
# the relevant value.
_EXPERIENCE_RE = re.compile(
    r"""
    \b(?P<requirement>
        (?:(?:at\s+least|minimum(?:\s+of)?|a\s+minimum\s+of)\s+)?
        (?P<years>\d+)\s*(?:\+|plus)?\s*years?
        (?:\s*(?:-|–|—|to)\s*\d+\s*years?)?
        (?:\s*(?:of\s+)?(?:relevant|professional|commercial|industry|software|work)?\s*experience)?
    )\b
    """,
    re.IGNORECASE | re.VERBOSE,
)
_EARLY_CAREER_RE = re.compile(
    r"\b(?:"
    r"no\s+(?:prior\s+)?experience\s+(?:is\s+)?(?:required|necessary)|"
    r"experience\s+(?:is\s+)?not\s+required|"
    r"new\s+graduates?|recent\s+graduates?|"
    r"entry[\s-]?level|early[\s-]?career"
    r")\b",
    re.IGNORECASE,
)


def _experience_requirements(description: str) -> list[tuple[int, str]]:
    """Return ``(minimum_years, source_text)`` pairs from a job description."""
    requirements: list[tuple[int, str]] = []
    for match in _EXPERIENCE_RE.finditer(description or ""):
        requirements.append((int(match.group("years")), match.group("requirement").strip()))
    return requirements


def _min_years_required(description: str) -> int | None:
    """Extract the lowest stated years-of-experience requirement from a JD."""
    requirements = _experience_requirements(description)
    return min((years for years, _ in requirements), default=None)


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
                            now: datetime | None = None,
                            variant_count: int | None = None) -> ApplicationDecision:
    app_cfg = cfg.get("application", {})
    title = (scored.job.title or "").casefold()
    excluded = [str(x).casefold() for x in app_cfg.get("excluded_seniority_keywords", [])]
    eligible = [str(x).casefold() for x in app_cfg.get("eligible_seniority_keywords", [])]
    freshness = freshness_bucket(scored.job.posted_date, cfg, now)

    excluded_hit = next((word for word in excluded if word and word in title), None)
    if excluded_hit:
        return ApplicationDecision(
            freshness, False, "manual_review",
            f"职位级别高于默认 entry/junior 范围（标题命中 {excluded_hit}）",
        )

    eligible_hit = next((word for word in eligible if word and word in title), None)
    if eligible_hit:
        eligibility_reason = f"标题命中级别词 {eligible_hit}，直接符合 entry/junior 范围"
    else:
        requirements = _experience_requirements(scored.job.description or "")
        max_years = int(app_cfg.get("max_years_experience", 2))
        min_years = min((years for years, _ in requirements), default=None)
        if min_years is not None and min_years > max_years:
            source_text = next(text for years, text in requirements if years == min_years)
            return ApplicationDecision(
                freshness, False, "manual_review",
                f"JD 要求至少 {min_years} 年经验（命中“{source_text}”），超过 entry/junior 上限 {max_years} 年",
            )
        early_career_hit = _EARLY_CAREER_RE.search(scored.job.description or "")
        if early_career_hit:
            eligibility_reason = (
                f"JD 命中初级信号“{early_career_hit.group(0)}”，按 entry/junior 范围放行"
            )
        elif min_years is None:
            eligibility_reason = "JD 未发现工作年限要求，按 entry/junior 策略放行"
        else:
            source_text = next(text for years, text in requirements if years == min_years)
            eligibility_reason = (
                f"JD 最低要求 {min_years} 年经验（命中“{source_text}”），不超过 entry/junior 上限 {max_years} 年"
            )

    if scored.score < int(app_cfg.get("broad_job_fit_threshold", 70)):
        return ApplicationDecision(
            freshness, False, "manual_review",
            f"{eligibility_reason}；岗位匹配分低于海投阈值",
        )

    # With one manifest entry there is no real choice to be made by the
    # resume-fit score.  ``None`` is kept backwards-compatible for direct
    # callers and means the normal single-variant configuration.
    single_variant = variant_count is None or variant_count == 1
    resume_gate_passed = single_variant or resume.fit_score >= int(
        app_cfg.get("targeted_resume_fit_threshold", 72)
    )
    if (
        scored.score >= int(app_cfg.get("targeted_job_fit_threshold", 80))
        and resume_gate_passed
    ):
        mode_reason = (
            "岗位达到精投阈值；单一简历版本不使用简历匹配门槛"
            if single_variant else "岗位与简历匹配均达到精投阈值"
        )
        return ApplicationDecision(freshness, True, "targeted", f"{eligibility_reason}；{mode_reason}")
    return ApplicationDecision(
        freshness, True, "broad",
        f"{eligibility_reason}；符合 entry/junior 海投阈值，使用匹配简历版本",
    )


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
        decision = decide_application_mode(
            scored, selection, cfg, now, variant_count=len(variants)
        )
        scored.resume_id = selection.resume_id
        scored.resume_path = str(selection.path)
        scored.resume_fit_score = selection.fit_score
        scored.resume_reason = selection.reason
        scored.application_mode = decision.mode
        scored.eligibility_reason = decision.reason
        scored.freshness_bucket = decision.freshness_bucket
    return scored_jobs
