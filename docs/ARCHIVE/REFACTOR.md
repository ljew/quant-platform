# 平台重构方案 —— 数据分层 · 投研流水线 · 因子工厂

> 状态：**待确认**。确认后按 Phase 1~5 顺序实施。
> 动机：当前数据采集/清洗/打分逻辑内嵌在各脚本中，结构化与非结构化数据无分层，
> 因子探索直接查业务库——新数据源接入成本高、清洗不可复用、无法重算历史。

---

## 一、现状问题诊断

| # | 问题 | 具体表现 |
|---|---|---|
| 1 | 原始数据与加工数据混在一起 | tushare 拉取→清洗→upsert 业务库一步完成，无法回溯原始数据 |
| 2 | 非结构化管道不成体系 | 公众号打分词典硬编码在 `build_news_factors.py`，换打分方法无法重算 |
| 3 | 无数据质量门禁 | 缺失/异常/重复静默通过，监控只能看到最终行数 |
| 4 | 因子缺乏统一管理 | 14 内置因子在 `factor_library`，GP 精英/文本因子各自存表，口径不一 |
| 5 | 新数据源接入无标准路径 | 每加一个源就要改 etl_daily / 新写脚本 |

## 二、目标架构（Medallion 四层）

```
L0 源层        tushare · akshare · wechat 语料库 · 头条语料(TODO) · (预留:LLM打分API)
               │ extractors/（一源一提取器 → 标准化schema，原样落盘）
L1 原始层 Bronze    data/raw/{market,text}/   ← Parquet，按抓取批次分区，只增不改
               │ cleaners/（日历对齐·去重·简称关联·质检指标计算）
L2 清洗层 Silver    data/silver/{bars,news_mentions,sentiment}/  ← Parquet + quality report
               │ factor engine（因子注册表驱动，逐因子计算截面）
L3 因子层 Gold      factor_values(SQLite/DuckDB 双写，现 factor_daily 升级版)
               │
L4 应用层       回测引擎 · GP挖掘 · 组合分析 · 监控 · REST/WebSocket
```

### 关键设计决策

1. **Parquet 分区存储**（新增 pyarrow 依赖）：Bronze/Silver 走列式文件而非 SQLite——
   分析查询快、支持全量重算、Git 不able 但数据可再生成。
2. **SQLite = 业务唯一事实**：回测记录/模拟盘/订单/结果仍在 quant_dev.db，不动。
3. **DuckDB = 分析查询层**：Gold 表继续自动同步（现有 duckdb_sync 机制保留）。
4. **打分器版本化**：`SentimentScorer` 统一接口，v1 词典 / v2 LLM 可切换可并存，
   Silver 层情绪得分带版本号，换打分方法只需重算该层。
5. **兼容策略**：旧 API/旧表不动，factor_daily 由注册表重新灌入，前端无感。

## 三、核心模块设计

### 3.1 数据枢纽 `app/datahub/`

```
app/datahub/
├── registry.py          # 数据集注册表：名称/schema/版本/路径
├── extractors/
│   ├── base.py          # BaseExtractor: fetch() -> Bronze 落盘
│   ├── bars_tushare.py  # 个股/指数日线
│   └── text_wechat.py   # 公众号文章（对接 knowledge.db）
├── cleaners/
│   ├── bars_cleaner.py  # 日历对齐/停牌标记/前后复权一致性校验
│   ├── text_cleaner.py  # 正文清洗/公司简称关联 -> mentions(date,symbol,article_id,bull,bear)
│   └── quality.py       # 质检：行数环比/缺失清单/极值检出 -> quality_report.json
├── scorers/
│   ├── base.py          # SentimentScorer.score(texts) -> scores
│   ├── dict_v1.py       # 现 24+26 词典法（迁移自 build_news_factors.py）
│   └── llm_v2.py        # DeepSeek/Qwen 批量打分（key 来自 XiaoAn .env，已确认存在）
└── pipelines.py         # daily_pipeline(): extract->clean->score->gold 调度入口
```

### 3.2 因子注册表 `factor_registry` 表

```
name | expr | direction | category | source(builtin/mined/news) | status | created_by | tested_at
```
- 14 内置因子迁移进表；`factor_library.py` 变为从表加载（保留内置 fallback）
- 单因子挖掘/GP 精英 → "登记待审"，一键启用后进入 ETL 计算清单
- `news_senti` 作为派生变量在 datahub 层提供（不入因子表，与 pe_ttm 同级）
- **值仍落 factor_daily 宽表**（新因子加入时扩列或转长表视数量而定）

### 3.3 每日管道（替代 etl_daily 的内部流程）

```
17:00 触发:
  ① extract_bars(增量) → bronze           [已有 _tushare_qfq 迁入]
  ② extract_articles(增量) → bronze       [新增]
  ③ clean_bars → silver + quality report
  ④ clean_text: 提及关联 + sentiment(score_version) → silver
  ⑤ gold: 因子注册表逐个计算截面 → factor_daily（含启用状态的挖掘/文本因子）
  ⑥ duckdb_sync（现有）
失败隔离：任一步骤失败停止后续，报告写入 quality report 并反映到监控页
```

## 四、实施计划（5 个 Phase）

| Phase | 内容 | 产出 | 预估 |
|---|---|---|---|
| P1 | datahub 骨架 + registry + 两个 extractor → Bronze parquet | pyarrow 依赖 / raw 层数据 / 注册表 | 半天 |
| P2 | cleaners + quality report + bars/text 清洗进 Silver | silver 层 + 质检 JSON + 监控页层数卡片 | 半天 |
| P3 | 打分工厂：dict_v1 迁移 + llm_v2 实现（DeepSeek 批量） | 版本化情绪得分，词典/LLM 对比 | 一天 |
| P4 | 因子注册表 + GP/挖掘精英登记流转 + factor_daily 注册表重算 | 因子全生命周期闭环（发现→验证→登记→每日生产） | 一天 |
| P5 | etl_daily 切换 pipeline_runner + 全链路回归 + 监控页数据血缘展示 | 新旧并行一周后下线旧路径 | 半天 |

**风险与依赖**
- pyarrow 安装（venv + 容器 requirements 各一次）
- LLM v2 需要 DeepSeek key 配置注入容器 env（XiaoAn .env 中 key 已确认存在）
- 头条 10 万语料仍未定位——P2 的 text extractor 先支持两种源格式，定位后即插
- 回归风险控制：P5 前 metabase 旧路径不删，问题随时切回

## 五、不做的事（明确边界）

- 不引入 Airflow/Celery（调度沿用现有 daemon 线程 + 子进程隔离）
- 不动前端六页框架（仅监控页加数据血缘卡）
- 不做实时流式管道（T+1 日频足够，日内另议）
