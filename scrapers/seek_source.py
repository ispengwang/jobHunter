"""SEEK 抓取 —— 基于 2026-07-22 实测的 jobsearch/v5 端点。

探针实测结论(probe2.py):
  - chalice-search/v4 已废弃,返回 404
  - 可用端点:https://www.seek.com.au/api/jobsearch/v5/search
  - 单条返回 22 个字段,与旧版差异很大:
        location  → locations   (数组)
        salary    → salaryLabel (字符串)
        workType  → workTypes   (数组)
        area      → 没了,并入 locations
  - 搜索结果只给 teaser + bulletPoints,完整 JD 要另外取详情

完整 JD 的取法(2026-07 改):旧代码只试几个没实测的 REST 详情端点,实际约 78%
岗位只拿到 teaser 摘要。改成优先用岗位页 HTML 里的 JSON-LD JobPosting.description
—— 这是招聘站通用的结构化数据,比猜内部 REST 端点稳得多;REST 端点作为兜底保留。
哪种真能拿到完整 JD,用 probe3.py 在有网的真机上验证(本仓库沙箱访问不到 SEEK)。

这是非公开接口,SEEK 再改版还会挂。搜索挂了跑 probe2.py,详情挂了跑 probe3.py。
"""
from __future__ import annotations

import html as _html
import json as _json
import logging
import re
import time
from typing import Optional

import requests

from schema import Job, parse_salary

log = logging.getLogger(__name__)

_SEARCH_URL = "https://www.seek.com.au/api/jobsearch/v5/search"
_JOB_URL = "https://www.seek.com.au/job/{job_id}"

# 取完整 JD 的策略,按稳健程度排。首个成功的会被缓存,后续岗位直接用它。
#   "graphql" —— SEEK 岗位详情页实际走的 GraphQL 接口,data.jobDetails.job.content
#                就是完整 JD 正文。probe3.py 在真机上实测 5/5 命中,每个岗位返回各自
#                真实的完整 JD。这是当前最可靠的来源。
#   "jsonld"  —— 岗位页 HTML 里的 JSON-LD JobPosting.description。probe3 实测当前
#                SEEK 页面没有这个块,留作兜底(万一改版加回来)。
#   其余是 REST 详情端点候选(实测都不给正文),最末兜底。
# 2026-07 现状:旧代码只试没实测的 REST 候选,实际约 78% SEEK 岗位只拿到 teaser
# 摘要。改成优先 GraphQL。SEEK 再改版挂了就重跑 probe3.py 重新定位。
_GRAPHQL_URL = "https://www.seek.com.au/graphql"
_GRAPHQL_QUERY = "query jobDetails($jobId: ID!) { jobDetails(id: $jobId) { job { content } } }"

_DETAIL_STRATEGIES = [
    "graphql",
    "jsonld",
    "https://www.seek.com.au/api/jobsearch/v5/job/{job_id}",
    "https://www.seek.com.au/api/jobsearch/v5/jobs/{job_id}",
]
_detail_strategy_cache: Optional[str] = None
_detail_disabled = False

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-AU,en;q=0.9",
    "Referer": "https://www.seek.com.au/",
}
_HTML_HEADERS = {
    "User-Agent": _HEADERS["User-Agent"],
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-AU,en;q=0.9",
}

_PAGE_SIZE = 22
_TAG_RE = re.compile(r"<[^>]+>")
_JSONLD_RE = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.S)


def _get(url: str, params: dict | None = None, timeout: int = 20) -> Optional[dict]:
    try:
        r = requests.get(url, params=params, headers=_HEADERS, timeout=timeout)
        if r.status_code != 200:
            log.debug("SEEK %s → HTTP %s", url, r.status_code)
            return None
        if "json" not in (r.headers.get("content-type") or ""):
            return None
        return r.json()
    except Exception as e:
        log.debug("SEEK 请求失败 %s: %s", url, e)
        return None


def _get_html(url: str, timeout: int = 25) -> Optional[str]:
    try:
        r = requests.get(url, headers=_HTML_HEADERS, timeout=timeout)
        if r.status_code == 200 and r.text:
            return r.text
        log.debug("SEEK 页面 %s → HTTP %s", url, r.status_code)
        return None
    except Exception as e:
        log.debug("SEEK 页面请求失败 %s: %s", url, e)
        return None


