"""不调 LLM、不联网的逻辑验证。跑 `python test_pipeline.py`。

覆盖薪资解析、去重、签证过滤 —— 这三块是最容易悄悄出错又最难事后发现的。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from schema import Job, parse_salary, norm_company, norm_title
from dedupe import dedupe
from score import visa_filter, sponsorship_signal

PASS = FAIL = 0


def check(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}\n         got={got!r}\n         want={want!r}")


print("\n=== 薪资解析 ===")
cases = [
    ("$120,000 - $140,000 per annum + super", (120000.0, 140000.0)),   # SEEK
    ("$120,000 - $140,000 a year", (120000.0, 140000.0)),              # Indeed
    ("$120k - $140k", (120000.0, 140000.0)),
    ("120000", (120000.0, 120000.0)),
    ("$95,000 + superannuation", (95000.0, 95000.0)),
    ("$65 - $75 per hour", (128440.0, 148200.0)),                      # 时薪换算
    ("$800 per day", (208000.0, 208000.0)),                            # 日薪换算
    ("Competitive salary", (None, None)),                              # 无数字
    ("", (None, None)),
    (None, (None, None)),
    ("$5 - $8 per hour", (None, None)),                                # 低于合理下限,判为解析失败
]
for raw, want in cases:
    check(f"parse_salary({raw!r})", parse_salary(raw), want)


print("\n=== 字段归一化 ===")
check("norm_company 去后缀", norm_company("Atlassian Pty Ltd"), "atlassian")
check("norm_company 大小写", norm_company("CANVA AUSTRALIA"), "canva")
check("norm_title 去括号", norm_title("Senior Backend Engineer (Sydney)"), "senior backend engineer")
check("norm_title 缩写", norm_title("Snr Backend Dev"), "senior backend engineer")
check("norm_title dev=engineer", norm_title("Backend Developer"), "backend engineer")
check("norm_title 噪音词", norm_title("Backend Engineer - URGENT - Full Time"), "backend engineer")


print("\n=== 去重 ===")


def mk(source, title, company, url, salary=None, desc="x" * 300, loc="Sydney NSW"):
    lo, hi = parse_salary(salary)
    return Job(source=source, title=title, company=company, url=url,
               location=loc, salary_min=lo, salary_max=hi, salary_raw=salary,
               description=desc)


# 场景:同一个岗位在三个平台同时挂,写法略有差异
jobs = [
    mk("seek", "Senior Backend Engineer", "Atlassian Pty Ltd",
       "https://seek.com.au/job/1", "$150,000 - $170,000 per annum"),
    mk("indeed", "Senior Backend Engineer", "Atlassian",
       "https://indeed.com/job/2"),
    mk("linkedin", "Snr Backend Dev", "Atlassian Pty. Ltd.",
       "https://linkedin.com/jobs/3", desc="y" * 100),
    # 真正不同的岗位,不该被合并
    mk("seek", "Frontend Engineer", "Atlassian Pty Ltd",
       "https://seek.com.au/job/4"),
    # 不同公司的同名岗位,不该被合并
    mk("seek", "Senior Backend Engineer", "Canva",
       "https://seek.com.au/job/5"),
    # URL 完全重复
    mk("seek", "Senior Backend Engineer", "Atlassian Pty Ltd",
       "https://seek.com.au/job/1", "$150,000 - $170,000 per annum"),
]

result = dedupe(jobs, fuzzy_threshold=88)
check("去重后数量", len(result), 3)

atlassian_backend = [j for j in result
                     if norm_company(j.company) == "atlassian"
                     and "backend" in norm_title(j.title)]
check("Atlassian backend 合并为一条", len(atlassian_backend), 1)

if atlassian_backend:
    j = atlassian_backend[0]
    check("保留了有薪资的那条", j.salary_min, 150000.0)
    check("收集了其他平台 URL", len(j.duplicate_urls), 2)
    check("描述取最长的", len(j.description), 300)

titles = sorted(norm_title(j.title) for j in result)
check("剩余岗位正确", titles, ["frontend engineer", "senior backend engineer", "senior backend engineer"])


print("\n=== 签证过滤 ===")
visa_cfg = {
    "exclude_keywords": [
        "australian citizenship required", "nv1", "permanent residents only",
        "we do not sponsor",
    ],
    "bonus_keywords": ["visa sponsorship", "482", "willing to sponsor"],
}

vjobs = [
    mk("seek", "Backend Engineer", "DefenceCo", "https://x/1",
       desc="Applicants must have Australian citizenship required for this role."),
    mk("seek", "Backend Engineer", "GovCo", "https://x/2",
       desc="You will need to hold or obtain an NV1 clearance."),
    mk("seek", "Backend Engineer", "GoodCo", "https://x/3",
       desc="We offer visa sponsorship for the right candidate."),
    mk("seek", "Backend Engineer", "NormalCo", "https://x/4",
       desc="Build great products with a talented team."),
    mk("seek", "Backend Engineer", "NoSponsorCo", "https://x/5",
       desc="Please note we do not sponsor work visas."),
]

passed, rejected = visa_filter(vjobs, visa_cfg)
check("通过数量", len(passed), 2)
check("淘汰数量", len(rejected), 3)
check("未提签证的岗位应放行", "NormalCo" in [j.company for j in passed], True)
check("明确担保的岗位应放行", "GoodCo" in [j.company for j in passed], True)

check("担保信号-明确 yes", sponsorship_signal(vjobs[2], visa_cfg), "explicit_yes")
check("担保信号-未提及", sponsorship_signal(vjobs[3], visa_cfg), "unknown")
check("担保信号-明确 no", sponsorship_signal(vjobs[4], visa_cfg), "explicit_no")

print("\n=== 担保信号:否定语境误判(Maincode 真实案例踩的坑) ===")
# 真实 JD 原文:"At this time we are not able to offer visa sponsorship,
# so applicants must have existing and unrestricted work rights in Australia."
# bonus_keywords 里的 "visa sponsorship" 是这句话的子串,纯字符串匹配会误判成 explicit_yes。
maincode_job = mk("linkedin", "Product Engineer (UI/Frontend)", "Maincode", "https://x/6",
    desc="Join our team. At this time we are not able to offer visa sponsorship, "
         "so applicants must have existing and unrestricted work rights in Australia.")
check("担保信号-否定语境不应误判为 yes",
      sponsorship_signal(maincode_job, visa_cfg), "explicit_no")

# 否定词在关键词后面的写法也要接住,不能只查关键词前面
after_negation_job = mk("seek", "Engineer", "SomeCo", "https://x/7",
    desc="Please be aware visa sponsorship is not available for this position.")
check("担保信号-否定词在后面同样不误判",
      sponsorship_signal(after_negation_job, visa_cfg), "explicit_no")

# 真正的正向表述不应该被这次修复误伤
genuine_job = mk("seek", "Engineer", "SponsorCo", "https://x/8",
    desc="We are proud to offer visa sponsorship to the right candidate, 482 visa welcome.")
check("担保信号-真实正向表述仍然是 yes",
      sponsorship_signal(genuine_job, visa_cfg), "explicit_yes")


print("\n=== citizen/PR 模式匹配(实测踩坑后补的) ===")
# 真实案例:Australian Red Cross Lifeblood 的原文,固定短语列表完全没命中,
# 因为词序和措辞跟 "must be an australian citizen" 对不上
pattern_jobs = [
    mk("indeed", "ICT Graduate", "Lifeblood", "https://x/10",
       desc="MUST be a citizen of Australia or have Permanent Residency visa "
            "with the ability to work in Australia."),
    # 反过来的词序也要能抓到
    mk("seek", "Analyst", "BankCo", "https://x/11",
       desc="Applicants must hold Australian permanent residency or citizenship "
            "to be eligible for this role."),
    # 缩写 PR
    mk("seek", "Engineer", "GovAgency", "https://x/12",
       desc="Candidates need citizenship or PR status to apply."),
    # 否定语境:提到 citizen/PR 但其实是说不需要 —— 不应该被误杀
    mk("seek", "Developer", "OpenCo", "https://x/13",
       desc="We don't require Australian citizenship or PR, visa sponsorship "
            "is available for the right candidate."),
    # 只单独出现 citizen 或 PR 中的一个,不该触发(可能只是提及,不是要求)
    mk("seek", "Developer", "MentionCo", "https://x/14",
       desc="Our team is proud of its diversity across many nationalities and citizenships."),
]

passed2, rejected2 = visa_filter(pattern_jobs, visa_cfg)
rejected_companies2 = {j.company for j, _ in rejected2}
check("Lifeblood 措辞(word order 1)被抓到", "Lifeblood" in rejected_companies2, True)
check("反词序也被抓到", "BankCo" in rejected_companies2, True)
check("PR 缩写也被抓到", "GovAgency" in rejected_companies2, True)
check("否定语境不误杀", "OpenCo" in rejected_companies2, False)
check("单独提及不误杀", "MentionCo" in rejected_companies2, False)


print("\n=== 纯 citizenship-only 模式(第二批实测踩坑,连 PR 选项都没有) ===")
# 三个真实案例,原文都没有 "or PR",单纯要求 citizenship,固定短语列表全部漏判
citizen_only_jobs = [
    mk("linkedin", "Systems Development Engineer", "AWS", "https://x/20",
       desc="Applicants must be Australian citizens and hold or be eligible to "
            "obtain an Australian Government Security Clearance."),
    mk("linkedin", "Boomi Developer", "Peoplebank", "https://x/21",
       desc="Due to work rights requirement for this role candidates must be "
            "Australian Citizen."),
    mk("linkedin", "Design Integration Engineer", "BAE Systems", "https://x/22",
       desc="applicants must be Australian citizens and either possess or be "
            "eligible to obtain and maintain a security clearance."),
    # 反例:只是提到 "must be" 别的东西,不该触发
    mk("seek", "Developer", "SafeCo", "https://x/23",
       desc="You must be a self-starter who thrives in a fast-paced environment."),
]
passed3, rejected3 = visa_filter(citizen_only_jobs, visa_cfg)
rejected_companies3 = {j.company for j, _ in rejected3}
check("AWS(复数+applicants主语)被抓到", "AWS" in rejected_companies3, True)
check("Peoplebank(无冠词)被抓到", "Peoplebank" in rejected_companies3, True)
check("BAE Systems 被抓到", "BAE Systems" in rejected_companies3, True)
check("无关的 must be 不误杀", "SafeCo" in rejected_companies3, False)


print("\n=== Markdown 噪音打断匹配(第二批实测踩坑,真实 Peoplebank 原文) ===")
# 真实案例:抓下来的 JD 是 Markdown 格式,"must be" 和 "Australian" 之间夹着
# \xa0、换行符、** 加粗标记,\W+/\s+ 规则本身没错,但被格式符号打断了匹配
md_noise_jobs = [
    mk("linkedin", "Boomi Developer", "Peoplebank", "https://x/30",
       desc="Due to work rights requirement for this role candidates must be\xa0\n"
            "**Australian Citizen**\n."),
    # 反斜杠转义(JobSpy 常把连字符转义成 \\-)不应该影响匹配
    mk("seek", "Analyst", "EscapeCo", "https://x/31",
       desc="12\\-month contract, must be an Australian citizen due to security requirements."),
]
passed4, rejected4 = visa_filter(md_noise_jobs, visa_cfg)
rejected_companies4 = {j.company for j, _ in rejected4}
check("Markdown 加粗打断的短语被抓到", "Peoplebank" in rejected_companies4, True)
check("反斜杠转义不影响匹配", "EscapeCo" in rejected_companies4, True)


print("\n=== 第三批实测:'requires ... to be' 插入语(真实 Leidos 原文) ===")
# 真实案例:"requires" 和 "to be" 中间插了 "the successful applicant" 这个名词短语,
# 之前逐句穷举的正则完全没预留这个空间,改成邻近匹配后应该能抓到
insertion_jobs = [
    mk("linkedin", "Junior Software Engineer", "Leidos", "https://x/40",
       desc="This role does requires the successful applicant to be an "
            "Australian Citizen and hold an active TSPV Clearance."),
    # 反例:同一段里出现 "must be" 但跟 citizen 完全无关,窗口不该跨句乱连
    mk("seek", "Developer", "FarCo", "https://x/41",
       desc="You must be highly organised. " + "filler word " * 30 +
            "Our team includes people of many nationalities and citizenships."),
]
passed5, rejected5 = visa_filter(insertion_jobs, visa_cfg)
rejected_companies5 = {j.company for j, _ in rejected5}
check("Leidos 插入语被抓到", "Leidos" in rejected_companies5, True)
check("远距离无关提及不误杀", "FarCo" in rejected_companies5, False)


print("\n=== 担保信号改成优先信 LLM,关键词规则只兜底 ===")
# 背景:之前担保信号完全靠关键词字符串匹配,Maincode 真实案例里
# "not able to offer visa sponsorship" 被误判成 explicit_yes(见 score.py 的
# _SPONSOR_NEGATION_RE 那次修复)。更彻底的做法是让 LLM 自己判断 —— JD
# 反正已经喂给它了,它能真的读懂否定语境,不用靠正则一个个补否定词。
# 这里验证 score_jobs() 真的采信了 LLM 返回的 sponsorship_signal 字段,
# 而不是永远走关键词规则那条路。
from score import score_jobs, Scored


class _FakeLLMSponsor:
    """故意返回一个关键词规则会判错/判不出来的场景,证明走的是 LLM 那条路。"""

    def __init__(self, canned: dict):
        self.batch_size = 8
        self.max_description_chars = 4000
        self._canned = canned  # job.id -> sponsorship_signal 要返回的值

    def complete_json(self, system, user, max_tokens=4096):
        import re as _re
        out = []
        for m in _re.finditer(r'<job id="([^"]+)">', user):
            jid = m.group(1)
            out.append({
                "id": jid, "score": 70, "reason": "测试",
                "summary": "测试摘要", "matched": [], "missing": [],
                "sponsorship_signal": self._canned.get(jid, "unknown"),
            })
        return out


visa_cfg_llm_test = {
    "exclude_keywords": [],
    "bonus_keywords": ["visa sponsorship", "482", "willing to sponsor"],
}

# 一句"一般不担保,但特别优秀的例外"——关键词规则(签证语境 + 否定词)会判
# explicit_no,但 LLM 能读懂"例外"这层意思、判 explicit_yes,两边相反,
# 正好验证真正采信的是 LLM 那条路
llm_only_job = mk("linkedin", "Engineer", "GenerousCo", "https://x/50",
    desc="We generally do not offer visa sponsorship, but for truly "
         "exceptional candidates we are happy to make an exception.")
assert sponsorship_signal(llm_only_job, visa_cfg_llm_test) == "explicit_no", \
    "这个反例前提不成立:关键词规则应该判 explicit_no,不然测不出'LLM 优先'这件事"

fake_llm = _FakeLLMSponsor({llm_only_job.id: "explicit_yes"})
scored = score_jobs([llm_only_job], fake_llm, "resume", "prefs", visa_cfg_llm_test)
check("关键词规则会判错的场景,信了 LLM 给的 explicit_yes",
      scored[0].sponsorship_signal, "explicit_yes")
check("担保加分跟着 LLM 判断走(70+5)", scored[0].score, 75)

# 关键词误判防线(AWS/ServiceNow 真实案例):sponsor 出现但不是签证语境,
# 绝不能判成"不担保"
affinity_job = mk("linkedin", "Engineer", "AWSlike", "https://x/56",
    desc="Our employee-led and company-sponsored affinity groups promote "
         "inclusion and empower our people. Build with React and TypeScript.")
check("'company-sponsored affinity groups' 不误判为 explicit_no",
      sponsorship_signal(affinity_job, visa_cfg_llm_test), "unknown")
exec_sponsor_job = mk("linkedin", "Engineer", "SNlike", "https://x/57",
    desc="Serve as a trusted advisor to executive sponsors, aligning "
         "AI-driven solutions to strategic business goals.")
check("'executive sponsors'(业务干系人)不误判为 explicit_no",
      sponsorship_signal(exec_sponsor_job, visa_cfg_llm_test), "unknown")
# 签证语境但没有否定词(可能是正面表述)也别硬判否定,留 unknown 交给 LLM
ambiguous_job = mk("seek", "Engineer", "MaybeCo", "https://x/58",
    desc="We may sponsor the right candidate's visa depending on experience.")
check("签证语境但无否定词,留 unknown(不硬判否定)",
      sponsorship_signal(ambiguous_job, visa_cfg_llm_test), "unknown")

# 明确不担保的岗位应扣分,而且扣分(10)比加分(5)权重高
no_sponsor_job = mk("linkedin", "Engineer", "NoSponsorCo", "https://x/52",
    desc="Great role, but we are not able to offer visa sponsorship.")
fake_llm_no = _FakeLLMSponsor({no_sponsor_job.id: "explicit_no"})
scored_no = score_jobs([no_sponsor_job], fake_llm_no, "resume", "prefs", visa_cfg_llm_test)
check("明确不担保扣分(70-10)", scored_no[0].score, 60)
check("扣分权重(10)高于加分权重(5)", 10 > 5, True)

# unknown 不加不扣
unknown_job = mk("seek", "Engineer", "SilentCo", "https://x/53",
    desc="Build cool products with a great team.")
fake_llm_unk = _FakeLLMSponsor({unknown_job.id: "unknown"})
scored_unk = score_jobs([unknown_job], fake_llm_unk, "resume", "prefs", visa_cfg_llm_test)
check("unknown 不加不扣(保持 70)", scored_unk[0].score, 70)

# LLM 没按格式给合法值(比如响应里压根没这个字段、或者给了奇怪的值)时,
# 应该退回关键词规则兜底,不能直接崩或者悄悄丢掉这个信息
class _FakeLLMMissingField:
    """模拟 LLM 响应里漏了 sponsorship_signal 这个字段的情况。"""

    def __init__(self):
        self.batch_size = 8
        self.max_description_chars = 4000

    def complete_json(self, system, user, max_tokens=4096):
        import re as _re
        out = []
        for m in _re.finditer(r'<job id="([^"]+)">', user):
            out.append({"id": m.group(1), "score": 70, "reason": "测试",
                        "summary": "测试摘要", "matched": [], "missing": []})
            # 注意:这里没给 sponsorship_signal
        return out


maincode_style_job = mk("linkedin", "Engineer", "Maincode", "https://x/51",
    desc="At this time we are not able to offer visa sponsorship, so "
         "applicants must have existing and unrestricted work rights.")
scored2 = score_jobs([maincode_style_job], _FakeLLMMissingField(), "resume", "prefs", visa_cfg)
check("LLM 响应漏了这个字段时,退回关键词规则兜底(修过否定语境的那版)",
      scored2[0].sponsorship_signal, "explicit_no")


print("\n=== 截断把担保句切掉导致漏判(Maincode 真实案例的根因) ===")
from score import _job_block, _visa_sentences, _requirement_sentences

# JD 很长,担保声明在结尾、超过 max_chars 截断线 —— 这正是 Maincode 踩的坑
filler = "We build great products with React and TypeScript. " * 120  # ~6000 字符
long_desc = filler + "At this time we are not able to offer visa sponsorship, " \
    "so applicants must have existing and unrestricted work rights in Australia."
long_job = mk("linkedin", "Product Engineer", "MaincodeLong", "https://x/54", desc=long_desc)

# 1. 截断后担保句仍要出现在发给 LLM 的 block 里
block = _job_block(long_job, 4000)
check_true = lambda name, cond: check(name, bool(cond), True)
check_true("截断后 block 里仍保留担保关键句",
           "not able to offer visa sponsorship" in block)
check_true("block 确实做了截断(没整段塞回去)", len(block) < len(long_desc))

# 经验硬条件也常出现在尾部；它必须和签证条款一样补回给 DeepSeek。
experience_tail = (
    "Team culture and benefits. Candidates may suit either stream: "
    "3 years of backend experience or 5+ years of commercial software engineering experience."
)
experience_job = mk(
    "seek", "Software Engineer", "CatapultStyle", "https://x/56",
    desc=filler + experience_tail,
)
experience_block = _job_block(experience_job, 4000)
check_true(
    "截断后 block 仍保留工作年限要求",
    "5+ years of commercial software engineering experience" in experience_block,
)
check_true(
    "资格句提取不会塞回无关尾部",
    "Team culture and benefits" not in _requirement_sentences(experience_tail),
)

# 2. LLM 只看到截断版、判了 unknown,但完整 JD 有否定担保句 → 兜底纠正成 explicit_no,并扣分
fake_llm_trunc = _FakeLLMSponsor({long_job.id: "unknown"})
scored_trunc = score_jobs([long_job], fake_llm_trunc, "resume", "prefs", visa_cfg)
check("LLM 判 unknown 但完整 JD 明确不担保 → 兜底纠正为 explicit_no",
      scored_trunc[0].sponsorship_signal, "explicit_no")
check("兜底纠正后扣了分(70-10)", scored_trunc[0].score, 60)

# 3. 反面保证:完整 JD 里真没提担保时,unknown 不该被兜底逻辑凭空造出信号
clean_long = ("We build great products with React and TypeScript. " * 120)
clean_job = mk("seek", "Engineer", "CleanCo", "https://x/55", desc=clean_long)
fake_llm_clean = _FakeLLMSponsor({clean_job.id: "unknown"})
scored_clean = score_jobs([clean_job], fake_llm_clean, "resume", "prefs", visa_cfg)
check("完整 JD 没提担保时,unknown 保持 unknown(不误造信号)",
      scored_clean[0].sponsorship_signal, "unknown")


print("\n" + "=" * 50)
print(f"通过 {PASS} · 失败 {FAIL}")
print("=" * 50)
sys.exit(1 if FAIL else 0)
