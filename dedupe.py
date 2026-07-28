"""三级去重。

同一个岗位中介会在 SEEK / Indeed / LinkedIn 同时挂,不去重会导致对同一家公司
重复投递 —— 在澳洲这种小市场里挺伤的。

策略:
  1. URL 精确匹配
  2. norm(company) + norm(title) + norm(location) 精确匹配
  3. 同公司内 title 模糊匹配 (rapidfuzz)
保留信息最全的那条,但把所有来源 URL 都合并进 duplicate_urls ——
一个岗位在几个平台同时挂,这本身是个信号。
"""
from __future__ import annotations

import logging
from collections import defaultdict

from schema import Job, norm_company, norm_title, norm_location

log = logging.getLogger(__name__)

# 来源优先级:SEEK 在澳洲信息最全,LinkedIn 常缺薪资
_SOURCE_RANK = {"seek": 3, "indeed": 2, "linkedin": 1}


def _better(a: Job, b: Job) -> Job:
    """选出信息更全的一条。完整度相同则按来源优先级。"""
    ca, cb = a.completeness(), b.completeness()
    if ca != cb:
        return a if ca > cb else b
    ra = _SOURCE_RANK.get(a.source, 0)
    rb = _SOURCE_RANK.get(b.source, 0)
    return a if ra >= rb else b


def _merge(keep: Job, drop: Job) -> Job:
    """合并:保留主记录,补全缺失字段,收集所有 URL。"""
    urls = set(keep.duplicate_urls) | set(drop.duplicate_urls)
    urls.add(drop.url)
    urls.discard(keep.url)
    keep.duplicate_urls = sorted(urls)

    # 逐字段补全:主记录没有的,从被丢弃的那条借
    if keep.salary_min is None and drop.salary_min is not None:
        keep.salary_min = drop.salary_min
        keep.salary_max = drop.salary_max
        keep.salary_raw = drop.salary_raw
    if not keep.location and drop.location:
        keep.location = drop.location
    if not keep.work_type and drop.work_type:
        keep.work_type = drop.work_type
    if not keep.posted_date and drop.posted_date:
        keep.posted_date = drop.posted_date
    if len(drop.description) > len(keep.description):
        keep.description = drop.description
    return keep


def dedupe(jobs: list[Job], fuzzy_threshold: int = 88) -> list[Job]:
    if not jobs:
        return []

    start = len(jobs)

    # ---- 一级:URL 精确
    by_url: dict[str, Job] = {}
    for j in jobs:
        if j.url in by_url:
            by_url[j.url] = _merge(*_ordered(by_url[j.url], j))
        else:
            by_url[j.url] = j
    stage1 = list(by_url.values())

    # ---- 二级:company + title + location 精确
    by_key: dict[tuple, Job] = {}
    for j in stage1:
        key = (norm_company(j.company), norm_title(j.title), norm_location(j.location))
        if key in by_key:
            by_key[key] = _merge(*_ordered(by_key[key], j))
        else:
            by_key[key] = j
    stage2 = list(by_key.values())

    # ---- 三级:同公司内 title 模糊匹配
    try:
        from rapidfuzz import fuzz
    except ImportError:
        log.warning("未安装 rapidfuzz,跳过模糊去重。pip install rapidfuzz")
        _log_result(start, len(stage2))
        return stage2

    buckets: dict[str, list[Job]] = defaultdict(list)
    for j in stage2:
        buckets[norm_company(j.company)].append(j)

    result: list[Job] = []
    for company, group in buckets.items():
        if not company or len(group) == 1:
            result.extend(group)
            continue

        kept: list[Job] = []
        for j in group:
            jt = norm_title(j.title)
            matched = False
            for i, k in enumerate(kept):
                # token_sort_ratio 对词序不敏感,
                # "Senior Backend Engineer" vs "Backend Engineer, Senior" 能匹配上
                if fuzz.token_sort_ratio(jt, norm_title(k.title)) >= fuzzy_threshold:
                    kept[i] = _merge(*_ordered(k, j))
                    matched = True
                    break
            if not matched:
                kept.append(j)
        result.extend(kept)

    _log_result(start, len(result))
    return result


def _ordered(a: Job, b: Job) -> tuple[Job, Job]:
    """返回 (保留的, 丢弃的)。"""
    keep = _better(a, b)
    drop = b if keep is a else a
    return keep, drop


def _log_result(start: int, end: int) -> None:
    removed = start - end
    pct = (removed / start * 100) if start else 0
    log.info("去重:%d → %d 条(移除 %d,%.1f%%)", start, end, removed, pct)
