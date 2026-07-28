#!/usr/bin/env python3
"""探针三 —— 定位 SEEK 完整 JD 的最佳来源。

背景:搜索接口(jobsearch/v5)只给 teaser + bulletPoints,完整 JD 要单独取。
seek_source.py 里试的那几个 REST 详情端点没实测过,实际抓下来约 78% 是薄的
(只有摘要)。这个脚本把"拿完整 JD"的几种办法都在真实岗位上试一遍,看哪个稳。

试的四种办法:
  A. 岗位页 HTML 里的 JSON-LD(<script type="application/ld+json"> 里的 JobPosting.description)
     —— 最稳,几乎所有招聘站都埋这个,不依赖猜内部接口
  B. 岗位页 HTML 里内嵌的前端状态(SEEK_APOLLO_DATA / SEEK_REDUX_DATA / __NEXT_DATA__)
  C. REST 详情端点(seek_source.py 现在在用的那几个候选 + 更多)
  D. GraphQL(SEEK 岗位详情页实际走的接口,能探到就最理想)

用法:
    source venv/bin/activate
    python probe3.py
不调 AI、不花钱。把完整输出贴回来,我据此改 seek_source.py 的取详情逻辑。
"""
from __future__ import annotations

import html as html_mod
import json
import re
import sys
import traceback

import requests

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
H_JSON = {"User-Agent": UA, "Accept": "application/json, text/plain, */*",
          "Accept-Language": "en-AU,en;q=0.9", "Referer": "https://www.seek.com.au/"}
H_HTML = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml",
          "Accept-Language": "en-AU,en;q=0.9"}

SEARCH_URL = "https://www.seek.com.au/api/jobsearch/v5/search"
TAG_RE = re.compile(r"<[^>]+>")


def strip_html(s: str) -> str:
    return html_mod.unescape(TAG_RE.sub(" ", s or "")).strip()


def get_json(url, params=None, timeout=20):
    try:
        r = requests.get(url, params=params, headers=H_JSON, timeout=timeout)
        if r.status_code == 200 and "json" in (r.headers.get("content-type") or ""):
            return r.json(), r.status_code
        return None, r.status_code
    except Exception as e:
        return None, f"ERR {type(e).__name__}"


# ---------------------------------------------------------------- 拿几个真实岗位 id

print("=" * 72)
print("第一步:搜几个真实岗位,拿 id + teaser 长度做基线")
print("=" * 72)
ids = []
data, code = get_json(SEARCH_URL, {
    "siteKey": "AU-Main", "sourcesystem": "houston", "where": "All-Melbourne-VIC",
    "page": 1, "keywords": "AI engineer", "pageSize": 6, "locale": "en-AU",
})
if not data:
    print(f"搜索接口挂了(HTTP/{code})—— 先跑 probe2.py A 段重新定位搜索端点。")
    sys.exit(1)
for it in (data.get("data") or [])[:5]:
    jid = str(it.get("id") or "")
    teaser = (it.get("teaser") or "")
    bullets = it.get("bulletPoints") or []
    base = len(teaser) + sum(len(str(b)) for b in bullets)
    if jid:
        ids.append(jid)
        print(f"  id={jid:12} teaser+bullets≈{base:4}字  {(it.get('title') or '')[:45]}")
if not ids:
    print("搜到 0 条,换个搜索词或地点再试。")
    sys.exit(1)


# ---------------------------------------------------------------- 四种取详情办法

def method_a_jsonld(jid):
    """JSON-LD JobPosting.description —— 最稳的来源。"""
    try:
        r = requests.get(f"https://www.seek.com.au/job/{jid}", headers=H_HTML, timeout=25)
        if r.status_code != 200:
            return None, f"页面 HTTP {r.status_code}", None
        blocks = re.findall(r'<script type="application/ld\+json">(.*?)</script>', r.text, re.S)
        for b in blocks:
            try:
                d = json.loads(b)
            except Exception:
                continue
            cand = d if isinstance(d, list) else [d]
            for obj in cand:
                if isinstance(obj, dict) and obj.get("@type") == "JobPosting":
                    desc = strip_html(obj.get("description") or "")
                    if len(desc) > 50:
                        return len(desc), "JSON-LD JobPosting.description", desc
        return None, f"页面里没找到 JobPosting(JSON-LD 块 {len(blocks)} 个)", None
    except Exception as e:
        return None, f"ERR {type(e).__name__}: {str(e)[:60]}", None


