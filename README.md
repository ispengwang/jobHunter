# jobhunt-au

SEEK + Indeed + LinkedIn 三平台岗位聚合 → 去重 → 打分排序。

针对澳洲市场和 485 签证 + 需要担保的情况做了专门处理。

默认流程以“抓取、筛选、排序、Dashboard 展示”为主。既可以调用 `applypilot-au` 运行完整流程，也可以在 Dashboard 对单个岗位点击投递。单岗位按钮直接把已有内部记录交给后台 ApplyPilot Agent，不需要打开队列或复制提示词。

JobHunter 不操作招聘网站。实际平台判断、允许的自动投递、每日限额、浏览器流程和提交证据由配置的 `applypilot-au` Skill 负责。

## 快速开始

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...      # 或 OPENAI_API_KEY / DEEPSEEK_API_KEY,配合 config.yaml 里的 provider

# 1. 填好你的简历(重要,这是唯一的事实来源,打分要靠它比对)
vim profile/resume.md
vim profile/preferences.md

# 2. 先只抓取,不烧 token,确认三个源都通
python run.py --scrape-only

# 3. 小样本试跑,看打分准不准
python run.py --from-cache --limit 20
open output/jobs-ranked.md

# 4. 满意后跑完整流程(默认到打分排序为止)
python run.py

# 或让当前 Codex Agent 运行完整自动流程
python run.py --autopilot
```

## 输出

```
output/
  jobs-raw.csv           # 去重后的全部岗位
  jobs-ranked.csv        # 按匹配度排序,适合筛选排序/导入表格工具
  jobs-ranked.md         # 同样的排名结果,单文档汇总:分数+链接+摘要,直接打开从头看到尾
  rejected-visa.csv      # 被签证条件淘汰的,扫一眼确认没误杀
data/
  application-dashboard.csv  # 岗位/状态/简历/行动队列的持久化主表
  application-events.csv     # 追加式操作历史
  application-attempts.csv   # Agent 内部执行、恢复和审计记录（不是用户队列）
```

`jobs-ranked.md` 是推荐先看的文件——不用开 Excel,每个岗位一段,含分数、链接、薪资、地点、LLM 写的一句话摘要(客观描述岗位是什么)、匹配理由、已满足/待补强。`jobs-ranked.csv` 字段更适合排序筛选。两者内容一致,选你顺手的用。

加 `--generate` 才会额外产出简历/cover letter(见下方"可选:生成申请材料")。

## 图形化设置面板(网页版,不用回终端)

```bash
python webapp.py
# 浏览器打开 http://127.0.0.1:5050
```

本地起一个网页服务,跟 Claude 对话完全独立——关掉这个对话、重启电脑之后照样能用,只要在项目目录下跑 `python webapp.py`。功能:

- **改设置**:`config.yaml` 里的搜索词、地点、数据源开关、签证关键词、LLM provider、打分阈值,以及整份 `profile/preferences.md`,填完点"保存设置"直接写回文件。`config.yaml` 用 `ruamel.yaml` 做往返编辑,只改被改动的字段,文件里的注释和格式都保留。
- **Candidate Profile**:`/profile` 首次从现有简历提取可确定的信息；之后可在网页编辑姓名、联系方式、求职方向、薪资、入职时间与签证事实。编辑后的档案不会被重新解析覆盖。
- **Dashboard**:`/dashboard` 同时提供历史记录与每日行动视图，保存岗位、链接、精确发布日期、DeepSeek JD 摘要、匹配分、使用的简历、精投/海投路径与事件历史。主界面状态精简为待投递、已提交、被拒绝、面试中四类；每张卡片可确认已提交或转换状态，每张合格岗位卡片还可直接发起一次 ApplyPilot 投递。
- **评分规则**:`/scoring-rules` 直接展示并编辑 ApplyPilot Skill 实际交给 DeepSeek 的完整规则。每次人工保存会先备份旧版本；普通运行不会自行改写规则。
- **一键运行**:页面上三个按钮——只抓取 / 抓取+打分 / 用缓存重新打分,后台跑,页面上能看实时日志。API key 可以直接在页面填(只在这次运行时用,不写进任何文件),也可以照旧用 `export`。
- **统一 Dashboard**:`/dashboard` 已合并结果界面与申请记忆：使用结果页的卡片 UI 和 accent color，按匹配分从高到低排列，并可按最低分、最近同步、每日行动、状态、模式、担保、来源和新鲜度筛选。首屏渐进显示 40 条；运行完成后已打开的 Dashboard 会自动刷新，页面也会按当前时间重新计算新鲜度标签和筛选，不会把旧的 `24 小时内` 状态永久保留。所有卡片共用一个状态弹窗，避免为每条记录重复渲染表单。用户已经在外部平台确认成功后，卡片上的“确认已提交”点一次即可，不需要再输入文字、URL 或勾选额外确认；确认提交和状态转换都只局部更新对应卡片及统计，不触发整页刷新。系统会自动同步主账本、事件历史和已有 attempt。投递按钮直接启动单岗位执行，进度在原卡片内刷新；内部 attempt CSV 仅用于去重和审计，不作为用户队列。

只在本机(`127.0.0.1`)监听,不对外网开放。

## LLM Provider:Anthropic / OpenAI / DeepSeek

普通评分可配置不同 provider；`--autopilot` 明确要求 `llm.provider: deepseek`，岗位匹配分由 DeepSeek API 产生，而不是由当前 Agent 估算。

```yaml
llm:
  provider: "deepseek"          # anthropic | openai | deepseek
  deepseek_model: "deepseek-v4-flash"
