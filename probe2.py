#!/usr/bin/env python3
"""探针二 —— 定位 SEEK 的真实接口 + 诊断 Indeed 零结果。

探针一的结论:
  - JobSpy 字段映射全对,不用改
  - LinkedIn 通,但 Indeed 一条没返回      → 本脚本 B 段诊断
  - SEEK chalice v4 返回 404,端点变了      → 本脚本 A 段暴力试探

用法:
    source venv/bin/activate
    python probe2.py

不调 AI,不花钱。把完整输出贴回来。
"""
import json
import re
import sys
import traceback

import requests

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
H = {"User-Agent": UA, "Accept": "application/json, text/plain, */*",
     "Referer": "https://www.seek.com.au/", "Accept-Language": "en-AU,en;q=0.9"}

BASE_PARAMS = {
    "siteKey": "AU-Main", "sourcesystem": "houston",
    "where": "All-Melbourne-VIC", "page": 1,
    "keywords": "software engineer", "pageSize": 5, "locale": "en-AU",
}

print("=" * 72)
print("A 段:暴力试探 SEEK 搜索端点")
print("=" * 72)

CANDIDATES = [
    "https://www.seek.com.au/api/chalice-search/v4/search",
    "https://www.seek.com.au/api/chalice-search/v5/search",
    "https://www.seek.com.au/api/jobsearch/v5/search",
    "https://www.seek.com.au/api/jobsearch/v4/search",
    "https://www.seek.com.au/api/searchjob/v1/search",
    "https://api.seek.com.au/chalice-search/v4/search",
    "https://www.seek.com.au/api/chalice-search/v4/search/",
]

winner = None
for url in CANDIDATES:
    try:
        r = requests.get(url, params=BASE_PARAMS, headers=H, timeout=15)
        ct = (r.headers.get("content-type") or "")[:40]
        is_json = "json" in ct
        n = "-"
        if is_json:
            try:
                d = r.json()
                n = len(d.get("data") or d.get("jobs") or d.get("results") or [])
            except Exception:
                n = "解析失败"
        flag = "  <<< 可用" if (r.status_code == 200 and is_json and n not in ("-", 0, "解析失败")) else ""
        print(f"  {r.status_code}  n={n:<6} {ct:<40} {url}{flag}")
        if flag and winner is None:
            winner = (url, r)
    except Exception as e:
        print(f"  ERR  {type(e).__name__:<12} {url}  {e}")

if winner:
    url, r = winner
    d = r.json()
    print()
    print(f"命中端点: {url}")
    print(f"顶层 key: {list(d.keys())}")
    items = d.get("data") or d.get("jobs") or d.get("results") or []
    if items:
        item = items[0]
        print(f"单条 {len(item)} 个字段:")
        print("  " + ", ".join(sorted(item.keys())))
        print()
        print("我代码依赖的字段:")
        for k in ["id", "title", "companyName", "advertiser", "location",
                  "area", "salary", "workType", "listingDate", "teaser"]:
            v = item.get(k)
            if isinstance(v, (dict, list)):
                v = json.dumps(v, ensure_ascii=False)[:70]
            print(f"  {k:14} {'有' if k in item else '>>>缺<<<':8} = {str(v)[:70]!r}")
else:
    print()
    print(">>> 所有候选端点都不可用 <<<")


print()
print("=" * 72)
print("B 段:从 SEEK 搜索页 HTML 里挖内嵌数据(最稳,不依赖猜端点)")
print("=" * 72)
try:
    page_url = "https://www.seek.com.au/software-engineer-jobs/in-All-Melbourne-VIC"
    r = requests.get(page_url, headers={"User-Agent": UA,
                                        "Accept": "text/html",
                                        "Accept-Language": "en-AU,en;q=0.9"}, timeout=20)
    print(f"页面 HTTP {r.status_code}, {len(r.text)} 字符")
    html = r.text

    # 常见的内嵌数据挂载点
    patterns = {
        "window.SEEK_REDUX_DATA": r"window\.SEEK_REDUX_DATA\s*=\s*(\{.*?\});?\s*</script>",
        "window.SEEK_APOLLO_DATA": r"window\.SEEK_APOLLO_DATA\s*=\s*(\{.*?\});?\s*</script>",
        "__NEXT_DATA__": r'<script id="__NEXT_DATA__"[^>]*>(\{.*?\})</script>',
        "__INITIAL_STATE__": r"__INITIAL_STATE__\s*=\s*(\{.*?\});?\s*</script>",
    }
    found_any = False
    for name, pat in patterns.items():
        m = re.search(pat, html, re.S)
        if m:
            found_any = True
            blob = m.group(1)
            print(f"\n  找到 {name}({len(blob)} 字符)")
            try:
                data = json.loads(blob)
                print(f"    顶层 key: {list(data.keys())[:15]}")
            except Exception as e:
                print(f"    JSON 解析失败: {e}")
        else:
            print(f"  没有 {name}")

    if not found_any:
        # 退一步:看看 JSON-LD 里有没有 JobPosting
        ld = re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)
        print(f"\n  JSON-LD 块数量: {len(ld)}")
        for i, block in enumerate(ld[:3]):
            try:
                data = json.loads(block)
                t = data.get("@type") if isinstance(data, dict) else "list"
                print(f"    [{i}] @type={t}")
            except Exception:
                print(f"    [{i}] 解析失败")

    # 页面里出现的 api 路径,这是最直接的线索
    api_paths = sorted(set(re.findall(r'"(/api/[a-zA-Z0-9\-_/]{4,60})"', html)))
    print(f"\n  HTML 里出现的 /api/ 路径({len(api_paths)} 个):")
    for p in api_paths[:25]:
        print(f"    {p}")
except Exception:
    print("FAIL:")
    traceback.print_exc()


print()
print("=" * 72)
print("C 段:诊断 Indeed 为什么零结果")
print("=" * 72)
try:
    from jobspy import scrape_jobs

    matrix = [
        {"label": "原参数", "location": "Melbourne VIC", "hours_old": 336,
         "search_term": "Junior Full Stack Developer"},
        {"label": "去掉 hours_old", "location": "Melbourne VIC", "hours_old": None,
         "search_term": "Junior Full Stack Developer"},
        {"label": "地点简化为 Melbourne", "location": "Melbourne", "hours_old": None,
         "search_term": "software engineer"},
        {"label": "搜索词最宽泛", "location": "Australia", "hours_old": None,
         "search_term": "developer"},
    ]

    for case in matrix:
        label = case.pop("label")
        try:
            df = scrape_jobs(site_name=["indeed"], country_indeed="australia",
                             results_wanted=5, description_format="markdown",
                             verbose=0, **case)
            n = 0 if df is None or df.empty else len(df)
            print(f"  {label:24} → {n} 条")
            if n:
                print(f"      示例: {df.iloc[0]['title']} @ {df.iloc[0]['company']}")
                break
        except Exception as e:
            print(f"  {label:24} → 报错 {type(e).__name__}: {str(e)[:100]}")
except Exception:
    print("FAIL:")
    traceback.print_exc()


print()
print("=" * 72)
print("跑完了,把全部输出贴回来。")
print("=" * 72)
