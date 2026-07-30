#!/usr/bin/env python3
"""jobhunt-au — SEEK + Indeed + LinkedIn 岗位聚合、DeepSeek 打分与 Dashboard。

默认到"打分排序 + Dashboard 同步"为止。``--autopilot`` 为当前 Codex Agent 生成
可立即执行的内部尝试清单；JobHunter 本身仍不操作招聘网站，``applypilot-au`` skill
负责平台规则、自动投递门槛、每日限额、浏览器流程和提交证据记录。

用法:
  python run.py                    # 完整流程,到打分排序为止
  python run.py --scrape-only      # 只抓取,存 CSV,不烧 token
  python run.py --generate         # 额外生成简历/cover letter(可选,非默认)
  python run.py --from-cache       # 复用上次抓取结果,只重跑打分
  python run.py --limit 20         # 只处理前 N 个,首次试跑用
  python run.py --autopilot        # 搜索、DeepSeek 打分、同步并交给当前 Agent 继续投递
  python run.py --handoff-list     # 输出当前已选/排队的本地投递清单
  python run.py --rescore-all      # 评分规则变更后强制重打全部岗位
  python run.py --incremental      # 定时增量抓取，使用 48 小时窗口
  python run.py --attempt-handoff ID
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
import logging
import re
import sys
from uuid import uuid4
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))

from schema import Job, FIELDS
from dedupe import dedupe

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("jobhunt")

ROOT = Path(__file__).parent
CACHE = "jobs-raw.csv"
RANKED = "jobs-ranked.csv"
RANKED_MD = "jobs-ranked.md"
RANKED_FIELDS = [
    "score", "title", "company", "location", "posted_date", "salary_raw",
    "sponsorship_signal", "summary", "reason", "matched", "missing",
    "resume_id", "resume_fit_score", "resume_reason", "application_mode",
    "eligibility_reason", "freshness_bucket", "artifact_path", "source", "url",
    "duplicate_urls",
]


def project_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def load_cfg(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def read_profile(cfg: dict) -> tuple[str, str]:
    resume_path = project_path(cfg["paths"]["resume"])
    prefs_path = project_path(cfg["paths"]["preferences"])

    if not resume_path.exists():
        log.error("找不到简历:%s —— 先把你的简历写进这个文件", resume_path)
        sys.exit(1)

    resume = resume_path.read_text(encoding="utf-8").strip()
    if len(resume) < 200:
        log.error("简历内容太短(%d 字符),看起来还是模板,请先填好", len(resume))
        sys.exit(1)

    prefs = prefs_path.read_text(encoding="utf-8").strip() if prefs_path.exists() else ""
    return resume, prefs


def scrape(cfg: dict) -> list[Job]:
    from scrapers import jobspy_source, seek_source

    jobs: list[Job] = []
    for name, mod in [("JobSpy(LinkedIn/Indeed)", jobspy_source), ("SEEK", seek_source)]:
        try:
            got = mod.fetch(cfg)
            log.info("%s 共 %d 条", name, len(got))
            jobs.extend(got)
        except Exception as e:
            # 一个源挂了不能拖垮整个流程
            log.error("%s 整体失败,跳过:%s", name, e)
    return jobs


def save_jobs(jobs: list[Job], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        for j in jobs:
            w.writerow(j.to_dict())
    log.info("已写入 %s (%d 条)", path, len(jobs))


_JUNIOR_SIGNAL = re.compile(
    r"\b(graduate|new\s+grad|entry[- ]level|junior|jnr|associate)\b", re.I,
)


def _posted_sort_value(job: Job) -> float:
    raw = (job.posted_date or "").strip().replace("Z", "+00:00")
    if not raw:
        return float("-inf")
    try:
        when = datetime.fromisoformat(raw)
    except ValueError:
        return float("-inf")
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.timestamp()


def representative_sample(jobs: list[Job], limit: int | None) -> list[Job]:
    """Take a fresh, representative sample after dedupe instead of fetch-order rows.

    A junior slice is reserved because junior terms are intentionally later in the configured
    search list. The rest is newest-first; ties retain the original deterministic order.
    """
    if not limit or limit >= len(jobs):
        return list(jobs)
    ordered = sorted(
        enumerate(jobs),
        key=lambda pair: (-_posted_sort_value(pair[1]), pair[0]),
    )
    junior = [pair for pair in ordered if _JUNIOR_SIGNAL.search(
        f"{pair[1].title} {pair[1].description[:500]}"
    )]
    junior_quota = min(len(junior), max(1, limit // 4))
    selected = [job for _, job in junior[:junior_quota]]
    selected_ids = {id(job) for job in selected}
    selected.extend(job for _, job in ordered if id(job) not in selected_ids)
    return selected[:limit]


def prepare_scraped_jobs(jobs: list[Job], cache_path: Path, limit: int | None = None) -> list[Job]:
    """Persist the complete scrape; a limit is applied later to the deduped/filtered sample."""
    if limit:
        log.info("--limit=%d 延后到去重和签证过滤后按代表性抽样；保持主抓取缓存不变: %s", limit, cache_path)
        return jobs
    save_jobs(jobs, cache_path)
    return jobs


def jobs_to_score(
    jobs: list[Job],
    existing_job_ids: set[str],
    *,
    rescore_all: bool = False,
) -> list[Job]:
    """Return only unseen jobs unless a rules-change run explicitly requests a full rescore."""
    if rescore_all:
        return list(jobs)
    return [job for job in jobs if job.id not in existing_job_ids]


def load_jobs(path: Path) -> list[Job]:
    jobs = []
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            jobs.append(Job(
                source=row["source"], title=row["title"], company=row["company"],
                url=row["url"], location=row.get("location") or None,
                salary_min=float(row["salary_min"]) if row.get("salary_min") else None,
                salary_max=float(row["salary_max"]) if row.get("salary_max") else None,
                salary_raw=row.get("salary_raw") or None,
                work_type=row.get("work_type") or None,
                posted_date=row.get("posted_date") or None,
                description=row.get("description") or "",
                duplicate_urls=[u for u in (row.get("duplicate_urls") or "").split("; ") if u],
                # Recompute IDs so an old URL-based jobs-raw.csv is upgraded
                # in memory when --from-cache is used after WF-101.
                id="",
            ))
    return jobs


def _ranked_score(value) -> int:
    try:
        return int(float(str(value or "").strip()))
    except (TypeError, ValueError):
        return -1


def ranked_rows_from_dashboard(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    """Project every Dashboard row into the stable ranked-export schema."""
    ranked = []
    for row in rows:
        ranked.append({
            "score": row.get("job_fit_score") or row.get("match_score") or "",
            "title": row.get("title", ""),
            "company": row.get("company", ""),
            "location": row.get("location", ""),
            "posted_date": row.get("posted_at", ""),
            "salary_raw": row.get("salary_raw", ""),
            "sponsorship_signal": row.get("sponsorship_signal", ""),
            "summary": row.get("job_summary", ""),
            "reason": row.get("job_fit_reason", ""),
            "matched": row.get("matched", ""),
            "missing": row.get("missing", ""),
            "resume_id": row.get("resume_id", ""),
            "resume_fit_score": row.get("resume_fit_score", ""),
            "resume_reason": row.get("resume_reason", ""),
            "application_mode": row.get("application_mode", ""),
            "eligibility_reason": row.get("eligibility_reason", ""),
            "freshness_bucket": row.get("freshness_bucket", ""),
            "artifact_path": row.get("artifact_path", ""),
            "source": row.get("source", ""),
            "url": row.get("url", ""),
            "duplicate_urls": row.get("duplicate_urls", ""),
        })
    ranked.sort(key=lambda row: (
        -_ranked_score(row["score"]),
        row["company"].casefold(),
        row["title"].casefold(),
        row["url"],
    ))
    return ranked


def _write_ranked_rows(rows: list[dict[str, str]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=RANKED_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in RANKED_FIELDS})
    log.info("已写入 %s", path)


def save_ranked_from_dashboard(rows: list[dict[str, str]], path: Path) -> None:
    """Write a complete ranking rebuilt from all persisted Dashboard rows."""
    _write_ranked_rows(ranked_rows_from_dashboard(rows), path)


def save_ranked(scored, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=RANKED_FIELDS)
        w.writeheader()
        for s in scored:
            j = s.job
            w.writerow({
                "score": s.score, "title": j.title, "company": j.company,
                "location": j.location, "posted_date": j.posted_date,
                "salary_raw": j.salary_raw, "sponsorship_signal": s.sponsorship_signal,
                "summary": s.summary, "reason": s.reason,
                "matched": ", ".join(s.matched), "missing": ", ".join(s.missing),
                "resume_id": s.resume_id, "resume_fit_score": s.resume_fit_score,
                "resume_reason": s.resume_reason, "application_mode": s.application_mode,
                "eligibility_reason": s.eligibility_reason, "freshness_bucket": s.freshness_bucket,
                "artifact_path": s.artifact_path,
                "source": j.source, "url": j.url,
                "duplicate_urls": "; ".join(j.duplicate_urls),
            })
    log.info("已写入 %s", path)


def save_ranked_markdown(scored, path: Path) -> None:
    """单文档汇总:分数 + 链接 + 摘要,按分数降序,方便直接打开看,不用开 Excel。

    CSV(save_ranked)适合筛选排序,这份 Markdown 适合"从头到尾看一遍"。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    valid = [s for s in scored if s.score >= 0]
    failed = [s for s in scored if s.score < 0]

    lines = [f"# 岗位打分汇总\n", f"共 {len(scored)} 条,{len(valid)} 条打分成功"]
    if failed:
        lines.append(f",{len(failed)} 条打分失败(见文末)")
    lines.append("。\n")

    for s in valid:
        j = s.job
        salary = j.salary_raw or "未列出"
        loc = j.location or "未列出"
        matched = "、".join(s.matched) if s.matched else "—"
        missing = "、".join(s.missing) if s.missing else "—"
        dup_note = ""
        if j.duplicate_urls:
            dup_note = f"\n- 同岗位其他链接: {' , '.join(j.duplicate_urls)}"

        lines.append(f"""## {s.score} 分 — {j.title} @ {j.company}

- **链接**: {j.url}
- **薪资**: {salary} · **地点**: {loc} · **来源**: {j.source}
- **摘要**: {s.summary or '(无摘要)'}
- **匹配理由**: {s.reason}
- **已满足**: {matched}
- **待补强**: {missing}{dup_note}
- **申请路径**: {s.application_mode} · **新鲜度**: {s.freshness_bucket}
- **简历**: {s.resume_id or '默认简历'} ({s.resume_fit_score or '未计算'}/100) — {s.resume_reason or '—'}
- **资格判断**: {s.eligibility_reason or '—'}
""")

    if failed:
        lines.append("---\n\n## 打分失败的岗位(需人工看)\n")
        for s in failed:
            j = s.job
            lines.append(f"- {j.title} @ {j.company} — {j.url}({s.reason}）")

    path.write_text("\n".join(lines), encoding="utf-8")
    log.info("已写入 %s(单文档汇总,含链接和分数)", path)


