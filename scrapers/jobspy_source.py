"""LinkedIn + Indeed 抓取,基于 python-jobspy。

字段映射已于 2026-07-22 用 probe.py 实测核对:用到的 12 个字段名
在实际返回的 34 个字段里全部命中。

两个实测发现:
  1. Indeed 传 hours_old 会返回零结果(实测:传 336 → 0 条,不传 → 5 条)。
     所以 Indeed 不传这个参数,改成抓完之后按 date_posted 自己过滤。
  2. LinkedIn 的薪资字段实测 5/5 全空,别指望从它那儿拿到薪资。

已知限制:LinkedIn 大约第 10 页开始限流,量大时需要配代理。
"""
from __future__ import annotations

import logging
import math
from datetime import date, datetime, timedelta
from typing import Optional

from schema import Job, parse_salary

log = logging.getLogger(__name__)

# Indeed 传 hours_old 会零结果,单独拉出来做黑名单,将来 JobSpy 修了改这里
_HOURS_OLD_BROKEN = {"indeed"}


def _clean(v) -> Optional[str]:
    """pandas 的 NaN 会污染下游,统一转成 None。"""
    if v is None:
        return None
    if isinstance(v, float) and math.isnan(v):
        return None
    s = str(v).strip()
    return s or None


def _salary_from_row(row) -> tuple[Optional[float], Optional[float], Optional[str]]:
    """JobSpy 给的是结构化字段,优先用;拿不到再退回文本解析。"""
    lo = row.get("min_amount")
    hi = row.get("max_amount")
    interval = _clean(row.get("interval"))  # yearly | monthly | hourly ...

    def ok(x):
        return x is not None and not (isinstance(x, float) and math.isnan(x))

    if ok(lo) or ok(hi):
        mult = {"yearly": 1, "monthly": 12, "weekly": 52,
                "daily": 5 * 52, "hourly": 38 * 52}.get(interval or "yearly", 1)
        lo = float(lo) * mult if ok(lo) else None
        hi = float(hi) * mult if ok(hi) else None
        raw = f"{lo or ''}-{hi or ''} ({interval})"
        return lo, hi, raw

    # 结构化字段为空,退回从文本解析
    raw = _clean(row.get("salary_source")) or ""
    lo, hi = parse_salary(raw)
    return lo, hi, raw or None


def _too_old(posted, cutoff: Optional[date]) -> bool:
    """按 date_posted 自己过滤 —— 因为 Indeed 不能用 hours_old。

    日期解析不出来时保留(宁可多几条,不要漏掉好岗位)。
    """
    if cutoff is None or posted is None:
        return False
    try:
        if isinstance(posted, datetime):
            d = posted.date()
        elif isinstance(posted, date):
            d = posted
        else:
            d = datetime.fromisoformat(str(posted)[:10]).date()
    except (ValueError, TypeError):
        return False
    return d < cutoff


def _scrape_one_site(scrape_jobs, site: str, term: str, cfg: dict):
    """一次只抓一个站 —— 一个站挂了不会连累另一个,参数也能按站定制。"""
    search = cfg["search"]
    sources = cfg["sources"]

    kwargs = dict(
        site_name=[site],
        search_term=term,
        location=search["location"],
        country_indeed=search.get("country", "australia"),
        results_wanted=search.get("results_per_term", 50),
        is_remote=search.get("is_remote", False),
        linkedin_fetch_description=sources.get("linkedin_fetch_description", True),
        proxies=sources.get("proxies") or None,
        description_format="markdown",
        verbose=0,
    )
    # Indeed 传了就零结果,只给能正常处理它的站传
    if site not in _HOURS_OLD_BROKEN and search.get("hours_old"):
        kwargs["hours_old"] = search["hours_old"]

    return scrape_jobs(**kwargs)


def fetch(cfg: dict) -> list[Job]:
    try:
        from jobspy import scrape_jobs
    except ImportError:
        log.error("未安装 python-jobspy,跳过 LinkedIn/Indeed。pip install python-jobspy")
        return []

    search = cfg["search"]
    sources = cfg["sources"]

    sites = [s for s in ("linkedin", "indeed") if sources.get(s)]
    if not sites:
        return []

    hours = search.get("hours_old")
    cutoff = (datetime.now().date() - timedelta(days=hours / 24)) if hours else None

    jobs: list[Job] = []
    stats: dict[str, int] = {s: 0 for s in sites}
    dropped_old = 0

    for site in sites:
        for term in search["terms"]:
            try:
                df = _scrape_one_site(scrape_jobs, site, term, cfg)
            except Exception as e:
                # 单个站 + 单个搜索词失败不应该中断整个 pipeline
                log.warning("JobSpy 抓取失败 site=%s term=%r: %s", site, term, e)
                continue

            if df is None or df.empty:
                log.info("JobSpy site=%s term=%r 无结果", site, term)
                continue

            for _, row in df.iterrows():
                title = _clean(row.get("title"))
                url = _clean(row.get("job_url"))
                if not (title and url):
                    continue

                posted = row.get("date_posted")
                if _too_old(posted, cutoff):
                    dropped_old += 1
                    continue

                lo, hi, raw = _salary_from_row(row)
                jobs.append(Job(
                    source=_clean(row.get("site")) or site,
                    title=title,
                    company=_clean(row.get("company")) or "Unknown",
                    url=url,
                    location=_clean(row.get("location")),
                    salary_min=lo,
                    salary_max=hi,
                    salary_raw=raw,
                    work_type=_clean(row.get("job_type")),
                    posted_date=_clean(posted),
                    description=_clean(row.get("description")) or "",
                ))
                stats[site] += 1

            log.info("JobSpy site=%s term=%r → %d 条", site, term, len(df))

    if dropped_old:
        log.info("按 date_posted 过滤掉 %d 条过旧岗位(cutoff=%s)", dropped_old, cutoff)
    log.info("JobSpy 分站统计:%s", "  ".join(f"{k}={v}" for k, v in stats.items()))
    return jobs
