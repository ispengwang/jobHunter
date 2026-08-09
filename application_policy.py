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


def eligibility_reason_for_job(job, job_fit_score: int, cfg: dict) -> str:
    """Recompute the local eligibility explanation without calling an LLM.

    This is also used to backfill older Dashboard rows.  A missing JD snapshot is
    reported as an explicit manual-review condition rather than being treated as
    evidence that the role has no experience requirement.
    """
    app_cfg = cfg.get("application", {})
    title = (job.title or "").casefold()
    excluded = [str(x).casefold() for x in app_cfg.get("excluded_seniority_keywords", [])]
    eligible = [str(x).casefold() for x in app_cfg.get("eligible_seniority_keywords", [])]

    excluded_hit = next((word for word in excluded if word and word in title), None)
    if excluded_hit:
        return f"职位级别高于默认 entry/junior 范围（标题命中 {excluded_hit}）"

    eligible_hit = next((word for word in eligible if word and word in title), None)
    if eligible_hit:
        eligibility_reason = f"标题命中级别词 {eligible_hit}，直接符合 entry/junior 范围"
    elif not (job.description or "").strip():
        return "当前本地没有 JD 快照，无法重新核验 entry/junior 资格，需人工确认"
    else:
        requirements = _experience_requirements(job.description or "")
        max_years = int(app_cfg.get("max_years_experience", 2))
        min_years = min((years for years, _ in requirements), default=None)
        if min_years is not None and min_years > max_years:
            source_text = next(text for years, text in requirements if years == min_years)
            return (
                f"JD 要求至少 {min_years} 年经验（命中“{source_text}”），"
                f"超过 entry/junior 上限 {max_years} 年"
            )
        early_career_hit = _EARLY_CAREER_RE.search(job.description or "")
        if early_career_hit:
            eligibility_reason = (
                f"JD 命中初级信号“{early_career_hit.group(0)}”，按 entry/junior 范围放行"
            )
        elif min_years is None:
            eligibility_reason = "JD 未发现工作年限要求，按 entry/junior 策略放行"
        else:
            source_text = next(text for years, text in requirements if years == min_years)
            eligibility_reason = (
                f"JD 最低要求 {min_years} 年经验（命中“{source_text}”），"
                f"不超过 entry/junior 上限 {max_years} 年"
            )

    if job_fit_score < int(app_cfg.get("broad_job_fit_threshold", 70)):
        return f"{eligibility_reason}；岗位匹配分低于海投阈值"
    return eligibility_reason


# Keep this parser deliberately small and explainable.  The numeric expression
# is intentionally broader than the final result: `_experience_requirements`
# also checks that the surrounding clause describes an applicant qualification,
# rather than an employer's age, history, or years of profitability.
_EXPERIENCE_RE = re.compile(
    r"""
    \b(?P<requirement>
        (?:(?:at\s+least|minimum(?:\s+of)?|a\s+minimum\s+of)\s+)?
        (?P<years>\d+)
        (?:
            \s*(?:-|–|—|to)\s*\d+\s*years?
            |\s*(?:\+|plus)?\s*years?
        )
        (?:\s*(?:of\s+)?(?:relevant|professional|commercial|industry|software|work)?\s*experience)?
    )\b
    """,
    re.IGNORECASE | re.VERBOSE,
)
_EXPERIENCE_LINK_RE = re.compile(
    r"(?:years?\s*(?:['’]\s*)?(?:of\s+)?(?:[\w/-]+\s+){0,5}(?:experience|expertise))\b",
    re.IGNORECASE,
)
_EXPERIENCE_DOMAIN_LINK_RE = re.compile(
    r"\byears?\s+(?:in|as|working\s+(?:in|with|on)|developing|building|"
    r"delivering|leading|managing|using)\b",
    re.IGNORECASE,
)
_EXPERIENCE_REQUIREMENT_CUE_RE = re.compile(
    r"\b(?:"
    r"this\s+role\s+requires?|we\s+require|required|requirements?|qualifications?|"
    r"about\s+you|who\s+we(?:'re|\s+are)\s+looking\s+for|looking\s+for|"
    r"what\s+you(?:'ll|\s+will)?\s+bring|you(?:'ll|\s+will)?\s+(?:have|bring|need)|"
    r"candidates?|applicants?|must(?:\s+have)?|should(?:\s+have)?|"
    r"essential|preferred"
    r")\b",
    re.IGNORECASE,
)
_MINIMUM_YEARS_CUE_RE = re.compile(
    r"\b(?:at\s+least|minimum(?:\s+of)?|a\s+minimum\s+of)\b",
    re.IGNORECASE,
)
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")
_EARLY_CAREER_RE = re.compile(
    r"\b(?:"
    r"no\s+(?:prior\s+)?experience\s+(?:is\s+)?(?:required|necessary)|"
    r"experience\s+(?:is\s+)?not\s+required|"
    r"new\s+graduates?|recent\s+graduates?|"
    r"entry[\s-]?level|early[\s-]?career"
    r")\b",
    re.IGNORECASE,
)


