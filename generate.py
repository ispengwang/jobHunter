"""为高分岗位生成定制简历 + cover letter(Markdown)。

红线:只允许重排、改写措辞、调整详略。绝不允许编造经历、技能或年限。
prompt 里反复强调这一点,因为模型在"优化简历"这个任务上特别容易自作主张。
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from score import Scored

log = logging.getLogger(__name__)


_RESUME_SYSTEM = """你是一位澳洲本地的技术简历顾问。

任务:把候选人的简历针对某个具体岗位重新组织,输出 Markdown。

绝对红线 —— 违反即为失败:
- 不得新增任何简历中不存在的技能、工具、公司、职位或项目
- 不得夸大年限、团队规模、业务指标
- 不得把"接触过"写成"精通"
如果候选人缺某项要求,就是缺 —— 不要用模糊措辞掩盖。

允许且应该做的:
- 调整 bullet 顺序,把与该岗位最相关的经历提前
- 改写措辞,让用词贴近 JD 的术语(前提是候选人确实做过这件事)
- 压缩不相关的经历到一行,给相关经历腾出篇幅
- 把 JD 里的关键词自然融入(为了过 ATS),但不得堆砌

澳洲本地惯例:
- 不要放照片、生日、婚姻状况、国籍
- 长度控制在 2 页以内
- 日期格式 "Mar 2023 – Present"
- 拼写用澳式英语(organise / specialise / analyse)

只输出简历 Markdown,不要任何前后说明。"""


_LIGHT_RESUME_SYSTEM = """你只负责对一份已经审核过的候选人简历做最小幅度的 ATS 定制。

这是海投材料，不是重新写简历。你的输出必须保留原简历的全部事实和结构证据。

允许且仅允许:
- 重排现有 section 或现有 bullet 的顺序，让岗位相关内容靠前
- 在原句事实不变的前提下，把一个词或很短的短语替换成 JD 的同义术语

绝对禁止:
- 新增任何技能、工具、公司、职位、项目、职责、数字、日期或资格
- 不得删除、合并、拆分或扩写 bullet；不得整篇重写，或改变原简历的事实结构
- 把 JD 关键词硬塞进没有事实依据的句子
- 把“接触过/参与过”升级成“负责/主导/精通”，或改变任何数字和程度

如果没有完全安全的调整，就原样保留相关内容。输出完整 Markdown 简历，不要前后说明。"""


_COVER_SYSTEM = """你是一位澳洲本地的求职信写手。

写一封 cover letter,Markdown 格式,严格控制在 250-320 词。

澳洲本地风格 —— 这点很重要,美式模板在这里会显得用力过猛:
- 直接、平实,不要 "I am thrilled to apply for this incredible opportunity"
- 不自夸形容词堆砌,用具体事实说话
- 语气专业但像个真人在说话,不要 AI 腔
- 澳式拼写(organise / specialise / realise)

结构:
1. 开头一句说清申请什么岗位、从哪看到的
2. 中间 2 段:各挑一个候选人真实做过的、和 JD 最相关的经历,说清楚做了什么、结果如何
3. 一句说明为什么是这家公司(必须引用 JD 或公司的具体信息,不能是通用套话)
4. 结尾一句,简短,不要卑微

绝对红线:
- 不得编造任何经历或数字
- 不得主动提及签证状态,除非 JD 明确要求说明工作权利