```

```bash
export DEEPSEEK_API_KEY=sk-...
python run.py --from-cache
```

DeepSeek 接口是 OpenAI 兼容的(复用 `openai` 这个包,只是换了 `base_url`),不用额外装包。打分这种量大但不复杂的任务,DeepSeek 比 Anthropic/OpenAI 便宜不少,几百个岗位打分性价比更高。

注意:DeepSeek 旧模型名 `deepseek-chat` / `deepseek-reasoner` 将于 2026-07-24 弃用,`config.yaml` 里默认已经用新名字 `deepseek-v4-flash`(便宜档)/ `deepseek-v4-pro`(如果想要更强的)。

**验证状态**:当前 273 项离线检查全部通过。2026-07-24 已完成一次真实 DeepSeek 批量评分并取得 243/243 个有效结果；漏项会只重试缺失岗位一次，不由当前 Agent 补造分数。

## 工作流程

```
抓取  JobSpy(LinkedIn+Indeed) + SEEK jobsearch/v5
  ↓
归一  统一 schema,薪资统一成 AUD 年薪
  ↓
去重  URL 精确 → company+title+location 精确 → 同公司内 title 模糊(rapidfuzz 88)
  ↓
过滤  签证关键词/模式硬排除(不花钱,先做)
  ↓
打分  DeepSeek/LLM 批量评分 0-100 + 理由 + 已满足/待补强 + 一句话摘要
  ↓
决策  entry/junior + 24 小时/3 天优先级 + 简历匹配 → broad / targeted / manual_review
  ↓
记忆  upsert 到 data/application-dashboard.csv + 追加事件日志
  ↓
(可选,--generate)  broad / targeted 岗位使用选定简历生成材料
  ↓
