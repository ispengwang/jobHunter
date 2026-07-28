# ApplyPilot AU Agent 自动运行手册

JobHunter 负责搜索、去重、确定性硬过滤、DeepSeek API 打分、简历选择和 Dashboard
展示。`applypilot-au` Skill 是唯一的平台策略与投递执行入口。用户可以运行完整流程，
也可以在 Dashboard 对一个岗位直接点击投递；两种方式都不需要操作内部队列或复制提示词。

## Dashboard 单岗位按钮

卡片按钮调用 `/dashboard/<job-id>/applypilot`，只接受当前 Dashboard 生成的动作令牌和
已有 `job_id`，不接收自由文本 Agent 指令。系统会：

1. 检查 Candidate Profile、岗位状态、重复 attempt 和当天剩余额度；
2. 建立或复用内部 attempt，并记录用户对这个具体岗位的明确启动动作；
3. LinkedIn、Indeed、SEEK 原站进入 `manual_submit`，打开申请页并写回 `needs_user`；
4. 直接外部 ATS 进入 `auto_if_allowed`，由 `applypilot_executor.py` 使用桌面应用内置
   Codex CLI 加载 `applypilot-au`；
5. 在原岗位卡片轮询显示“等待 Agent / 正在处理 / 需要你处理 / 已提交”。

后台浏览器任务串行执行，避免多个 Agent 同时控制一个登录会话。仅包含启动时间、结束时间
和退出码的元数据日志保存在本地 `data/applypilot-executions/`，不会记录 Agent 工具输出
或候选人资料。Web App 或 Agent
重启不会把内部选择当作提交；用户可在同一张卡片继续已有 attempt。

如果用户已经在外部平台完成提交，可在同一张 Dashboard 卡片展开“确认已提交”，填写
成功页文字、确认编号或确认 URL，并再次明确勾选确认。系统会同步 Dashboard 主记录、
已有 ApplyPilot attempt、`submitted_at` 和事件历史。卡片也保留“转换申请状态”，用于
在待投递、已提交、被拒绝和面试中之间更新；从终态改回待投递必须填写原因。

## 启动完整流程

在 Codex 中调用 `applypilot-au` 并要求运行求职流程。当前 Codex Agent 应直接进入项目：

```bash
cd /Users/pengwang/jobhunter
venv/bin/python run.py --autopilot
```

`--autopilot` 会：

1. 搜索配置的岗位来源；
2. 去重并执行确定性资格过滤；
3. 从已安装 Skill 读取固定的 `references/deepseek-scoring-rules.md`；
4. 调用 DeepSeek API 生成岗位匹配分、理由、匹配项和缺口；
5. 选择简历并区分精准/广覆盖/人工复核；
6. 将发布日期与 DeepSeek JD 摘要写入 Dashboard；列表仍按分数降序展示；
7. 生成 `output/agent-run.json`，供当前 Agent 立即继续投递。

当前 Agent 不得自己替代 DeepSeek 生成分数。评分规则不会每轮生成或改写；运行清单会记录
规则文件的哈希，方便追踪评分版本。用户可在 `/scoring-rules` 查看、审核并手动修改规则；
保存前系统会备份旧版本。

## 当前 Agent 继续投递

命令结束后，当前 Agent 立即读取 `output/agent-run.json`，不暂停等待用户操作。内部
`data/application-attempts.csv` 只用于去重、恢复和审计，不是用户需要操作的队列。

需要检查单条内部记录时可运行：

```bash
venv/bin/python run.py --attempt-handoff <attempt-id>
```

随后必须遵守已安装 Skill 中的：

- `references/autonomous-jobhunter-run.md`
- `references/jobhunter-integration.md`
- `references/platform-playbook.md`
- `references/safety-and-boundaries.md`

平台默认：

- LinkedIn、Indeed：`manual_submit`，不自动操作申请提交；
- SEEK 原站：当前保守默认 `manual_submit`；
- 直接外部 ATS：只有当前具体流程允许且全部门槛通过时才使用 `auto_if_allowed`。

执行顺序与额度：

- 24 小时内发布的合格岗位先处理，同一新鲜度内按匹配分降序；
- Australia/Melbourne 每日软目标 30、硬上限 40；
- 24 小时岗位可使用 30–40 的缓冲，但不得放宽匹配度或安全门；
- 每 5 次尝试对账，连续阻塞或平台警告立即停止。

验证码、登录、2FA、验证、未知必填问题、测评、视频、推荐人、付款或平台警告必须停止并
写回 `needs_user`。用户允许自动投递不代表平台允许。

## 写回状态

真实外部状态变化后，通过 CLI 写回：

```bash
venv/bin/python run.py \
  --attempt-update <attempt-id> \
  --attempt-status filling
```

需要用户处理：

```bash
venv/bin/python run.py \
  --attempt-update <attempt-id> \
  --attempt-status needs_user \
  --attempt-reason "<发生了什么，以及用户需要做什么>"
```

确认提交成功：

```bash
venv/bin/python run.py \
  --attempt-update <attempt-id> \
  --attempt-status submitted \
  --submission-confirmed \
  --submission-evidence "<成功页文本或确认 URL>"
```

没有明确成功证据时不得记录 `submitted`。Agent 选择、打开网页、自动填写、上传简历或
点击 Submit 本身都不是提交证据。
