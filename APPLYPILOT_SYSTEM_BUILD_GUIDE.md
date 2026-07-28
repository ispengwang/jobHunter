# ApplyPilot / JobHunter 后续系统搭建指南

> 本文是后续开发的产品说明与实施合同。它把“候选人档案、投递仪表盘、筛选规则、简历策略”落成可执行的文件结构、数据字段和 Codex 工作流程。
>
> 适用项目：当前仓库 `jobhunt-au`。当前项目已经具备岗位抓取、去重、签证过滤、LLM 打分和可选的申请材料生成；本文指导在此基础上继续搭建“可追踪的求职工作流”，不要求重写现有抓取器。

## 1. 目标与边界

### 目标

系统每次运行后，都应该能回答四个问题：

1. 我是谁，正在找什么工作，有哪些不可违反的限制？
2. 这次发现了哪些岗位，为什么保留或跳过？
3. 每个岗位应该使用哪一份简历，理由是什么？
4. 哪些岗位已经投递、待投递、跳过或卡住，下一步是什么？

### 自动化边界

自动化负责：

- 抓取和去重公开岗位；
- 根据候选人档案和筛选规则进行排序、解释和推荐；
- 选择简历版本、生成可供人工审核的申请材料；
- 更新仪表盘和操作日志。
- 在一次 Skill 调用内自动搜索、调用 DeepSeek 打分、更新 Dashboard，并为当前 Agent 生成内部执行记录。

JobHunter 不操作招聘网站。实际平台判断、资料校验、每日限额、浏览器执行和证据记录由 `applypilot-au` Skill 负责。内部执行记录不是资料外发授权、浏览器操作或提交，用户无需操作队列或复制提示词。验证码、未知开放题、账号/权限异常和平台安全控制必须人工接管。不要把评论区“暂时没有被风控”的个案当作合规依据，也不要绕过 LinkedIn、Indeed 或其他平台当前的服务条款、频率限制和反自动化措施。

系统的核心原则是：**JobHunter 搜索展示，ApplyPilot 决策执行；所有状态有证据、可追踪。**

## 2. 当前项目与目标系统的对应关系

| 现有能力 | 现有位置 | 后续扩展 |
| --- | --- | --- |
| 岗位统一数据结构 | `schema.py` | 增加稳定的 `job_id`、岗位快照和筛选结果字段 |
| 岗位抓取 | `scrapers/`、`run.py` | 保持现有来源；增加运行批次标识和抓取时间 |
| 去重 | `dedupe.py` | 用 canonical job key 连接仪表盘，避免重复记录 |
| 签证过滤与 LLM 打分 | `score.py` | 把硬规则、软评分和“需要人工判断”分开 |
| 简历/求职信生成 | `generate.py` | 从简历目录中选择版本，禁止补造经历 |
| 结果展示 | `output/jobs-ranked.md`、`webapp.py` | 增加仪表盘、状态更新、跳过原因和下一步动作 |
| 个人事实来源 | `profile/resume.md`、`profile/preferences.md` | 新增结构化候选人档案和多份简历清单 |

## 3. 推荐目录与数据职责

后续实现建议采用下面的目录。第一阶段可以只创建文件和 CSV，暂时不引入数据库。

```text
profile/
  candidate_profile.md       # 联系方式、所在地、求职方向、签证等事实
  preferences.md             # 求职偏好、硬规则、软规则和评分说明
  resume.md                  # 现有完整事实简历，仍是事实来源之一
  resumes/
    manifest.yaml            # 各简历版本的元数据
    resume-ai-product.md
    resume-growth-pm.md
    resume-fullstack.md
    ...

data/
  application-dashboard.csv  # 持久化的岗位申请状态；不可因重新抓取而清空
  application-events.csv     # 追加式操作日志，记录 Codex 做过什么

output/
  jobs-raw.csv
  jobs-ranked.csv
  jobs-ranked.md
  applications/
    <job-id>-<company>-<title>/
      00-summary.md
      resume.md
      cover-letter.md
```

`output/` 是可重新生成的结果目录；`data/` 是系统记忆，不能把仪表盘只放在 `output/` 中，否则下一轮抓取会丢失历史。

## 4. 模块一：候选人档案 Candidate Profile

### 4.1 设计要求

候选人档案是唯一的“我是谁”来源。Codex 必须先读取档案，再处理岗位；档案缺字段时只能标记 `unknown` 或请求人工补充，不能自行推测。

特别不能猜测：

