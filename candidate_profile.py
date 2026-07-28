"""Candidate Profile 的本地事实来源。

首次运行从现有简历和偏好中提取可确定的信息，随后以
``profile/candidate_profile.md`` 为准。用户已编辑过的档案绝不在后续运行中被
简历解析结果静默覆盖。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
import hashlib
from pathlib import Path
import re
from typing import Any


UNKNOWN = "unknown"


_FIELD_LABELS = {
    "full_name": "Full name",
    "preferred_name": "Preferred name",
    "email": "Email",
    "phone": "Phone",
    "linkedin_url": "LinkedIn URL",
    "portfolio_url": "Portfolio / GitHub",
    "current_location": "Current location",
    "work_arrangement": "Work arrangement",
    "open_to_relocation": "Open to relocation",
    "earliest_start_date": "Earliest start date",
    "visa_status": "Current visa / work-right status",
    "work_right_expiry": "Work-right expiry",
    "requires_sponsorship_now": "Requires sponsorship now",
    "may_require_sponsorship_later": "May require sponsorship later",
    "work_right_notes": "Evidence or notes",
    "primary_directions": "Primary directions",
    "secondary_directions": "Secondary directions",
    "seniority": "Seniority",
    "preferred_locations": "Preferred locations",
    "minimum_salary_aud": "Minimum salary (AUD, annual)",
    "target_salary_range_aud": "Target salary range (AUD, annual)",
    "industries_to_prioritise": "Industries to prioritise",
    "industries_to_avoid": "Industries to avoid",
    "claims_may_use": "Claims that may be used",
    "claims_need_confirmation": "Claims that require my confirmation",
    "skills_studied_only": "Skills I have only studied or tried",
    "skills_not_held": "Skills / experience I do not have",
    "source_resume_sha256": "Source resume SHA-256",
    "last_reviewed": "Last reviewed",
}
_LABEL_TO_FIELD = {label.casefold(): name for name, label in _FIELD_LABELS.items()}
_LIST_FIELDS = {
    "primary_directions", "secondary_directions", "preferred_locations",
    "industries_to_prioritise", "industries_to_avoid", "claims_may_use",
    "claims_need_confirmation", "skills_studied_only", "skills_not_held",
}
FORM_SECTIONS = [
    ("Identity", ["full_name", "preferred_name", "email", "phone", "linkedin_url", "portfolio_url"]),
    ("Location and availability", ["current_location", "work_arrangement", "open_to_relocation", "earliest_start_date"]),
    ("Work rights", ["visa_status", "work_right_expiry", "requires_sponsorship_now", "may_require_sponsorship_later", "work_right_notes"]),
    ("Search target", ["primary_directions", "secondary_directions", "seniority", "preferred_locations", "minimum_salary_aud", "target_salary_range_aud", "industries_to_prioritise", "industries_to_avoid"]),
    ("Truth constraints", ["claims_may_use", "claims_need_confirmation", "skills_studied_only", "skills_not_held"]),
]
_EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
_PHONE_RE = re.compile(r"(?:\+?\d[\d ()-]{7,}\d)")
_URL_RE = re.compile(r"https?://[^\s)>]+", re.I)
_SALARY_RANGE_RE = re.compile(
    r"\$?\s*(\d{2,3})\s*(?:k|000)?\s*(?:-|–|—|to)\s*\$?\s*(\d{2,3})\s*k\b",
    re.I,
)


def _list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if not value or str(value).strip().casefold() == UNKNOWN:
        return []
    return [part.strip() for part in str(value).split(",") if part.strip()]


@dataclass
class CandidateProfile:
    full_name: str = UNKNOWN
    preferred_name: str = UNKNOWN
    email: str = UNKNOWN
    phone: str = UNKNOWN
    linkedin_url: str = UNKNOWN
    portfolio_url: str = UNKNOWN
    current_location: str = UNKNOWN
    work_arrangement: str = UNKNOWN
    open_to_relocation: str = "ask me"
    earliest_start_date: str = UNKNOWN
    visa_status: str = UNKNOWN
    work_right_expiry: str = UNKNOWN
    requires_sponsorship_now: str = UNKNOWN
    may_require_sponsorship_later: str = UNKNOWN
    work_right_notes: str = UNKNOWN
    primary_directions: list[str] = field(default_factory=list)
    secondary_directions: list[str] = field(default_factory=list)
    seniority: str = "entry level, junior"
    preferred_locations: list[str] = field(default_factory=list)
    minimum_salary_aud: str = UNKNOWN
    target_salary_range_aud: str = UNKNOWN
    industries_to_prioritise: list[str] = field(default_factory=list)
    industries_to_avoid: list[str] = field(default_factory=list)
    claims_may_use: list[str] = field(default_factory=list)
    claims_need_confirmation: list[str] = field(default_factory=list)
    skills_studied_only: list[str] = field(default_factory=list)
    skills_not_held: list[str] = field(default_factory=list)
    source_resume_sha256: str = UNKNOWN
    last_reviewed: str = UNKNOWN

    @classmethod
    def from_markdown(cls, text: str) -> "CandidateProfile":
        data: dict[str, Any] = {}
        for raw in text.splitlines():
            match = re.match(r"^\s*-\s*([^:]+):\s*(.*)$", raw)
            if not match:
                continue
            field = _LABEL_TO_FIELD.get(match.group(1).strip().casefold())
            if not field:
                continue
            value = match.group(2).strip() or UNKNOWN
            data[field] = _list(value) if field in _LIST_FIELDS else value
        return cls(**data)

    def to_markdown(self) -> str:
        values = asdict(self)

        def line(field: str) -> str:
            value = values[field]
            if field in _LIST_FIELDS:
                value = ", ".join(value) if value else UNKNOWN
            return f"- {_FIELD_LABELS[field]}: {value or UNKNOWN}"

        groups = [
            ("Identity", ["full_name", "preferred_name", "email", "phone", "linkedin_url", "portfolio_url"]),
            ("Location and availability", ["current_location", "work_arrangement", "open_to_relocation", "earliest_start_date"]),
            ("Work rights", ["visa_status", "work_right_expiry", "requires_sponsorship_now", "may_require_sponsorship_later", "work_right_notes"]),
            ("Search target", ["primary_directions", "secondary_directions", "seniority", "preferred_locations", "minimum_salary_aud", "target_salary_range_aud", "industries_to_prioritise", "industries_to_avoid"]),
            ("Truth constraints", ["claims_may_use", "claims_need_confirmation", "skills_studied_only", "skills_not_held"]),
            ("Provenance", ["source_resume_sha256", "last_reviewed"]),
        ]
        blocks = ["# Candidate Profile", "", "> This file is the editable local source of truth. `unknown` means the system must ask, not guess.", ""]
        for title, fields in groups:
            blocks.extend([f"## {title}", *(line(name) for name in fields), ""])
        return "\n".join(blocks).rstrip() + "\n"

    def validation_items(self) -> list[str]:
        """Fields to surface in UI before a real external submission.

        Scoring can still work with an incomplete profile. Real form filling cannot silently
        treat missing identity/work-right values as facts.
        """
        required = {
            "full_name": "姓名",
            "email": "邮箱",
            "current_location": "当前所在地",
            "visa_status": "签证/工作权利",
            "primary_directions": "主要求职方向",
        }
        missing: list[str] = []
        values = asdict(self)
        for field, label in required.items():
            value = values[field]
            if not value or value == UNKNOWN or value == []:
                missing.append(label)
        return missing

    def safe_context(self) -> str:
        """Prompt-facing profile context. It contains only stored facts, never inferred data."""
        return self.to_markdown()

    def matching_context(self) -> str:
        """Return only fields relevant to matching, excluding contact/identity data."""
        values = asdict(self)
        fields = [
            "current_location", "work_arrangement", "open_to_relocation", "earliest_start_date",
            "visa_status", "work_right_expiry", "requires_sponsorship_now", "may_require_sponsorship_later",
            "work_right_notes", "primary_directions", "secondary_directions", "seniority",
            "preferred_locations", "minimum_salary_aud", "target_salary_range_aud",
            "industries_to_prioritise", "industries_to_avoid", "claims_may_use",
            "claims_need_confirmation", "skills_studied_only", "skills_not_held",
        ]
        lines = ["# Candidate Profile (matching facts only)"]
        for field in fields:
            value = values[field]
            if field in _LIST_FIELDS:
                value = ", ".join(value) if value else UNKNOWN
            lines.append(f"- {_FIELD_LABELS[field]}: {value or UNKNOWN}")
        return "\n".join(lines)


def extract_from_resume(resume: str, preferences: str = "", cfg: dict | None = None) -> CandidateProfile:
    """Extract only unambiguous values; everything else stays ``unknown``.

    This is intentionally conservative. It is an initial form-fill helper, not a resume
    hallucination engine.
    """
    text = resume or ""
    combined = f"{text}\n{preferences}"
    urls = _URL_RE.findall(text)
    linkedin = next((u for u in urls if "linkedin.com" in u.casefold()), UNKNOWN)
    portfolio = next((u for u in urls if "github.com" in u.casefold() or "portfolio" in u.casefold()), UNKNOWN)
    email = (_EMAIL_RE.search(text).group(0) if _EMAIL_RE.search(text) else UNKNOWN)
    phone_match = _PHONE_RE.search(text)
    phone = phone_match.group(0).strip() if phone_match else UNKNOWN

    full_name = UNKNOWN
    for line in text.splitlines():
        match = re.match(r"^#\s+(.+?)\s*$", line)
        if match and "resume" not in match.group(1).casefold():
            full_name = match.group(1).strip()
            break

    location = UNKNOWN
    for city in ("Melbourne", "Sydney", "Brisbane", "Perth", "Adelaide", "Canberra", "Hobart"):
        if re.search(rf"\b{re.escape(city)}\b", combined, re.I):
            location = city
            break

    salary = UNKNOWN
    salary_match = _SALARY_RANGE_RE.search(combined)
    if salary_match:
        salary = f"AUD {int(salary_match.group(1)) * 1000:,}–{int(salary_match.group(2)) * 1000:,}"

    cfg = cfg or {}
    search = cfg.get("search", {})
    visa = cfg.get("visa", {})
    visa_status = str(visa.get("status") or UNKNOWN)
    needs_future_sponsorship = "yes" if "sponsorship" in visa_status.casefold() else UNKNOWN
    directions = [str(x) for x in search.get("terms", [])[:4] if str(x).strip()]
    preferred_locations = [str(search.get("location"))] if search.get("location") else ([] if location == UNKNOWN else [location])

    return CandidateProfile(
        full_name=full_name,
        email=email,
        phone=phone,
        linkedin_url=linkedin,
        portfolio_url=portfolio,
        current_location=location,
        visa_status=visa_status,
        requires_sponsorship_now="no" if visa_status == "485_needs_sponsorship" else UNKNOWN,
        may_require_sponsorship_later=needs_future_sponsorship,
        primary_directions=directions,
        seniority="entry level, junior",
        preferred_locations=preferred_locations,
        target_salary_range_aud=salary,
        source_resume_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        last_reviewed=date.today().isoformat(),
    )


def load_or_initialise(path: Path, resume: str, preferences: str = "", cfg: dict | None = None) -> CandidateProfile:
    """Load an edited profile, or create it once from the resume if it does not exist."""
    if path.exists():
        return CandidateProfile.from_markdown(path.read_text(encoding="utf-8"))
    profile = extract_from_resume(resume, preferences, cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(profile.to_markdown(), encoding="utf-8")
    return profile


def save_profile(path: Path, profile: CandidateProfile) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(profile.to_markdown(), encoding="utf-8")


def profile_form_sections(profile: CandidateProfile) -> list[tuple[str, list[dict[str, str | bool]]]]:
    values = asdict(profile)
    multiline = {"work_right_notes", "claims_may_use", "claims_need_confirmation", "skills_studied_only", "skills_not_held"}
    sections = []
    for title, fields in FORM_SECTIONS:
        items = []
        for field in fields:
            value = values[field]
            if field in _LIST_FIELDS:
                value = ", ".join(value)
            items.append({
                "name": field, "label": _FIELD_LABELS[field], "value": str(value if value else UNKNOWN),
                "multiline": field in multiline,
            })
        sections.append((title, items))
    return sections


def update_from_form(profile: CandidateProfile, form: Any) -> CandidateProfile:
    values = asdict(profile)
    for field in _FIELD_LABELS:
        if field in {"source_resume_sha256", "last_reviewed"}:
            continue
        raw = str(form.get(field, "")).strip()
        values[field] = _list(raw) if field in _LIST_FIELDS else (raw or UNKNOWN)
    values["last_reviewed"] = date.today().isoformat()
    return CandidateProfile(**values)
