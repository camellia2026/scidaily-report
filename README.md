# 每日科学前沿报告

每天早上自动生成一份科学前沿简报，推到你的邮箱。覆盖 NSC（Nature/Science/Cell）、
四大医学刊（NEJM/Lancet/JAMA）、CS 顶刊（IEEE TPAMI / IEEE TC / IJCV / JMLR）、
计算数学刊（SIAM SISC / Acta Numerica），以及值得看的 arXiv 预印本与科学要闻。

跑在 **GitHub Actions** 上——因为晚上会停电、电脑会离线，本地定时器靠不住。

---

## 它和「AI 日报」那个项目的关系

骨架继承自 `每日ai发展报告`，但换知识域时撞上了一道硬墙：

> **原项目建立在 RSS 之上，而顶刊没有可用的 RSS。**

实测（2026-10-01）：

| 源 | 结果 |
|---|---|
| Nature / Science / Cell / NEJM / Lancet | 有 TOC feed，可用 |
| **JAMA** | 地址查得到，但 RSS 返回 **403** |
| **IEEE TPAMI / IJCV / JMLR / SISC / Acta Numerica** | **根本没有 feed** |
| **JMLR 在 Crossref** | `total=0`——该刊不在 Crossref 索引体系内 |

所以采集层从「抓 feed」整个重写成「查学术 API」，主力是 **OpenAlex**。
这一条推翻了原项目的一个隐含前提，是本项目最大的新增工作量。

---

## 它长什么样

```
reports/2026-10-02.md          ← 给人看的报告
data/history/2026-10-02.json   ← 给程序看的双胞胎（含源健康、校验结果）
data/seen.json                 ← 跨天去重状态（这就是「日报」和「信息洪流」的区别）
tests/fixtures/api/            ← 录制的 API 响应固件（离线可跑）
```

报告结构：

```
今日综述
版本升级（预印本 → 正式发表）   ← 只有出现时才有一节
热度上升                        ← 引用速度热文
顶刊论文                        ← 按预打分排序，中英双语
预印本前沿（未经同行评审）
科学要闻
值得继续关注
数据源状态                      ← 表格：哪个 API 挂了，一眼看出
原始链接
```

---

## 快速开始（本地，不需要任何 API key）

```bash
pip install -r requirements.txt

# 纯离线冒烟：读固件，不联网、不推送、不调模型
python run.py --smoke

# 规则模式出一份真报告（联网抓 API，但不调模型）
python run.py --no-llm --dry-run

# 逐个源体检：哪个 API 挂了、哪个期刊 0 条
python run.py --check-sources

# 跑测试（187 个，零网络）
python -m pytest tests/ -q
```

`reports/<今天>.md` 就是产物。**这一步不需要任何 key。**

---

## 完整命令

| 命令 | 作用 |
|---|---|
| `python run.py --smoke` | 离线冒烟（= `--provider mock --offline --dry-run`） |
| `python run.py --no-llm --dry-run` | 规则模式，不调模型，不推送 |
| `python run.py --dry-run` | 真跑（调模型）但不推送 |
| `python run.py` | 真跑并推送 |
| `python run.py weekly` | 每周顶刊盘点（慢刊回看） |
| `python run.py --check-sources` | 逐源体检 |
| `python run.py --seed` | 抓一次固化成离线固件 |
| `python run.py selftest` | 发一封测试邮件，验证推送渠道 |
| `python run.py --force` | 忽略「今天已生成」强制重跑 |

---

## 它怎么工作

```
sources/openalex.py   主力：一次 OR 请求覆盖全部期刊 ISSN
                      自带 locations 聚类（预印本↔正式版同一 work）
                      自带 counts_by_year（引用速度原料）
   ↓
sources/pubmed.py     医学刊摘要的最佳来源；JAMA 的唯一抓手（RSS 403）
sources/crossref.py   补漏（无摘要）
sources/rss.py        期刊 TOC 与科学要闻
   ↓
sources/merge.py      预印本 ↔ 正式版合并 + 「版本升级」判定
   ↓
filter.py             去重 → 期刊分级 → 学科分区 → 预打分 → 分通道候选
   ↓
velocity.py           引用速度热文
   ↓
ai.py                 LLM → 结构化 Digest（中英双语）
   ↓
render.py             校验 + 渲染 → md / json / html   ★ 反幻觉闸门
   ↓
notify.py             邮件（multipart/alternative）
```

---

## 四个增强点（相对于原项目）

### 1. 学科分区

报告分四节：`综合` / `医学` / `计算机` / `数学与计算数学`。
分区由期刊元数据决定，不由模型决定。

### 2. 期刊分级权重

`T0`（NSC + 四医学刊）= 3.0，`T1`（TPAMI/TC/IJCV/JMLR）= 2.0，`T2`（SIAM）= 1.4。
进预打分。改 `config/journals.yaml` 一行即可调整。