浏览器  ApplyPilot 对允许的平台自动执行；异常人工接管，提交成功才标记 submitted
```

## 澳洲市场的针对性设计

**去重是重点,不是附加功能。** 中介会把同一个岗位同时挂在 SEEK、Indeed、LinkedIn 上,写法还略有不同("Backend Developer" / "Snr Backend Dev" / "Backend Engineer")。不去重就会对同一家公司重复投递,在澳洲这个小市场里挺伤的。所以 `norm_title` 把 developer/engineer/programmer 归一成同一个 token。合并后所有来源 URL 都存在 `duplicate_urls` 里 —— 一个岗位挂在几个平台,这本身是信号。

**签证过滤只砍明确排除的。** 澳洲大量 JD 根本不写签证要求,一刀切会误杀太多。只有 JD 明确说 citizenship required / PR only / security clearance / we do not sponsor 才淘汰,淘汰的单独存 `rejected-visa.csv` 供复查。担保信号还会影响最终分:明确愿意担保 **+5**,明确不担保 **-10**(扣分权重比加分高,因为对 485 + 需要后续担保的情况,一家明确不担保的公司是长期规划上的实质减分项,比"愿意担保"是锦上添花更值得警惕)。权重在 `score.py` 顶部的 `_SPONSOR_BONUS` / `_NO_SPONSOR_PENALTY` 两个常量里,想调直接改。

**打分区分"缺经验"和"缺技术栈",不一视同仁扣分。** 框架、库、云服务、具体工具这类学起来快的东西,即使 JD 列了一堆候选人没逐一用过的,也只轻微影响分数、列进待补强就行,不会因此压到 60 以下。只有明确的年限要求(如"5+ years")、不同范式的语言深度(纯 Java/.NET 后端)、系统架构主导、ML 建模这类需要长期积累的能力,缺了才明显扣分。**AI/LLM 应用工程方向额外加权**(候选人当前重点方向),但会区分"AI 产品工程"(往高分走)和"ML 研究/建模/平台"(仍按真实 gap 扣分)。这些规则在 `score.py` 的 `_SYSTEM` prompt 和 `profile/preferences.md` 里,想调改这两处。

**担保信号(explicit_yes/no/unknown)优先由 LLM 判断,不是纯关键词匹配。** 早期版本靠字符串包含判断("JD 里出现 'visa sponsorship' 就是 yes"),结果踩到真实案例:Maincode 和 BuildPass 的 JD 原文其实是"we are **not able to offer** visa sponsorship",纯字符串匹配完全不管前面那个"not able to offer",直接误判成愿意担保还加了 5 分。现在打分时让 LLM 顺带判断这个字段——JD 反正已经喂给它了,它能读懂否定语境,不用像正则那样一个个补否定词打补丁。关键词规则降级成兜底,只在 LLM 没按格式返回这个字段时才用。

**打分故意偏严。** JD 写得极其笼统的会被压到 60 分以下,那种通常是中介凑数的泛招广告,投了也是石沉大海。

**cover letter 用澳式风格。** 美式那套 "I am thrilled to apply for this incredible opportunity" 在这里会显得用力过猛。prompt 里锁定了平实语气 + 澳式拼写 + 250-320 词。

## 可选:生成申请材料

默认流程不生成简历/cover letter。想用的话,对已经跑出来的排名结果单独加一步:

```bash
python run.py --from-cache --generate
```

会为 `jobs-ranked.csv` 里 ≥ `config.yaml` 里 `scoring.generate_threshold`(默认 70)分的岗位生成材料,存到 `output/applications/<分数>-<公司>-<职位>/`,每个岗位一个文件夹:`00-summary.md`(摘要 + 待补强项 + 投递前 checklist)、`resume.md`(针对该岗位重排的简历)、`cover-letter.md`(澳式风格 cover letter)。

**红线**:prompt 里三处强调不得编造经历、技能、年限,只允许重排、改写措辞、调整详略。但模型仍然可能润色过头——把"参与过"写成"主导了",或者把 JD 关键词硬塞进你的经历。**每一份投出去之前必须人工过一遍**,`00-summary.md` 里有 checklist。

`profile/resume.md` 写得越全越好,生成器只从这里挑,不会凭空加。

## 验证状态(重要,先读这段)

2026-07-22 用 `probe.py` 在真实环境(macOS, Python 3.14, python-jobspy 1.1.82)实测过:

| 部分 | 状态 |
|---|---|
| 薪资解析 / 去重 / 归一化 / 签证过滤 / 担保信号加扣分 | ✅ 52 项单元测试 |
| SEEK v5 字段解析 + 日期过滤 | ✅ 52 项单元测试(用实测结构造的样本) |
| 全链路接线(去重→过滤→打分→单文档汇总→生成→写文件) | ✅ 34 项端到端离线测试,桩 LLM |
| DeepSeek provider 初始化/报错逻辑 | ✅ 假 key 验证通过;⚠️ 未做真实 API 调用 |
| JobSpy 调用签名 | ✅ 20 个参数全部存在 |
| JobSpy 输出字段映射 | ✅ **实测通过** —— 用到的 12 个字段在 34 个返回字段里全部命中 |
| LinkedIn 抓取 | ✅ **实测通过** —— 5/5 条,JD 正文 6000+ 字符 |
| Indeed 抓取 | ✅ **实测通过** —— 去掉 hours_old 后 5/5 条 |
| SEEK 搜索端点 | ✅ **实测通过** —— `jobsearch/v5`,200 + 5 条 |
| SEEK 详情端点(完整 JD) | ⚠️ 未实测 —— 运行时自动试探 3 个候选,失败则退回 teaser+bullets |
| 真实 LLM 打分质量 | ⚠️ 未验证 —— 需要 API key 实跑,只能你来 |

三个源现在都是开的。唯一没验证的是 SEEK 的**详情**接口(取完整 JD 正文)。代码会在首次调用时依次试 3 个候选端点,成功就缓存,全失败就退回用 `teaser` + `bulletPoints` 当描述并打一条 warning——不会崩,但 SEEK 岗位的打分质量会下降。日志里出现「SEEK 详情端点全部失败」就说明命中了这个降级路径。

实测顺带确认:**LinkedIn 的薪资字段 5/5 全空**。所以去重按 `seek > indeed > linkedin` 排优先级是对的,SEEK 是薪资数据的主要来源。

## 测试

```bash
python test_pipeline.py      # 52 项:薪资解析、归一化、去重、签证过滤、担保信号加扣分
python test_seek_parse.py    # 52 项:SEEK v5 字段解析、降级路径、日期过滤
python test_e2e_offline.py   # 34 项:全链路接线,桩数据 + 桩 LLM,含单文档汇总
python probe.py              # 实抓 5 条,验证数据源还活着
python probe2.py             # 端点失效时用来重新定位
```

## 已知脆弱点

**SEEK 用的是非公开接口,会再挂。** 2026-07-22 实测 `chalice-search/v4` 已废弃(404),现在用的是 `jobsearch/v5`。SEEK 下次改版这条还会断。日志里出现「SEEK 请求失败,可能端点又变了」就跑 `probe2.py` 重新定位,改 `seek_source.py` 里的 `_SEARCH_URL` 和 `_parse_item` 即可——`_first_label` / `_join_labels` 做了多形态兼容,字段结构小改一般不用动解析逻辑。

需要说明:本项目**没有**使用 GitHub 上那个 `BlackFalconData-org/seek-scraper` —— 那个仓库只有一个 README,没有代码,是 Apify 付费 actor 的推广页。SEEK 这块没有现成的开源方案可用,只能自己写,或者付费用 Apify($0.002/条)。

**LinkedIn 限流严重**,大约第 10 页开始。量大时在 `config.yaml` 的 `sources.proxies` 里配代理。Indeed 目前最稳。

平台规则、频率限制和自动化限制会变化。`applypilot-au` 只应在允许的流程中处理明确选择的岗位；它不会处理 CAPTCHA、绕过反自动化检测或替你接受平台条款。

## Dashboard 单岗位投递

系统支持精投与海投两条路径：DeepSeek 的岗位匹配分与所选简历的匹配度共同决定路径；默认处理 entry level / junior 岗位。24 小时内发布的合格岗位先于旧岗位进入执行，同一新鲜度内再按匹配分排序。固定评分规则属于已安装 Skill 的 `references/deepseek-scoring-rules.md`；普通运行不会重新生成，用户可从 `/scoring-rules` 审核、修改并自动备份。

点击 Dashboard 卡片的按钮会记录这一次具体岗位的用户授权，然后：

- LinkedIn、Indeed 与 SEEK 原站显示“打开并辅助投递”，由用户完成平台操作；
- 直接外部 ATS 显示“开始投递”，后台使用桌面应用内置的 Codex CLI 加载 `applypilot-au`，核验当前条款、候选人资料、简历、问题和页面安全门；
- 多次点击产生的浏览器任务在后台串行执行，避免抢占同一个登录会话；
- 登录、验证码、2FA、未知必填题、声明、测评或平台限制会显示“需要你处理”；
- 只有明确成功证据出现后才写入 `submitted`。

单岗位投递与完整 `run.py --autopilot` 流程共用同一内部 attempt 账本、Australia/Melbourne 每日软目标 30 和硬上限 40。详见 [BROWSER_APPLICATION_RUNBOOK.md](BROWSER_APPLICATION_RUNBOOK.md)。

## 调参提示

- 一个岗位都没到 70 分 → 先看 `jobs-ranked.csv` 的理由,通常是搜索词太宽或简历写得太简略,而不是阈值问题
- 去重移除比例超过 50% → 正常,说明中介重复挂得厉害
- 去重移除比例低于 10% → 检查 `norm_company` 是否漏了某种公司名后缀
- 同一公司名在不同来源写法不一致(品牌名 vs 集团名,或抓取时公司名解析失败变成 "Unknown")会导致去重漏判,目前是已知盲点,人工看排名时留意一下
