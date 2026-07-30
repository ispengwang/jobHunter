"""Offline checks for complete Dashboard-backed ranked exports."""
from __future__ import annotations

import csv
from pathlib import Path
import tempfile

from dashboard import DASHBOARD_FIELDS, Dashboard
from run import (
    RANKED_FIELDS,
    ranked_rows_from_dashboard,
    save_ranked_from_dashboard,
    save_ranked_markdown_from_dashboard,
)
from schema import Job


CFG = {
    "application": {
        "eligible_seniority_keywords": ["graduate", "entry", "junior", "jnr"],
        "excluded_seniority_keywords": ["senior", "staff", "principal", "lead", "manager"],
        "max_years_experience": 2,
        "broad_job_fit_threshold": 70,
    },
}


def main() -> None:
    root = Path(tempfile.mkdtemp())
    dashboard_path = root / "data" / "application-dashboard.csv"
    events_path = root / "data" / "application-events.csv"
    dashboard = Dashboard(dashboard_path, events_path)

    job = Job(
        source="seek",
        title="Junior AI Engineer",
        company="Example Co",
        url="https://seek.example/jobs/1",
        location="Melbourne VIC",
        salary_raw="$70,000 - $80,000",
        description="Junior role building AI products.",
    )
    row = dashboard.upsert_recommendation(
        job,
        job_fit_score=84,
        job_fit_reason="方向匹配",
        sponsorship_signal="unknown",
        resume_id="resume-default",
        resume_fit_score=80,
        resume_reason="通用版本",
        application_mode="targeted",
        freshness_bucket="within_3d",
        salary_raw=job.salary_raw,
        matched=["Python", "LLM"],
        missing=["AWS"],
        eligibility_reason="标题命中级别词 junior",
    )
    assert row["salary_raw"] == job.salary_raw
    assert row["matched"] == "Python, LLM"
    assert row["missing"] == "AWS"
    assert row["eligibility_reason"] == "标题命中级别词 junior"

    # An old Dashboard header without the four new fields remains readable and
    # gains blank columns on the next normal write.
    old_path = root / "data" / "old-dashboard.csv"
    old_fields = [field for field in DASHBOARD_FIELDS if field not in {
        "salary_raw", "matched", "missing", "eligibility_reason",
    }]
    with old_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=old_fields)
        writer.writeheader()
        writer.writerow({field: row.get(field, "") for field in old_fields})
    old_dashboard = Dashboard(old_path, root / "data" / "old-events.csv")
    old_loaded = old_dashboard.load_rows()
    assert "salary_raw" not in old_loaded[0]
    old_dashboard.upsert_recommendation(
        job,
        job_fit_score=85,
        job_fit_reason="更新",
        sponsorship_signal="unknown",
        resume_id="resume-default",
        resume_fit_score=80,
        resume_reason="通用版本",
        application_mode="targeted",
        freshness_bucket="within_3d",
    )
    with old_path.open(encoding="utf-8", newline="") as handle:
        assert set(DASHBOARD_FIELDS).issubset(set(csv.DictReader(handle).fieldnames or []))

    # Backfill salary and local eligibility only; historical LLM fields stay empty.
    backfill_job = Job(
        source="seek",
        title="Junior AI Engineer",
        company="Backfill Co",
        url="https://seek.example/jobs/2",
        salary_raw="$90,000",
        description="This junior role has no fixed experience requirement.",
    )
    dashboard.upsert_recommendation(
        backfill_job,
        job_fit_score=76,
        job_fit_reason="方向匹配",
        sponsorship_signal="unknown",
        resume_id="resume-default",
        resume_fit_score=70,
        resume_reason="通用版本",
        application_mode="broad",
        freshness_bucket="older",
    )
    dashboard.backfill_export_fields([backfill_job], CFG)
    backfilled = dashboard.get(backfill_job.id)
    assert backfilled["salary_raw"] == "$90,000"
    assert backfilled["matched"] == ""
    assert backfilled["missing"] == ""
    assert "junior" in backfilled["eligibility_reason"]

    legacy_job = Job(
        source="seek",
        title="Graduate Developer",
        company="Legacy Co",
        url="https://seek.example/jobs/3",
        description="Graduate software role.",
    )
    dashboard.upsert_recommendation(
        legacy_job,
        job_fit_score=85,
        job_fit_reason="方向匹配",
        sponsorship_signal="unknown",
        resume_id="resume-default",
        resume_fit_score=70,
        resume_reason="通用版本",
        application_mode="broad",
        freshness_bucket="older",
    )

    rows = dashboard.load_rows()
    ranked = ranked_rows_from_dashboard(rows)
    assert len(ranked) == len(rows)
    assert [item["score"] for item in ranked] == ["85", "84", "76"]
    assert list(ranked[0]) == RANKED_FIELDS

    csv_path = root / "output" / "jobs-ranked.csv"
    md_path = root / "output" / "jobs-ranked.md"
    save_ranked_from_dashboard(rows, csv_path)
    save_ranked_markdown_from_dashboard(rows, md_path)
    with csv_path.open(encoding="utf-8", newline="") as handle:
        exported = list(csv.DictReader(handle))
    assert len(exported) == len(rows)
    assert list(exported[0]) == RANKED_FIELDS
    assert exported[-1]["matched"] == ""
    markdown = md_path.read_text(encoding="utf-8")
    assert "共 3 条" in markdown
    assert "Example Co" in markdown and "Backfill Co" in markdown
    assert "Junior AI Engineer" in markdown

    print("ranked export checks passed")


if __name__ == "__main__":
    main()