### 3. 预印本 → 正式发表合并

**OpenAlex 已经把同一篇论文的 arXiv 版和期刊版聚在同一个 work 里。**
实测一篇 Nature 论文的 `locations` 同时含 Nature(publishedVersion)、PubMed、
PubMed Central、机构库(submittedVersion)。所以合并的实质不是「事后拼」，
而是**按 work id 去重**。

副产品是一条原项目没有的能力——**版本升级**：

> 一篇上周以预印本身份报过的论文，本周正式发表在 Nature 上。

按「不重复上报」的铁律它该被静默，但那本身就是新闻。所以规则从
「不重复上报」升级为「**不重复上报，但升级要报一次**」。

### 4. 引用速度热文

原项目只按新鲜度排序，对科学文献不够。但**按引用数排序是错的**——
实测热文候选榜首是 JAMA 重新发表的《赫尔辛基宣言》，总引用 1523，
按总量排它永远第一。那是「著名」，不是「热」。

指标：`hotness = 近年引用数 × 近年引用占比`，加 24 个月年龄上限。

**实测修正**：占比法并没有拉下《赫尔辛基宣言》——OpenAlex 把它 2024 年的
重新发表当成独立新 work，近年占比是 98%。真正解决问题的是**粘性去重**：
热文和新闻走同一套 seen 去重，同一篇论文第一次跨过阈值时报一次，之后静默。
这一条是实测逼出来的，不是设计时想到的。

---

## 部署到 GitHub Actions

### 1. 建仓库并推送

```bash
git init && git add -A && git commit -m "init"
git remote add origin git@github.com:<你>/scidaily-report.git
git push -u origin main
```

### 2. 配 Secrets

`Settings → Secrets and variables → Actions → Secrets`：

| 名字 | 值 |
|---|---|
| `DEEPSEEK_API_KEY` | DeepSeek 的 key |
| `MAIL_USERNAME` | 你的 QQ/163 邮箱地址 |
| `MAIL_AUTH_CODE` | **SMTP 授权码，不是登录密码** |
| `MAIL_TO` | 收件地址 |
| `NCBI_API_KEY` | 可选。有则 PubMed 提速到 10 req/s |

`Variables`（非密配置）：`SCIDAILY_CONTACT`（礼貌池邮箱）、`LLM_PROVIDER`、
`LLM_FALLBACK_PROVIDER`、`MAIL_SMTP_HOST`、`MAIL_SMTP_PORT`、`SCIDAILY_ENV=ci`。

> 键名和 `.env` 里的**完全一致**是刻意的：代码走 `os.getenv`，
> 所以同一份代码在本地（读 `.env`）和 CI（读 Actions 注入）之间零分支。

### 3. 先验证渠道，再信任它

Actions 页面点 **Run workflow**，勾选 `selftest`。手机收到测试邮件 = 渠道通了。

**不要跳过这一步。** 等到第一个早上才发现授权码写错了，代价是这个习惯本身。

---

## 关于「每天早上 7 点」的诚实说明

**GitHub Actions 做不到准点。** 这不是配置问题，是它的设计：

- cron **只认 UTC**。北京 05:23 = `23 21 * * *`（前一天 21:23 UTC）
- 官方文档明确写了：定时任务**在高负载时会延迟，整点尤其严重**
- 定时任务只在**默认分支**跑，fork 里不跑，最短间隔 5 分钟
- 公开仓库**连续 60 天无活动会自动禁用**定时任务

三层应对：避开整点（`:23`/`:47`）、双 cron 兜底、幂等保证
（「已生成**且**已推送」才算完成）。

**准确的心理预期是「早上 5:20–7:30 之间送到」，不是 7:00 准点。**

---

## 设计决策（改代码前请先读）

