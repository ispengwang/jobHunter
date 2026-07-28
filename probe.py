#!/usr/bin/env python3
"""接口探针 —— 在你本地跑,确认两个数据源到底能不能通。

我的沙盒有网络限制,验证不了这两个源。你的机器可以。

用法:
    source venv/bin/activate
    python probe.py

它只抓 5 条,不调用 AI,不花钱。把完整输出贴回给我,我按真实结构修代码。
"""
import json
import sys
import traceback

print("=" * 70)
print("探针 1 / 3:JobSpy 是否安装、版本多少")
print("=" * 70)
try:
    import jobspy
    import importlib.metadata as md
    try:
        ver = md.version("python-jobspy")
    except Exception:
        ver = "未知"
    print(f"OK  python-jobspy 版本: {ver}")
except ImportError:
    print("FAIL  没装 python-jobspy,先跑 pip install -r requirements.txt")
    sys.exit(1)


print()
print("=" * 70)
print("探针 2 / 3:Indeed + LinkedIn 实抓(各 5 条)")
print("=" * 70)
try:
    from jobspy import scrape_jobs

    df = scrape_jobs(
        site_name=["indeed", "linkedin"],
        search_term="Junior Full Stack Developer",
        location="Melbourne VIC",
        country_indeed="australia",
        results_wanted=5,
        hours_old=336,
        description_format="markdown",
        linkedin_fetch_description=True,
        verbose=1,
    )

    if df is None or df.empty:
        print("WARN  跑通了但零结果 —— 可能被限流,或者搜索词太窄")
    else:
        print(f"OK  抓到 {df.shape[0]} 条,{df.shape[1]} 个字段")
        print()
        print("实际字段名:")
        print("  " + ", ".join(df.columns))
        print()
        print("按来源统计:")
        print(df["site"].value_counts().to_string())
        print()
        print("第一条记录的关键字段:")
        row = df.iloc[0]
        for k in ["site", "title", "company", "location", "job_url", "job_type",
                  "date_posted", "min_amount", "max_amount", "interval",
                  "salary_source", "currency", "is_remote"]:
            val = row.get(k)
            if k == "job_url" and val:
                val = str(val)[:80]
            print(f"  {k:18} = {val!r}")
        desc = row.get("description")
        print(f"  {'description':18} = {len(str(desc)) if desc else 0} 字符")
        print()
        print("薪资字段填充率(这决定去重时能不能靠薪资判断信息完整度):")
        for k in ["min_amount", "max_amount", "interval", "salary_source"]:
            if k in df.columns:
                print(f"  {k:18} 有值 {df[k].notna().sum()}/{len(df)}")
except Exception:
    print("FAIL  JobSpy 抓取报错:")
    traceback.print_exc()


print()
print("=" * 70)
print("探针 3 / 3:SEEK chalice 接口是否还活着")
print("=" * 70)
try:
    import requests

    url = "https://www.seek.com.au/api/chalice-search/v4/search"
    params = {
        "siteKey": "AU-Main", "sourcesystem": "houston",
        "where": "All-Melbourne-VIC", "page": 1,
        "keywords": "software engineer", "pageSize": 5,
        "include": "seodata", "locale": "en-AU",
        "classification": "6281",
    }
    headers = {
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
        "Accept": "application/json",
        "Referer": "https://www.seek.com.au/",
    }

    r = requests.get(url, params=params, headers=headers, timeout=20)
    print(f"HTTP 状态码: {r.status_code}")
    print(f"Content-Type: {r.headers.get('content-type')}")

    if r.status_code != 200:
        print("FAIL  接口返回非 200 —— 大概率 SEEK 改版了")
        print("响应前 500 字符:")
        print(r.text[:500])
    else:
        data = r.json()
        print(f"OK  顶层 key: {list(data.keys())}")
        items = data.get("data") or []
        print(f"data 数组长度: {len(items)}")
        if items:
            item = items[0]
            print()
            print(f"单条记录共 {len(item)} 个字段,字段名:")
            print("  " + ", ".join(sorted(item.keys())))
            print()
            print("我的代码依赖的字段(缺任何一个都要改代码):")
            for k in ["id", "title", "companyName", "advertiser", "location",
                      "area", "salary", "workType", "listingDate", "teaser"]:
                present = "有" if k in item else ">>> 缺失 <<<"
                val = item.get(k)
                if isinstance(val, (dict, list)):
                    val = json.dumps(val, ensure_ascii=False)[:60]
                elif val is not None:
                    val = str(val)[:60]
                print(f"  {k:14} {present:10} = {val!r}")

            # 详情接口
            jid = item.get("id")
            if jid:
                print()
                print(f"测试详情接口(job id={jid}):")
                d = requests.get(
                    f"https://www.seek.com.au/api/jobsearch/v2/jobs/{jid}",
                    headers=headers, timeout=20)
                print(f"  HTTP {d.status_code}")
                if d.status_code == 200:
                    dd = d.json()
                    print(f"  顶层 key: {list(dd.keys())}")
                    content = dd.get("content")
                    if content:
                        print(f"  content 字段: {len(str(content))} 字符  ← 这是 JD 正文")
                    else:
                        print("  >>> 没有 content 字段,JD 正文位置变了,要改 _fetch_description <<<")
                else:
                    print("  >>> 详情接口挂了,JD 会退回只用 teaser 摘要 <<<")
        else:
            print(">>> data 数组为空,可能是参数名变了 <<<")
except Exception:
    print("FAIL  SEEK 请求报错:")
    traceback.print_exc()


print()
print("=" * 70)
print("跑完了。把上面全部输出贴回给我。")
print("=" * 70)
