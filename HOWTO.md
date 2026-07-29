# 怎么在你自己电脑上跑

简历和偏好已经填好了（Melbourne / junior / 70-90k）。你需要做的只有装依赖和配一个 API key。

## 一次性准备

### 1. 确认有 Python

打开终端（Mac 上按 `Cmd+空格` 搜 "Terminal"），输入：

```bash
python3 --version
```

出现 `Python 3.10` 或更高就行。没有的话去 python.org 装一个。

### 2. 进入项目目录

把 `jobhunt-au` 文件夹放到你想放的地方，比如桌面，然后：

```bash
cd ~/Desktop/jobhunt-au
```

### 3. 建虚拟环境并装依赖

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

装完终端提示符前面会出现 `(venv)`。**以后每次新开终端都要先跑 `source venv/bin/activate`**，否则会提示找不到包。

### 4. 配 API key

去 console.anthropic.com 建一个 key，然后：

```bash
export ANTHROPIC_API_KEY=sk-ant-你的key
```

这个只在当前终端窗口有效。想永久生效，把上面那行加到 `~/.zshrc` 文件末尾。

**想省钱可以换成 DeepSeek**：去 platform.deepseek.com 建一个 key，`export DEEPSEEK_API_KEY=sk-你的key`，然后把 `config.yaml` 里 `llm.provider` 改成 `"deepseek"`。接口兼容，代码不用改，就是便宜很多（见下面「大概花多少钱」）。

## 不想用命令行?用图形化设置面板

```bash
python webapp.py
```

然后浏览器打开 `http://127.0.0.1:5050`。这是本地网页：页面上能改常用设置、运行抓取/打分、查看结果；`/profile` 管理 Candidate Profile，`/dashboard` 管理每日行动、申请历史和单岗位投递。合格岗位卡片会显示明确按钮：平台原站打开给你操作，允许自动化的直接外部 ATS 才启动后台 ApplyPilot Agent。

下面的四步走是命令行版本,两种方式效果一样,挑顺手的用。

## 日常使用：四步走

### 第一步：确认能抓到数据（不花钱）

```bash
python run.py --scrape-only
```

这步只抓取，不调用 AI，不花一分钱。大概跑 5-15 分钟（SEEK 那边为了不被封，每个请求之间会等 0.6 秒）。

跑完看 `output/jobs-raw.csv`，正常应该有几百条。日志里会显示：

```
JobSpy(LinkedIn/Indeed) 共 312 条
SEEK 共 198 条
去重:510 → 287 条(移除 223,43.7%)
```

**如果 SEEK 是 0 条**：说明它改接口了，README 里有修复说明。先不管，LinkedIn + Indeed 也够用。
**如果全是 0**：检查网络，或者被限流了，等半小时再试。

### 第二步：小样本试跑（花几毛钱）

```bash
python run.py --from-cache --limit 20
```

`--from-cache` 复用上一步的抓取结果，不重新抓。只对 20 个岗位打分。**默认不生成简历**，跑完就结束了。

跑完打开 `output/jobs-ranked.md`——单文档汇总,不用开 Excel,每个岗位一段,含分数、链接、AI 写的一句话摘要、匹配理由。想按列筛选排序就用 `output/jobs-ranked.csv`,两个文件内容一样。**重点看「匹配理由」那部分**——AI 给的理由靠不靠谱。

如果发现打分明显不对（比如把要求 8 年经验的岗位给了 90 分），改 `profile/preferences.md` 里的「打分时请注意」那一段，说得更具体，然后重跑这一步。

### 第三步：完整跑一遍

```bash
python run.py --from-cache
```

给全部岗位打分，输出 `output/jobs-ranked.md`（单文档汇总，推荐先看）和 `output/jobs-ranked.csv`，并同步 `data/application-dashboard.csv`。系统会同时记录岗位匹配分、简历匹配分、`broad` / `targeted` 路径、发布时间优先级和当前申请状态。打开 Dashboard 时新鲜度按当前时间计算；运行完成后已打开页面会自动刷新，并可用“最近同步”查看本轮写入的岗位。默认不批量生成材料；需要时再做下一步。

### 第四步（可选）：对看中的岗位生成申请材料

看完 `jobs-ranked.csv`，确定想投哪几个了，再跑：

```bash
python run.py --from-cache --generate
```

会给 ≥70 分的岗位生成简历和 cover letter（阈值和最多生成数在 `config.yaml` 的 `scoring` 里改）。材料在 `output/applications/` 下，每个岗位一个文件夹：