| 决策 | 为什么 |
|---|---|
| **`report_date()` 是全项目唯一日期来源** | cron 延迟跨过 UTC 午夜时，用错日期会把报告归档到错误的一天，同时污染去重窗口。任何地方不要裸用 `datetime.now()` |
| **去重键是 OpenAlex work id，不是 URL/DOI** | 只有 work id 能把预印本和正式版归到一起。DOI 不行——arXiv 版和 Nature 版是两个不同的 DOI |
| **`ref` 在截断之后分配，且跨通道全局重编号** | 五个通道各自从 1 编号，直接拼起来会有五个 `ref=1`。错位的 ref 查不出「越界」，只会让条目静默落到错误的节里 |
| **分节由通道决定，不由模型决定** | 让模型输出 section 只会引入一整类可避免的错误（把预印本归到顶刊论文里） |
| **热文通道也走 seen 去重** | 热文有粘性，不去重的话同一篇论文会天天出现 |
| **论文窗口 72 小时，新闻 24 小时** | 期刊不是均匀发布的：NEJM 每周四出刊、Acta Numerica 一年一卷。24 小时窗口会让很多天「一篇论文都没有」 |
| **论文新鲜度半衰期 48h，新闻 12h** | 三天前的 Nature 论文仍值得看，三天前的科技新闻不一定 |
| **预印本用「引用数阈值」而不是「最新」** | arXiv 每天约 3800 篇，全量灌进来只会淹掉报告。而且 OpenAlex 对 arXiv 有 4-5 天收录滞后（实测 09-28 有 2465 篇、10-01 只剩 12 篇） |
| **校验失败折中处理** | 单条 `refs` 越界 → 丢弃该条；丢弃率 >30% → 不推送 + `exit 1`。全量一票否决会因为一条坏引用让整个早上没有报告 |
| **邮件用 multipart/alternative** | 手机上看纯 Markdown 很难受，HTML 那一 part 不能省 |

---

## 已知限制（不要以为它比实际更聪明）

### 反幻觉闸门拦的是「编造引用」，不是「编造内容」

`render.py::sanitize` 能保证：模型引用的编号真实存在、报告里的 URL 都在本次抓取集里、
摘要不是雷同的填充文字。

它**不能**保证模型把某篇论文总结对了。真正的防线是：短抽取式摘要 +
每条带原始链接 + 你自己看一眼。报告末尾那句免责声明是认真的。

### 摘要覆盖率不是 100%

实测 347 篇期刊论文里只有 122 篇 OpenAlex 带摘要（35%）。
PubMed 通道补上了医学刊，但 CS 与数学刊的覆盖率仍然有限。
没有摘要的条目会显示 `(该源未提供摘要)`，模型只能依据标题写——报告里会体现出来。

### 顶刊里的「非研究内容」只能挡掉大部分

JAMA / NEJM 会发新闻稿、社论、通讯、播客、人文小品。它们在 OpenAlex 里
`type=article`、`type_crossref` 又是空的——**OpenAlex 没有分辨它们的信息**。
三层过滤实测下来：

| 手段 | 效果 |
|---|---|
| 惩罚词（podcast / in this issue / …） | 挡掉播客与固定栏目 |
| PubMed `PublicationType` 黑名单 | 挡掉社论、通讯、新闻稿（实测排除 12–108 条） |
| 三项全空判据（无摘要 + 无主题 + 无参考文献） | 挡掉纯非学术条目（"Holding Hope"、"The Mayor"、刊名记录） |

**残余**：有主题但没摘要的 JAMA 新闻（实测 "Updated Stroke Guideline
Emphasizes Early, Holistic Intervention"）仍会出现在列表里。
规则层不硬猜——那类交给 LLM 那一层判断，日报里它会被自然降权。

> 一条走过弯路记在这里：一开始试的是「用 PubMed 决定保留哪些」，结果把
> NEJM 的正经研究论文整批删了——**PubMed 的 `[EDAT]`（入库日）与 OpenAlex 的
> `publication_date`（出版日）不是一回事**，一篇两周前上线的论文可能几个月前
> 就入库了，于是被当成「PubMed 不认识」而删掉。
> 只用来**排除被明确判定为非研究**的条目就安全了。

### 速度指标只有年度粒度

OpenAlex 的 `counts_by_year` 是年度桶，没有可过滤的「近期引用」字段。
真正的「近 90 天被引」需要对短名单逐个查询（Tier B，每篇 1 个请求），
所以只对前 10 篇做。

### 源会死，而且死法很怪

实测样本（2026-10-01）：

| 源 | 结果 | 结论 |
|---|---|---|
| `science.org/rss/news_current.xml` | curl 200 / httpx 403 | **TLS 指纹拦截，不是 UA 问题** |
| `jamanetwork.com/rss/...` | 403 | 走 PubMed 兜底 |
| Cambridge Core RSS | 403 | 走 OpenAlex/Crossref |
| OpenAlex / Crossref / PubMed | 200 | 稳定 |

`science.org` 那一条值得单独说：同一时刻 **curl（任意 UA）全通、httpx 全拒**，
换 UA、换 Accept、换 HTTP/2 都无效——说明它做的是 JA3 层面的拦截。
这是原项目「User-Agent 三态」教训之外的**另一类坑：换 header 解决不了的问题，
换客户端才可能解决**。本项目选择性牺牲这一条源（已有 4 个可用要闻源），
想救的话用 curl 做传输层是唯一验证过可行的路。

### 快报的三个真实边界

- **OpenAlex 对 arXiv 有 4–5 天收录滞后**：所以预印本通道改用
  「引用数阈值 + 60 天窗口」，等一篇预印本攒够引用时索引早已跟上。
  代价是**看不到「昨天刚出的预印本」**——那是 arXiv RSS 能做的、OpenAlex 做不到的。