- 姓名、邮箱、电话、LinkedIn URL；
- 所在城市、工作权利、签证有效期和是否需要未来担保；
- 学历、工作年限、项目经历、技术熟练程度；
- 期望薪资、入职时间和是否接受搬迁；
- 任何“看起来合理但档案没有写”的业绩数字。

### 4.2 `profile/candidate_profile.md` 最小模板

后续创建文件时使用下面的字段，不要把真实隐私写入本指南：

```markdown
# Candidate Profile

## Identity
- Full name:
- Preferred name:
- Email:
- Phone:
- LinkedIn URL:
- Portfolio / GitHub:

## Location and availability
- Current location:
- Work arrangement: onsite / hybrid / remote / unknown
- Open to relocation: yes / no / ask me
- Earliest start date:

## Work rights
- Current visa / work-right status:
- Work-right expiry:
- Requires sponsorship now: yes / no / unknown
- May require sponsorship later: yes / no / unknown
- Evidence or notes:

## Search target
- Primary directions:
- Secondary directions:
- Seniority: new grad / entry level / junior / mid / other
- Preferred locations:
- Minimum salary (AUD, annual):
- Target salary range (AUD, annual):
- Industries to prioritise:
- Industries to avoid:

## Truth constraints
- Claims that may be used:
- Claims that require my confirmation:
- Skills I have only studied or tried:
- Skills / experience I do not have:
- Last reviewed: YYYY-MM-DD
```

### 4.3 读取和校验规则

实现 `CandidateProfile` 或等价的解析器时，至少要做以下校验：

- 必填字段为空时在运行开始阶段报错或显示待补充清单；
- 薪资统一为 AUD 年薪，无法解析时保留 `unknown`，不自行换算；
- 日期使用 ISO 格式 `YYYY-MM-DD`；
- `yes / no / unknown` 三态字段不能被空字符串替代；
- 运行日志只显示脱敏后的邮箱和电话，API 日志不得输出完整隐私字段。

## 5. 模块二：投递仪表盘 Dashboard

### 5.1 仪表盘是系统记忆

每个岗位只允许有一条主记录。重新抓取到同一岗位时，应根据 `job_id` 或 canonical key 更新岗位信息，而不是新增一条“看起来不同”的申请。

仪表盘至少要知道：岗位是谁家的、链接是什么、何时发现、匹配度如何、用了哪份简历、当前状态、为什么跳过或卡住、最近一次动作是什么。

### 5.2 `data/application-dashboard.csv` 建议字段

| 字段 | 说明 |
| --- | --- |
| `job_id` | 由岗位 URL 或归一化公司+职位生成的稳定 ID |
| `company` | 公司名称 |
| `title` | 职位名称 |
| `source` | `linkedin` / `indeed` / `seek` / 其他 |
| `url` | 主投递链接 |
| `duplicate_urls` | 同岗位的其他来源链接 |
| `location` | 地点和工作模式 |
| `posted_at` | 岗位发布日期，未知则留空 |
| `discovered_at` | 本系统发现时间 |
| `match_score` | 0–100；不是录用概率 |
| `sponsorship_signal` | `explicit_yes` / `explicit_no` / `unknown` |
| `resume_id` | 选中的简历版本 |
| `resume_reason` | 选择该版本的简短解释 |
| `status` | 见下方状态机 |
| `skip_reason` | 跳过时必填 |
| `blocked_reason` | 卡住时必填 |
| `needs_user_reason` | 需要用户处理时必填 |
| `submitted_at` | 实际提交时间；未提交为空 |
| `submission_evidence` | 平台成功页文本或确认 URL |
| `next_action` | 下一步动作和截止时间 |
| `last_action` | 最近一次人工或 Codex 动作 |
| `last_updated_at` | 最近更新时间 |
| `notes` | 人工备注 |

### 5.3 状态机

内部状态使用稳定英文值，界面可以显示中文：

```text
new → review → ready_to_apply → applying → submitted → follow_up
       │             │             │           │
       ├→ skipped    └→ needs_user └→ blocked  └→ rejected / interview / withdrawn
```

主 Dashboard 将这些详细执行状态投影成四个用户状态：`ready_to_apply`（待投递）、
`submitted`（已提交）、`rejected`（被拒绝）、`interview`（面试中）。内部的
`review`、`applying`、`needs_user`、`blocked` 等状态和原因继续保留用于审计，
不会因为界面精简而删除。

最小可用状态：

