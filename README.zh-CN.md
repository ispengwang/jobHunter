# JobHunter

[English](README.md) | **简体中文**

面向澳洲市场的本地求职搜索与申请记录工具。JobHunter 聚合 **SEEK、Indeed 和 LinkedIn** 的岗位，去重后结合候选人档案评分排序，并保留可追溯的投递历史。

JobHunter 负责搜索、评分和 Dashboard；单独安装的 **`applypilot-au` Skill** 负责平台规则判断与允许的浏览器投递流程。

## 功能

- **多平台搜索：** SEEK 集成与用于 Indeed、LinkedIn 的 JobSpy，统一岗位字段和 AUD 年薪。
- **岗位去重：** URL、标准化公司名/职位/地点匹配，再进行职位模糊匹配；保留其他来源链接。
- **可解释匹配：** LLM 输出岗位匹配分、摘要、已满足项、缺口与担保信号；另行计算简历匹配度和选择理由。
- **按候选人条件筛选：** 可配置工作权利规则、entry/junior 资格，投递优先处理 24 小时内、其次 3 天内的岗位。
- **持久化 Dashboard：** 岗位卡片、筛选、申请状态、所选简历与追加式事件历史。
- **可选申请材料：** 基于已有候选人事实调整简历，生成澳式求职信。
- **本地网页操作：** 编辑设置与候选人资料、查看评分规则、运行搜索、配置 macOS 定时搜索。

## 快速开始

以下命令使用 macOS/Linux shell 和 Python 虚拟环境。定时搜索使用 macOS LaunchAgents。浏览器投递流程需要 Codex 与单独安装的 ApplyPilot Skill。

### 1. 安装依赖

```bash
git clone https://github.com/ispengwang/jobHunter.git
cd jobHunter
python3 -m venv venv
source venv/bin/activate
python -m pip install -r requirements.txt
```

### 2. 准备个人资料与配置

个人资料文件不纳入 Git，需要在本地创建：

| 文件 | 用途 |
| --- | --- |
| `profile/resume.md` | 真实的原始简历：经历、教育、技能和项目。 |
| `profile/preferences.md` | 目标岗位、地点、薪资偏好和筛选限制。 |
| `profile/candidate_profile.md` | 可编辑的候选人事实档案，仅首次从简历初始化；在网页中补全和核对。 |
| `profile/resumes/manifest.yaml` | 简历版本元数据；按自己的文件修改条目和路径。 |

运行前检查 `config.yaml`，设置搜索词、地点、来源、工作权利过滤、匹配阈值和文件路径。仓库中的配置需要按你的环境调整。

**评分还需要单独安装本地 `applypilot-au` Skill。** 本仓库不包含这个 Skill。把 `applypilot.skill_path` 指向安装目录，并确保其中存在 `references/deepseek-scoring-rules.md`。普通评分和 autopilot 都依赖该规则文件；只抓取不需要 LLM 密钥或评分规则。

使用 DeepSeek 时，在现有配置中修改以下字段，保留其他设置：

```yaml
llm:
  provider: "deepseek"
  # 将 deepseek_model 设置为你的 API 账号可用的模型。

applypilot:
  skill_path: "/absolute/path/to/applypilot-au"
```

在启动 JobHunter 的 shell 中设置 API key：

```bash
export DEEPSEEK_API_KEY="YOUR_DEEPSEEK_API_KEY"
```

普通评分也支持 Anthropic（`ANTHROPIC_API_KEY`）和 OpenAI（`OPENAI_API_KEY`），需配套设置 `llm.provider` 和模型。**`--autopilot` 必须使用 DeepSeek。**

### 3. 小样本试跑

```bash
# 只抓取，不调用 LLM。
python run.py --scrape-only

# 从缓存结果中选取最多 20 个岗位评分。
python run.py --from-cache --limit 20

# 运行搜索、评分和 Dashboard 同步。
python run.py
```

打开 `output/jobs-ranked.md` 查看岗位摘要和匹配理由，或用 `output/jobs-ranked.csv` 做表格筛选。排名导出基于持久化 Dashboard，因此可能包含以前运行发现的岗位。

Dashboard 中已有的岗位默认不会重新评分。修改个人资料或评分规则后，可以使用 `python run.py --from-cache --rescore-all` 强制重评缓存岗位；这会产生新的 LLM 调用。

## 本地网页

```bash
python webapp.py
```