def _html_to_text(fragment: str) -> str:
    """JSON-LD 里的 description 是 HTML,转成可读纯文本。

    要兼容两种编码:有的站点 description 是真 HTML 标签(<p>...),有的是实体编码
    (&lt;p&gt;...)。所以先反转义一次(把 &lt; 变回 <),再去标签,再反转义一次
    (清掉双重编码残留的 &amp;amp; 之类),这样两种情况都能正确变成纯文本。
    """
    text = _html.unescape(fragment or "")   # &lt;p&gt; → <p>;真标签不受影响
    text = _TAG_RE.sub("\n", text)          # 去掉 <p> <li> 等标签
    text = _html.unescape(text)             # &amp;amp; → &amp; → & 之类残留
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _jsonld_description(page_html: str) -> str:
    """从岗位页 HTML 的 JSON-LD JobPosting 里取完整 JD。"""
    for block in _JSONLD_RE.findall(page_html or ""):
        try:
            data = _json.loads(block)
        except Exception:
            continue
        for obj in (data if isinstance(data, list) else [data]):
            if isinstance(obj, dict) and obj.get("@type") == "JobPosting":
                desc = _html_to_text(obj.get("description") or "")
                if len(desc) > 50:
                    return desc
    return ""


def _parse_graphql_content(data) -> str:
    """从 GraphQL 响应里取 data.jobDetails.job.content 并转成纯文本。"""
    node = data
    for key in ("data", "jobDetails", "job", "content"):
        node = node.get(key) if isinstance(node, dict) else None
        if node is None:
            return ""
    return _html_to_text(node) if isinstance(node, str) else ""


def _graphql_description(job_id: str) -> str:
    """走 SEEK 岗位详情页的 GraphQL 接口取完整 JD。probe3.py 实测可用。"""
    try:
        r = requests.post(
            _GRAPHQL_URL,
            json={
                "operationName": "jobDetails",
                "variables": {"jobId": str(job_id), "locale": "en-AU"},
                "query": _GRAPHQL_QUERY,
            },
            headers=_HEADERS,
            timeout=20,
        )
        if r.status_code != 200 or "json" not in (r.headers.get("content-type") or ""):
            log.debug("SEEK graphql → HTTP %s", r.status_code)
            return ""
        return _parse_graphql_content(r.json())
    except Exception as e:
        log.debug("SEEK graphql 请求失败: %s", e)
        return ""


# ---------------------------------------------------------------- 字段解析
# v5 的这几个字段结构没实测过样本,所以每个都做多形态兼容:
# 可能是 ["Melbourne VIC"],也可能是 [{"label": "Melbourne VIC"}]

def _first_label(value, keys=("label", "name", "description", "text")) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        for k in keys:
            v = value.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return None
    if isinstance(value, list):
        for item in value:
            got = _first_label(item, keys)
            if got:
                return got
    return None


def _join_labels(value, sep=", ", limit=2) -> Optional[str]:
    if not isinstance(value, list):
        return _first_label(value)
    out = []
    for item in value[:limit]:
        got = _first_label(item)
        if got and got not in out:
            out.append(got)
    return sep.join(out) or None


def _extract_description(detail: dict) -> str:
    """详情接口的 JD 正文位置未知,几个可能的路径都试。"""
    if not isinstance(detail, dict):
        return ""
    candidates = [
        ("content",), ("description",),
        ("data", "content"), ("data", "description"),
        ("job", "content"), ("job", "description"),
        ("data", "jobDetails", "job", "content"),
    ]
    for path in candidates:
        node = detail
        for key in path:
            node = node.get(key) if isinstance(node, dict) else None
            if node is None:
                break
        if isinstance(node, str) and len(node.strip()) > 50:
            return node.strip()
    return ""


def _run_strategy(strategy: str, job_id: str) -> str:
    """按某个策略取一次完整 JD,拿不到返回空串。"""
    if strategy == "graphql":
        return _graphql_description(job_id)
    if strategy == "jsonld":
        page = _get_html(_JOB_URL.format(job_id=job_id))
        return _jsonld_description(page) if page else ""
    # 否则是 REST 端点模板
    data = _get(strategy.format(job_id=job_id))
    return _extract_description(data) if data else ""


def _fetch_description(job_id: str) -> str:
    """取完整 JD。首次调用逐个试策略,第一个成功的被缓存,之后只用它。"""
    global _detail_strategy_cache, _detail_disabled

    if _detail_disabled:
        return ""

    if _detail_strategy_cache:
        return _run_strategy(_detail_strategy_cache, job_id)

    for strategy in _DETAIL_STRATEGIES:
        desc = _run_strategy(strategy, job_id)
        if desc:
            _detail_strategy_cache = strategy
            label = {"graphql": "GraphQL 接口", "jsonld": "JSON-LD(岗位页)"}.get(strategy, strategy)
            log.info("SEEK 完整 JD 来源已确定:%s", label)
            return desc

    # 全部策略都拿不到,后续不再浪费请求,退回 teaser + bulletPoints
    _detail_disabled = True
    log.warning("SEEK 完整 JD 全部策略失败,JD 将只有摘要 —— 打分质量会下降。"
                "跑 probe3.py 在真机上重新定位可用来源。")
    return ""