只输出信件正文 Markdown,不要任何前后说明。"""


_ATS_REPLACEMENTS = (
    ("node.js", "nodejs"),
    ("react.js", "reactjs"),
    ("next.js", "nextjs"),
    ("ci/cd", "cicd"),
    ("c++", "cplusplus"),
    ("c#", "csharp"),
    (".net", "dotnet"),
)


def _normalise_ats_text(value: str | None) -> str:
    """Normalise punctuation variants without making fuzzy claims about a skill."""
    text = str(value or "").casefold()
    for source, replacement in _ATS_REPLACEMENTS:
        text = text.replace(source, replacement)
    return " ".join(re.findall(r"[a-z0-9]+", text))


def _contains_ats_keyword(text: str, keyword: str) -> bool:
    needle = _normalise_ats_text(keyword)
    haystack = _normalise_ats_text(text)
    return bool(needle and f" {needle} " in f" {haystack} ")


def extract_hard_keywords(s: Scored) -> list[str]:
    """Use the existing DeepSeek matched/missing fields as ATS keyword candidates.

    ``score.py`` already asks the single scoring call for evidence-backed requirements. Keeping
    this extraction local avoids another model request and prevents a second, inconsistent
    interpretation of the JD.
    """
    keywords: list[str] = []
    seen: set[str] = set()
    for raw in [*(s.matched or []), *(s.missing or [])]:
        for item in re.split(r"\s*(?:,|;)\s*|\s+/\s+", str(raw or "")):
            keyword = re.sub(r"\s+", " ", item.strip(" -*•\t"))
            normalised = _normalise_ats_text(keyword)
            if keyword and normalised and normalised not in seen:
                seen.add(normalised)
                keywords.append(keyword)
    return keywords


def keyword_coverage(
    s: Scored,
    generated_resume: str,
    candidate_resume: str,
) -> dict[str, object]:
    """Measure generated-resume coverage and classify uncovered verified facts safely."""
    keywords = extract_hard_keywords(s)
    covered = [keyword for keyword in keywords if _contains_ats_keyword(generated_resume, keyword)]
    uncovered = [keyword for keyword in keywords if keyword not in covered]
    candidate_has_but_omitted = [
        keyword for keyword in uncovered if _contains_ats_keyword(candidate_resume, keyword)
    ]
    candidate_unverified_or_missing = [
        keyword for keyword in uncovered if keyword not in candidate_has_but_omitted
    ]
    total = len(keywords)
    return {
        "keywords": keywords,
        "covered": covered,
        "uncovered": uncovered,
        "candidate_has_but_omitted": candidate_has_but_omitted,
        "candidate_unverified_or_missing": candidate_unverified_or_missing,
        "covered_count": len(covered),
        "total_count": total,
        "coverage_percent": round(len(covered) * 100 / total, 1) if total else None,
    }


def _safe_name(s: str, limit: int = 40) -> str:
    s = re.sub(r"[^\w\s-]", "", s).strip()
    s = re.sub(r"[\s_]+", "-", s)
    return s[:limit].strip("-") or "unknown"


def _job_context(s: Scored, max_chars: int) -> str:
    job = s.job
    desc = (job.description or "").strip()
    if len(desc) > max_chars:
        desc = desc[:max_chars] + "\n...(已截断)"
    return f"""职位: {job.title}
公司: {job.company}
地点: {job.location or '未列出'}
薪资: {job.salary_raw or '未列出'}
来源: {job.source}
链接: {job.url}

匹配度评估: {s.score}/100 — {s.reason}
已满足: {', '.join(s.matched) or '—'}
待补强: {', '.join(s.missing) or '—'}

职位描述:
{desc}"""


def _write_summary(
    out_dir: Path,
    s: Scored,
    coverage: dict[str, object] | None = None,
) -> None:
    job = s.job
    dup = "\n".join(f"- {u}" for u in job.duplicate_urls) or "- (无)"
    salary = job.salary_raw or "未列出"
    signal = {
        "explicit_yes": "✅ JD 明确提到可提供担保",
        "explicit_no": "⚠️ JD 提到 sponsorship 但疑似否定表述,投前请自行确认",
        "unknown": "— JD 未提及签证",
    }[s.sponsorship_signal]
    if coverage is None:
        keyword_section = "## ATS 关键词覆盖\n- 简历尚未生成，暂未计算。"
    else:
        total = int(coverage["total_count"])
        covered_count = int(coverage["covered_count"])
        percent = coverage["coverage_percent"]
        rate = (
            f"{percent:g}%（{covered_count}/{total}）"
            if percent is not None else "N/A（DeepSeek 未返回硬关键词）"
        )
        uncovered = coverage["uncovered"] or []
        has_but_omitted = coverage["candidate_has_but_omitted"] or []
        unverified_or_missing = coverage["candidate_unverified_or_missing"] or []
        keyword_section = f"""## ATS 关键词覆盖
**覆盖率**: {rate}
**已覆盖**: {', '.join(coverage['covered']) or '—'}
**未覆盖关键词**: {', '.join(uncovered) or '—'}

### 候选人确实具备但生成简历没写出来（应补）
{', '.join(has_but_omitted) or '—'}

### 候选人未验证/不具备（永远不得添加）
{', '.join(unverified_or_missing) or '—'}

> “未验证/不具备”表示原始候选人简历没有可核对证据；这不是允许猜测或补写事实。
"""

    (out_dir / "00-summary.md").write_text(f"""# {job.title} — {job.company}

**匹配度**: {s.score}/100
**理由**: {s.reason}

**申请路径**: {s.application_mode or 'manual_review'}
**简历版本**: {s.resume_id or 'resume-default'} · **简历匹配度**: {s.resume_fit_score or '未计算'}
**简历选择理由**: {s.resume_reason or '使用默认简历'}
**资格判断**: {s.eligibility_reason or '需人工审核'}

