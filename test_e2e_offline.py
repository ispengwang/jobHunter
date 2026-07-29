"""端到端离线测试 —— 不联网、不调 LLM,验证抓取层之后的全部接线。

用桩替换两个东西:
  1. 抓取层 → 固定的 12 条样本岗位(照 probe.py 实测到的真实字段结构造的)
  2. LLM    → 假 client,按关键词规则打分并返回固定文本

验证的是真实代码路径:dedupe → visa_filter → score_jobs → generate → 写文件。
跑 `python test_e2e_offline.py`。
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import yaml

from schema import Job, parse_salary
from dedupe import dedupe
from score import visa_filter, score_jobs
from generate import generate

PASS = FAIL = 0


def check(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}\n         got={got!r}\n         want={want!r}")


def check_true(name, cond, hint=""):
    check(name + (f" ({hint})" if hint else ""), bool(cond), True)


# ---------------------------------------------------------------- 桩:样本岗位

def mk(source, title, company, url, salary=None, desc="", loc="Melbourne VIC",
       work_type="fulltime", posted="2026-07-19"):
    lo, hi = parse_salary(salary)
    return Job(source=source, title=title, company=company, url=url, location=loc,
               salary_min=lo, salary_max=hi, salary_raw=salary, work_type=work_type,
               posted_date=posted, description=desc)


REACT_JD = ("We are looking for a Junior Frontend Developer to join our team. "
            "You will work with React, TypeScript and Next.js building customer-facing "
            "features. 1-3 years experience. We offer mentorship and visa sponsorship "
            "for the right candidate. " * 6)

SENIOR_JD = ("Senior Backend Engineer required. 8+ years experience with Java, Spring "
             "Boot and Kubernetes. You will lead a team of engineers and own our "
             "microservices architecture. " * 6)

GOV_JD = ("Software Developer for a federal government project. Australian citizenship "
          "required and applicants must hold or be able to obtain an NV1 clearance. " * 6)

VAGUE_JD = "Great opportunity for a developer. Apply now! Competitive salary."

FULLSTACK_JD = ("Full Stack Developer wanted. Node.js, TypeScript, React, PostgreSQL. "
                "You will build end-to-end features across our web platform. "
                "Suitable for junior to mid-level candidates. " * 6)


def fake_scrape() -> list[Job]:
    """模拟三个源的返回,含跨平台重复、签证排除、seniority 不匹配等情况。"""
    return [
        # 同一岗位挂在三个平台,写法不同 —— 应合并成 1 条
        mk("seek", "Junior Frontend Developer", "Canva Pty Ltd",
           "https://seek.com.au/job/101", "$85,000 - $95,000 per annum", REACT_JD),
        mk("indeed", "Junior Frontend Developer", "Canva",
           "https://indeed.com/job/101", None, REACT_JD[:400]),
        mk("linkedin", "Jnr Frontend Dev", "Canva Pty. Ltd.",
           "https://linkedin.com/jobs/101", None, REACT_JD),

        # 完全重复的 URL —— 应合并
        mk("seek", "Junior Frontend Developer", "Canva Pty Ltd",
           "https://seek.com.au/job/101", "$85,000 - $95,000 per annum", REACT_JD),

        # 好岗位,应高分
        mk("seek", "Full Stack Developer", "Atlassian",
           "https://seek.com.au/job/102", "$90,000 - $110,000 per annum", FULLSTACK_JD),

        # seniority 严重不匹配,应低分
        mk("seek", "Senior Backend Engineer", "REA Group",
           "https://seek.com.au/job/103", "$180,000 per annum", SENIOR_JD),

        # 签证硬排除,应在打分前被淘汰
        mk("seek", "Software Developer", "Defence Systems",
           "https://seek.com.au/job/104", "$120,000 per annum", GOV_JD),
        mk("indeed", "Software Engineer", "Federal Agency",
           "https://indeed.com/job/105", None,
           "Permanent residents only. " + GOV_JD),

        # JD 极其笼统,应被压低分
        mk("seek", "Developer", "Recruitment Partners",
           "https://seek.com.au/job/106", None, VAGUE_JD),

        # 时薪岗位,验证薪资换算走通
        mk("seek", "Contract React Developer", "Some Agency",
           "https://seek.com.au/job/107", "$70 - $80 per hour", REACT_JD,
           work_type="contract"),

        # 不同公司同名岗位,不应被合并
        mk("seek", "Full Stack Developer", "Zip Co",
           "https://seek.com.au/job/108", "$95,000 per annum", FULLSTACK_JD),

        # 缺 description,验证不崩
        mk("linkedin", "Web Developer", "Tiny Startup",
           "https://linkedin.com/jobs/109", None, ""),
    ]


# ---------------------------------------------------------------- 桩:假 LLM

class FakeLLM:
    """按关键词规则打分,模拟真实 LLM 的输出格式(含 code fence 包裹)。"""

    def __init__(self):
        self.provider = "fake"
        self.model = "fake-1"
        self.batch_size = 4
        self.max_description_chars = 4000
        self.score_calls = 0
        self.text_calls = 0

    def complete_json(self, system, user, max_tokens=4096):
        self.score_calls += 1
        import re as _re
        out = []
        for m in _re.finditer(r'<job id="([^"]+)">(.*?)</job>', user, _re.S):
            jid, body = m.group(1), m.group(2).lower()
            if "8+ years" in body or "lead a team" in body:
                s, reason = 35, "要求 8 年经验,seniority 不匹配"
            elif "great opportunity" in body and len(body) < 600:
                s, reason = 45, "JD 笼统,疑似中介泛招"
            elif "react" in body and "typescript" in body:
                s, reason = 88, "React + TypeScript 高度吻合"
            elif "node.js" in body or "full stack" in body:
                s, reason = 82, "全栈技术栈匹配"
            else:
                s, reason = 55, "信息不足"
            out.append({"id": jid, "score": s, "reason": reason,
                        "summary": "(桩数据摘要) 一个前端/全栈岗位",
                        "matched": ["React", "TypeScript"], "missing": ["AWS"]})
        # 模拟模型爱裹 code fence 的毛病,顺便验证 extract_json
        import json as _json
        from llm import extract_json
        return extract_json("```json\n" + _json.dumps(out) + "\n```")

    def complete(self, system, user, max_tokens=4096):
        self.text_calls += 1
        if "求职信" in system or "cover letter" in system.lower():
            return "Dear Hiring Manager,\n\n(fake cover letter body)\n\nKind regards,\nPeng Wang"
        return "# Peng Wang\n\n(fake tailored resume)\n"


# ---------------------------------------------------------------- 跑

print("\n=== 1. 去重(桩数据 12 条) ===")
jobs = fake_scrape()
check("输入数量", len(jobs), 12)

deduped = dedupe(jobs, fuzzy_threshold=88)
check("去重后数量", len(deduped), 9)

canva = [j for j in deduped if "canva" in j.company.lower()]
check("Canva 合并为一条", len(canva), 1)
if canva:
    check("保留了有薪资的记录", canva[0].salary_min, 85000.0)
    check("收集到其他平台 URL", len(canva[0].duplicate_urls), 2)

fs = [j for j in deduped if "full stack" in j.title.lower()]
check("不同公司的同名岗位未被误合并", len(fs), 2)

hourly = [j for j in deduped if j.work_type == "contract"]
# $70/h × 38h(澳洲标准全职周工时) × 52 周 = 138,320
check_true("时薪换算成年薪", hourly and hourly[0].salary_min == 138320.0,
           f"got={hourly[0].salary_min if hourly else None}")


print("\n=== 2. 签证硬过滤 ===")
cfg = yaml.safe_load(open(Path(__file__).parent / "config.yaml", encoding="utf-8"))
passed, rejected = visa_filter(deduped, cfg["visa"])
check("被淘汰数量", len(rejected), 2)
rejected_companies = {j.company for j, _ in rejected}
check_true("Defence Systems 被淘汰", "Defence Systems" in rejected_companies)
check_true("Federal Agency 被淘汰", "Federal Agency" in rejected_companies)
check("通过数量", len(passed), 7)


print("\n=== 3. LLM 打分(假 client) ===")
llm = FakeLLM()
resume = "Peng Wang, junior full stack developer. React, TypeScript, Node.js."
prefs = "Junior, Melbourne, 70-90k, 485 visa needs sponsorship."
scored = score_jobs(passed, llm, resume, prefs, cfg["visa"])

check("打分结果数量", len(scored), 7)
check("按分数降序", [s.score for s in scored] == sorted([s.score for s in scored], reverse=True), True)
check_true("分批调用", llm.score_calls == 2, f"batch_size=4, 7 条 → {llm.score_calls} 批")
check_true("没有 -1(解析失败)", all(s.score >= 0 for s in scored))

canva_s = next((s for s in scored if "canva" in s.job.company.lower()), None)
check_true("担保加分生效", canva_s and canva_s.score == 93,
           f"88 + 5 = {canva_s.score if canva_s else None}")
check("担保信号识别", canva_s.sponsorship_signal if canva_s else None, "explicit_yes")

senior_s = next((s for s in scored if "REA" in s.job.company), None)
check_true("seniority 不匹配被压低", senior_s and senior_s.score == 35)

check_true("summary 字段有内容", all(s.summary for s in scored),
           "所有结果都应该带摘要,不是空字符串")


print("\n=== 3b. 单文档 Markdown 汇总(含链接和分数) ===")
from run import save_ranked_markdown

md_tmp = Path(tempfile.mkdtemp()) / "jobs-ranked.md"
save_ranked_markdown(scored, md_tmp)
check_true("Markdown 文件写出来了", md_tmp.exists())
md_text = md_tmp.read_text(encoding="utf-8")
check_true("含分数标题", "分 —" in md_text)
check_true("含链接", "https://" in md_text)
check_true("含摘要字段标签", "摘要" in md_text)
check_true("按分数降序排列(最高分先出现)",
           md_text.index(f"{scored[0].score} 分") < md_text.index(f"{scored[-1].score} 分"))
md_tmp.unlink()
md_tmp.parent.rmdir()


print("\n=== 4. 生成材料 + 写文件 ===")
tmp = Path(tempfile.mkdtemp())
cfg["paths"]["output_dir"] = str(tmp)
written = generate(scored, llm, resume, prefs, cfg)

threshold = cfg["scoring"]["generate_threshold"]
expected = len([s for s in scored if s.score >= threshold])
check(f"生成数量(阈值 {threshold})", len(written), expected)

if written:
    d = written[0]
    check_true("00-summary.md 存在", (d / "00-summary.md").exists())
    check_true("resume.md 存在", (d / "resume.md").exists())
    check_true("cover-letter.md 存在", (d / "cover-letter.md").exists())

    summary = (d / "00-summary.md").read_text(encoding="utf-8")
    check_true("summary 含申请链接", "https://" in summary)
    check_true("summary 含 checklist", "投递前检查" in summary)
    check_true("summary 含签证信号", "签证信号" in summary)
    check_true("summary 含 ATS 关键词覆盖", "ATS 关键词覆盖" in summary)
    check_true("summary 含未覆盖关键词分类", "永远不得添加" in summary)

    names = sorted(p.name for p in tmp.glob("applications/*"))
    check_true("目录名以分数开头便于排序", names[0][:3].isdigit(), names[0])
    check_true("目录名无非法字符", all("/" not in n and ":" not in n for n in names))

check_true("每个岗位调用 2 次生成", llm.text_calls == len(written) * 2,
           f"{llm.text_calls} vs {len(written) * 2}")

shutil.rmtree(tmp, ignore_errors=True)

print("\n" + "=" * 52)
print(f"通过 {PASS} · 失败 {FAIL}")
print("=" * 52)
sys.exit(1 if FAIL else 0)