def _looks_like_experience_requirement(description: str, match: re.Match[str]) -> bool:
    """Return whether a numeric years phrase is an applicant requirement.

    Job descriptions frequently contain employer-history prose such as "for 25
    years" or "57 years of unbroken profitability".  A years phrase is accepted
    only when the local line/clause provides a qualification cue, or when an
    experience phrase appears as a standalone/listed requirement.
    """
    start, end = match.span("requirement")
    line_start = description.rfind("\n", 0, start) + 1
    line_end = description.find("\n", end)
    if line_end < 0:
        line_end = len(description)
    line = description[line_start:line_end]
    # Include a nearby section heading (often the preceding line, such as
    # "Required skills") while keeping the context narrow.
    local_start = max(0, start - 160)
    local_end = min(len(description), end + 120)
    local_context = description[local_start:local_end]

    years_context = description[start:min(line_end, end + 100)]
    experience_linked = bool(_EXPERIENCE_LINK_RE.search(years_context))
    domain_linked = bool(_EXPERIENCE_DOMAIN_LINK_RE.search(years_context))
    minimum_cued = bool(_MINIMUM_YEARS_CUE_RE.search(local_context))
    requirement_cued = bool(_EXPERIENCE_REQUIREMENT_CUE_RE.search(local_context))
    line_prefix = description[line_start:start]
    inline_list_item = bool(re.search(
        r"(?:^|\s)(?:[-*•]|\d+[.)])(?:\s|\\~|[*_])*$", line_prefix,
    ))
    starts_requirement = (
        not line_prefix.strip() or bool(_LIST_ITEM_RE.match(line)) or inline_list_item
    )
    linked_to_candidate_work = experience_linked or domain_linked
    if requirement_cued and (linked_to_candidate_work or minimum_cued):
        return True
    if inline_list_item and (linked_to_candidate_work or minimum_cued):
        return True
    return starts_requirement and (linked_to_candidate_work or minimum_cued)


def _experience_requirements(description: str) -> list[tuple[int, str]]:
    """Return ``(minimum_years, source_text)`` pairs from a job description."""
    # LinkedIn Markdown escapes range punctuation (for example ``2\-5``).
    # Removing only those escape slashes preserves the meaning before matching.
    normalised = re.sub(r"\\(?=[+\-–—])", "", description or "")
    requirements: list[tuple[int, str]] = []
    for match in _EXPERIENCE_RE.finditer(normalised):
        if not _looks_like_experience_requirement(normalised, match):
            continue
        requirements.append((int(match.group("years")), match.group("requirement").strip()))
    return requirements


def _min_years_required(description: str) -> int | None:
    """Extract the lowest stated years-of-experience requirement from a JD."""
    requirements = _experience_requirements(description)
    return min((years for years, _ in requirements), default=None)


def freshness_bucket(
    posted_at: str | None,
    cfg: dict,
    now: datetime | None = None,
    *,
    first_seen_at: str | None = None,
) -> str:
    """Classify freshness, using first discovery when a source only gives a date.

    A date-only ``posted_at`` is not precise enough for the 24-hour queue. In that case the
    dashboard's ``first_seen_at`` is the honest fallback; the UI labels it as first discovered,
    never as the posting time.
    """
    raw_posted = str(posted_at or "").strip()
    effective = first_seen_at
    if raw_posted and len(raw_posted) > 10:
        try:
            datetime.fromisoformat(raw_posted.replace("Z", "+00:00"))
        except ValueError:
            pass
        else:
            effective = posted_at
    if not effective:
        return "unknown"
    try:
        raw = str(effective).strip().replace("Z", "+00:00")
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
                            variant_count: int | None = None,
                            first_seen_at: str | None = None) -> ApplicationDecision:
    app_cfg = cfg.get("application", {})
    freshness = freshness_bucket(
        scored.job.posted_date, cfg, now, first_seen_at=first_seen_at,
    )
    eligibility_reason = eligibility_reason_for_job(scored.job, scored.score, cfg)
    if (
        "职位级别高于默认 entry/junior 范围" in eligibility_reason
        or eligibility_reason.startswith("JD 要求至少")
        or "无法重新核验 entry/junior 资格" in eligibility_reason
    ):
        return ApplicationDecision(freshness, False, "manual_review", eligibility_reason)
    if scored.score < int(app_cfg.get("broad_job_fit_threshold", 70)):
        return ApplicationDecision(freshness, False, "manual_review", eligibility_reason)

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
                       now: datetime | None = None,
                       first_seen_at: str | None = None,
                       first_seen_at_by_job: dict[str, str] | None = None) -> list[Scored]:
    """Attach resume selection and workflow policy to already LLM-scored jobs."""
    for scored in scored_jobs:
        seen_at = (first_seen_at_by_job or {}).get(scored.job.id) or first_seen_at
        if scored.score < 0:
            scored.application_mode = "manual_review"
            scored.eligibility_reason = "DeepSeek 打分失败，需人工判断"
            scored.freshness_bucket = freshness_bucket(
                scored.job.posted_date, cfg, now, first_seen_at=seen_at,
            )
            continue
        selection = choose_resume(scored.job, variants, root)
        decision = decide_application_mode(
            scored, selection, cfg, now, variant_count=len(variants), first_seen_at=seen_at
        )
        scored.resume_id = selection.resume_id
        scored.resume_path = str(selection.path)
        scored.resume_fit_score = selection.fit_score
        scored.resume_reason = selection.reason
        scored.application_mode = decision.mode
        scored.eligibility_reason = decision.reason
        scored.freshness_bucket = decision.freshness_bucket
    return scored_jobs
