"""签证硬过滤 + LLM 打分。

顺序很关键:先用关键词把明显不可能的岗位砍掉,再送 LLM。
485 + 需要担保的情况下,这一步通常能砍掉 20-40%,省下来的都是 token 钱。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

from schema import Job

log = logging.getLogger(__name__)


@dataclass
class Scored:
    job: Job
    score: int
    reason: str
    matched: list[str]
    missing: list[str]
    sponsorship_signal: str  # explicit_yes | explicit_no | unknown
    summary: str = ""  # 一两句话概括这个岗位是做什么的,不用打开 JD 原文也能看懂
    # 以下字段在 LLM 打分后由 application_policy.py 补齐，区分岗位匹配与简历匹配。
    resume_id: str = ""
    resume_path: str = ""
    resume_fit_score: int = 0
    resume_reason: str = ""
    application_mode: str = "manual_review"
    eligibility_reason: str = ""
    freshness_bucket: str = "unknown"
    artifact_path: str = ""


# ------------------------------------------------------------ 硬过滤

_MD_NOISE_RE = re.compile(r"[*_`~]+")
_ESCAPED_PUNCT_RE = re.compile(r"\\([\-.!?()\[\]])")
_WS_RE = re.compile(r"\s+")


def _norm_text(job: Job) -> str:
    """清洗 Markdown 噪音,否则规则匹配会被格式符号打断。

    实测踩过的坑:LinkedIn/JobSpy 抓下来的 JD 是 Markdown 格式,
    "must be **Australian Citizen**" 里 "be" 和 "Australian" 之间
    夹着换行符、不换行空格(\\xa0)和加粗标记 **,\\s+ 只匹配空白字符,
    不认星号,导致 "must be ... australian citizen" 这个模式直接断裂、
    完全匹配不上 —— 即便固定短语列表和正则规则本身都写对了也没用。

    这里把 *_`~ 这些 Markdown 强调符号、反斜杠转义(JobSpy 喜欢把
    连字符转义成 \\-)、以及不换行空格,统一清成普通空格再做匹配。
    """
    text = f"{job.title}\n{job.description}".lower()
    text = text.replace("\xa0", " ")
    text = _MD_NOISE_RE.sub(" ", text)
    text = _ESCAPED_PUNCT_RE.sub(r"\1", text)
    return _WS_RE.sub(" ", text)


# 固定短语列表撑不住自然语言的写法变化 —— 实测踩过的坑:
# Red Cross Lifeblood 的原文是
#   "MUST be a citizen of Australia or have Permanent Residency visa
#    with the ability to work in Australia."
# 这跟 "must be an australian citizen" 这种固定短语对不上(词序不同、
# 多了 "or have Permanent Residency"),纯字符串匹配漏掉了。
#
# "citizen 类词 与 permanent resident 类词在同一句附近同时出现" 这个模式,
# 比穷举短语更能扛住变体。命中后再检查附近有没有否定/担保词,
# 避免把 "we don't require citizenship or PR, sponsorship available"
# 这种反而合适的岗位误杀。
_CITIZEN_TERMS = r"(?:australian\s+)?citizen(?:ship)?"
# 注意:"residency" 的词干是 "residen" + "cy",不是 "resident" + "cy" ——
# 这里第一版写成 resident(?:cy|ship)? 导致 "permanent residency" 完全匹配不上,
# 靠单元测试里塞真实 JD 原文才测出来。
_PR_TERMS = r"(?:permanent\s+residen(?:t|ts|cy|ce)?|\bpr\b)"
_CITIZEN_OR_PR_RE = re.compile(
    rf"{_CITIZEN_TERMS}\W+(?:\w+\W+){{0,6}}(?:or|and|/)\W+(?:\w+\W+){{0,4}}{_PR_TERMS}"
    rf"|{_PR_TERMS}\W+(?:\w+\W+){{0,6}}(?:or|and|/)\W+(?:\w+\W+){{0,4}}{_CITIZEN_TERMS}",
    re.I,
)
_NEGATION_NEARBY_RE = re.compile(
    r"\b(don'?t require|do not require|not required|no need|not necessary|"
    r"needn'?t|willing to sponsor|visa sponsorship|we sponsor|able to sponsor)\b",
    re.I,
)
_WINDOW = 60  # 命中点前后各扩这么多字符,检查有没有否定/担保词

# 第二批实测又踩了同一类坑,但这次连 "or PR" 都没有,是纯 citizenship-only 要求
# (常见于安全审查/国防岗位),比 citizen-or-PR 更严格:
#   AWS Systems Development Engineer: "Applicants must be Australian citizens
#     and hold or be eligible to obtain an Australian Government Security Clearance"
#   Peoplebank Boomi Developer:       "candidates must be Australian Citizen"
#   BAE Systems:                      "applicants must be Australian citizens
#     and either possess or be eligible to obtain ... clearance"
# 固定短语 "must be an australian citizen" 好几个真实案例全部没抓到,
# 原因一次比一次刁钻:
#   "applicants MUST BE australian citizens"                 复数 + applicants 做主语
#   "candidates must be Australian Citizen"                   压根没有 "an" 这个冠词
#   "... does REQUIRES the successful applicant TO BE an
#    Australian Citizen ..."                                  "requires" 和 "to be"
#                                                               中间插了个名词短语
# 每次都是"逐句穷举固定搭配"这个思路本身的问题:自然语言的语法变化
# (单复数、冠词、主语、插入语)组合起来是发散的,补丁打不完。
# 换成"要求类动词与 australian citizen(s) 在一个短窗口内同时出现"这个更
# 宽松的邻近匹配,不再关心中间的语法结构长什么样。
_REQUIREMENT_VERB = r"(?:must|requires?|required?|needs?|necessary|mandatory)"
_MUST_BE_CITIZEN_RE = re.compile(
    rf"\b{_REQUIREMENT_VERB}\b(?:\W+\w+){{0,6}}\W+(?:an?\s+)?australian\s+citizens?\b",
    re.I,
)


def _find_citizen_or_pr_hit(text: str) -> Optional[str]:
    """在全文里找 "citizen 类词 or/and permanent resident 类词" 的模式,
    以及不带 PR 选项的纯 "must be australian citizen(s)" 模式。

    命中后检查前后 _WINDOW 字符内有没有否定/担保词,
    避免误杀 "we don't require citizenship or PR, visa sponsorship available" 这种。
    """
    for pattern in (_CITIZEN_OR_PR_RE, _MUST_BE_CITIZEN_RE):
        for m in pattern.finditer(text):
            lo, hi = max(0, m.start() - _WINDOW), min(len(text), m.end() + _WINDOW)
            context = text[lo:hi]
            if _NEGATION_NEARBY_RE.search(context):
                continue
            return m.group(0).strip()
    return None


def visa_filter(jobs: list[Job], visa_cfg: dict) -> tuple[list[Job], list[tuple[Job, str]]]:
    """返回 (通过的, [(被淘汰的, 原因)])。

    两层过滤:
      1. config.yaml 里配置的固定短语(exclude_keywords)—— 精确、可控,
         适合你已经确认过的措辞。
      2. 内置的 "citizen 类词 与 PR 类词同句出现" 规则匹配 —— 泛化能力更强,
         能抓住短语列表没穷举到的写法。

    注意:JD 里完全没提签证的一律放行 —— 澳洲大量岗位根本不写签证要求,
    一刀切会误杀太多。
    """
    excludes = [k.lower() for k in visa_cfg.get("exclude_keywords", [])]
    passed, rejected = [], []

    for job in jobs:
        text = _norm_text(job)

        hit = next((k for k in excludes if k in text), None)
        if hit:
            rejected.append((job, f"签证硬性排除:命中固定短语 “{hit}”"))
            continue

        pattern_hit = _find_citizen_or_pr_hit(text)
        if pattern_hit:
            rejected.append((job, f"签证硬性排除:命中 citizen/PR 模式 “{pattern_hit}”"))
            continue

        passed.append(job)

    log.info("签证过滤:%d → %d 条(淘汰 %d)", len(jobs), len(passed), len(rejected))
    return passed, rejected


_SPONSOR_NEGATION_RE = re.compile(
    r"\b(not|n't|no|unable|cannot|can'?t|without|don'?t|does\s?n'?t|won'?t|"
    r"unfortunately|regret)\b",
    re.I,
)
_SPONSOR_WINDOW = 40  # 命中点前后各扩这么多字符,检查有没有否定词

# "sponsor" 这个词根很泛,必须确认它确实在【签证】语境里,才当担保信号看。
# 踩过的坑(AWS/ServiceNow 真实案例):
#   "company-sponsored affinity groups"   → 是多元共融社团,不是签证
#   "serve as a trusted advisor to executive sponsors" → 是业务干系人,不是签证
# 这两个都被旧逻辑("出现 sponsor 就判 explicit_no")误判成"不担保",
# AWS 好几个岗位、ServiceNow 无辜被扣分。加一道签证语境门槛过滤掉这类。
_VISA_CONTEXT_RE = re.compile(
    r"(visa|immigration|work(?:ing)?\s+rights?|work\s+permit|482|tss|"
    r"skilled\s+migration|right\s+to\s+work|working\s+in\s+australia)",
    re.I,
)
_SPONSOR_CONTEXT_WINDOW = 60  # 判断 sponsor 是不是签证语境时的前后窗口


def sponsorship_signal(job: Job, visa_cfg: dict) -> str:
    """踩过的坑(Maincode 真实案例):JD 原文是 "we are not able to offer
    visa sponsorship",纯字符串匹配 bonus_keywords 里的 "visa sponsorship"
    照样命中 —— 完全不管前面那个 "not able to offer"。结果一个明确拒绝担保
    的岗位被打上 explicit_yes,还加了 5 分,分数和结论完全反了。

    跟 visa_filter 里 citizen/PR 检测同一个思路:命中关键词后,看前后
    _SPONSOR_WINDOW 个字符内有没有否定词,有就跳过、找下一个候选,
    而不是见到关键词字符串就直接判定为正面信号。

    explicit_no 的判定也收紧了(AWS/ServiceNow 案例):不能见到 "sponsor"
    就判不担保 —— 必须同时满足"签证语境 + 附近有否定词",才算明确拒绝担保。
    只有签证语境没否定词(可能是正面表述)、或压根不在签证语境("赞助社团"、
    "业务 sponsor")的,一律留 unknown,不凭空造否定信号。
    """
    text = _norm_text(job)
    for k in visa_cfg.get("bonus_keywords", []):
        kw = k.lower()
        for m in re.finditer(re.escape(kw), text):
            lo, hi = max(0, m.start() - _SPONSOR_WINDOW), min(len(text), m.end() + _SPONSOR_WINDOW)
            if _SPONSOR_NEGATION_RE.search(text[lo:hi]):
                continue
            return "explicit_yes"

    for m in re.finditer(r"sponsor\w*", text):
        lo = max(0, m.start() - _SPONSOR_CONTEXT_WINDOW)
        hi = min(len(text), m.end() + _SPONSOR_CONTEXT_WINDOW)
        ctx = text[lo:hi]
        # 必须是签证语境,且附近有否定词,才判"明确不担保"
        if _VISA_CONTEXT_RE.search(ctx) and _SPONSOR_NEGATION_RE.search(ctx):
            return "explicit_no"
    return "unknown"


def _resolve_sponsorship_signal(job: Job, visa_cfg: dict, llm_value) -> str:
    """担保信号优先让 LLM 判断 —— JD 已经整段喂给它了,不用多花钱,
    而且它真的能读懂 "not able to offer visa sponsorship" 这种否定句,
    不会像关键词字符串匹配那样只认字面出现没出现。

    这里的关键词规则(sponsorship_signal)降级成兜底:只有 LLM 没按格式
    给出合法值时(那一条没返回、返回了别的东西)才用它顶上。
    """
    signal = str(llm_value or "").strip().lower()
    # LLM 明确判了 yes/no —— 它读懂了上下文(含否定),直接采信。
    if signal in ("explicit_yes", "explicit_no"):
        return signal
    # 到这里 signal 要么是 "unknown",要么是不合法的值。两种都用关键词规则
    # 在【完整】JD(未截断)上再兜一道:
    #   - "unknown" 很可能只是"LLM 没看到"—— 担保句可能被 max_description_chars
    #     截掉了(Maincode 案例),LLM 判 unknown 不代表 JD 里真没写。
    #   - 关键词规则读的永远是完整的 job.description,能补上这个盲区。
    # 关键词规则若也判 unknown,结果仍是 unknown,不会凭空造信号。
    return sponsorship_signal(job, visa_cfg)


# ------------------------------------------------------------ LLM 打分

# 担保信号的加/扣分权重。明确不担保的扣分(10)比明确担保的加分(5)权重高 ——
# 对 485 + 需要后续担保的情况,"不担保"是长期规划的实质减分项,值得更警惕。
_SPONSOR_BONUS = 5
_NO_SPONSOR_PENALTY = 10

_SYSTEM = """你是一个务实的澳洲岗位匹配评估器。
严格遵守用户消息中提供的完整评分策略，只使用已验证的候选人事实。
逐个岗位返回评分策略要求的 JSON 数组，不要输出 Markdown 或额外说明。"""


def build_scoring_input(
    rules: str,
    *,
    preferences: str,
    candidate_context: str,
    cfg: dict,
) -> str:
    """Build the DeepSeek request in memory without generating or changing the stable rules."""
    if len(rules.strip()) < 300:
        raise ValueError("applypilot-au 的 DeepSeek 打分规则缺失或内容过短")
    app = cfg.get("application", {})
    return f"""# Stable scoring rules from applypilot-au

{rules.strip()}

# Runtime thresholds

- Targeted job-fit threshold: {int(app.get("targeted_job_fit_threshold", 80))}
- Targeted resume-fit threshold: {int(app.get("targeted_resume_fit_threshold", 72))}
- Broad job-fit threshold: {int(app.get("broad_job_fit_threshold", 70))}
- Freshness priority: within {int(app.get("priority_within_hours", 24))} hours, then within
  {int(app.get("recent_within_hours", 72))} hours.
- Eligible seniority keywords: {", ".join(str(x) for x in app.get("eligible_seniority_keywords", [])) or "not configured"}
- Excluded seniority keywords: {", ".join(str(x) for x in app.get("excluded_seniority_keywords", [])) or "not configured"}

# Candidate preferences and restrictions

{preferences.strip() or "(No additional preferences configured.)"}

# Candidate matching context

{candidate_context.strip() or "(No additional matching context configured.)"}
"""


# 签证/担保相关的句子,截断时要保证补回给 LLM,否则会漏判。
# 踩过的坑(Maincode 真实案例):JD 5235 字,"not able to offer visa sponsorship"
# 出现在第 5147 字,超过 max_description_chars(4000)截断线,LLM 根本没看到那句话,
# 只能判 unknown —— 签证担保判断直接失效。签证条款经常是 JD 结尾的样板文字,
# 最容易被截掉,所以专门把这类句子从被截断的尾部捞回来。
_VISA_HINT_RE = re.compile(
    r"(sponsor|visa|citizen|permanent\s+residen|\bpr\b|work(?:ing)?\s+rights|"
    r"clearance|nv1|nv2|baseline)",
    re.I,
)
_REQUIREMENT_HINT_RE = re.compile(
    r"(\b\d+\s*(?:\+\s*)?(?:years?|yrs?)\b|years?\s+of\s+(?:commercial\s+)?"
    r"experience|\b(?:must|required|essential|minimum|qualification|degree|"
    r"certification)\b)",
    re.I,
)


def _visa_sentences(text: str, limit: int = 5, max_len: int = 600) -> str:
    """从一段文本里挑出含签证/担保关键词的句子,去重、限量返回。"""
    parts = re.split(r"(?<=[.!?])\s+|\n+", text)
    out: list[str] = []
    for p in parts:
        p = p.strip()
        if p and _VISA_HINT_RE.search(p) and p not in out:
            out.append(p)
        if len(out) >= limit:
            break
    return "  ".join(out)[:max_len]


def _requirement_sentences(text: str, limit: int = 6, max_len: int = 900) -> str:
    """Keep experience and mandatory qualification clauses from a truncated JD tail."""
    parts = re.split(r"(?<=[.!?])\s+|\n+", text)
    out: list[str] = []
    for part in parts:
        sentence = part.strip()
        if sentence and _REQUIREMENT_HINT_RE.search(sentence) and sentence not in out:
            out.append(sentence)
        if len(out) >= limit:
            break
    return "  ".join(out)[:max_len]


def _job_block(job: Job, max_chars: int) -> str:
    desc = (job.description or "").strip()
    if len(desc) > max_chars:
        head, tail = desc[:max_chars], desc[max_chars:]
        desc = head + "\n...(中间省略)"
        # 把尾部的资格硬条件和签证条款补回来。招聘网站常把这些内容放在 JD
        # 末尾；只保留开头会让 DeepSeek 看不到工作年限、学历或工作权利要求。
        critical = [
            item for item in (
                _requirement_sentences(tail),
                _visa_sentences(tail),
            )
            if item
        ]
        if critical:
            desc += "\n[以下为完整 JD 尾部的关键资格、经验或签证句]\n"
            desc += "  ".join(dict.fromkeys(critical))
    salary = job.salary_raw or "未列出"
    return f"""<job id="{job.id}">
职位: {job.title}
公司: {job.company}
地点: {job.location or '未列出'}
薪资: {salary}
描述:
{desc or '(无描述)'}
</job>"""


def score_jobs(jobs: list[Job], llm, resume: str, scoring_policy: str,
               visa_cfg: dict) -> list[Scored]:
    if not jobs:
        return []

    results: list[Scored] = []
    batch_size = llm.batch_size

    def request_batch(batch: list[Job]):
        blocks = "\n\n".join(_job_block(j, llm.max_description_chars) for j in batch)
        user = f"""# 本次 DeepSeek 评分策略
{scoring_policy}

# 候选人简历
{resume}

# 待评估岗位({len(batch)} 个)
{blocks}

请为以上每个岗位打分,输出 JSON 数组。"""

        # DeepSeek needs enough output room for one structured result per job.
        # A fixed 4096-token cap can truncate an otherwise valid eight-job JSON
        # array, so scale the response budget with the configured batch size.
        score_output_tokens = min(8192, max(4096, 1024 * len(batch)))
        return llm.complete_json(_SYSTEM, user, max_tokens=score_output_tokens)

    for i in range(0, len(jobs), batch_size):
        batch = jobs[i:i + batch_size]
        data = request_batch(batch)

        if not isinstance(data, list):
            log.warning("批次 %d 打分失败,该批次全部标记为 -1 待人工处理", i // batch_size)
            for job in batch:
                results.append(Scored(job, -1, "LLM 打分失败,需人工查看", [], [],
                                      sponsorship_signal(job, visa_cfg),
                                      summary="(打分失败,没有摘要)"))
            continue

        by_id = {str(d.get("id")): d for d in data if isinstance(d, dict)}
        missing_jobs = [job for job in batch if job.id not in by_id]
        if missing_jobs:
            log.warning(
                "批次 %d 漏回 %d 个岗位,仅重试漏项一次",
                i // batch_size,
                len(missing_jobs),
            )
            retry_data = request_batch(missing_jobs)
            if isinstance(retry_data, list):
                by_id.update({
                    str(item.get("id")): item
                    for item in retry_data
                    if isinstance(item, dict)
                })

        for job in batch:
            d = by_id.get(job.id)
            if not d:
                results.append(Scored(job, -1, "LLM 未返回该岗位结果", [], [],
                                      sponsorship_signal(job, visa_cfg),
                                      summary="(未返回,没有摘要)"))
                continue

            try:
                score = int(d.get("score", -1))
            except (TypeError, ValueError):
                score = -1

            results.append(Scored(
                job=job,
                score=max(-1, min(100, score)),
                reason=str(d.get("reason", ""))[:120],
                matched=[str(x) for x in (d.get("matched") or [])][:4],
                missing=[str(x) for x in (d.get("missing") or [])][:4],
                sponsorship_signal=_resolve_sponsorship_signal(job, visa_cfg, d.get("sponsorship_signal")),
                summary=str(d.get("summary", ""))[:150],
            ))

        log.info("已打分 %d/%d", min(i + batch_size, len(jobs)), len(jobs))

    # 担保信号影响最终分。你是 485 + 后续需要雇主担保,所以这个信号很值钱:
    #   - 明确愿意担保 → 加分
    #   - 明确不担保   → 扣分,而且权重比"愿意担保"的加分略高。因为对你来说,
    #     一家明确说不担保的公司是长期规划上的实质减分项(485 到期后没法续),
    #     比"愿意担保"是锦上添花更值得警惕。
    for r in results:
        if r.score < 0:
            continue
        if r.sponsorship_signal == "explicit_yes":
            r.score = min(100, r.score + _SPONSOR_BONUS)
            r.reason = (r.reason + f" [明确提供担保 +{_SPONSOR_BONUS}]").strip()
        elif r.sponsorship_signal == "explicit_no":
            r.score = max(0, r.score - _NO_SPONSOR_PENALTY)
            r.reason = (r.reason + f" [明确不担保 -{_NO_SPONSOR_PENALTY}]").strip()

    results.sort(key=lambda r: r.score, reverse=True)
    return results