def save_ranked_markdown_from_dashboard(rows: list[dict[str, str]], path: Path) -> None:
    """Write the complete Markdown ranking rebuilt from Dashboard rows."""
    ranked = ranked_rows_from_dashboard(rows)
    valid = [row for row in ranked if _ranked_score(row["score"]) >= 0]
    failed = [row for row in ranked if _ranked_score(row["score"]) < 0]

    lines = [f"# 岗位打分汇总\n", f"共 {len(ranked)} 条,{len(valid)} 条打分成功"]
    if failed:
        lines.append(f",{len(failed)} 条打分失败(见文末)")
    lines.append("。\n")

    for row in valid:
        salary = row["salary_raw"] or "未列出"
        location = row["location"] or "未列出"
        matched = row["matched"] or "—"
        missing = row["missing"] or "—"
        duplicate_urls = [url for url in row["duplicate_urls"].split("; ") if url]
        duplicate_note = ""
        if duplicate_urls:
            duplicate_note = f"\n- 同岗位其他链接: {' , '.join(duplicate_urls)}"
        lines.append(f"""## {_ranked_score(row["score"])} 分 — {row["title"]} @ {row["company"]}

- **链接**: {row["url"]}
- **薪资**: {salary} · **地点**: {location} · **来源**: {row["source"]}
- **摘要**: {row["summary"] or '(无摘要)'}
- **匹配理由**: {row["reason"]}
- **已满足**: {matched}
- **待补强**: {missing}{duplicate_note}
- **申请路径**: {row["application_mode"]} · **新鲜度**: {row["freshness_bucket"]}
- **简历**: {row["resume_id"] or '默认简历'} ({row["resume_fit_score"] or '未计算'}/100) — {row["resume_reason"] or '—'}
- **资格判断**: {row["eligibility_reason"] or '—'}
""")

    if failed:
        lines.append("---\n\n## 打分失败的岗位(需人工看)\n")
        for row in failed:
            lines.append(
                f"- {row['title']} @ {row['company']} — {row['url']}（{row['reason']}）"
            )

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    log.info("已写入 %s(单文档汇总,含链接和分数)", path)


