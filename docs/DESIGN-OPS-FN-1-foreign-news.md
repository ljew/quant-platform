# 技术契约：OPS-FN-1 国外最新资讯日爬

> 作者：Quant Architect · 2026-11-08（Asia/Shanghai）  
> 产品定稿：`/workspace/docs-design-foreign-news-daily.md`（Product **已定稿** 2026-11-08）  
> 切片 ID：**OPS-FN-1**（产品名：国外最新资讯日爬）· **W47 暂缓**，勿占 W47-*  
> Desk 拆法：**OPS-FN-1a**（日爬入库 + `#foreign-news` tab）→ **OPS-FN-1b**（最终总结纳入境外块 + DeepSeek 模型口径）  
> 对照体例：`docs-design-W47-1-paper-ux.md` / `docs-design-W46-2-walk-forward.md`  
> **硬闸门**：无实盘；密钥不进源码（X Bearer / DeepSeek 走 env 或 `llm_providers` 出库）；不擅自轮换密钥；不擅自推远程；不做付费墙破解；**本文只写设计，不改业务代码。**  
> **冻结状态（2026-11-08）**：**Product 已冻结**。确认：扩 `news_feed` 非新表；API 专用 `/news/foreign/*`；§6.1 五入口全纳入；旧 DeepSeek model 不自动改写 DB（预设引导）。无微调。Planner 可派 Desk **OPS-FN-1a**。  
> 读码基线：本机 `~/quant-platform` tip `cfe07e3`；box 快照 `/workspace/quant-latest` 可能滞后，**以本机仓为准**。

---

## 1. 背景 / 目标 / 非目标

### 1.1 背景（读码）

| 区域 | 现状 | 缺口 |
|---|---|---|
| 今日观点 hub | `web/src/App.tsx`：`today` / `news` / `reports` / `market` / `etfflow` / `wechat`；无境外 tab；`LEGACY_TAB_MAP` 无 `foreign*` | 缺 `#foreign-news` |
| 统一流 | `NewsFeedItem`（`news_feed`）+ `services/news_feed.py`：`SOURCE_LABELS` 仅国内（cls/eastmoney/sina/deepseek/tushare/wechat/guba）；`uid=sha1(source\|key)[:32]`；`RETENTION_DAYS=120`；`summary` 截 600 | 无 `fn_*` 源；无 RSS/X 适配器 |
| 国内调度 | `core/news_feed_scheduler.py`：快源 10min / 慢源每小时 :05 / 股吧独立；`GET /news/sync-status` | **不宜**把境外三槽塞进 10min 循环 |
| HotBrief | `services/hot_brief.py`：腿 = 财联社快讯 `F*` + DeepSeek 搜索 `S*`；`_ref_map` / `_resolve_src` 仅认 `[FS]`；system 已要求「海外能传导 A 股才留」 | **无自持境外日爬块**；隔夜外盘靠搜索碰运气 |
| HotBrief 调度 | `hot_brief_scheduler.py`：交易日 07/09/10/11/13/14/16/21（`:02`） | 境外应在 07:00 / 21:00 **先于**简报入库存货 |
| 报告 | `ReportsPage` ← `market_reports`（WorkBuddy 外生成、平台只读）；模板有「海外」章但**不读**本仓境外库 | 无平台侧境外摘要 API / 深链 |
| orchestrator | `build_report`：东财要闻 + 腾讯 `global_quotes`（**报价≠新闻**）+ 雪球热榜 | 资讯段无境外标题 |
| LLM | `llm_config.VENDOR_PRESETS` DeepSeek 仍列 `deepseek-chat` / `deepseek-reasoner`；`config.llm_model` 默认 `deepseek-chat`；密钥：DB `llm_providers` → `.env` `QUANT_LLM_API_KEY`；`mask_key` | 旧模型名已退役；须改预设/默认建议 |
| 密钥模式 | Tushare=`QUANT_TUSHARE_TOKEN`；LLM 如上；**无** X Bearer 字段 | 本切片新增 `QUANT_X_BEARER`（仅 env，不进源码） |

### 1.2 目标