| 内部值 | 展示名 | 进入条件 |
| --- | --- | --- |
| `review` | 待审核 | 已发现，尚未决定是否准备申请 |
| `ready_to_apply` | 待投递 | 通过筛选，材料已准备并经人工检查 |
| `needs_user` | 需要你处理 | 缺资料、验证、未知问题或其他必须由用户完成的动作 |
| `submitted` | 已提交 | 平台显示明确成功证据 |
| `skipped` | 已跳过 | 明确决定不投，必须填写原因 |
| `blocked` | 卡住 | 链接失效、资格不明、需要补充信息或平台环节无法继续 |

`submitted` 不能由 Agent 选择、生成材料、打开链接、自动填表、上传简历或仅点击 Submit 触发，只能在平台出现明确成功证据后写入。`skipped`、`blocked` 和 `needs_user` 必须有原因，不能用模糊的“AI decided”代替。

### 5.4 仪表盘写入协议

每次运行遵循以下顺序：

1. 读取现有仪表盘，不覆盖历史记录。
2. 用 canonical key 合并新岗位。
3. 对新增岗位写入 `review`，记录 `discovered_at` 和运行批次。
4. 写入筛选结论、匹配分、签证信号和跳过/卡住原因。
5. 只有材料通过人工检查后才改为 `ready_to_apply`。
6. 平台出现明确成功证据后，再记录 `submitted_at`、`submission_evidence` 和 `submitted`。
7. 每次状态变化同时追加一条 `application-events.csv` 事件。

事件日志建议字段：`event_id, job_id, run_id, timestamp, actor, from_status, to_status, action, reason, artifact_path`。`actor` 使用 `codex`、`user` 或 `system`，这样可以清楚区分 AI 做了什么和用户亲自做了什么。

## 6. 模块三：筛选规则

岗位匹配分必须由配置的 DeepSeek API 生成。固定评分规则存放在已安装
`applypilot-au/references/deepseek-scoring-rules.md` 中，普通运行不得重新生成或静默修改；
当前 Agent 只负责把规则、脱敏匹配上下文、简历和 JD 交给 DeepSeek，并验证结构化返回。
确定性硬过滤仍在 API 调用前执行。

用户可在本地 `/scoring-rules` 页面查看和编辑完整规则。保存是明确的人工操作，
系统先备份旧版本并校验 DeepSeek JSON 输出字段；下一轮评分记录新的规则哈希。

### 6.1 规则优先级

规则冲突时按以下顺序执行：

1. 事实和合规边界；
2. 硬性排除条件；
3. 重复岗位和已处理岗位；
4. 职位方向与级别匹配；
5. 新鲜度、地点、薪资和工作模式；
6. 简历版本选择与材料生成。

无法确定时输出 `unknown` 并进入人工审核，不要为了让结果看起来完整而猜测。

### 6.2 规则应分成三类

**硬性排除**：命中后通常不进入申请队列，例如明显高于目标级别的岗位、明确不符合工作权利的岗位、明显低于最低薪资的岗位、明确不接受的地点或工作模式。

**优先条件**：用于排序，不直接决定投递，例如 24 小时内新发布、目标方向、目标城市、薪资清晰、职责与现有经历高度重合。

**人工确认条件**：不能由关键词直接决定，例如签证措辞含糊、职位级别与职责冲突、公司名称疑似重复、岗位描述过短或链接无法打开。

### 6.3 建议的首版默认策略

下面是可写入 `profile/preferences.md` 的示例。实际值必须以候选人档案为准：

```markdown
## Screening policy

### Prioritise
- New grad / graduate / entry level / junior roles.
- Primary directions: AI Product, Growth, Product Marketing.
- Jobs posted within the last 24 hours get a freshness boost.
- Prefer the configured locations and salary range.

### Skip by default
- Senior, staff, principal, lead or manager roles unless I explicitly approve them.
- Explicit citizenship / PR / security-clearance requirements that I cannot meet.
- Explicit no-sponsorship language when sponsorship is required.
- Duplicate jobs already submitted, skipped or currently blocked.

### Review manually
- Missing or contradictory seniority, location, salary or work-right information.
- Unknown sponsorship signal.
- A job description too short to judge reliably.

### Match threshold
- Preferred: 80% or above.
- Broad-search minimum: 70%.
- Below 70%: skip or hold unless I override it.

### Quantity rule
- Do not optimise for application count.
- Follow the ApplyPilot automation authorization, platform mode, safety gates and hard daily cap.
```