def submitted_today(rows: list[dict[str, str]], timezone_name: str) -> int:
    """Count evidenced submissions for compatibility with older callers."""
    from application_attempts import submitted_today_by_platform

    return sum(submitted_today_by_platform(rows, timezone_name).values())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--scrape-only", action="store_true")
    ap.add_argument("--generate", action="store_true",
                     help="额外生成简历/cover letter(默认不生成,见文件顶部说明)")
    ap.add_argument("--no-generate", action="store_true",
                     help="[已弃用,现在是默认行为,保留只为兼容旧命令]")
    ap.add_argument("--from-cache", action="store_true")
    ap.add_argument(
        "--autopilot", action="store_true",
        help="运行完整搜索和 DeepSeek 打分，自动选出可投岗位并生成当前 Agent 的执行清单",
    )
    ap.add_argument(
        "--handoff-list", action="store_true",
        help="输出 selected/queued 投递记录的本地 Agent handoff 清单，然后退出",
    )
    ap.add_argument(
        "--rescore-all", action="store_true",
        help="忽略 Dashboard 中的现有 job_id，强制重新评分全部岗位",
    )
    ap.add_argument(
        "--incremental", action="store_true",
        help="增量抓取模式，将 hours_old 收窄为 48 小时",
    )
    ap.add_argument("--attempt-handoff",
                    help="打印某条 Agent 内部执行记录的无敏感信息说明，然后退出")
    ap.add_argument("--attempt-update",
                    help="由 ApplyPilot 写回某条内部记录的真实执行状态，然后退出")
    ap.add_argument("--attempt-status",
                    help="与 --attempt-update 配合，例如 filling / needs_user / submitted")
    ap.add_argument("--attempt-reason", default="",
                    help="needs_user / failed 等状态的明确原因")
    ap.add_argument("--submission-evidence", default="",
                    help="submitted 状态的平台成功页文本或确认 URL")
    ap.add_argument("--submission-confirmed", action="store_true",
                    help="确认外部平台已明确显示提交成功")
    ap.add_argument("--limit", type=int,
                     help="去重和签证过滤后按新鲜度抽取 N 个代表性岗位。试跑/日常快速刷新用。")
    args = ap.parse_args()

    cfg = load_cfg(ROOT / args.config)
    if args.incremental:
        if args.from_cache:
            ap.error("--incremental 需要重新抓取，不能与 --from-cache 同时使用")
        cfg.setdefault("search", {})["hours_old"] = 48
        log.info("增量抓取模式：hours_old=48（Indeed 仍按 date_posted 本地过滤）")
    if args.autopilot and args.scrape_only:
        ap.error("--autopilot 不能与 --scrape-only 同时使用")
    if args.autopilot and cfg.get("llm", {}).get("provider", "").lower() != "deepseek":
        ap.error("--autopilot 要求 config.yaml 的 llm.provider 为 deepseek")
    if args.autopilot and not bool(cfg.get("applypilot", {}).get("autonomous", True)):
        ap.error("config.yaml 已关闭 applypilot.autonomous")
    if args.handoff_list and (args.attempt_handoff or args.attempt_update):
        ap.error("--handoff-list 不能与 --attempt-handoff/--attempt-update 同时使用")
    if args.handoff_list or args.attempt_handoff or args.attempt_update:
        from application_attempts import ApplicationAttempts
        from dashboard import Dashboard
        board = Dashboard(
            project_path(cfg["paths"].get("dashboard", "data/application-dashboard.csv")),
            project_path(cfg["paths"].get("application_events", "data/application-events.csv")),
            freshness_config=cfg,
        )
        configured_skill = cfg.get("applypilot", {}).get("skill_path", "")
        attempts = ApplicationAttempts(
            project_path(cfg["paths"].get("application_attempts", "data/application-attempts.csv")),
            board,
            browser_enabled=bool(cfg.get("application", {}).get("browser_enabled", True)),
            skill_path=project_path(configured_skill) if configured_skill else None,
            project_root=ROOT,
            platform_config=cfg.get("applypilot", {}),
        )
        if args.handoff_list:
            result = attempts.handoff_list()
        elif args.attempt_handoff:
            result = attempts.handoff_payload(args.attempt_handoff)
        else:
            if not args.attempt_status:
                ap.error("--attempt-update 必须同时提供 --attempt-status")
            result = attempts.advance(
                args.attempt_update,
                args.attempt_status,
                actor="applypilot",
                reason=args.attempt_reason,
                submission_confirmed=args.submission_confirmed,
                submission_evidence=args.submission_evidence,
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    out_dir = project_path(cfg["paths"]["output_dir"])
    cache_path = out_dir / CACHE

    # ---- 1. 抓取
    if args.from_cache:
        if not cache_path.exists():
            log.error("没有缓存 %s,先跑一次完整抓取", cache_path)
            sys.exit(1)
        jobs = load_jobs(cache_path)
        log.info("从缓存读取 %d 条", len(jobs))
    else:
        jobs = scrape(cfg)
        if not jobs:
            log.error("一条都没抓到。检查网络/代理,或先跑 --scrape-only 单独排查。")
            sys.exit(1)
        # ---- 2. 归一化已在各 source 内完成,直接去重
        jobs = dedupe(jobs, cfg["dedupe"]["fuzzy_threshold"])
        jobs = prepare_scraped_jobs(jobs, cache_path, args.limit)

    if args.scrape_only:
        log.info("--scrape-only 完成,不调用 LLM。")
        return

    # ---- 3. 签证硬过滤(不花钱,先做)
    from score import visa_filter, score_jobs, build_scoring_input
    jobs, rejected = visa_filter(jobs, cfg["visa"])
    if rejected:
        rej_path = out_dir / "rejected-visa.csv"
        save_jobs([j for j, _ in rejected], rej_path)
        log.info("被签证条件淘汰的岗位已单独存档,建议扫一眼确认没误杀")

    if args.limit:
        jobs = representative_sample(jobs, args.limit)
        log.info("--limit 生效,去重/签证过滤后按新鲜度和 junior 代表性抽取 %d 条", len(jobs))

    if not jobs:
        log.error("过滤后没有剩余岗位。config.yaml 的 exclude_keywords 可能过严。")
        sys.exit(1)

    from dashboard import Dashboard
    dashboard = Dashboard(
        project_path(cfg["paths"].get("dashboard", "data/application-dashboard.csv")),
        project_path(cfg["paths"].get("application_events", "data/application-events.csv")),
        freshness_config=cfg,
    )
    dashboard.backfill_export_fields(jobs, cfg)
    run_id = uuid4().hex[:12]
    dashboard_rows = dashboard.load_rows()
    existing_job_ids = {row.get("job_id", "") for row in dashboard_rows if row.get("job_id")}
    first_seen_at_by_job = {
        row.get("job_id", ""): row.get("first_seen_at") or row.get("discovered_at", "")
        for row in dashboard_rows if row.get("job_id")
    }
    sync_timestamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    jobs_for_scoring = jobs_to_score(
        jobs, existing_job_ids, rescore_all=args.rescore_all,
    )
    existing_jobs = [job for job in jobs if job.id in existing_job_ids]
    touched = dashboard.sync_existing_jobs(existing_jobs, run_id=run_id)
    if not args.rescore_all:
        log.info(
            "增量打分：%d 条岗位中 %d 条已有记录仅更新 last_synced_at，%d 条新岗位进入 DeepSeek",
            len(jobs), touched, len(jobs_for_scoring),
        )
    if not jobs_for_scoring:
        save_ranked_from_dashboard(
            dashboard.load_rows(), out_dir / RANKED,
        )
        save_ranked_markdown_from_dashboard(
            dashboard.load_rows(), out_dir / RANKED_MD,
        )
        print("本轮没有新岗位需要 DeepSeek 打分；已有岗位只更新了同步时间。")
        return

    # ---- 4. LLM 打分（增量模式只处理 jobs_for_scoring）
    resume, prefs = read_profile(cfg)
    from candidate_profile import load_or_initialise
    from resume_catalog import ensure_default_manifest, load_catalog

    candidate_path = project_path(cfg["paths"].get("candidate_profile", "profile/candidate_profile.md"))
    candidate = load_or_initialise(candidate_path, resume, prefs, cfg)
    missing_profile = candidate.validation_items()
    if missing_profile:
        log.warning("Candidate Profile 尚待补充: %s（不影响本轮筛选，但真实投递前必须完成）", "、".join(missing_profile))

    manifest_path = project_path(cfg["paths"].get("resume_manifest", "profile/resumes/manifest.yaml"))
    ensure_default_manifest(manifest_path, cfg["paths"]["resume"])
    variants = load_catalog(manifest_path, ROOT)
    from llm import LLM
    llm = LLM(cfg)

    configured_skill = cfg.get("applypilot", {}).get("skill_path", "")
    skill_path = project_path(configured_skill) if configured_skill else None
    scoring_rules_path = (
        skill_path / "references" / "deepseek-scoring-rules.md"
        if skill_path else None
    )
    if not scoring_rules_path or not scoring_rules_path.exists():
        log.error("找不到 applypilot-au 的 DeepSeek 评分规则: %s", scoring_rules_path or "未配置 Skill")
        sys.exit(1)
    scoring_rules = scoring_rules_path.read_text(encoding="utf-8")
    scoring_policy = build_scoring_input(
        scoring_rules,
        preferences=prefs,
        candidate_context=candidate.matching_context(),
        cfg=cfg,
    )
    scoring_rules_hash = hashlib.sha256(scoring_rules.encode("utf-8")).hexdigest()[:12]
    log.info("DeepSeek 使用 Skill 固定评分规则: %s (sha256:%s)", scoring_rules_path, scoring_rules_hash)
    scored = score_jobs(jobs_for_scoring, llm, resume, scoring_policy, cfg["visa"])
    from application_policy import enrich_scored_jobs
    scored = enrich_scored_jobs(
        scored, variants, ROOT, cfg,
        first_seen_at=sync_timestamp,
        first_seen_at_by_job=first_seen_at_by_job,
    )

    for s in scored:
        dashboard.upsert_recommendation(
            s.job, job_fit_score=s.score, job_fit_reason=s.reason,
            job_summary=s.summary,
            sponsorship_signal=s.sponsorship_signal, resume_id=s.resume_id,
            resume_fit_score=s.resume_fit_score, resume_reason=s.resume_reason,
            application_mode=s.application_mode, freshness_bucket=s.freshness_bucket,
            salary_raw=s.job.salary_raw, matched=s.matched, missing=s.missing,
            eligibility_reason=s.eligibility_reason,
            resume_path=s.resume_path,
            run_id=run_id, artifact_path=s.artifact_path,
        )
    save_ranked_from_dashboard(dashboard.load_rows(), out_dir / RANKED)
    save_ranked_markdown_from_dashboard(dashboard.load_rows(), out_dir / RANKED_MD)

    valid = [s for s in scored if s.score >= 0]
    if valid:
        top = valid[0]
        log.info("最高分 %d:%s — %s", top.score, top.job.company, top.job.title)
        buckets = {"85+": 0, "75-84": 0, "60-74": 0, "<60": 0}
        for s in valid:
            key = "85+" if s.score >= 85 else "75-84" if s.score >= 75 else \
                  "60-74" if s.score >= 60 else "<60"
            buckets[key] += 1
        log.info("分数分布:%s", "  ".join(f"{k}={v}" for k, v in buckets.items()))

    should_generate = args.generate or args.autopilot
    if not should_generate:
        print()
        print("=" * 60)
        print(f"完成。单文档汇总(推荐先看这个): {out_dir / RANKED_MD}")
        print(f"CSV(适合筛选排序): {out_dir / RANKED}")
        print("Dashboard 已同步到 data/application-dashboard.csv，里面保留申请状态和每日行动队列。")
        print("想生成材料,对高分岗位单独跑:python run.py --from-cache --generate")
        print("=" * 60)
        return

    # ---- 5. 生成材料(--autopilot 自动执行；普通流程仍需显式 --generate)
    from generate import generate
    written = generate(scored, llm, resume, prefs, cfg, root=ROOT)

    for s in scored:
        if s.artifact_path:
            dashboard.upsert_recommendation(
                s.job, job_fit_score=s.score, job_fit_reason=s.reason,
                job_summary=s.summary,
                sponsorship_signal=s.sponsorship_signal, resume_id=s.resume_id,
                resume_fit_score=s.resume_fit_score, resume_reason=s.resume_reason,
                application_mode=s.application_mode, freshness_bucket=s.freshness_bucket,
                salary_raw=s.job.salary_raw, matched=s.matched, missing=s.missing,
                eligibility_reason=s.eligibility_reason,
                resume_path=s.resume_path,
                run_id=run_id, artifact_path=s.artifact_path,
            )
    # 生成阶段会补上 artifact_path，重新导出让 CSV/Markdown 与 Dashboard 保持一致。
    save_ranked_from_dashboard(dashboard.load_rows(), out_dir / RANKED)
    save_ranked_markdown_from_dashboard(dashboard.load_rows(), out_dir / RANKED_MD)

    if args.autopilot:
        # JobHunter prepares deterministic execution records. The current Codex Agent consumes
        # this manifest immediately under applypilot-au; no Dashboard click or copied prompt exists.
        from application_attempts import (
            ApplicationAttempts,
            configured_platform_limits,
            platform_limit_key,
            submitted_today_by_platform,
        )

        apply_cfg = cfg.get("applypilot", {})
        timezone_name = str(apply_cfg.get("timezone", "Australia/Melbourne"))
        soft_target = int(apply_cfg.get("soft_target_per_day", 30))
        audit_batch = int(apply_cfg.get("audit_batch_size", 5))
        auto_submit_enabled = bool(apply_cfg.get("auto_submit_enabled", False))
        platform_limits = configured_platform_limits(apply_cfg)
        platform_counts = submitted_today_by_platform(
            dashboard.load_rows(), timezone_name, apply_cfg,
        )

        configured_skill = apply_cfg.get("skill_path", "")
        attempts = ApplicationAttempts(
            project_path(cfg["paths"].get("application_attempts", "data/application-attempts.csv")),
            dashboard,
            browser_enabled=bool(cfg.get("application", {}).get("browser_enabled", True)),
            skill_path=project_path(configured_skill) if configured_skill else None,
            project_root=ROOT,
            platform_config=apply_cfg,
        )
        existing_runnable = [
            row for row in attempts.load_rows()
            if row.get("status") in {
                "selected", "queued", "browser_opened", "filling", "ready_to_submit",
            }
        ]
        existing_platform_counts: dict[str, int] = {}
        for row in existing_runnable:
            key = platform_limit_key(row.get("platform", ""), row.get("url", ""), apply_cfg)
            existing_platform_counts[key] = existing_platform_counts.get(key, 0) + 1
        platform_remaining = {
            key: max(
                0,
                limit - platform_counts.get(key, 0) - existing_platform_counts.get(key, 0),
            )
            for key, limit in platform_limits.items()
        }
        confirmed_today = sum(platform_counts.values())
        hard_remaining = sum(platform_remaining.values())
        soft_remaining = max(
            0,
            min(soft_target, sum(platform_limits.values()))
            - confirmed_today - len(existing_runnable),
        )
        max_new = hard_remaining if auto_submit_enabled else 0
        soft_max_new = max(0, min(max_new, soft_remaining))
        selection = attempts.select_eligible(
            candidate, candidate_path, max_new=max_new,
            soft_max_new=soft_max_new, actor="agent",
            platform_remaining=platform_remaining,
        )
        payloads = [
            attempts.handoff_payload(row["attempt_id"])
            for row in selection["runnable"]
        ]
        manifest = {
            "workflow": "applypilot-autonomous-run",
            "integration": "jobhunter",
            "executor": "applypilot-au",
            "run_id": run_id,
            "project_root": str(ROOT),
            "scoring_provider": "deepseek",
            "scoring_rules_path": str(scoring_rules_path),
            "scoring_rules_sha256": scoring_rules_hash,
            "dashboard_path": str(dashboard.path),
            "ranked_path": str(out_dir / RANKED),
            "jobs_scored": len(scored),
            "new_execution_records": len(selection["selected"]),
            "priority_24h_execution_records": len(selection["priority_selected"]),
            "manual_platform_jobs": len(selection["manual_only"]),
            "assisted_platform_jobs": sum(
                1 for item in payloads if item.get("platform_mode") == "assisted"
            ),
            "automation_policy": {
                "mode": str(apply_cfg.get("automation_mode", "light")),
                "auto_submit_enabled": auto_submit_enabled,
                "final_submit_requires_user": True,
                "priority_within_hours": int(
                    cfg.get("application", {}).get("priority_within_hours", 24)
                ),
                "priority_rule": (
                    "Eligible jobs posted within 24 hours are selected first and may use the "
                    "buffer between the soft target and hard cap."
                ),
            },
            "daily_limit": {
                "timezone": timezone_name,
                "confirmed_today": confirmed_today,
                "soft_target": soft_target,
                "platform_limits": platform_limits,
                "platform_counts": platform_counts,
                "platform_remaining": platform_remaining,
                "remaining_before_platform_limits": hard_remaining,
                "audit_batch_size": audit_batch,
            },
            "attempts": payloads,
            "agent_instruction": (
                "Continue these attempts now in score order with the installed applypilot-au skill. "
                "Process jobs posted within 24 hours first, then continue by score in audit batches. "
                "Write every outcome through run.py --attempt-update, "
                "stop before the final submit control so the user can click it, "
                "and do not ask the user to click Dashboard buttons or copy prompts."
            ),
        }
        manifest_path = out_dir / "agent-run.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print()
        print("=" * 60)
        print("Agent 自动流程已准备完成；当前 Agent 应立即继续处理执行清单。")
        print(f"DeepSeek 固定评分规则: {scoring_rules_path} (sha256:{scoring_rules_hash})")
        print(f"Dashboard: {dashboard.path}")
        print(f"Agent 执行清单: {manifest_path}")
        print(f"可继续尝试: {len(payloads)}；平台要求手动提交: {len(selection['manual_only'])}")
        print("=" * 60)
        return

    print()
    print("=" * 60)
    print(f"完成。生成了 {len(written)} 份申请材料")
    print(f"单文档汇总: {out_dir / RANKED_MD}")
    print(f"排名表(CSV): {out_dir / RANKED}")
    print(f"申请材料: {out_dir / 'applications'}")
    print()
    print("投递前务必逐份人工检查 —— 模型仍可能润色出你没做过的事。")
    print("=" * 60)


if __name__ == "__main__":
    main()