```
output/applications/
  082-Canva-Junior-Frontend-Engineer/
    00-summary.md       ← 先看这个
    resume.md
    cover-letter.md
```

`00-summary.md` 里有投递链接和一个 checklist。**每一份都要自己读一遍**，重点确认简历里没有被 AI 润色出来的、你其实没做过的事。使用完整 Agent 流程时，这些材料会被内部执行记录引用，不需要手动加入队列。

完整流程仍可直接在 Codex 调用 `applypilot-au`；如果只想投某一个岗位，则在 `/dashboard` 点击对应卡片的按钮。按钮只会加入本地投递清单，不启动后台进程；当前 Agent 用 `venv/bin/python run.py --handoff-list` 读取。真实投递仍以 Skill 的平台模式、平台限额和提交证据规则为准。详见 [BROWSER_APPLICATION_RUNBOOK.md](BROWSER_APPLICATION_RUNBOOK.md)。

Markdown 转 PDF 最简单的办法是用 VS Code 装 "Markdown PDF" 插件，或者直接复制粘贴到 Word 里排版。

## 定时抓取（准备好后再由用户安装）

仓库提供两个 macOS `launchd` User Agent 模板：

- `launchd/au.jobhunter.incremental.plist.template`：每 3 小时运行一次 `--incremental`，使用 48 小时窗口；
- `launchd/au.jobhunter.full.plist.template`：每天 02:00 运行一次完整抓取，沿用 `config.yaml` 的 336 小时窗口。

模板只负责独立启动抓取，不启动 `webapp.py`，也不增加并发。`ThrottleInterval` 提供失败后的最小退避；SEEK 现有请求间隔保持不变。注意 Indeed 不接受 `hours_old`，增量模式对 Indeed 仍然是抓取后按 `date_posted` 本地过滤，请观察日志中的请求量。

本次只交付模板，**没有替用户安装或启用 launchd**。用户确认后，在项目根目录执行：

```bash
mkdir -p output/launchd "$HOME/Library/LaunchAgents"
sed "s|__PROJECT_ROOT__|$PWD|g" launchd/au.jobhunter.incremental.plist.template \
  > "$HOME/Library/LaunchAgents/au.jobhunter.incremental.plist"
sed "s|__PROJECT_ROOT__|$PWD|g" launchd/au.jobhunter.full.plist.template \
  > "$HOME/Library/LaunchAgents/au.jobhunter.full.plist"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/au.jobhunter.incremental.plist"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/au.jobhunter.full.plist"
```

安装前先确认 API key、`config.yaml`、虚拟环境和输出目录均可用；首次启用后应在用户在场时观察两份日志，确认增量请求量和 `last_synced_at` 行为，再决定是否保留。

## 手动完整刷新

```bash
source venv/bin/activate
export ANTHROPIC_API_KEY=sk-ant-你的key
python run.py
```

不加 `--from-cache` 就是完整重新抓取；`config.yaml` 默认 `hours_old: 336`，保留 14 天窗口。若已安装上面的 launchd 模板，日常由定时任务负责，手动命令适合排查或主动刷新。

## 大概花多少钱

用 claude-sonnet-5，只跑打分排序（不加 `--generate`）：约 300 个岗位打分大概 1 澳元左右。加 `--generate` 再给 15 个岗位生成材料，额外多花 1-4 澳元。想省钱就把 `config.yaml` 里的 `max_description_chars` 从 4000 调到 2000，或者把 `llm.provider` 换成 `"deepseek"`（同样任务量能便宜到几分之一，见上面「配 API key」）。

## 常见问题

**`command not found: python3`** — 没装 Python。

**`ModuleNotFoundError: No module named 'jobspy'`** — 忘了 `source venv/bin/activate`。

**`缺少环境变量 ANTHROPIC_API_KEY`（或 `DEEPSEEK_API_KEY` / `OPENAI_API_KEY`）** — 忘了 export，或者开了新终端窗口。报错里的变量名取决于 `config.yaml` 里 `llm.provider` 选的是哪个。

**`简历内容太短`** — `profile/resume.md` 被清空了，从备份恢复。

**LinkedIn 抓一半停了** — 被限流了，正常。已经抓到的会存进缓存，等一小时后用 `--from-cache` 继续后面的步骤就行。

**一个岗位都没到 70 分** — 先看 `jobs-ranked.csv` 里的理由。通常不是阈值问题，而是搜索词太窄或者 Melbourne 的 junior 岗位在窗口内确实少。可以把 `config.yaml` 的 `hours_old` 从 336 调大，但完整窗口越长，抓取量和限流风险也越高。