| 项目 | 内容 |
|---|---|
| 地点 | {job.location or '未列出'} |
| 薪资 | {salary} |
| 类型 | {job.work_type or '未列出'} |
| 发布 | {job.posted_date or '未知'} |
| 来源 | {job.source} |

**签证信号**: {signal}

## 申请链接
- {job.url}

### 其他平台同岗位
{dup}

## 已满足
{chr(10).join('- ' + m for m in s.matched) or '- —'}

## 待补强 — 面试大概率会问
{chr(10).join('- ' + m for m in s.missing) or '- —'}

{keyword_section}

## 投递前检查
- [ ] 简历里的每一句都属实
- [ ] cover letter 里公司相关的那句不是套话
- [ ] 确认这家不是中介重复挂的同一个岗位
- [ ] 投完在 LinkedIn 上找到 hiring manager 或 recruiter
""", encoding="utf-8")


def generate(scored: list[Scored], llm, resume: str, preferences: str,
             cfg: dict, root: Path | None = None) -> list[Path]:
    threshold = cfg["scoring"]["generate_threshold"]
    max_gen = cfg["scoring"]["max_generate"]
    project_root = (root or Path(__file__).resolve().parent).resolve()
    configured_output = Path(cfg["paths"]["output_dir"])
    out_root = (
        configured_output
        if configured_output.is_absolute()
        else project_root / configured_output
    ) / "applications"
    out_root = out_root.resolve()
    out_root.mkdir(parents=True, exist_ok=True)

    # 旧调用方不会提供 resume_id/application_mode，保留其生成行为；新流程则只为
    # 已进入 broad/targeted 队列的岗位准备材料。
    targets = [
        s for s in scored
        if s.score >= threshold and (
            not s.resume_id or s.application_mode in {"broad", "targeted"}
        )
    ][:max_gen]
    if not targets:
        log.warning("没有岗位达到 %d 分。可以调低 generate_threshold 或放宽搜索词。", threshold)
        return []

    log.info("为 %d 个岗位生成材料(阈值 %d)", len(targets), threshold)
    written: list[Path] = []

    for idx, s in enumerate(targets, 1):
        job = s.job
        dir_name = f"{s.score:03d}-{job.id}-{_safe_name(job.company)}-{_safe_name(job.title, 30)}"
        out_dir = out_root / dir_name
        out_dir.mkdir(parents=True, exist_ok=True)

        context = _job_context(s, llm.max_description_chars)
        _write_summary(out_dir, s)

        selected_resume = resume
        if s.resume_path:
            selected_path = Path(s.resume_path)
            if selected_path.exists():
                selected_resume = selected_path.read_text(encoding="utf-8")
            else:
                log.warning("岗位 %s 指定的简历不存在，退回默认简历: %s", job.id, selected_path)

        light_tailoring = s.application_mode == "broad" and s.resume_id
        resume_instruction = (
            "请只做轻量 ATS 调整：仅重排现有 bullet/section，或做不改变事实的同义词微调；"
            "不得整篇重写、增删 bullet 或新增任何事实。"
            if light_tailoring else
            "请针对这个岗位重组简历。记住:不得编造。"
        )
        user_resume = f"""# 候选人原始简历
{selected_resume}

# 候选人偏好与限制
{preferences}

# 目标岗位
{context}

{resume_instruction}"""

        user_cover = f"""# 候选人简历
{selected_resume}

# 候选人偏好与限制
{preferences}

# 目标岗位
{context}

请写这封 cover letter。记住:不得编造,不要主动提签证。"""

        try:
            if light_tailoring:
                tailored = str(llm.complete(_LIGHT_RESUME_SYSTEM, user_resume, max_tokens=3000) or "")
            else:
                tailored = str(llm.complete(_RESUME_SYSTEM, user_resume, max_tokens=4096) or "")
            generated_resume = tailored if tailored.strip() else selected_resume
            (out_dir / "resume.md").write_text(generated_resume, encoding="utf-8")
            _write_summary(
                out_dir, s,
                keyword_coverage(s, generated_resume, selected_resume),
            )

            cover = llm.complete(_COVER_SYSTEM, user_cover, max_tokens=1500)
            (out_dir / "cover-letter.md").write_text(cover, encoding="utf-8")

            written.append(out_dir)
            s.artifact_path = str(out_dir)
            log.info("[%d/%d] %s — %s", idx, len(targets), job.company, job.title)
        except Exception as e:
            # 单个岗位生成失败不中断,summary 已经写了,回头可以单独重跑
            log.error("生成失败 %s — %s: %s", job.company, job.title, e)

    return written
