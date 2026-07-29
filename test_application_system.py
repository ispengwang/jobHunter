"""离线验证 Candidate Profile、Dashboard 与申请决策基础层。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import shutil
import sys
import tempfile

import yaml

from application_policy import _min_years_required, decide_application_mode, freshness_bucket
from application_attempts import (
    ApplicationAttempts,
    configured_platform_limits,
    platform_limit_key,
    platform_submission_mode,
    submitted_today_by_platform,
)
from candidate_profile import UNKNOWN, load_or_initialise, save_profile
from dashboard import Dashboard
from resume_catalog import choose_resume, ensure_default_manifest, load_catalog
from schema import Job
from score import Scored, build_scoring_input, score_jobs


PASS = FAIL = 0


def check(name, got, want=True):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}\n       got={got!r}\n      want={want!r}")


def raises(name, fn, error):
    try:
        fn()
    except error:
        check(name, True)
    except Exception as exc:
        check(name, type(exc).__name__, error.__name__)
    else:
        check(name, False)


tmp = Path(tempfile.mkdtemp())
try:
    root = tmp
    profile_dir = root / "profile"
    profile_dir.mkdir()
    resume_path = profile_dir / "resume.md"
    resume_path.write_text(
        "# Jane Example\n\nEmail: jane@example.com\nPhone: +61 400 123 456\n"
        "LinkedIn: https://linkedin.com/in/jane-example\n"
        "Melbourne-based junior developer. Seeking $70k - $90k roles.\n",
        encoding="utf-8",
    )
    prefs = "Junior roles in Melbourne. 485 visa needs sponsorship."
    cfg = {
        "search": {"location": "Melbourne VIC", "terms": ["AI Product", "Junior Developer"]},
        "visa": {"status": "485_needs_sponsorship"},
        "application": {
            "priority_within_hours": 24, "recent_within_hours": 72,
            "eligible_seniority_keywords": ["graduate", "entry", "junior", "jnr"],
            "excluded_seniority_keywords": ["senior", "staff", "principal", "lead", "manager"],
            "max_years_experience": 2,
            "targeted_job_fit_threshold": 80, "targeted_resume_fit_threshold": 72,
            "broad_job_fit_threshold": 70,
        },
    }

    print("\n=== Candidate Profile ===")
    profile_path = profile_dir / "candidate_profile.md"
    profile = load_or_initialise(profile_path, resume_path.read_text(encoding="utf-8"), prefs, cfg)
    check("首次从简历提取姓名", profile.full_name, "Jane Example")
    check("首次从简历提取邮箱", profile.email, "jane@example.com")
    check("首次从配置提取签证", profile.visa_status, "485_needs_sponsorship")
    check("候选档案已持久化", profile_path.exists())
    check("匹配上下文不含邮箱", "jane@example.com" in profile.matching_context(), False)
    profile.full_name = "Jane Edited"
    save_profile(profile_path, profile)
    reloaded = load_or_initialise(profile_path, "# Different Resume", "", cfg)
    check("编辑后的档案不会被后续解析覆盖", reloaded.full_name, "Jane Edited")
    check("未知字段会保留 unknown", reloaded.work_right_expiry, UNKNOWN)

    print("\n=== Resume catalogue and policy ===")
    manifest_path = profile_dir / "resumes" / "manifest.yaml"
    ensure_default_manifest(manifest_path, "profile/resume.md")
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["resumes"].append({
        "id": "resume-ai-product", "path": "profile/resume.md", "label": "AI Product",
        "target_roles": ["AI Product"], "keywords": ["LLM", "experimentation"],
        "seniority": ["junior", "entry"],
    })
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    variants = load_catalog(manifest_path, root)
    now = datetime(2026, 7, 24, 12, tzinfo=timezone.utc)
    job = Job(
        source="seek", title="Junior AI Product Engineer", company="Acme",
        url="https://example.test/jobs/1", posted_date=(now - timedelta(hours=8)).isoformat(),
        description="AI Product experimentation with LLM workflows.",
    )
    selection = choose_resume(job, variants, root)
    check("选择角色匹配的简历版本", selection.resume_id, "resume-ai-product")
    check("简历匹配度达到精投阈值", selection.fit_score >= 72)
    scored = Scored(
        job, 88, "岗位匹配", ["LLM"], [], "unknown",
        summary="AI 产品工程岗位摘要",
    )
    decision = decide_application_mode(scored, selection, cfg, now)
    check("24 小时内岗位新鲜度", decision.freshness_bucket, "within_24h")
    check("高双匹配岗位走精投", decision.mode, "targeted")
    senior = Job("seek", "Senior AI Product Engineer", "Acme", "https://example.test/jobs/2", posted_date=job.posted_date)
    senior_decision = decide_application_mode(Scored(senior, 95, "x", [], [], "unknown"), selection, cfg, now)
    check("Senior 默认进入人工判断", senior_decision.mode, "manual_review")
    check("标题命中白名单并写入依据", "标题命中级别词 junior" in decision.reason)
    check("黑名单依据包含命中词", "senior" in senior_decision.reason)
    years_job = Job(
        "seek", "Software Engineer", "YearsCo", "https://example.test/jobs/years",
        description="This role requires 5+ years of professional experience.",
    )
    years_decision = decide_application_mode(
        Scored(years_job, 85, "x", [], [], "unknown"), selection, cfg, now,
    )
    check("独立函数提取 5 年要求", _min_years_required(years_job.description), 5)
    check("JD 要求 5 年进入人工判断", years_decision.mode, "manual_review")
    check("人工判断理由保留年限原文", "5+ years" in years_decision.reason)
    one_year_job = Job(
        "seek", "Software Engineer", "OneYearCo", "https://example.test/jobs/one-year",
        description="Minimum 1 year experience is preferred; mentoring is available.",
    )
    one_year_decision = decide_application_mode(
        Scored(one_year_job, 75, "x", [], [], "unknown"), selection, cfg, now,
    )
    check("独立函数提取 1 年要求", _min_years_required(one_year_job.description), 1)
    check("范围年限取最低值", _min_years_required("1-3 years experience"), 1)
    check("JD 要求 1 年可进入海投", one_year_decision.mode, "broad")
    check("1 年要求理由可解释", "1 year" in one_year_decision.reason)
    no_year_job = Job(
        "seek", "Software Engineer", "NoYearsCo", "https://example.test/jobs/no-years",
        description="Build customer-facing features with a supportive engineering team.",
    )
    no_year_decision = decide_application_mode(
        Scored(no_year_job, 75, "x", [], [], "unknown"), selection, cfg, now,
    )
    check("JD 无年限要求默认放行", no_year_decision.mode, "broad")
    check("无年限理由可解释", "未发现工作年限要求" in no_year_decision.reason)
    graduate_job = Job(
        "seek", "Software Engineer", "GraduateSignalCo", "https://example.test/jobs/graduate",
        description="This role welcomes new graduate applicants.",
    )
    graduate_decision = decide_application_mode(
        Scored(graduate_job, 75, "x", [], [], "unknown"), selection, cfg, now,
    )
    check("JD new graduate 信号放行", graduate_decision.mode, "broad")
    check("new graduate 理由可解释", "new graduate" in graduate_decision.reason)
    generic_job = Job(
        "seek", "Software Engineer", "GenericCo", "https://example.test/jobs/generic",
        description="Build useful software with a supportive engineering team.",
    )
    generic_selection = choose_resume(generic_job, variants, root)
    check("多变体通用岗位简历分数低于门槛", generic_selection.fit_score < 72)
    multi_variant_decision = decide_application_mode(
        Scored(generic_job, 85, "x", [], [], "unknown"), generic_selection, cfg, now,
        variant_count=len(variants),
    )
    check("多变体仍执行 resume-fit targeted 门槛", multi_variant_decision.mode, "broad")
    single_variant_decision = decide_application_mode(
        Scored(generic_job, 85, "x", [], [], "unknown"), generic_selection, cfg, now,
        variant_count=1,
    )
    check("单变体只凭 job-fit 进入 targeted", single_variant_decision.mode, "targeted")
    check("三天内岗位新鲜度", freshness_bucket((now - timedelta(hours=60)).isoformat(), cfg, now), "within_3d")
    check("更早岗位新鲜度", freshness_bucket((now - timedelta(days=6)).isoformat(), cfg, now), "older")
    check(
        "日期精度不足时用 first_seen_at 判断新鲜度",
        freshness_bucket(
            now.date().isoformat(), cfg, now,
            first_seen_at=(now - timedelta(hours=2)).isoformat(),
        ),
        "within_24h",
    )
    check(
        "无效发布时间也回退到 first_seen_at",
        freshness_bucket(
            "not-a-timestamp", cfg, now,
            first_seen_at=(now - timedelta(hours=2)).isoformat(),
        ),
        "within_24h",
    )
    rules = "# Stable rules\n" + ("Use verified facts and return structured scores. " * 12)
    deepseek_input = build_scoring_input(
        rules, preferences=prefs, candidate_context=reloaded.matching_context(), cfg=cfg,
    )
    check("DeepSeek 输入包含 Skill 固定规则", "Stable rules" in deepseek_input)
    check("DeepSeek 输入包含候选人偏好", prefs in deepseek_input)
    check("DeepSeek 输入不含联系方式", "jane@example.com" in deepseek_input, False)

    class CaptureDeepSeek:
        batch_size = 8
        max_description_chars = 4000

        def complete_json(self, system, user, max_tokens=4096):
            self.system = system
            self.user = user
            return [{
                "id": job.id, "score": 88, "summary": "AI 产品工程",
                "reason": "方向和基础能力匹配", "matched": ["LLM"], "missing": [],
                "sponsorship_signal": "unknown",
            }]

    deepseek = CaptureDeepSeek()
    api_scored = score_jobs([job], deepseek, resume_path.read_text(encoding="utf-8"),
                            deepseek_input, {"bonus_keywords": []})
    check("岗位匹配分来自 DeepSeek 返回", api_scored[0].score, 88)
    check("固定 Skill 规则实际发送给 DeepSeek", "Stable rules" in deepseek.user)

    class OmitsOneJobOnce:
        batch_size = 8
        max_description_chars = 4000

        def __init__(self):
            self.calls = 0
            self.requested_ids = []

        def complete_json(self, system, user, max_tokens=4096):
            import re

            self.calls += 1
            ids = re.findall(r'<job id="([^"]+)">', user)
            self.requested_ids.append(ids)
            if self.calls == 1:
                ids = ids[:1]
            return [{
                "id": job_id, "score": 76, "summary": "测试岗位",
                "reason": "测试", "matched": [], "missing": [],
                "sponsorship_signal": "unknown",
            } for job_id in ids]

    second_job = Job(
        source="seek", title="Junior Software Engineer", company="Beta",
        url="https://example.test/jobs/retry", posted_date=job.posted_date,
        description="Junior TypeScript and Node.js role.",
    )
    omits_once = OmitsOneJobOnce()
    retried_scores = score_jobs(
        [job, second_job], omits_once, resume_path.read_text(encoding="utf-8"),
        deepseek_input, {"bonus_keywords": []},
    )
    check("DeepSeek 漏项只重试一次", omits_once.calls, 2)
    check("漏项重试只发送缺失岗位", omits_once.requested_ids[1], [second_job.id])
    check("漏项重试后没有无效分数", all(item.score >= 0 for item in retried_scores))

    print("\n=== Broad application materials ===")
    from generate import generate

    class MaterialsLLM:
        max_description_chars = 4000

        def __init__(self):
            self.calls = 0

        def complete(self, system, user, max_tokens=4096):
            self.calls += 1
            return "Verified cover letter"

    broad = Scored(job, 78, "可海投", ["React"], [], "unknown")
    broad.resume_id = "resume-default"
    broad.resume_path = str(resume_path)
    broad.resume_fit_score = 64
    broad.application_mode = "broad"
    material_llm = MaterialsLLM()
    material_cfg = {
        "scoring": {"generate_threshold": 70, "max_generate": 5},
        "paths": {"output_dir": str(root / "material-output")},
    }
    broad_written = generate(
        [broad], material_llm, resume_path.read_text(encoding="utf-8"), prefs, material_cfg,
    )
    check("海投材料已生成", len(broad_written), 1)
    check(
        "海投使用原始已审核简历",
        (broad_written[0] / "resume.md").read_text(encoding="utf-8"),
        resume_path.read_text(encoding="utf-8"),
    )
    check("海投只调用模型生成求职信", material_llm.calls, 1)
    relative_material_cfg = {
        "scoring": {"generate_threshold": 70, "max_generate": 5},
        "paths": {"output_dir": "material-output"},
    }
    relative_written = generate(
        [broad], material_llm, resume_path.read_text(encoding="utf-8"), prefs,
        relative_material_cfg, root=root,
    )
    check("相对输出路径锚定传入的项目根", relative_written[0].is_relative_to(root.resolve() / "material-output"))

    print("\n=== Dashboard ===")
    dashboard = Dashboard(root / "data" / "application-dashboard.csv", root / "data" / "application-events.csv")
    row = dashboard.upsert_recommendation(
        job, job_fit_score=scored.score, job_fit_reason=scored.reason,
        job_summary=scored.summary,
        sponsorship_signal=scored.sponsorship_signal, resume_id=selection.resume_id,
        resume_fit_score=selection.fit_score, resume_reason=selection.reason,
        application_mode=decision.mode, freshness_bucket=decision.freshness_bucket, run_id="run-1",
        resume_path=str(selection.path),
    )
    check("首次推荐进入 review", row["status"], "review")
    check("首次推荐记录 first_seen_at", bool(row["first_seen_at"]), True)
    before_sync = dict(row)
    event_count = len(dashboard.events_for(job.id))
    touched = dashboard.sync_existing_jobs([job], run_id="incremental-1")
    after_sync = dashboard.get(job.id)
    check("增量同步命中已有岗位", touched, 1)
    check("旧岗位增量不改评分", after_sync["job_fit_score"], before_sync["job_fit_score"])
    check("旧岗位增量不改状态", after_sync["status"], before_sync["status"])
    check("旧岗位保留首次发现时间", after_sync["first_seen_at"], before_sync["first_seen_at"])
    check("旧岗位记录同步运行 ID", after_sync["last_run_id"], "incremental-1")
    check("旧岗位增量不新增事件", len(dashboard.events_for(job.id)), event_count)
    skill_path = root / "applypilot-au"
    (skill_path / "references").mkdir(parents=True)
    (skill_path / "SKILL.md").write_text("---\nname: applypilot-au\ndescription: test\n---\n", encoding="utf-8")
    (skill_path / "references" / "deepseek-scoring-rules.md").write_text(rules, encoding="utf-8")
    attempts = ApplicationAttempts(
        root / "data" / "application-attempts.csv", dashboard,
        skill_path=skill_path, project_root=root,
    )
    attempt = attempts.create(job.id, reloaded, profile_path)
    check("Agent 自动创建内部执行记录", attempt["status"], "selected")
    check("选择记录不等于资料外发授权", attempt["data_transmission_confirmed"], "")
    check("SEEK 默认由 Skill 检查后决定是否自动", attempt["platform_mode"], "auto_if_allowed")
    check("LinkedIn 默认 assisted", platform_submission_mode("linkedin"), "assisted")
    check("Indeed 默认 assisted", platform_submission_mode("indeed"), "assisted")
    check(
        "自定义 assisted host 生效",
        platform_submission_mode(
            "custom", "https://forms.example/jobs/1",
            {"assisted_hosts": ["forms.example"], "manual_hosts": ["manual.example"]},
        ),
        "assisted",
    )
    check(
        "自定义 manual host 生效",
        platform_submission_mode(
            "custom", "https://manual.example/jobs/1",
            {"assisted_hosts": ["forms.example"], "manual_hosts": ["manual.example"]},
        ),
        "manual_submit",
    )
    check("LinkedIn Easy Apply 计入 LinkedIn 限额", platform_limit_key("linkedin", "https://www.linkedin.com/jobs/1"), "linkedin")
    check("LinkedIn 外部 ATS 计入 external ATS 限额", platform_limit_key("linkedin", "https://boards.greenhouse.io/acme/1"), "external_ats")
    check("默认平台限额", configured_platform_limits(), {"linkedin": 8, "indeed": 8, "external_ats": 40})
    today_submission = datetime.now(timezone.utc).isoformat()
    platform_counts = submitted_today_by_platform([
        {"status": "submitted", "submission_evidence": "ok", "submitted_at": today_submission,
         "source": "linkedin", "url": "https://www.linkedin.com/jobs/1"},
        {"status": "submitted", "submission_evidence": "ok", "submitted_at": today_submission,
         "source": "indeed", "url": "https://www.indeed.com/jobs/2"},
        {"status": "submitted", "submission_evidence": "ok", "submitted_at": today_submission,
         "source": "linkedin", "url": "https://boards.greenhouse.io/acme/3"},
    ], "Australia/Melbourne")
    check("已提交按平台分别计数", platform_counts, {"linkedin": 1, "indeed": 1, "external_ats": 1})
    check("Agent 选择后岗位进入待投递", dashboard.get(job.id)["status"], "ready_to_apply")
    duplicate = attempts.create(job.id, reloaded, profile_path)
    check("同一岗位不会重复创建活动交接", duplicate["attempt_id"], attempt["attempt_id"])

    second_seek = Job(
        "seek", "Graduate AI Engineer", "Beta", "https://example.test/jobs/3",
        posted_date=job.posted_date, description="AI product engineering",
    )
    linked_in = Job(
        "linkedin", "Junior AI Engineer", "Gamma", "https://www.linkedin.com/jobs/view/4",
        posted_date=job.posted_date, description="AI product engineering",
    )
    for candidate_job, candidate_score in [(second_seek, 86), (linked_in, 95)]:
        dashboard.upsert_recommendation(
            candidate_job, job_fit_score=candidate_score, job_fit_reason="匹配",
            job_summary="AI 工程岗位摘要",
            sponsorship_signal="unknown", resume_id=selection.resume_id,
            resume_fit_score=selection.fit_score, resume_reason=selection.reason,
            application_mode="targeted", freshness_bucket="within_24h",
            resume_path=str(selection.path), run_id="agent-run",
        )
    auto_selection = attempts.select_eligible(reloaded, profile_path, max_new=5)
    check("Agent 自动选择 SEEK 可执行岗位和 LinkedIn assisted 岗位", len(auto_selection["selected"]), 2)
    check("LinkedIn 不再落入 manual_only", len(auto_selection["manual_only"]), 0)
    check(
        "SEEK 原站默认手动操作",
        platform_submission_mode("seek", "https://www.seek.com.au/job/123"),
        "manual_submit",
    )
    check(
        "直接外部 ATS 可交给 ApplyPilot 核验",
        platform_submission_mode("seek", "https://boards.greenhouse.io/acme/jobs/123"),
        "auto_if_allowed",
    )
    check("自动执行选择记录对应最高可自动平台岗位", auto_selection["selected"][0]["job_id"], second_seek.id)
    runnable_scores = [
        int(dashboard.get(item["job_id"])["job_fit_score"])
        for item in auto_selection["runnable"]
    ]
    check("自动执行清单按分数降序", runnable_scores, sorted(runnable_scores, reverse=True))
    older_seek = Job(
        "seek", "Graduate Platform Engineer", "Delta", "https://example.test/jobs/5",
        posted_date=(now - timedelta(days=5)).isoformat(),
        description="Graduate platform engineering",
    )
    fresh_seek = Job(
        "seek", "Graduate Web Engineer", "Epsilon", "https://example.test/jobs/6",
        posted_date=(now - timedelta(hours=2)).isoformat(),
        description="Graduate web engineering",
    )
    fresh_seek_buffer = Job(
        "seek", "Junior Product Engineer", "Zeta", "https://example.test/jobs/7",
        posted_date=(now - timedelta(hours=3)).isoformat(),
        description="Junior product engineering",
    )
    for candidate_job, candidate_score, freshness in [
        (older_seek, 99, "older"),
        (fresh_seek, 74, "within_24h"),
        (fresh_seek_buffer, 72, "within_24h"),
    ]:
        dashboard.upsert_recommendation(
            candidate_job, job_fit_score=candidate_score, job_fit_reason="匹配",
            job_summary="测试 JD 摘要", sponsorship_signal="unknown",
            resume_id=selection.resume_id, resume_fit_score=selection.fit_score,
            resume_reason=selection.reason, application_mode="broad",
            freshness_bucket=freshness, resume_path=str(selection.path), run_id="freshness-run",
        )
    freshness_selection = attempts.select_eligible(
        reloaded, profile_path, max_new=2, soft_max_new=1,
    )
    check(
        "24h 岗位可使用软目标到硬上限之间的缓冲",
        [item["job_id"] for item in freshness_selection["selected"]],
        [fresh_seek.id, fresh_seek_buffer.id],
    )
    mixed_selection = attempts.select_eligible(
        reloaded, profile_path, max_new=1, soft_max_new=1,
    )
    freshness_order = [
        dashboard.get(item["job_id"])["freshness_bucket"]
        for item in mixed_selection["runnable"]
    ]
    first_older = next(
        (index for index, bucket in enumerate(freshness_order) if bucket != "within_24h"),
        len(freshness_order),
    )
    check(
        "执行清单把所有 24h 岗位排在旧岗位之前",
        all(bucket == "within_24h" for bucket in freshness_order[:first_older]),
    )
    raises("浏览器异常必须填写原因", lambda: attempts.advance(
        attempt["attempt_id"], "needs_user", actor="system"), ValueError)
    paused = attempts.advance(
        attempt["attempt_id"], "needs_user", actor="system", reason="CAPTCHA needs manual takeover")
    check("CAPTCHA 转为人工接管", paused["status"], "needs_user")
    check("人工接管同步 Dashboard needs_user", dashboard.get(job.id)["status"], "needs_user")
    handoff = attempts.handoff_payload(attempt["attempt_id"])
    check("浏览器交接不复制邮箱等敏感资料", "jane@example.com" in str(handoff), False)
    check("浏览器交接包含选定简历路径", handoff["resume_path"], str(selection.path))
    check("浏览器交接明确由 ApplyPilot 执行", handoff["executor"], "applypilot-au")
    check("启动提示词明确引用 Skill", "applypilot-au" in attempts.launch_prompt(attempt["attempt_id"]))
    handoff_list = attempts.handoff_list()
    check("handoff 清单只含 selected/queued", all(
        item["attempt_id"] != attempt["attempt_id"] for item in handoff_list
    ))
    check("handoff 清单包含待执行岗位", any(
        item["job_fit_score"] == "86" for item in handoff_list
    ))
    check("handoff 清单不含候选人邮箱", "jane@example.com" in str(handoff_list), False)
    dashboard.upsert_recommendation(
        job, job_fit_score=91, job_fit_reason="重新评分", sponsorship_signal="unknown",
        job_summary="更新后的 JD 摘要",
        resume_id=selection.resume_id, resume_fit_score=selection.fit_score,
        resume_reason=selection.reason, application_mode=decision.mode,
        freshness_bucket=decision.freshness_bucket, resume_path=str(selection.path), run_id="run-2",
    )
    rows = dashboard.load_rows()
    check("同一岗位幂等合并为一条", sum(1 for item in rows if item["job_id"] == job.id), 1)
    check("重新评分保留人工状态", rows[0]["status"], "needs_user")
    check("Dashboard 记录最近同步时间", bool(rows[0].get("last_synced_at")))
    raises("blocked 状态必须填写原因", lambda: dashboard.transition(job.id, "blocked", actor="user"), ValueError)
    raises("submitted 需要明确成功结果", lambda: dashboard.transition(job.id, "submitted", actor="user"), ValueError)
    raises("submitted 需要提交证据", lambda: dashboard.transition(
        job.id, "submitted", actor="user", submission_confirmed=True), ValueError)
    raises("ApplyPilot submitted 同样需要证据", lambda: attempts.advance(
        attempt["attempt_id"], "submitted", actor="applypilot", submission_confirmed=True), ValueError)
    attempts.advance(
        attempt["attempt_id"], "submitted", actor="applypilot", submission_confirmed=True,
        submission_evidence="Application submitted confirmation page",
    )
    submitted = dashboard.get(job.id)
    check("明确提交后写入状态", submitted["status"], "submitted")
    check("明确提交后写入时间", bool(submitted["submitted_at"]))
    check("明确提交后写入证据", bool(submitted["submission_evidence"]))
    check("事件日志记录状态变化", len(dashboard.events_for(job.id)) >= 3)

    print("\n=== Web app routes ===")
    app_cfg_path = root / "webapp-config.yaml"
    app_cfg = {
        "paths": {
            "resume": str(resume_path), "preferences": str(profile_dir / "preferences.md"),
            "candidate_profile": str(profile_path), "resume_manifest": str(manifest_path),
            "dashboard": str(root / "data" / "application-dashboard.csv"),
            "application_events": str(root / "data" / "application-events.csv"),
            "application_attempts": str(root / "data" / "application-attempts.csv"),
            "scoring_rule_backups": str(root / "data" / "scoring-rule-backups"),
            "output_dir": str(root / "output"),
        },
        "search": {"terms": ["Junior Developer"], "location": "Melbourne VIC", "hours_old": 72},
        "sources": {"linkedin": True, "indeed": True, "seek": True},
        "visa": {"exclude_keywords": [], "bonus_keywords": [], "status": "485_needs_sponsorship"},
        "llm": {"provider": "deepseek"}, "scoring": {"generate_threshold": 70},
        "applypilot": {
            "skill_path": str(skill_path), "auto_submit_enabled": True,
            "timezone": "Australia/Melbourne", "soft_target_per_day": 30,
            "hard_cap_per_day": 40,
        },
        "application": cfg["application"],
    }
    (profile_dir / "preferences.md").write_text("Junior roles", encoding="utf-8")
    app_cfg_path.write_text(yaml.safe_dump(app_cfg, sort_keys=False), encoding="utf-8")
    import webapp
    old_config_path = webapp.CONFIG_PATH
    webapp.CONFIG_PATH = app_cfg_path
    try:
        client = webapp.app.test_client()
        check(
            "Dashboard 可解析评分维度权重",
            webapp._extract_score_dimensions("| Role fit | 0–30 |"),
            [{"name": "Role fit", "points": "0–30"}],
        )
        check("Candidate Profile 页面可打开", client.get("/profile").status_code, 200)
        rules_page = client.get("/scoring-rules")
        check("评分规则页面可打开", rules_page.status_code, 200)
        check("评分规则页面显示完整 Skill 规则", "Stable rules" in rules_page.get_data(as_text=True))
        check("Dashboard 页面可打开", client.get("/dashboard").status_code, 200)
        dashboard_html = client.get("/dashboard").get_data(as_text=True)
        check("Dashboard 显示最近同步入口", "最近同步" in dashboard_html)
        check("Dashboard 提供最近同步筛选", 'data-v="latest"' in dashboard_html)
        check("Dashboard 页面禁止缓存", client.get("/dashboard").headers.get("Cache-Control"), "no-store, max-age=0")
        sync_status = client.get("/dashboard/status")
        check("Dashboard 同步状态接口可读取", sync_status.status_code, 200)
        check("Dashboard 同步状态接口禁止缓存", sync_status.headers.get("Cache-Control"), "no-store, max-age=0")
        stale_rows = webapp._unified_dashboard_rows(
            [{
                "job_id": "stale", "title": "Old posting", "company": "Example",
                "source": "seek", "url": "https://example.test/stale",
                "posted_at": (datetime(2026, 7, 22, 12, tzinfo=timezone.utc)).isoformat(),
                "freshness_bucket": "within_24h", "status": "review",
            }], set(), {}, timezone_name="Australia/Melbourne",
            freshness_config=app_cfg,
            now=datetime(2026, 7, 28, 12, tzinfo=timezone.utc),
        )
        check("Dashboard 按当前时间重算过期新鲜度", stale_rows[0]["freshness_bucket"], "older")
        check("Dashboard 不再显示手动队列按钮", "加入 ApplyPilot 投递队列" in dashboard_html, False)
        check("Dashboard 不再要求复制启动提示词", "复制启动提示词" in dashboard_html, False)
        check("Dashboard 说明点击后只加入本地清单", "投递清单 · 点击后只记录本地任务" in dashboard_html)
        check("Dashboard 显示加入投递清单按钮", "加入投递清单" in dashboard_html)
        check("Dashboard 不启动后台 ApplyPilot", "直接启动 ApplyPilot" in dashboard_html, False)
        check("Dashboard 显示确认已提交入口", "确认已提交" in dashboard_html)
        check("Dashboard 保留状态转换入口", "转换申请状态" in dashboard_html)
        check("Dashboard 状态编辑只渲染一个共享表单", dashboard_html.count('id="status-form"'), 1)
        check("Dashboard 不再为每张卡片渲染 details 表单", "<details" in dashboard_html, False)
        check("Dashboard 使用渐进加载避免首屏过长", 'id="load-more"' in dashboard_html)
        check("Dashboard 显示 JD 摘要", "更新后的 JD 摘要" in dashboard_html)
        check("Dashboard 显示具体发布日期", "发布于 2026-07-24 14:00 AEST" in dashboard_html)
        check("Dashboard 状态筛选只保留四类", all(
            token in dashboard_html for token in [
                'data-v="ready_to_apply"', 'data-v="submitted"',
                'data-v="rejected"', 'data-v="interview"',
            ]
        ))
        check("Dashboard 不再显示内部状态筛选", all(
            token not in dashboard_html for token in [
                'data-v="review"', 'data-v="applying"',
                'data-v="needs_user"', 'data-v="blocked"',
            ]
        ))
        check("旧 ApplyPilot 页面重定向 Dashboard", client.get("/applypilot").status_code, 302)
        check("旧投递地址重定向 Dashboard", client.get("/attempts").status_code, 302)
        check("ApplyPilot 交接 JSON 可读取", client.get(
            f"/applypilot/{attempt['attempt_id']}/handoff").status_code, 200)
        detail = client.get(f"/dashboard/{job.id}")
        check("Dashboard 详情页可打开", detail.status_code, 200)
        check("详情页显示选择的简历", "resume-ai-product" in detail.get_data(as_text=True))
        check("详情状态表单带动作令牌", webapp.ACTION_TOKEN in detail.get_data(as_text=True))
        unscored_rows = webapp._unified_dashboard_rows([{
            "job_id": "unscored", "title": "Needs scoring", "company": "Example",
            "source": "linkedin", "url": "https://example.test/unscored",
            "status": "review", "freshness_bucket": "unknown",
        }], set(), {})
        check("未评分岗位在最低分 -1 时仍可见", unscored_rows[0]["score_int"], "-1")

        missing_status_token = client.post(
            f"/dashboard/{linked_in.id}/transition",
            data={"status": "submitted"},
        )
        check("状态转换需要页面动作令牌", missing_status_token.status_code, 403)
        second_seek_events_before = len(
            webapp._dashboard(webapp.load_config()).events_for(second_seek.id)
        )
        same_status = client.post(
            f"/dashboard/{second_seek.id}/transition",
            data={
                "action_token": webapp.ACTION_TOKEN,
                "return_to": "dashboard",
                "status": "ready_to_apply",
            },
        )
        second_seek_events_after = len(
            webapp._dashboard(webapp.load_config()).events_for(second_seek.id)
        )
        check("相同状态不会生成冗余事件", second_seek_events_after, second_seek_events_before)
        check("相同状态返回清晰错误", "error=" in same_status.headers.get("Location", ""))

        missing_token = client.post(
            f"/dashboard/{linked_in.id}/applypilot",
            headers={"Accept": "application/json"},
        )
        check("投递按钮需要页面动作令牌", missing_token.status_code, 403)
        manual_start = client.post(
            f"/dashboard/{linked_in.id}/applypilot",
            data={"action_token": webapp.ACTION_TOKEN},
            headers={"Accept": "application/json"},
        )
        manual_payload = manual_start.get_json()
        check("加入清单保留 assisted 平台模式", manual_payload["platform_mode"], "assisted")
        check("加入清单不返回外部打开地址", "open_url" in manual_payload, False)
        linked_attempt = webapp._attempts(webapp.load_config()).get(manual_payload["attempt_id"])
        check("点击按钮不代表资料外发授权", linked_attempt["data_transmission_confirmed"], "")
        check("点击按钮只创建 selected 记录", linked_attempt["status"], "selected")
        missing_evidence = client.post(
            f"/dashboard/{linked_in.id}/transition",
            data={
                "action_token": webapp.ACTION_TOKEN,
                "return_to": "dashboard",
                "status": "submitted",
                "submission_confirmed": "on",
            },
        )
        check("手动确认提交仍要求成功证据", missing_evidence.status_code, 302)
        check(
            "缺少证据不会误记提交",
            webapp._dashboard(webapp.load_config()).get(linked_in.id)["status"],
            "ready_to_apply",
        )
        confirmed_submission = client.post(
            f"/dashboard/{linked_in.id}/transition",
            data={
                "action_token": webapp.ACTION_TOKEN,
                "return_to": "dashboard",
                "status": "submitted",
                "submission_confirmed": "on",
                "submission_evidence": "Application received · confirmation AU-123",
            },
        )
        check("用户可从 Dashboard 确认已提交", confirmed_submission.status_code, 302)
        confirmed_row = webapp._dashboard(webapp.load_config()).get(linked_in.id)
        check("确认提交写入主状态", confirmed_row["status"], "submitted")
        check("确认提交写入时间", bool(confirmed_row["submitted_at"]))
        check("确认提交保存证据", confirmed_row["submission_evidence"], "Application received · confirmation AU-123")
        confirmed_attempt = webapp._attempts(webapp.load_config()).get(manual_payload["attempt_id"])
        check("确认提交同步内部 attempt", confirmed_attempt["status"], "submitted")
        converted_status = client.post(
            f"/dashboard/{linked_in.id}/transition",
            data={
                "action_token": webapp.ACTION_TOKEN,
                "return_to": "dashboard",
                "status": "interview",
                "reason": "收到第一轮面试邀请",
            },
        )
        check("用户可转换申请状态", converted_status.status_code, 302)
        converted_row = webapp._dashboard(webapp.load_config()).get(linked_in.id)
        check("状态转换写入面试中", converted_row["status"], "interview")
        check("状态转换保留提交证据", converted_row["submission_evidence"], "Application received · confirmation AU-123")

        automatic_start = client.post(
            f"/dashboard/{second_seek.id}/applypilot",
            data={"action_token": webapp.ACTION_TOKEN},
            headers={"Accept": "application/json"},
        )
        automatic_payload = automatic_start.get_json()
        check("外部 ATS 点击后只加入清单", automatic_payload["attempt_status"], "selected")
        check("加入清单响应不含后台 executor", "executor_state" in automatic_payload, False)
        check("旧后台状态轮询路由已移除", client.get(
            f"/applypilot/{automatic_payload['attempt_id']}/status"
        ).status_code, 404)
        valid_rules = rules + "\n" + " id score summary reason matched missing sponsorship_signal " * 8
        saved_rules = client.post("/scoring-rules/save", data={
            "expected_hash": webapp._rules_hash(rules),
            "content": valid_rules,
        })
        check("用户可保存经审核的评分规则", saved_rules.status_code, 302)
        check(
            "保存评分规则前自动备份",
            len(list((root / "data" / "scoring-rule-backups").glob("*.md"))),
            1,
        )
    finally:
        webapp.CONFIG_PATH = old_config_path
finally:
    shutil.rmtree(tmp)


print(f"\n=== result: {PASS} passed, {FAIL} failed ===")
if FAIL:
    sys.exit(1)