1. **OPS-FN-1a**：RSS（默认 8 源）+ 可选 X API 日爬入库；「今日观点」下独立 tab **境外资讯** `#foreign-news`；源状态条 + 三槽调度 + 手动同步。  
2. **OPS-FN-1b**：凡「最终总结」入口纳入境外块；DeepSeek 预设升级为 `deepseek-flash`（默认）/ `deepseek-v4-pro`（回退）；无 LLM / 无 X 时 UI 明示降级。  
3. **单一入库**：境外与国内共用 `news_feed`，靠 `source` 前缀 `fn_*` / `fn_x` 区分；禁止各入口各写各爬。

### 1.3 非目标

| 不做 | 原因 |
|---|---|
| 实盘 / 券商 | 硬闸 |
| 付费墙正文破解 / 浏览器登录爬 / Nitter | 合规与稳定性 |
| X 密码或 cookie 进源码；依赖 Grok Bot X 工具 | 硬闸 + 平台独立 |
| Yahoo/Bloomberg/Reuters **官方**脆弱 HTML 主爬 | 产品探活失败 |
| 重做「全部资讯」IA 或砍国内源 | 超切片 |
| 完整多语言翻译引擎 | 可后置；中文摘要由 LLM 输出 |
| W47-1/2/3 落地 | 本轮暂缓 |
| 把境外塞进国内 10min 快刷 | 产品明确不做 |

---

## 2. 模块边界（1a / 1b）

### 2.1 OPS-FN-1a（日爬 + tab）

| 层 | 路径 | 职责 |
|---|---|---|
| 源注册表 | **新建** `backend/app/services/foreign_news_sources.py` | 写死源键/URL/默认开闭/标签；X 默认 handles |
| RSS 适配器 | **新建** `backend/app/services/foreign_news_rss.py` | HTTP 拉 RSS/Atom → 归一条目；单源超时 10s |
| X 适配器 | **新建** `backend/app/services/foreign_news_x.py` | X API v2 recent search / user timeline；读 `QUANT_X_BEARER`；无 key → `unconfigured` |
| 入库编排 | **新建** `backend/app/services/foreign_news.py` | 调适配器 → 复用 `news_feed.ingest` / `_rows` 路径；扩展 `SOURCE_LABELS`；整批 90s；返回 per-source 结果 |
| 调度 | **新建** `backend/app/core/foreign_news_scheduler.py` | 三槽 07:00 / 12:30 / 21:00 Asia/Shanghai；`get_status`；`QUANT_FOREIGN_NEWS` 开关（默认开） |
| 启动 | `backend/app/main.py` | `start_foreign_news_scheduler()`（try/except，失败不拖垮主服务）；logger 名挂上 |
| 路由 | `backend/app/routers/news.py`（或薄封装同文件） | `GET/POST` foreign list / sources-status / sync / sync-status（§4） |
| ORM | `models.NewsFeedItem` | **不改表结构**；`source` 已 `String(24)` 够用（最长 `fn_gnews_reu`=12） |
| 前端 tab | `web/src/App.tsx` | hub `today` 增 tab；`LEGACY_TAB_MAP`：`foreign`/`overseas_news`→`foreign-news`；`HubContent` 挂页 |
| 前端页 | **新建** `web/src/pages/ForeignNewsPage.tsx` | 源状态条 + 芯片筛选 + 列表 + 立即同步（可复用 NewsFeedPage 列表样式，**不**混国内源为主入口） |
| API client | `web/src/api/client.ts` | foreign 相关方法 |

### 2.2 OPS-FN-1b（总结 + 模型）

| 层 | 路径 | 职责 |
|---|---|---|
| 取数 | `hot_brief.collect_inputs` / **新建** `collect_foreign` | 过去 N 小时 `fn_*`/`fn_x` 条目；编号 `O*` |
| Prompt | `hot_brief.USER_TEMPLATE` / `_build_prompt` | 插入【境外日爬】块；输入顺序：快讯 → **境外** → 搜索 |
| 引用解析 | `_ref_map` / `_resolve_src` | 扩展认 `O\d+`（现仅 `[FS]`） |
| 合并 | `_llm_merge` | 语料可含境外主题（同 cfg；不另开通道） |
| 正式报告 | `orchestrator.build_report` | 资讯段追加境外标题 N 条（或「境外摘要」小节）；报价段不动 |
| 报告页 | `ReportsPage.tsx` | 链到 `#foreign-news`；可选展示「境外日摘要」片段 |
| 境外摘要 API | `GET /news/foreign/digest` | 供 WorkBuddy/人工粘贴；1b 交付（1a 可不做） |
| DeepSeek 预设 | `llm_config.VENDOR_PRESETS` + `config.llm_model` | `deepseek-flash` / `deepseek-v4-pro`；旧名退役勿新配 |
| 无 LLM UI | `HotBriefCard` / ForeignNewsPage / Reports | 文案：「未配置 LLM，仅列表不总结」 |

