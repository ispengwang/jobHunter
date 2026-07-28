"""Resume-variant catalogue and explainable local selector."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any

import yaml

from schema import Job


@dataclass(frozen=True)
class ResumeVariant:
    id: str
    path: str
    label: str
    target_roles: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    seniority: tuple[str, ...] = ("entry", "junior", "graduate")


@dataclass(frozen=True)
class ResumeSelection:
    resume_id: str
    path: Path
    fit_score: int
    reason: str


def ensure_default_manifest(path: Path, default_resume: str) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "resumes": [{
            "id": "resume-default",
            "path": default_resume,
            "label": "Default resume",
            "target_roles": [],
            "keywords": [],
            "seniority": ["graduate", "entry", "junior"],
        }]
    }
    path.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8")


def load_catalog(path: Path, root: Path) -> list[ResumeVariant]:
    raw: Any = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    entries = raw.get("resumes", []) if isinstance(raw, dict) else raw
    if not isinstance(entries, list):
        raise ValueError("resume manifest 的 resumes 必须是列表")

    variants: list[ResumeVariant] = []
    seen: set[str] = set()
    for item in entries:
        if not isinstance(item, dict):
            raise ValueError("resume manifest 的每项必须是对象")
        variant_id = str(item.get("id") or "").strip()
        relative_path = str(item.get("path") or "").strip()
        if not variant_id or not relative_path:
            raise ValueError("resume manifest 每项都需要 id 和 path")
        if variant_id in seen:
            raise ValueError(f"resume manifest 存在重复 id: {variant_id}")
        if not (root / relative_path).resolve().is_relative_to(root.resolve()):
            raise ValueError(f"resume path 必须位于项目内: {relative_path}")
        seen.add(variant_id)
        variants.append(ResumeVariant(
            id=variant_id,
            path=relative_path,
            label=str(item.get("label") or variant_id),
            target_roles=tuple(str(x) for x in item.get("target_roles", []) if str(x).strip()),
            keywords=tuple(str(x) for x in item.get("keywords", []) if str(x).strip()),
            seniority=tuple(str(x) for x in item.get("seniority", []) if str(x).strip()) or ("entry", "junior", "graduate"),
        ))
    if not variants:
        raise ValueError("resume manifest 至少需要一份简历")
    return variants


def _contains(text: str, phrase: str) -> bool:
    return bool(phrase and phrase.casefold() in text)


def choose_resume(job: Job, variants: list[ResumeVariant], root: Path) -> ResumeSelection:
    """Return a deterministic selection so every recommendation is explainable offline.

    DeepSeek supplies the wider job-fit assessment. This score answers a narrower question:
    which pre-approved resume version exposes the most relevant existing facts?
    """
    text = f"{job.title}\n{job.description}".casefold()
    title = (job.title or "").casefold()
    best: ResumeSelection | None = None
    for index, variant in enumerate(variants):
        role_hits = [x for x in variant.target_roles if _contains(text, x)]
        keyword_hits = [x for x in variant.keywords if _contains(text, x)]
        score = 60 + min(24, 12 * len(role_hits)) + min(16, 4 * len(keyword_hits))
        seniority_hits = [x for x in variant.seniority if _contains(title, x)]
        if seniority_hits:
            score += 4
        score = max(0, min(100, score))
        reason_bits: list[str] = []
        if role_hits:
            reason_bits.append("角色匹配 " + ", ".join(role_hits[:2]))
        if keyword_hits:
            reason_bits.append("关键词匹配 " + ", ".join(keyword_hits[:3]))
        if not reason_bits:
            reason_bits.append("使用默认/通用简历版本")
        candidate = ResumeSelection(
            resume_id=variant.id,
            path=(root / variant.path).resolve(),
            fit_score=score,
            reason="；".join(reason_bits),
        )
        if best is None or candidate.fit_score > best.fit_score or (
            candidate.fit_score == best.fit_score and index == 0
        ):
            best = candidate
    assert best is not None
    return best


def read_resume(selection: ResumeSelection) -> str:
    if not selection.path.exists():
        raise FileNotFoundError(f"找不到简历版本: {selection.path}")
    return selection.path.read_text(encoding="utf-8").strip()