def method_b_embedded(jid):
    """页面内嵌前端状态里的 JD。"""
    try:
        r = requests.get(f"https://www.seek.com.au/job/{jid}", headers=H_HTML, timeout=25)
        html = r.text
        for name, pat in [
            ("SEEK_APOLLO_DATA", r"window\.SEEK_APOLLO_DATA\s*=\s*(\{.*?\});?\s*</script>"),
            ("SEEK_REDUX_DATA", r"window\.SEEK_REDUX_DATA\s*=\s*(\{.*?\});?\s*</script>"),
            ("__NEXT_DATA__", r'<script id="__NEXT_DATA__"[^>]*>(\{.*?\})</script>'),
        ]:
            m = re.search(pat, html, re.S)
            if m:
                blob = m.group(1)
                # 找里面最长的一段像 JD 正文的字符串
                longest = max(re.findall(r'"content":"(.*?[^\\])"', blob) or [""], key=len, default="")
                dec = strip_html(longest.encode().decode("unicode_escape", "ignore")) if longest else ""
                return (len(dec) if len(dec) > 50 else None), f"内嵌 {name}", dec[:400]
        return None, "没有已知内嵌状态块", None
    except Exception as e:
        return None, f"ERR {type(e).__name__}", None


def method_c_rest(jid):
    """REST 详情端点候选(含 seek_source.py 现在用的那几个)。"""
    cands = [
        "https://www.seek.com.au/api/jobsearch/v5/job/{}",
        "https://www.seek.com.au/api/jobsearch/v5/jobs/{}",
        "https://www.seek.com.au/api/jobsearch/v2/jobs/{}",
        "https://www.seek.com.au/api/jobsearch/v3/jobs/{}",
        "https://www.seek.com.au/api/chalice-search/v4/job/{}",
        "https://www.seek.com.au/api/jobad/v1/jobs/{}",
    ]
    for tpl in cands:
        d, code = get_json(tpl.format(jid))
        if isinstance(d, dict):
            # 到处找一段像正文的长字符串
            for path in [("content",), ("description",), ("data", "content"),
                         ("data", "description"), ("job", "content"),
                         ("data", "jobDetails", "job", "content")]:
                node = d
                for k in path:
                    node = node.get(k) if isinstance(node, dict) else None
                    if node is None:
                        break
                if isinstance(node, str) and len(strip_html(node)) > 50:
                    return len(strip_html(node)), f"REST {tpl.split('/api/')[1].format('ID')}", strip_html(node)[:400]
    return None, "所有 REST 候选都没给到正文", None


def method_d_graphql(jid):
    """SEEK 岗位详情页实际走的 GraphQL。查询体是猜的,主要看端点通不通、报什么错。"""
    url = "https://www.seek.com.au/graphql"
    query = {
        "operationName": "jobDetails",
        "variables": {"jobId": jid, "locale": "en-AU"},
        "query": "query jobDetails($jobId: ID!) { jobDetails(id: $jobId) { job { content } } }",
    }
    try:
        r = requests.post(url, json=query, headers=H_JSON, timeout=20)
        body = r.text[:200]
        return None, f"GraphQL HTTP {r.status_code}: {body[:150]}", None
    except Exception as e:
        return None, f"ERR {type(e).__name__}", None


print()
print("=" * 72)
print("第二步:对每个岗位,四种办法各试一遍")
print("=" * 72)

winners = {}
for jid in ids:
    print(f"\n── 岗位 {jid} ──")
    for label, fn in [("A JSON-LD ", method_a_jsonld), ("B 内嵌状态", method_b_embedded),
                      ("C REST    ", method_c_rest), ("D GraphQL ", method_d_graphql)]:
        try:
            length, note, sample = fn(jid)
        except Exception as e:
            length, note, sample = None, f"崩了 {type(e).__name__}", None
        star = ""
        if length:
            winners[label.strip()] = winners.get(label.strip(), 0) + 1
            star = f"  ← 拿到 {length} 字"
        print(f"  {label}: {note}{star}")
        if length and sample and label.startswith("A"):
            print(f"      正文样例: {sample[:180]}...")

print()
print("=" * 72)
print("小结:哪种办法拿到完整 JD 的次数最多")
print("=" * 72)
if winners:
    for k, v in sorted(winners.items(), key=lambda x: -x[1]):
        print(f"  {k}: {v}/{len(ids)} 个岗位拿到了完整 JD")
    print("\n把上面全部输出贴回来,我据此把 seek_source.py 的取详情逻辑换成最稳的那种。")
else:
    print("  四种都没拿到完整 JD。把输出贴回来,尤其是各行的报错/HTTP 码,我再想别的办法。")