### 2.3 责任

| 角色 | 动作 |
|---|---|
| Desk | 冻结后 **1a → 1b** 两刀；一次一项 Doing |
| QA | §9 分刀勾选 |
| Architect | 本文；冲突补丁 |
| Product | 冻结文首 + 验收 |
| Planner | 冻结后派 Desk；W47 Ready 保留不派 |

---

## 3. 数据模型与源注册表

### 3.1 关键取舍：扩 `news_feed`，不新建表

| 方案 | 结论 |
|---|---|
| **A. 扩 `news_feed`（采用）** | 已有 uid 去重 / query / prune / NewsFeedPage 可按 source 筛；产品原则「三者共用同一境外入库」；`source` 前缀 `fn_` 一眼可辨 |
| B. 新表 `foreign_news` | 平行库增加 sync/API/UI 分叉；本切片无字段冲突需证伪 → **不做** |

落点对齐产品 §3.3：

| 字段 | 落点 |
|---|---|
| title | `title`（&lt;4 字丢弃，沿用现规约） |
| url | `url` |
| source | `fn_*` / `fn_x` |
| source_label | `SOURCE_LABELS` 扩展 |
| published_at | 源时间；失败则 `fetched_at` |
| summary | RSS description / X text，截 600 |
| extra_json.lang | 默认 `"en"` |
| extra_json.tags | 如 `["central-bank","us"]` |
| extra_json.handle / tweet_id | X 用 |
| uid | `sha1(f"{source}|{自然主键}")[:32]`；主键优先 url → guid/tweet_id → `title+published_at` |

**保留期**：复用 `RETENTION_DAYS=120`（与国内同 prune；境外不单开 90，避免两套清理）。  
**「全部资讯」**：可只读看到 `fn_*`（同一 query）；**主入口仍是 `#foreign-news`**，1a 不强制改 NewsFeedPage 默认筛选。

### 3.2 源注册表（写死；对齐产品清单）

#### 默认开（8）

| source | label | url | tags 建议 |
|---|---|---|---|
| `fn_fed` | Fed 新闻稿 | `https://www.federalreserve.gov/feeds/press_all.xml` | central-bank, us |
| `fn_fed_mon` | Fed 货币 | `https://www.federalreserve.gov/feeds/press_monetary.xml` | central-bank, us |
| `fn_sec` | SEC 新闻稿 | `https://www.sec.gov/news/pressreleases.rss` | regulator, us |
| `fn_bbc_biz` | BBC Business | `https://feeds.bbci.co.uk/news/business/rss.xml` | media |
| `fn_wsj_mkt` | WSJ Markets | `https://feeds.a.dj.com/rss/RSSMarketsMain.xml` | media |
| `fn_cnbc` | CNBC Top | `https://www.cnbc.com/id/100003114/device/rss/rss.html` | media |
| `fn_mw` | MarketWatch | `https://feeds.marketwatch.com/marketwatch/topstories/` | media |
| `fn_ecb` | ECB Press | `https://www.ecb.europa.eu/rss/press.html` | central-bank, eu |

#### 默认关（可配置打开）

| source | label | url | 备注 |
|---|---|---|---|
| `fn_ft` | FT | `https://www.ft.com/?format=rss` | 付费墙；仅标题级 |
| `fn_boe` | BoE News | `https://www.bankofengland.co.uk/rss/news` | 英央行 |
| `fn_gnews_reu` | GNews·Reuters 镜像 | Google News RSS（产品给定 query） | **非**路透官方 |
| `fn_gnews_bbg` | GNews·Bloomberg 镜像 | Google News RSS | **非**彭博官方 |

