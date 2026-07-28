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


def _write_summary(out_dir: Path, s: Scored) -> None:
    job = s.job
    dup = "\n".join(f"- {u}" for u in job.duplicate_urls) or "- (无)"
    salary = job.salary_raw or "未列出"
    signal = {
        "explicit_yes": "✅ JD 明确提到可提供担保",
        "explicit_no": "⚠️ JD 提到 sponsorship 但疑似否定表述,投前请自行确认",
        "unknown": "— JD 未提及签证",
    }[s.sponsorship_signal]

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

## 投递前检查
- [ ] 简历里的每一句都属实
- [ ] cover letter 里公司相关的那句不是套话
- [ ] 确认这家不是中介重复挂的同一个岗位
- [ ] 投完在 LinkedIn 上找到 hiring manager 或 recruiter
""", encoding="utf-8")


def generate(scored: list[Scored], llm, resume: str, preferences: str,
             cfg: dict) -> list[Path]:
    threshold = cfg["scoring"]["generate_threshold"]
    max_gen = cfg["scoring"]["max_generate"]
    out_root = Path(cfg["paths"]["output_dir"]) / "applications"
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

        user_resume = f"""# 候选人原始简历
{selected_resume}

# 候选人偏好与限制
{preferences}

# 目标岗位
{context}

请针对这个岗位重组简历。记住:不得编造。"""

        user_cover = f"""# 候选人简历
{selected_resume}

# 候选人偏好与限制
{preferences}

# 目标岗位
{context}

请写这封 cover letter。记住:不得编造,不要主动提签证。"""

        try:
            # Broad applications deliberately use a pre-approved stable resume.
            # Rewriting it adds cost and can introduce unsupported claims; only
            # targeted applications may ask the model to reorganise resume facts.
            if s.application_mode == "broad" and s.resume_id:
                (out_dir / "resume.md").write_text(selected_resume, encoding="utf-8")
            else:
                tailored = llm.complete(_RESUME_SYSTEM, user_resume, max_tokens=4096)
                (out_dir / "resume.md").write_text(tailored, encoding="utf-8")

            cover = llm.complete(_COVER_SYSTEM, user_cover, max_tokens=1500)
            (out_dir / "cover-letter.md").write_text(cover, encoding="utf-8")

            written.append(out_dir)
            s.artifact_path = str(out_dir)
            log.info("[%d/%d] %s — %s", idx, len(targets), job.company, job.title)
        except Exception as e:
            # 单个岗位生成失败不中断,summary 已经写了,回头可以单独重跑
            log.error("生成失败 %s — %s: %s", job.company, job.title, e)

    return written