def location_slugs(seek_cfg: dict) -> list[str]:
    """Return configured SEEK regions, preferring the new list setting.

    An empty or invalid list falls back to the legacy single ``location_slug``
    so existing configurations keep their current behaviour.
    """
    configured = seek_cfg.get("location_slugs")
    if isinstance(configured, (list, tuple)):
        slugs = [str(value).strip() for value in configured if str(value).strip()]
        if slugs:
            return slugs
    legacy = str(seek_cfg.get("location_slug") or "All-Australia").strip()
    return [legacy or "All-Australia"]


def build_search_params(
    term: str,
    page: int,
    location_slug: str,
    *,
    classification: str | None = None,
    daterange: int | None = None,
) -> dict:
    """Build one SEEK request without changing the endpoint's throttle policy."""
    params = {
        "siteKey": "AU-Main",
        "sourcesystem": "houston",
        "where": location_slug,
        "page": page,
        "keywords": term,
        "pageSize": _PAGE_SIZE,
        "locale": "en-AU",
    }
    if classification:
        params["classification"] = classification
    if daterange:
        params["daterange"] = daterange
    return params


def clean_seek_title(value: str) -> str:
    """Remove SEEK's glued-on ``New`` badge without touching real title words."""
    title = (value or "").strip()
    return re.sub(r"(?<=[a-z])New$", "", title).strip()


def _parse_item(item: dict, with_description: bool) -> Optional[Job]:
    job_id = str(item.get("id") or "").strip()
    title = clean_seek_title(item.get("title") or "")
    if not (job_id and title):
        return None

    company = (
        (item.get("companyName") or "").strip()
        or _first_label(item.get("advertiser")) or ""
        or _first_label(item.get("employer")) or ""
    ).strip() or "Unknown"

    location = _join_labels(item.get("locations"))
    work_type = _join_labels(item.get("workTypes"), sep="/", limit=2)

    salary_raw = item.get("salaryLabel") or ""
    if not isinstance(salary_raw, str):
        salary_raw = _first_label(salary_raw) or ""
    lo, hi = parse_salary(salary_raw)

    # 摘要:teaser 一句话 + bulletPoints 几条,详情拿不到时至少有这些
    fallback_parts = []
    teaser = item.get("teaser")
    if isinstance(teaser, str) and teaser.strip():
        fallback_parts.append(teaser.strip())
    bullets = item.get("bulletPoints")
    if isinstance(bullets, list):
        for b in bullets:
            s = _first_label(b)
            if s:
                fallback_parts.append(f"- {s}")
    fallback = "\n".join(fallback_parts)

    description = ""
    if with_description:
        description = _fetch_description(job_id)
        if not _detail_disabled:
            time.sleep(0.4)  # 别把人家接口打挂
    if not description:
        description = fallback

    return Job(
        source="seek",
        title=title,
        company=company,
        url=_JOB_URL.format(job_id=job_id),
        location=location,
        salary_min=lo,
        salary_max=hi,
        salary_raw=salary_raw or None,
        work_type=work_type,
        posted_date=item.get("listingDate"),
        description=description,
    )


def fetch(cfg: dict) -> list[Job]:
    if not cfg["sources"].get("seek"):
        log.info("SEEK 在 config 里是关闭的,跳过")
        return []

    search = cfg["search"]
    seek_cfg = cfg.get("seek", {})
    wanted = search.get("results_per_term", 50)
    pages = max(1, -(-wanted // _PAGE_SIZE))

    hours = search.get("hours_old")
    daterange = max(1, round(hours / 24)) if hours else None
    slugs = location_slugs(seek_cfg)

    jobs: list[Job] = []
    seen_ids: set[str] = set()

    if len(slugs) > 1:
        log.info("SEEK 多地区搜索: %s（请求量约为单地区的 %d 倍）", ", ".join(slugs), len(slugs))

    for location_slug in slugs:
        for term in search["terms"]:
            for page in range(1, pages + 1):
                params = build_search_params(
                    term, page, location_slug,
                    classification=seek_cfg.get("classification"),
                    daterange=daterange,
                )

                data = _get(_SEARCH_URL, params)
                if not data:
                    log.warning(
                        "SEEK location=%r term=%r page=%d 请求失败,可能端点又变了",
                        location_slug, term, page,
                    )
                    break

                items = data.get("data") or []
                if not items:
                    break

                new = 0
                for item in items:
                    jid = str(item.get("id") or "")
                    if not jid or jid in seen_ids:
                        continue
                    seen_ids.add(jid)
                    job = _parse_item(item, with_description=True)
                    if job:
                        jobs.append(job)
                        new += 1

                log.info(
                    "SEEK location=%r term=%r page=%d → %d 条(新增 %d)",
                    location_slug, term, page, len(items), new,
                )
                time.sleep(0.6)

    if jobs:
        with_salary = sum(1 for j in jobs if j.salary_min is not None)
        log.info("SEEK 共 %d 条,其中 %d 条有薪资", len(jobs), with_salary)
    return jobs