#### X

| source | label | 说明 |
|---|---|---|
| `fn_x` | X · 宏观账号 | 一条 source；`extra_json.handle` 区分账号 |

默认 handles（12）：`federalreserve`, `USTreasury`, `SecGov`, `IMFNews`, `ecb`, `bankofengland`, `WSJ`, `FT`, `Reuters`, `CNBC`, `business`, `DeItaone`。  
可选默认关：`FirstSquawk`, `Fxhedgers`, `SCMPNews`。

注册表结构（契约级伪代码，Desk 实现）：

```python
FOREIGN_SOURCES = [
  {"key": "fn_fed", "label": "Fed 新闻稿", "kind": "rss",
   "url": "https://www.federalreserve.gov/feeds/press_all.xml",
   "enabled_default": True, "tags": ["central-bank", "us"]},
  # ... 其余同上
  {"key": "fn_x", "label": "X · 宏观账号", "kind": "x",
   "enabled_default": True},  # 无 Bearer 时运行态 → unconfigured
]
```

启用态：进程内可被 API/配置覆盖（文件或简单 JSON 即可；**本切片不做**完整管理后台）。默认开闭以注册表为准。

### 3.3 `SOURCE_LABELS` 扩展

在 `news_feed.SOURCE_LABELS` **追加**全部 `fn_*` / `fn_x` 展示名（含默认关，避免入库后标签空白）。  
**不**把 `fn_*` 塞进 `FAST_SOURCES` / `SLOW_SOURCES`（国内调度不跑境外）。

---

## 4. 调度与 API

### 4.1 调度（`foreign_news_scheduler`）

| 项 | 值 |
|---|---|
| 时区 | **Asia/Shanghai**（`datetime.now()` 本机/容器已按 CST；与现有 news/hot_brief 一致，不引入 pytz 新依赖亦可） |
| 槽 | **07:00 / 12:30 / 21:00**（精确到分钟；每分钟 tick 检查「今日该槽是否已跑」） |
| 与 HotBrief | 07:00 / 21:00 槽 **早于** HotBrief `:02`，保证简报能吃到本轮入库 |
| 开关 | `QUANT_FOREIGN_NEWS` 缺省 `1`；`0`/`false` 关调度（手动 sync 仍可用） |
| 单源超时 | **10s**（落在产品 8–12s 中位） |
| 整批硬顶 | **90s**；超时则停后续源，已成功写入保留 |
| 隔离 | 单源异常 → 记 `last_error`，**继续下一源** |
| X 配额 | 独立计数；超配额当日该源 `degraded`，不重试死循环 |
| 状态内存 | 对标 `news_feed_scheduler._status`：`last_run_at` / `last_result` / `last_error` / `runs_total` / per-source 快照 |

**不做**：10 分钟级境外快刷；不并入 `news_feed_scheduler._loop`。

### 4.2 API（挂 `routers/news.py`，前缀保持 `/news`）

| 方法 | 路径 | 作用 | 刀 |
|---|---|---|---|
| `GET` | `/news/foreign/items` | 列表；内部 `news_feed.query(sources=fn_*)`；参数 `q/hours/limit/offset/sources` | 1a |
| `GET` | `/news/foreign/sources` | 源状态条：注册表 ∪ 运行态 ∪ 近 N 小时条数 | 1a |
| `POST` | `/news/foreign/sync` | 强制全源一轮（带 90s 顶）；返回 per-source `{added, status, error}` | 1a |
| `GET` | `/news/foreign/sync-status` | 调度器 `get_status()` | 1a |
| `GET` | `/news/foreign/digest` | 境外日摘要：最近 `hours`（默认 24）标题列表 + 可选短拼接文本；**不强制调 LLM** | 1b |

源状态条字段（产品验收）：

| 字段 | 含义 |
|---|---|
| `source` / `label` | 源键与展示名 |
| `enabled` | 配置是否启用 |
| `last_ok_at` | 上次成功写入 |
| `last_count` | 上次成功新增 |
| `last_error` | 最近错误摘要（截断）；成功则空 |
| `status` | `ok` / `degraded` / `unconfigured` / `fail` / `disabled` |

规则：