打开[本地应用](http://127.0.0.1:5050)，服务监听 `127.0.0.1`。

| 页面 | 功能 |
| --- | --- |
| `/` | 编辑设置、启动搜索、查看日志和配置定时任务。 |
| `/profile` | 核对和编辑候选人资料，之后不会因重新导入简历而覆盖。 |
| `/dashboard` | 按匹配分、来源、新鲜度、状态和每日活动筛选，查看岗位并更新投递进度。 |
| `/scoring-rules` | 查看或编辑已安装 Skill 的评分规则；人工保存时先备份旧版本。 |

Dashboard 按钮将岗位加入本地投递清单，不会启动后台执行器或打开招聘网站。当前 ApplyPilot Agent 使用 `python run.py --handoff-list` 读取这些记录，再按 Skill 规则继续。如果你已在外部平台成功提交，可使用卡片上的“确认已提交”记录结果。

保存定时设置只会保存配置。使用独立的“保存并应用到 macOS 定时任务”操作，才会更新 LaunchAgents。定时进程不会继承交互式 shell 导出的 API key；配置方式见 [HOWTO.md](HOWTO.md) 的定时任务部分。

## 申请流程

```text
抓取 → 归一化 → 去重 → 硬过滤 → LLM 评分
                                  ↓
                         资格 + 新鲜度 + 简历选择
                                  ↓
                         Dashboard + 排名导出 + 历史
                                  ↓
                         可选材料 → ApplyPilot → 结果
```

岗位匹配度与简历匹配度分别计算。合格岗位进入 `broad`（广覆盖）或 `targeted`（精投）材料准备路径；其他岗位保留为 `manual_review`。投递先按新鲜度分组，组内按分数排序；Dashboard 列表仍按分数排序。

### 生成材料

```bash
python run.py --from-cache --generate
```

生成遵守 `scoring.generate_threshold` 和 `scoring.max_generate`，处理本轮评分中合格的 `broad`/`targeted` 岗位。如果缓存岗位已存在于 Dashboard，添加 `--rescore-all` 重新评分并生成材料。

每个申请目录包含 `00-summary.md`、`resume.md` 和 `cover-letter.md`。使用前逐份核对：允许重排和改写已有事实，不得新增虚构的经历、技能、日期或业绩。

### 交给 ApplyPilot 继续

在 Codex 中要求当前 Agent 使用 `applypilot-au` 完成搜索与投递流程。其流水线入口为：

```bash
python run.py --autopilot
```

当新岗位进入评分阶段时，该模式还会准备材料，并写入 `output/agent-run.json`，由当前 Agent 读取后继续符合条件的尝试。单独运行命令不会执行浏览器提交；没有岗位需要评分时会提前返回。

- 当前集成中，LinkedIn、Indeed 和 SEEK 原站默认由用户完成提交。
- 直接雇主 ATS 只有在当前授权与平台、候选人检查全部通过时，才使用 `auto_if_allowed`。
- CAPTCHA、登录/2FA、未知必填答案、法律条款及不支持的表单需要用户处理。
- 内部选中或材料生成都不代表提交。只有外部成功证据，或用户明确确认外部提交成功，才能记录为 `submitted`。

内部 attempt 账本用于去重、恢复和审计，不是用户操作队列。完整流程和单岗位流程共用每日限额。平台模式、限额和 CLI 结果写回见[浏览器投递手册](BROWSER_APPLICATION_RUNBOOK.md)。其中关于 Dashboard 后台执行器的说明已过时；当前按钮只创建本地交接记录。

## 文件与持久化

```text
scrapers/                         SEEK 和 JobSpy 数据源适配器
run.py                            流水线与内部投递记录 CLI
webapp.py                         本地设置与 Dashboard 服务
config.yaml                       搜索、LLM、投递与路径设置
profile/                          本地候选人事实与简历版本
data/
  application-dashboard.csv       持久化岗位/申请主账本
  application-events.csv          追加式事件历史
  application-attempts.csv        Agent 内部执行账本
output/
  jobs-raw.csv                     去重后的搜索结果
  jobs-ranked.csv                 Dashboard 排名导出
  jobs-ranked.md                  可直接阅读的排名摘要
  rejected-visa.csv               被工作权利规则排除的岗位
  agent-run.json                  Agent 后续执行清单
  applications/                   生成的申请材料
```

`data/` 保存申请记忆，刷新导出时应保留。个人资料文件、简历 Markdown、`data/`、`output/` 和 `.env` 已被 Git 忽略。`config.yaml` 和简历 manifest 仍被 Git 跟踪，不要在其中写入密钥或私人候选人信息。

数据存储在本地，但 LLM 评分和生成会向配置的 API 提供商发送简历及岗位内容；浏览器投递会在授权后向所选招聘服务发送候选人资料。

## 验证

在项目根目录运行离线回归测试：

```bash
python test_pipeline.py
python test_seek_parse.py
python test_e2e_offline.py
python test_application_system.py
```

覆盖归一化、去重、工作权利过滤、SEEK 解析、使用模拟 LLM 的流水线、候选人档案、申请策略和 Dashboard 行为。离线通过不代表真实招聘接口或投递当前可用。

## 已知限制

- SEEK 使用非公开接口，可能变更。无法取得完整 JD 时，适配器可降级使用简述和要点，评分上下文会减少。
- 数据源可用性和限流情况会变化。应遵守平台限制，不绕过 CAPTCHA 或反自动化控制。
- 公司名差异、信息不足仍可能造成去重遗漏或匹配不确定。
- 担保信号、生成摘要与匹配分需要核对；匹配分不代表面试概率，也不构成工作权利判定。
- 网页界面及部分生成文字目前为中文；此处的语言切换仅覆盖 README 文档。

## 更多文档

- [实施计划与验收标准](IMPLEMENTATION_PLAN.md) — 英文。
- [系统搭建指南](APPLYPILOT_SYSTEM_BUILD_GUIDE.md) — 中文；设计背景与数据合同。
- [浏览器投递手册](BROWSER_APPLICATION_RUNBOOK.md) — 中文；执行与提交规则。
- [使用与定时任务指南](HOWTO.md) — 中文；本地设置与定时搜索。
- [Agent 指令](AGENTS.md) — 仓库操作规则。
