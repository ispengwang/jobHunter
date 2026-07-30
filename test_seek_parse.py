"""SEEK v5 字段解析测试。

用 probe2.py 实测到的真实结构造样本(22 个字段,含 advertiser 的嵌套形态)。
locations / workTypes 的内部结构探针没打印出来,所以每种可能形态都测:
字符串数组、对象数组(label/name/description 三种 key)、以及缺失。

跑 `python test_seek_parse.py`。不联网。
"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from scrapers.seek_source import (
    _parse_item, _first_label, _join_labels, _extract_description,
    _jsonld_description, _html_to_text, _parse_graphql_content, clean_seek_title,
    location_slugs, build_search_params,
)
from scrapers.jobspy_source import _too_old

PASS = FAIL = 0


def check(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}\n         got={got!r}\n         want={want!r}")


# probe2 实测返回的真实字段集合
REAL_ITEM = {
    "id": "93460881",
    "title": "Lead Software Engineer - AI Platform Services",
    "companyName": "SEEK",
    "advertiser": {"id": "27372708", "description": "SEEK Limited"},
    "listingDate": "2026-07-21T05:34:55Z",
    "listingDateDisplay": "1d ago",
    "teaser": "Lead Software Engineer role focusing on AI/ML systems at SEEK.",
    "bulletPoints": ["Hybrid working", "AI platform team", "Melbourne based"],
    "locations": [{"label": "Melbourne VIC"}],
    "workTypes": ["Full time"],
    "salaryLabel": "$150,000 - $180,000 per annum",
    "classifications": [], "employer": {}, "branding": {}, "displayStyle": {},
    "displayType": "premium", "isFeatured": False, "roleId": "software-engineer",
    "solMetadata": {}, "tracking": {}, "workArrangements": {},
    "companyProfileStructuredDataId": "123",
}


print("\n=== _first_label 多形态兼容 ===")
check("字符串", _first_label("Melbourne VIC"), "Melbourne VIC")
check("对象 label", _first_label({"label": "Melbourne VIC"}), "Melbourne VIC")
check("对象 name", _first_label({"name": "Melbourne VIC"}), "Melbourne VIC")
check("对象 description", _first_label({"description": "SEEK Limited"}), "SEEK Limited")
check("字符串数组", _first_label(["Melbourne VIC", "Sydney NSW"]), "Melbourne VIC")
check("对象数组", _first_label([{"label": "Melbourne VIC"}]), "Melbourne VIC")
check("空", _first_label(None), None)
check("空数组", _first_label([]), None)
check("空对象", _first_label({}), None)
check("无关 key 的对象", _first_label({"id": "123"}), None)

print("\n=== _join_labels 多值合并 ===")
check("两个地点", _join_labels([{"label": "Melbourne VIC"}, {"label": "Sydney NSW"}]),
      "Melbourne VIC, Sydney NSW")
check("超过 limit 截断", _join_labels(["A", "B", "C"]), "A, B")
check("去重", _join_labels(["A", "A"]), "A")
check("自定义分隔符", _join_labels(["Full time", "Contract"], sep="/"), "Full time/Contract")

print("\n=== _parse_item 解析实测结构 ===")
job = _parse_item(REAL_ITEM, with_description=False)
check("非空", job is not None, True)
check("id", job.url, "https://www.seek.com.au/job/93460881")
check("title", job.title, "Lead Software Engineer - AI Platform Services")
check("company", job.company, "SEEK")
check("location(从 locations 数组)", job.location, "Melbourne VIC")
check("work_type(从 workTypes 数组)", job.work_type, "Full time")
check("salary_raw(从 salaryLabel)", job.salary_raw, "$150,000 - $180,000 per annum")
check("salary_min 解析", job.salary_min, 150000.0)
check("salary_max 解析", job.salary_max, 180000.0)
check("posted_date", job.posted_date, "2026-07-21T05:34:55Z")
check("source", job.source, "seek")
check("teaser + bullets 作为摘要", job.description.startswith("Lead Software Engineer role"), True)
check("bullets 进了摘要", "- Hybrid working" in job.description, True)
check("SEEK New 徽标不会粘进职位名", clean_seek_title("Software EngineerNew"), "Software Engineer")
check("真实以 New 结尾的单词不被删除", clean_seek_title("Build Something New"), "Build Something New")

print("\n=== _parse_item 降级路径 ===")
# companyName 缺失 → 退回 advertiser.description
item = dict(REAL_ITEM); item.pop("companyName")
check("company 退回 advertiser", _parse_item(item, False).company, "SEEK Limited")

# locations 是纯字符串数组
item = dict(REAL_ITEM); item["locations"] = ["Melbourne VIC"]
check("locations 字符串数组", _parse_item(item, False).location, "Melbourne VIC")

# locations 用 name 而非 label
item = dict(REAL_ITEM); item["locations"] = [{"name": "Sydney NSW"}]
check("locations 用 name key", _parse_item(item, False).location, "Sydney NSW")

# 全部可选字段缺失,不应崩
item = {"id": "1", "title": "Dev"}
j = _parse_item(item, False)
check("最小字段不崩", j is not None, True)
check("缺失时 company 兜底", j.company, "Unknown")
check("缺失时 location 为 None", j.location, None)
check("缺失时 salary 为 None", j.salary_min, None)
check("缺失时 description 为空", j.description, "")

# 缺 id 或 title 应返回 None
check("缺 id 返回 None", _parse_item({"title": "Dev"}, False), None)
check("缺 title 返回 None", _parse_item({"id": "1"}, False), None)

# salaryLabel 不是字符串时
item = dict(REAL_ITEM); item["salaryLabel"] = {"label": "$90,000 per annum"}
check("salaryLabel 是对象", _parse_item(item, False).salary_min, 90000.0)

print("\n=== _extract_description 多路径 ===")
check("顶层 content", _extract_description({"content": "x" * 100}), "x" * 100)
check("data.content", _extract_description({"data": {"content": "y" * 100}}), "y" * 100)
check("深层嵌套", _extract_description(
    {"data": {"jobDetails": {"job": {"content": "z" * 100}}}}), "z" * 100)
check("太短不算", _extract_description({"content": "short"}), "")
check("找不到", _extract_description({"foo": "bar"}), "")
check("非 dict", _extract_description(None), "")

print("\n=== 完整 JD:岗位页 JSON-LD 提取(2026-07 改用的取详情主策略) ===")
# 招聘站的 JobPosting 结构化数据,description 可能是实体编码的 HTML,也可能是真标签,
# 两种都要能正确变成纯文本
_jsonld_entity = ('<script type="application/ld+json">{"@type":"WebSite"}</script>'
    '<script type="application/ld+json">{"@type":"JobPosting","title":"AI Engineer",'
    '"description":"&lt;p&gt;Hiring an &lt;strong&gt;AI Engineer&lt;/strong&gt;.&lt;/p&gt;'
    '&lt;ul&gt;&lt;li&gt;3+ years Python&lt;/li&gt;&lt;li&gt;LangChain &amp;amp; RAG&lt;/li&gt;&lt;/ul&gt;"}</script>')
_desc_e = _jsonld_description(_jsonld_entity)
check("JSON-LD(实体编码)含正文", "3+ years Python" in _desc_e and "LangChain & RAG" in _desc_e, True)
check("JSON-LD(实体编码)去掉了标签", "<" not in _desc_e and ">" not in _desc_e, True)

_jsonld_raw = ('<script type="application/ld+json">{"@type":"JobPosting","title":"Dev",'
    '"description":"<p>We are looking for a developer to build web applications with '
    '<b>React</b> &amp; TypeScript.</p><ul><li>2+ years of commercial experience</li>'
    '<li>Familiar with Node.js and REST APIs</li></ul>"}</script>')
_desc_r = _jsonld_description(_jsonld_raw)
check("JSON-LD(真标签)含正文", "web applications" in _desc_r and "React" in _desc_r and "2+ years" in _desc_r, True)
check("JSON-LD(真标签)实体正确还原", "& TypeScript" in _desc_r, True)
check("JSON-LD(真标签)去掉了标签", "<" not in _desc_r and ">" not in _desc_r, True)

check("页面无 JobPosting 时返回空", _jsonld_description('<script type="application/ld+json">{"@type":"WebSite"}</script>'), "")
check("页面无 JSON-LD 时返回空", _jsonld_description("<html>nothing here</html>"), "")
check("HTML 转文本压掉多余空白", _html_to_text("<p>a</p>\n\n\n\n<p>b</p>"), "a\n\nb")

# GraphQL 接口(probe3.py 实测 5/5 命中的取详情主策略):
# 响应形状 = {"data":{"jobDetails":{"job":{"content":"<html JD>"}}}}
_gql_ok = {"data": {"jobDetails": {"job": {"content":
    "<p>LK Group is putting AI to work across our brands — building the agents, "
    "pipelines, and automations that take the busywork out of the day.</p>"
    "<ul><li>3+ years building with Python</li><li>LLM &amp; RAG experience</li></ul>"}}}}
_gql_desc = _parse_graphql_content(_gql_ok)
check("GraphQL 提取到完整 JD 正文", "putting AI to work" in _gql_desc and "3+ years building with Python" in _gql_desc, True)
check("GraphQL 正文实体还原(&amp;→&)", "LLM & RAG" in _gql_desc, True)
check("GraphQL 正文去掉了 HTML 标签", "<" not in _gql_desc and ">" not in _gql_desc, True)
# 各种缺字段/异常形状都要稳,返回空串而不是崩
check("GraphQL 缺 content 字段", _parse_graphql_content({"data": {"jobDetails": {"job": {}}}}), "")
check("GraphQL data 为 null", _parse_graphql_content({"data": None}), "")
check("GraphQL 报错响应", _parse_graphql_content({"errors": [{"message": "nope"}]}), "")
check("GraphQL 空字典", _parse_graphql_content({}), "")

print("\n=== SEEK 多地区配置 ===")
check(
    "location_slugs 优先于旧单值",
    location_slugs({"location_slug": "legacy", "location_slugs": ["A", "B"]}),
    ["A", "B"],
)
check(
    "旧 location_slug 向后兼容",
    location_slugs({"location_slug": "legacy"}),
    ["legacy"],
)
check(
    "空 location_slugs 回退旧配置",
    location_slugs({"location_slug": "legacy", "location_slugs": []}),
    ["legacy"],
)
params = build_search_params(
    "AI Engineer", 2, "West-Gippsland-Latrobe-Valley-VIC",
    classification="6281", daterange=14,
)
check("多地区 where 使用当前 slug", params["where"], "West-Gippsland-Latrobe-Valley-VIC")
check("多地区保留分页参数", params["page"], 2)
check("多地区保留分类和时间窗口", (params["classification"], params["daterange"]), ("6281", 14))


print("\n=== Indeed 日期过滤(替代坏掉的 hours_old) ===")
cutoff = date(2026, 7, 15)
check("旧岗位应丢弃", _too_old(date(2026, 7, 10), cutoff), True)
check("新岗位应保留", _too_old(date(2026, 7, 20), cutoff), False)
check("边界当天保留", _too_old(date(2026, 7, 15), cutoff), False)
check("字符串日期", _too_old("2026-07-10", cutoff), True)
check("ISO 带时间", _too_old("2026-07-10T05:34:55Z", cutoff), True)
check("无 cutoff 时全保留", _too_old(date(2020, 1, 1), None), False)
check("日期为 None 时保留", _too_old(None, cutoff), False)
check("日期解析失败时保留", _too_old("not a date", cutoff), False)

print("\n" + "=" * 52)
print(f"通过 {PASS} · 失败 {FAIL}")
print("=" * 52)
sys.exit(1 if FAIL else 0)