- 无 `QUANT_X_BEARER` → `fn_x` = **`unconfigured`**；文案：「未配置 X API，RSS 源仍可用」。  
- 默认关源 → `disabled`（或列表默认不展示；状态接口仍返回）。  
- **密钥/Bearer 永不回传前端**（状态里只布尔 `configured`）。

也可复用现有 `GET /news/items?sources=fn_fed,fn_x`；契约要求 **foreign 专用路径** 便于状态条与权限语义清晰，实现上允许 thin wrapper。

---

## 5. 前端 `#foreign-news`

### 5.1 导航

| 项 | 值 |
|---|---|
| Hub | `today`（今日观点） |
| tab key | `foreign-news` |
| 标签 | **境外资讯** |
| hash | `#foreign-news` |
| 插入位置 | `news`（全部资讯）与 `reports`（报告）之间 |
| LEGACY | `foreign` / `overseas_news` → `foreign-news` |

`HubContent`：`case "foreign-news": return <ForeignNewsPage />`。

### 5.2 页面骨架（对齐产品，不重做设计系统）

1. **顶栏源状态条**：每源芯片色 = status（ok 绿 / fail 红 / unconfigured 灰 / degraded 黄）。  
2. **筛选**：源芯片（可多选）+ 时间范围（24h / 72h / 7d）+ 关键词。  
3. **列表**：title / source_label / published_at / 外链；无正文爬取。  
4. **「立即同步」**：调 `POST /news/foreign/sync`；进行中 disable；结束后刷新列表+状态。  
5. **无 LLM**：本页 **不**强制总结按钮；若放「生成境外摘要」属 1b 可选，无 key 时 disable + 文案「未配置 LLM，仅列表不总结」。

视觉：复用 `NewsFeedPage` 的 PageHeader / Btn / 表格密度即可；**独立文件**避免国内/境外状态逻辑缠在一起。

---

## 6. 1b：最终总结纳入境外块

### 6.1 入口清单（必须完整）

| # | 入口 | 路径 | 本切片动作 |
|---|---|---|---|
| A | 热点简报 · 今日综述 | `HotBriefCard` ← `hot_brief.run_brief` → `summary` | **硬纳入**境外块；`src` 可含 `O*` |
| B | 热点简报 · 今日主题合并 | `_llm_merge` | 合并语料含境外主题 |
| C | 市场资讯报告 | `ReportsPage` / WorkBuddy | 平台提供 `digest` + 深链 `#foreign-news`；**不**改 WorkBuddy 仓（若不可达） |
| D | 自动研究正式报告 | `orchestrator.build_report` | 资讯段增加境外标题 N 条（建议 6）；**不**把 `global_quotes` 当新闻 |
| E | DeepSeek 搜索腿 | `collect_search` | **保留**兜底；顺序：快讯 → **境外日爬** → 搜索 |

**非最终总结**：`NewsFeedPage` / `#foreign-news` 原文浏览 — 不强制页内再总结。

### 6.2 Prompt 块形状

在现有「一手快讯 / 网页搜索」之间插入：

```text
【境外日爬】（平台自持 RSS/X，过去 {hours} 小时，共 {n_foreign} 条）
[O1] {title}｜{source_label}｜{summary[:300]}
[O2] ...
```

规则：

- 海外段**只写能传导到 A 股**的（保持现 SYSTEM_PROMPT §5）。  
- `src` 可写 `"O3"` / `"F1,O2"`；`_resolve_src` 正则改为 `[FSOfso]\s*\d+`。  
- 境外块为空：summary 海外段写明「境外源本轮无新增或未配置」，**禁止编造外盘新闻**。  
- `collect_foreign(hours=...)`：读 `news_feed` where `source like 'fn_%'`，按 `published_at` 倒序，上限建议 40 条（防 prompt 爆）。  
- HotBrief 窗口：与 `collect_flash` 同一 `minutes` 对齐；隔夜窗（21→07）用 scheduler 已有 window 逻辑，境外 `hours=max(minutes/60, 1)` 且 cap 12h。

### 6.3 缺 LLM / 缺 X 行为