这里的 70%–80% 是匹配度门槛，不是“能拿到面试”的概率。系统必须同时输出匹配依据、缺口、签证判断和使用的简历版本。

### 6.4 新鲜度和频率

“24 小时内发布”应作为排序加分或优先队列条件，而不是在日期缺失时一刀切。每次运行应保留 `posted_at` 和 `discovered_at`，防止同一岗位因来源更新时间变化而重复进入队列。

系统可以限制每轮展示或准备的数量。平台登录、自动投递门槛、每日限额和提交由 `applypilot-au` Skill 管理，不在 JobHunter 中复制第二套策略。

## 7. 模块四：简历策略

### 7.1 简历目录和版本清单

预先维护 5–6 份真实、方向不同的简历，比为每个岗位从零生成一份更稳定。建议在 `profile/resumes/manifest.yaml` 中维护元数据：

```yaml
- id: resume-ai-product
  path: profile/resumes/resume-ai-product.md
  label: AI Product
  target_roles: [AI Product, Product, Product Operations]
  keywords: [AI, LLM, user research, experimentation, roadmap]
  seniority: [graduate, entry, junior]
  last_reviewed: 2026-07-24

- id: resume-growth-pm
  path: profile/resumes/resume-growth-pm.md
  label: Growth / Product Marketing
  target_roles: [Growth, Product Marketing, Marketing Operations]
  keywords: [growth, lifecycle, campaign, analytics, content]
  seniority: [graduate, entry, junior]
  last_reviewed: 2026-07-24
```

版本名称只是示例。每一份简历都必须能追溯到 `candidate_profile.md` 或 `resume.md` 中的事实。

### 7.2 选择算法

对每个岗位：

1. 读取职位名称、职责、必需技能、加分技能、级别和行业。
2. 与 manifest 中的 `target_roles`、`keywords`、`seniority` 比较。
3. 选择一个主版本，并输出 `resume_reason`。
4. 只允许重排顺序、调整措辞、改变详略和突出已有事实。
5. 任何新增数字、年限、职责、客户、工具熟练度或业绩，都标记为需要人工确认。

示例输出：

```text
resume_id: resume-ai-product
resume_reason: 职位重点是 AI 产品、用户反馈和实验；该版本已有对应项目事实。
customisation: 仅重排项目顺序并突出已有的 OpenAI API 集成经历。
needs_confirmation: false
```

### 7.3 两种投递模式

**精准模式**：只处理匹配度约 80% 以上的岗位，针对岗位重排简历并生成简短、事实准确的 cover letter。

**广覆盖模式**：使用预先准备好的 5–6 份简历，匹配度达到约 70% 即可进入人工审核队列；不为每个低质量岗位无限生成新材料，也不为了数量放宽硬性规则。

无论哪种模式，最终提交前都要人工检查：姓名和联系方式、工作权利、日期、技能熟练度、项目归属、所有数字和链接。

## 8. Codex 工作契约

后续可以把下面的规则加入项目级说明或运行提示词：

```text
你是加载 applypilot-au 的当前 Codex Agent，负责连续完成搜索、评分编排、Dashboard 同步和允许的投递。

开始任务前必须读取：
1. profile/candidate_profile.md
2. profile/preferences.md
3. profile/resume.md
4. profile/resumes/manifest.yaml（如果存在）
5. data/application-dashboard.csv（如果存在）

不要猜测候选人的个人信息、工作权利、经历、技能熟练度、年限、薪资或业绩。
无法确认的内容使用 unknown，并将岗位标为 review 或 blocked。
每个岗位必须输出：筛选结论、匹配分、理由、缺口、签证信号、resume_id 和下一步。
岗位匹配分只能来自使用 Skill 固定规则的 DeepSeek API；不得由当前 Agent 自行估分。
任何状态变更都要更新 dashboard，并追加 application event。
生成材料或创建内部执行记录不等于已投递；只有明确平台成功证据才能写入 submitted。
真实投递必须交给配置的 applypilot-au Skill，不得在 JobHunter 内复制浏览器策略。
```

## 9. 在当前仓库中的实施顺序

按以下顺序开发，先建立可验证的记忆和决策，再考虑界面：

### 阶段 A：文件和数据合同

- 新增 `profile/candidate_profile.md` 模板；
- 新增 `profile/resumes/manifest.yaml` 和至少两份测试简历版本；
- 新增空的 `data/application-dashboard.csv` 与事件日志表头；
- 在 `config.yaml` 增加 `candidate_profile`、`resume_manifest`、`dashboard` 路径。