- **Acta Numerica 一年一卷**：日报里几乎永远是空的，这是正常的，不是源坏了。
  它靠每周盘点覆盖（窗口自动放宽到 90 天）。
- **JMLR 不在 Crossref**：只能走 OpenAlex。去掉那条通道这一刊就永远是空的。

---

## 成本

- **Actions 分钟**：日跑约 1–2 分钟（OpenAlex 一次请求覆盖全部期刊，
  实测 347 条论文 8 秒抓完）。
- **OpenAlex**：约 20 次请求/天，对照响应头里的 `X-RateLimit-Limit: 1000`
  配额，占 2%。免费，无需 key。
- **LLM**：约 100 条候选 × 90 token ≈ 9k 输入 + 2k 输出/天。
  DeepSeek 约 **¥0.15–0.4/天 ≈ ¥5–12/月**。
- **邮件**：¥0。

省钱的杠杆是「送摘要而不是全文」——这同时是成本杠杆、延迟杠杆，
**也是反幻觉杠杆**：上下文里的原始素材越少，可编造的空间越小。

---

## 目录

```
├── run.py                      入口
├── scidaily/
│   ├── settings.py             配置加载 + report_date()
│   ├── state.py                跨天去重 + 版本升级状态
│   ├── sources/
│   │   ├── base.py             记录模型 + 节流 + 礼貌头
│   │   ├── cache.py            TTL 缓存 + 离线固件
│   │   ├── openalex.py         主力采集
│   │   ├── pubmed.py           医学刊摘要 + JAMA 兜底
│   │   ├── crossref.py         补漏
│   │   ├── rss.py              期刊 TOC 与科学要闻
│   │   └── merge.py            预印本↔正式版合并
│   ├── filter.py               去重 / 分级 / 分区 / 预打分
│   ├── velocity.py             引用速度热文
│   ├── ai.py                   LLM → Digest（双语）
│   ├── render.py               ★ 反幻觉闸门 + 分节渲染
│   ├── notify.py               邮件推送
│   ├── preflight.py            启动自检
│   ├── summary.py              Actions 运行摘要
│   └── main.py                 编排 + CLI
├── common/                     从原项目 vendored 的 7 个模块
├── config/
│   ├── journals.yaml           期刊清单（ISSN / 分级 / 分区 / 通道）
│   └── settings.yaml           窗口 / 配额 / 关键词 / 推送
├── tests/                      187 个测试，零网络
│   └── fixtures/api/           录制的 API 响应
├── reports/                    报告归档（提交进仓库）
├── data/                       状态与历史（提交进仓库）
└── .github/workflows/          daily / weekly / ci
```

### 关于 `common/`

从原项目 vendored 了 7 个模块（`config` `llm` `mock_llm` `messages`
`structured` `console` `reliability`），**并去掉了 6 个**：
`agent` `context` `hitl` `subagent` `tools` `tracing`——它们服务于
「工具调用 / 多智能体 / 上下文压缩」拓扑，本流水线是单次 `call_structured`，
那 966 行（约占原包 46%）在这里是死代码。

保留了一处有价值的改动：`llm.py` 里 OpenAI 兼容分支的响应守卫
（`resp.choices` 为空时抛出带诊断信息的 `LLMError`），来自原项目 2026-09-25 的事故复盘。

---

## 依赖

`openai`、`python-dotenv`、`PyYAML`、`pydantic`、`httpx`、`feedparser`、`pytest`。

**不需要** `anthropic`（`common/llm.py` 只在 anthropic 分支里延迟导入它）、
**不需要** `markdown` / `pygments`。

---

## 测试

187 个测试，全部离线、确定性。重点覆盖的都是**真实踩到过**的失败：

| 测试 | 钉住的事故 |
|---|---|
| `test_ai::TestClipRespectsSchema` | config 的 `max_title_chars=90` 撞上 pydantic 字段的 `max_length=60` → 规则模式直接崩 |
| `test_render::TestSmallSampleGate` | 单条候选 + 占位摘要被判「100% 过短」→ 整份报告误判不合格 |
| `test_merge::TestVenueResolution` | OpenAlex 的 `issn_l` ≠ 印刷版 ISSN（JAMA：0002-9955 vs 0098-7484）→ T0 医学刊静默降级成 T2 |
| `test_render::TestUrlCleaning` | Cell 链接含括号被正则截断 → 合法链接被误报成伪造链接 |
| `test_filter::TestRefNumbering` | 五个通道各自从 1 编号 → `ref` 冲突，条目静默落到错误的节 |
| `test_state::TestVenueUpgrade` | 版本升级必须只上报一次 |
| `test_velocity` | 老名篇不能被算成热文 |