| 场景 | 行为 |
|---|---|
| 无 LLM key | 列表/日爬照常；HotBrief / 总结按钮 → `LLMUnavailable` → UI「未配置 LLM，仅列表不总结」 |
| 无 X Bearer | `fn_x=unconfigured`；RSS 照常；总结仍用 RSS 境外块 |
| 境外块 0 条 | prompt 仍带空块说明；模型禁止编造 |

### 6.4 orchestrator / Reports

- `build_report`：在「东财要闻」旁或下增加 **「境外资讯」** `<ul>`，取 `foreign_news` 最近成功条（无则一行「暂无境外入库，见平台境外资讯」+ 纯文本提示）。  
- `ReportsPage`：Header 或空态增加按钮/链接「查看境外资讯」→ `quant-nav` `#foreign-news`。  
- WorkBuddy：1b 文档注明「生成侧应读 `/news/foreign/digest`」；本仓 Desk **不**改外部 WorkBuddy 仓库。

---

## 7. DeepSeek 模型预设迁移（1b）

| 角色 | model id | 动作 |
|---|---|---|
| 默认推荐 | **`deepseek-flash`** | `VENDOR_PRESETS` DeepSeek `models[0]`；`config.llm_model` 默认值改为 flash |
| 回退 / 重活 | **`deepseek-v4-pro`** | `models[1]`；HotBrief 默认仍用当前 resolve 的 model（不强制每轮切 pro） |
| 退役 | `deepseek-chat` / `deepseek-reasoner` | 从预设列表移除；**不**自动改写用户 DB 里已存旧 model 名（避免静默劫持）；连通性失败时 UI/文案提示「旧模型已退役，请改为 deepseek-flash」 |

思考模式：日更简报默认 **非 thinking / low**（若 API 支持开关则关；无开关则不传）。  
密钥口径不变：DB `llm_providers` 优先 → `.env` `QUANT_LLM_API_KEY`；响应一律 `mask_key`。

`LlmConfigModal`：datalist 来自 `/llm/presets`，改预设后前端自动跟上；placeholder 文案由 `deepseek-chat` 改为 `deepseek-flash`。

---

## 8. 密钥出库

| 密钥 | 出库方式 | 禁止 |
|---|---|---|
| DeepSeek / 其它 LLM | 现有 `llm_providers` + `QUANT_LLM_API_KEY` | 写进源码 / 前端明文 |
| X API Bearer | **仅**环境变量 **`QUANT_X_BEARER`**（`.env` / compose secrets） | 进 git；存 X 密码；搬 Bot cookie；API 回传明文 |
| 可选后续 | 若需 UI 配置 X：比照 `llm_providers` 另表或同表 vendor=`x`——**本切片不做**，避免扩 scope |

`foreign_news_x.py`：`token = os.getenv("QUANT_X_BEARER", "").strip()`；空 → 跳过拉取并标 `unconfigured`。  
日志：禁止打印 Bearer；错误信息截断且脱敏。

---

## 9. 验收清单

### 9.1 OPS-FN-1a

- [ ] 「今日观点」下有独立 tab **境外资讯**（`#foreign-news`）；`foreign`/`overseas_news` 别名可跳转  
- [ ] 默认 ≥5 个（目标 8）已探活 RSS 可同步入库；列表可见 title / source / time / 外链  
- [ ] 源状态条展示上次成功、条数、错误；单源失败不清空其它源  
- [ ] 07:00 / 12:30 / 21:00（Asia/Shanghai）可在 `sync-status` 查询；支持「立即同步」  
- [ ] X：有 Bearer 则默认账号可入库；无 Bearer 显示 `unconfigured` 且 RSS 不受影响  
- [ ] 重复同步不爆表（uid 幂等）；Bearer/LLM key 不出现在前端与仓库  
- [ ] 国内快讯调度 / NewsFeedPage 回归不坏  

### 9.2 OPS-FN-1b

- [ ] HotBrief 输入含【境外日爬】；`summary` 海外段能引用真实 `O*` 或显式「无新增/未配置」  
- [ ] `_resolve_src` 识别 `O*`；不把境外误标为财联社  
- [ ] `VENDOR_PRESETS` / 新装默认建议为 `deepseek-flash`；可选 `deepseek-v4-pro`；旧 chat/reasoner 不在预设  
- [ ] 无 LLM key 时总结入口明示「仅列表不总结」  
- [ ] `build_report` 资讯段含境外（或明确链出）；Reports 可发现 `#foreign-news`  
- [ ] `GET /news/foreign/digest` 可用  
- [ ] 不回归：国内快讯、HotBrief 时段表、搜索腿开关  