### 阶段 B：核心逻辑

- 在 `schema.py` 增加候选人档案、筛选结果和申请记录的数据结构；
- 新增 `dashboard.py`，实现读取、按 `job_id` 幂等合并、状态变更和事件追加；
- 把 `score.py` 的硬过滤、软评分和人工审核原因拆成独立结果；
- 新增简历 manifest 读取和 `resume_id` 选择逻辑；
- 让 `run.py` 在每轮结束时更新仪表盘，而不是只写 `output/jobs-ranked.*`。

### 阶段 C：申请材料工作流

- 只为达到阈值且状态为 `review` 的岗位生成材料；
- 生成目录使用稳定 `job_id`，避免重复生成；
- `00-summary.md` 必须包含岗位链接、匹配分、筛选理由、签证信号、简历版本、事实检查清单和人工下一步；
- 材料生成后状态仍是 `review`，人工检查通过后才进入 `ready_to_apply`。

### 阶段 D：仪表盘界面

在已有 `webapp.py` 上增加：

- 按状态、匹配分、发布日期、公司和方向筛选；
- 查看岗位快照、重复链接、生成材料和事件历史；
- 允许用户把岗位改为 `submitted`、`skipped` 或 `blocked`；
- 状态变更必须要求原因或确认，写入事件日志；
- 仅绑定本机地址，不把含个人信息的仪表盘暴露到公网。

## 10. 验收标准

实现完成后，至少用离线测试验证以下行为：

- 缺少候选人档案必填字段时，系统不会继续猜测并生成申请材料；
- 同一个岗位从 SEEK、Indeed、LinkedIn 出现时只有一条 dashboard 主记录；
- 已提交、已跳过和卡住的岗位不会在下一轮被重复推荐；
- 跳过、卡住和需要用户处理没有原因时，状态更新失败；
- `submitted` 必须同时具有明确成功证据；
- 24 小时内的新岗位排序优先，日期未知的岗位不会被错误删除；
- senior 以上、明确不符合工作权利和明确不担保的岗位遵守硬规则；
- 每个推荐岗位都有 `match_score`、理由、缺口、签证信号和 `resume_id`；
- 简历选择可解释，且生成内容不包含事实来源中不存在的新经历；
- 重跑同一批岗位不会产生重复申请记录或重复事件；
- API key、完整邮箱、电话和签证敏感信息不出现在普通运行日志中。

## 11. 每日使用流程

```text
抓取 → 去重 → 硬过滤 → 匹配评分 → 写入 dashboard
                         ↓
                 人工查看 review 队列
                         ↓
              选简历/生成材料/事实检查
                         ↓
                   ready_to_apply
                         ↓
              applypilot-au 执行或人工接管
                         ↓
                有明确证据才 submitted
```

每日操作建议：

1. 先看新增 `review`，不要先看“已投递数量”；
2. 优先处理 24 小时内且匹配度高的岗位；
3. 对每个岗位确认简历、工作权利、地点、薪资和提交链接；
4. ApplyPilot 或人工完成后立即写回状态、时间和提交证据；
5. 一周后根据面试、拒信和无回应记录，调整 `preferences.md`、简历版本或筛选阈值。

## 12. 后续可选迭代

第一版稳定后再考虑：

- 面试反馈和拒信原因的结构化记录；
- 按简历版本统计回复率、面试率和无回应率；
- 生成给招聘方的人工消息草稿，但不自动发送；
- 对职位描述变化做 diff，避免重复处理同一岗位；
- 将 CSV 迁移到 SQLite，同时保留 CSV/Markdown 导出；
- 增加 dashboard 的备份和脱敏导出。

投递既可由当前 Agent 消费 `run.py --autopilot` 的内部执行记录，也可由用户在 Dashboard 对单个 `broad` / `targeted` 岗位明确点击发起。单岗位按钮直接调用后台 ApplyPilot 执行器，不暴露用户操作队列或复制提示词；平台原站由用户完成操作，允许的直接外部 ATS 才进入自动核验和提交。CAPTCHA、绕过平台检测和无记录的批量提交仍不属于目标。ApplyPilot 的价值首先是**减少判断和记录成本**，而不是制造更多不可追踪的申请数量。

参考实现：用户提供的公开项目 `yvonnehe772/applypilot`；使用前应自行检查其当前代码、依赖和平台合规边界。