---

## 10. 落地序与风险

### 10.1 Desk 落地序

| 序 | 刀 | 建议步骤 | 估时 |
|---|---|---|---|
| 1 | **1a** | 注册表 + RSS 适配器（`httpx` + stdlib `xml.etree`，**不**新加 feedparser 除非必要）→ `foreign_news.sync` → 扩展 `SOURCE_LABELS` → scheduler + main 挂载 → API → `ForeignNewsPage` + App tab | ~1 Desk 日 |
| 2 | **1a** | X 适配器 + `QUANT_X_BEARER` + unconfigured 态（可与上同 PR 或紧随） | +0.3–0.5 |
| 3 | **1b** | `collect_foreign` + prompt/`_ref_map`/`_resolve_src` → HotBrief 接线 | ~0.5 |
| 4 | **1b** | DeepSeek 预设 + config 默认 + Modal placeholder | ~0.2 |
| 5 | **1b** | orchestrator 境外列表 + digest API + Reports 深链 + 无 LLM 文案 | ~0.5 |

**门禁**：1a 合入（至少 RSS+tab+调度）后再拉 1b；禁止「无数据先改 prompt」。

### 10.2 风险与缓解

| 风险 | 缓解 |
|---|---|
| RSS 源偶发 403/空 | 单源隔离；状态条可见；默认关脆弱源 |
| SEC/Fed 要 User-Agent | 适配器统一带浏览器 UA；契约要求 Desk 测通 |
| X 配额/费用 | 三槽 + 账号白名单；超配额 degraded |
| Prompt 过长 | 境外 cap 40；summary 截 300 |
| 旧 DB model 名失效 | 不自动改写；预设引导用户手改；test 接口报错可读 |
| 时区 | 与现网一致用本地 now；容器须 CST（已有） |
| 整批 90s 与同步请求 | sync API 同步等待；前端 timeout ≥ 100s 或展示「后台同步中」 |

### 10.3 明确不做的依赖

- 不引入 OpenBB 运行时 / Finnhub 付费唯一源  
- 不依赖 Grok Bot X MCP  
- 不加 feedparser **除非** stdlib 解析实战失败（优先 stdlib）

---

## 附录 A · 读码锚点

| 区域 | 路径 |
|---|---|
| 导航 | `web/src/App.tsx` |
| 全部资讯 UI | `web/src/pages/NewsFeedPage.tsx` |
| 简报 UI | `web/src/components/HotBriefCard.tsx` · `pages/TodayPage.tsx` |
| 报告 UI | `web/src/pages/ReportsPage.tsx` |
| 统一流 | `backend/app/services/news_feed.py` · `models.NewsFeedItem` |
| 国内调度 | `backend/app/core/news_feed_scheduler.py` |
| HotBrief | `backend/app/services/hot_brief.py` · `core/hot_brief_scheduler.py` |
| LLM | `backend/app/services/llm_config.py` · `config.py` · `routers/llm.py` · `LlmConfigModal.tsx` |
| 正式报告 | `backend/app/services/orchestrator.py` → `build_report` |
| 启动 | `backend/app/main.py`（news_feed / hot_brief 挂载旁挂 foreign） |

## 附录 B · 产品对齐核对

| 产品决定 | 本契约 |
|---|---|
| 切片 OPS-FN-1；1a→1b | §0 / §2 |
| `#foreign-news` 境外资讯 | §5 |
| RSS 默认 8 + 可选关 + X Bearer | §3.2 / §8 |
| cron 07:00/12:30/21:00 + 手动；单源隔离；90s | §4.1 |
| 最终总结全入口纳入 | §6.1 |
| deepseek-flash / v4-pro；旧名退役 | §7 |
| W47 暂缓 | 文首 / §1.3 |
| 扩 news_feed 非新表 | §3.1（Architect 确认采用） |

---

**Product 已冻结（2026-11-08）**：§3.1/§4/§6.1/§7 均按契约推荐确认。Planner 派 Desk **OPS-FN-1a**。
