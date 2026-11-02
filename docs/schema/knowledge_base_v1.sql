-- ============================================================================
-- 量化投研知识库 v1 —— 核心 Schema
-- 落库：SQLite（quant_dev.db，业务写入）→ 每日 ETL 同步 DuckDB（分析）
-- 兼容：SQLite 3 / DuckDB 1.x
-- 设计原则：① 一文档一 doc_id ② 一切数值带 available_at ③ 特征长表存、宽表算
-- ============================================================================

PRAGMA foreign_keys = ON;

-- ============================================================================
-- L1 · 文档层
-- ============================================================================

-- 文档主表：所有非结构化数据的唯一入口，append-only，不覆盖
CREATE TABLE IF NOT EXISTS doc_document (
    doc_id                 TEXT PRIMARY KEY,   -- sha1(source || '|' || source_doc_id)
    content_hash           TEXT NOT NULL,      -- sha1(title || body)，跨源查重键
    dup_group_id           TEXT,               -- 转载组 ID（同内容多源归并）
    is_canonical           INTEGER DEFAULT 1,  -- 1=组内主文档（只有主文档进特征计算）
    source                 TEXT NOT NULL,      -- eastmoney_ann / eastmoney_news / wechat / cninfo
    source_doc_id          TEXT,               -- 源站 ID
    doc_type               TEXT NOT NULL,      -- announcement / news / research / social / qa
    title                  TEXT,
    body                   TEXT,
    body_len               INTEGER,
    url                    TEXT,
    published_at           TEXT,               -- 源声明发布时间（可能缺失/不可信）
    first_seen_at          TEXT NOT NULL,      -- 我方首抓时间（ISO8601）
    available_at           TEXT NOT NULL,      -- ★ PIT 基准：可用于决策的最早时刻
    trade_date_applicable  TEXT,               -- ★ 适用于哪个交易日的截面
    market_scope           TEXT,               -- stock / industry / market
    link_status            TEXT DEFAULT 'pending',   -- pending/done/failed
    extract_status         TEXT DEFAULT 'pending',   -- pending/done/failed
    extractor_version      TEXT,
    quality_flag           TEXT DEFAULT 'ok',  -- ok/short/noisy/duplicate
    raw_json               TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_doc_source   ON doc_document(source, source_doc_id) WHERE source_doc_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_doc_hash            ON doc_document(content_hash);
CREATE INDEX IF NOT EXISTS ix_doc_avail           ON doc_document(available_at);
CREATE INDEX IF NOT EXISTS ix_doc_trade_date      ON doc_document(trade_date_applicable);
CREATE INDEX IF NOT EXISTS ix_doc_stage           ON doc_document(link_status, extract_status);

-- 分段表：长文切分，供段落级事件抽取 / 后续 RAG 检索
CREATE TABLE IF NOT EXISTS doc_chunk (
    chunk_id     TEXT PRIMARY KEY,
    doc_id       TEXT NOT NULL REFERENCES doc_document(doc_id),
    seq          INTEGER NOT NULL,
    text         TEXT NOT NULL,
    char_len     INTEGER,
    section      TEXT,           -- 标题/正文/风险提示/财务附注
    embedding    BLOB            -- 可选，v1 留空
);
CREATE INDEX IF NOT EXISTS ix_chunk_doc ON doc_chunk(doc_id, seq);

-- 实体主表：统一 ID 体系 + 别名弹药库（实体链接的词典来源）
CREATE TABLE IF NOT EXISTS ent_entity (
    entity_id      TEXT PRIMARY KEY,   -- sh600519 / SW_801780 / sh000300（沿用全库 sh/sz 前缀式，勿用 .SZ 后缀）
    entity_type    TEXT NOT NULL,      -- stock / industry / concept / index / person / institution
    name           TEXT NOT NULL,      -- 当前简称
    full_name      TEXT,               -- 工商全称
    aliases_json   TEXT,               -- ["平安银行","深发展","000001"] 含曾用名
    industry_code  TEXT,               -- 申万一级/二级
    list_date      TEXT,
    delist_date    TEXT,
    is_active      INTEGER DEFAULT 1,
    updated_at     TEXT
);
CREATE INDEX IF NOT EXISTS ix_ent_type ON ent_entity(entity_type, is_active);

-- 实体链接结果：一篇文档涉及哪些主体（带证据与置信度）
CREATE TABLE IF NOT EXISTS doc_entity (
    doc_id        TEXT NOT NULL REFERENCES doc_document(doc_id),
    entity_type   TEXT NOT NULL,
    entity_id     TEXT NOT NULL,
    role          TEXT,           -- subject / object / mentioned
    confidence    REAL DEFAULT 0, -- 0~1，低于阈值不进聚合
    match_type    TEXT,           -- exact_alias / full_name / code / fuzzy
    evidence      TEXT,           -- 命中的原文片段
    span_start    INTEGER,
    span_end      INTEGER,
    PRIMARY KEY (doc_id, entity_type, entity_id)
);
CREATE INDEX IF NOT EXISTS ix_de_entity ON doc_entity(entity_id, doc_id);

-- ============================================================================
-- L2 · 语义层：事件 + 关系
-- ============================================================================

-- 事件类型字典（seed 见文件末尾）
CREATE TABLE IF NOT EXISTS ev_taxonomy (
    event_type     TEXT PRIMARY KEY,
    category       TEXT NOT NULL,   -- earnings / holder / capital / risk / operation / other
    label_cn       TEXT NOT NULL,
    default_polarity INTEGER DEFAULT 0,
    is_risk        INTEGER DEFAULT 0,
    note           TEXT
);

-- 事件表：从文档抽出的结构化事实，一条事实一行
CREATE TABLE IF NOT EXISTS ev_event (
    event_id          TEXT PRIMARY KEY,   -- sha1(doc_id || event_type || subject_id)
    doc_id            TEXT NOT NULL REFERENCES doc_document(doc_id),
    event_type        TEXT NOT NULL,      -- 见 ev_taxonomy
    event_subtype     TEXT,               -- 预增/预减/扭亏/首亏 …
    subject_type      TEXT NOT NULL,      -- stock / industry / index / market
    subject_id        TEXT NOT NULL,
    object_type       TEXT,
    object_id         TEXT,
    event_date        TEXT,               -- 事件发生日（公告正文所述）
    announce_at       TEXT,               -- 披露时刻
    available_at      TEXT NOT NULL,      -- ★ PIT
    trade_date_applicable TEXT NOT NULL,  -- ★ 生效交易日
    polarity          INTEGER DEFAULT 0,  -- +1 / -1 / 0
    strength          REAL,               -- 归一到 0~1 的强度
    magnitude         REAL,               -- 原始量：金额 / 比例 / 股数
    magnitude_unit    TEXT,               -- cny / pct / share / day
    amount_cny        REAL,               -- 统一折算人民币，跨事件可比
    confidence        REAL DEFAULT 0,
    extractor         TEXT,               -- rule_v1 / llm_v3
    span_text         TEXT,               -- 原文依据（可审计，必备）
    payload_json      TEXT                -- 事件专属字段
);
CREATE INDEX IF NOT EXISTS ix_ev_subject ON ev_event(subject_id, trade_date_applicable);
CREATE INDEX IF NOT EXISTS ix_ev_type    ON ev_event(event_type, trade_date_applicable);
CREATE INDEX IF NOT EXISTS ix_ev_doc     ON ev_event(doc_id);

-- 实体关系：产业链 / 股权 / 竞争，支撑"事件传导"类特征
CREATE TABLE IF NOT EXISTS ent_relation (
    src_entity_id  TEXT NOT NULL,
    dst_entity_id  TEXT NOT NULL,
    relation_type  TEXT NOT NULL,   -- supplier / customer / subsidiary / shareholder / competitor / same_controller
    weight         REAL DEFAULT 1.0,
    valid_from     TEXT,
    valid_to       TEXT,
    source_doc_id  TEXT,
    PRIMARY KEY (src_entity_id, dst_entity_id, relation_type)
);
CREATE INDEX IF NOT EXISTS ix_rel_dst ON ent_relation(dst_entity_id, relation_type);

-- ============================================================================
-- L3 · 特征层 & 标签层（长表，加特征不改 schema）
-- ============================================================================

CREATE TABLE IF NOT EXISTS feat_registry (
    feature_id     TEXT PRIMARY KEY,   -- text.senti_delta_5d
    name_cn        TEXT NOT NULL,
    category       TEXT NOT NULL,      -- text / event / behavior / market / fundamental / spillover
    entity_level   TEXT NOT NULL,      -- stock / industry / market
    dtype          TEXT DEFAULT 'float',
    direction_hint INTEGER DEFAULT 0,  -- 先验方向，仅用于初筛，不作结论
    pit_column     TEXT DEFAULT 'available_at',
    source_tables  TEXT,
    compute_sql    TEXT,               -- 口径固化，可一键重算
    version        TEXT DEFAULT 'v1',
    status         TEXT DEFAULT 'draft',   -- draft / active / deprecated
    coverage       REAL,               -- 覆盖率，自动回填
    ic_mean        REAL,               -- 最近一次验证
    icir           REAL,
    updated_at     TEXT
);

CREATE TABLE IF NOT EXISTS feat_value (
    feature_id   TEXT NOT NULL,
    entity_id    TEXT NOT NULL,
    trade_date   TEXT NOT NULL,
    value        REAL,
    available_at TEXT NOT NULL,   -- ★ 与 trade_date 同时存，便于 PIT 校验
    version      TEXT DEFAULT 'v1',
    PRIMARY KEY (feature_id, entity_id, trade_date, version)
);
CREATE INDEX IF NOT EXISTS ix_fv_date ON feat_value(trade_date, feature_id);

CREATE TABLE IF NOT EXISTS label_registry (
    label_id     TEXT PRIMARY KEY,   -- fwd_ret_1d / fwd_excess_20d / is_top_decile
    name_cn      TEXT NOT NULL,
    horizon      INTEGER NOT NULL,   -- 持有天数
    label_type   TEXT NOT NULL,      -- return / excess / binary / rank
    benchmark    TEXT,               -- csi800 / industry
    compute_sql  TEXT,
    version      TEXT DEFAULT 'v1',
    updated_at   TEXT
);

CREATE TABLE IF NOT EXISTS label_value (
    label_id     TEXT NOT NULL,
    entity_id    TEXT NOT NULL,
    trade_date   TEXT NOT NULL,
    value        REAL,
    PRIMARY KEY (label_id, entity_id, trade_date)
);
CREATE INDEX IF NOT EXISTS ix_lv_date ON label_value(trade_date, label_id);

-- ============================================================================
-- L4 · 应用层：可复现的数据集快照 + 质量门禁
-- ============================================================================

CREATE TABLE IF NOT EXISTS ds_snapshot (
    snapshot_id  TEXT PRIMARY KEY,   -- 20261026_csi800_v3
    name         TEXT NOT NULL,
    feature_ids  TEXT NOT NULL,      -- JSON 数组，顺序即列序
    label_id     TEXT NOT NULL,
    universe     TEXT NOT NULL,      -- csi800 / all_a / custom:<id>
    start_date   TEXT NOT NULL,
    end_date     TEXT NOT NULL,
    n_rows       INTEGER,
    n_features   INTEGER,
    data_md5     TEXT,               -- 内容指纹，实验可复现的唯一凭据
    created_at   TEXT
);

-- 质量门禁：每次管道运行后落检查结果，不达标特征不得进候选池
CREATE TABLE IF NOT EXISTS qa_check (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT,
    check_type   TEXT NOT NULL,      -- coverage / pit_leak / dup_ratio / entity_link_rate / label_align
    target       TEXT,               -- feature_id / table 名
    trade_date   TEXT,
    metric       REAL,
    threshold    REAL,
    passed       INTEGER DEFAULT 1,
    detail       TEXT,
    created_at   TEXT
);
CREATE INDEX IF NOT EXISTS ix_qa_run ON qa_check(run_id, check_type);

-- ============================================================================
-- 全文检索（"能查"是知识库的最低要求；先用 SQLite FTS5，零依赖）
-- ============================================================================
CREATE VIRTUAL TABLE IF NOT EXISTS doc_fts USING fts5(
    title, body,
    content='doc_document', content_rowid='rowid',
    tokenize='unicode61'
);

-- ============================================================================
-- 与现有表的关系（不重建，只增量）
--   stocks / kline_daily / adj_factor_daily  …… 不动，作为特征计算的行情源
--   factor_daily（14 因子宽表）              …… 保留，服务"当前截面"分析
--   news_stock_daily / news_market_daily      …… 保留对照，v2 起由 feat_value 承接
--   factor_registry                            …… 迁移并入 feat_registry
-- ============================================================================

-- ============================================================================
-- Seed：事件类型字典（A 股实证有效性排序，风险类优先）
-- ============================================================================
INSERT OR IGNORE INTO ev_taxonomy VALUES
 ('risk.investigation',  'risk',      '立案调查',        -1, 1, '强负向，尾部风险'),
 ('risk.inquiry_letter', 'risk',      '问询函/监管函',   -1, 1, '交易所关注'),
 ('risk.penalty',        'risk',      '行政处罚',        -1, 1, ''),
 ('risk.lawsuit',        'risk',      '诉讼仲裁',        -1, 1, ''),
 ('risk.audit_qualified','risk',      '非标审计意见',    -1, 1, '退市预警前兆'),
 ('risk.st_flag',        'risk',      'ST/退市风险',     -1, 1, ''),
 ('risk.pledge',         'risk',      '股权质押',        -1, 0, '比例高时负向'),
 ('holder.increase',     'holder',    '增持',            +1, 0, '真金白银，强正向'),
 ('holder.decrease',     'holder',    '减持',            -1, 0, ''),
 ('holder.buyback',      'holder',    '回购',            +1, 0, '看执行率，非预案'),
 ('holder.incentive',    'holder',    '股权激励',        +1, 0, '行权价即管理层心理价位'),
 ('holder.unlock',       'holder',    '限售解禁',        -1, 0, ''),
 ('holder.block_trade',  'holder',    '大宗交易',         0, 0, '折价率是关键'),
 ('earnings.forecast',   'earnings',  '业绩预告',         0, 0, '方向看 subtype'),
 ('earnings.express',    'earnings',  '业绩快报',         0, 0, ''),
 ('earnings.report',     'earnings',  '定期报告',         0, 0, ''),
 ('earnings.guidance',   'earnings',  '业绩说明会',       0, 0, ''),
 ('capital.placement',   'capital',   '定增/再融资',      0, 0, '短期摊薄，长期看项目'),
 ('capital.ma',          'capital',   '并购重组',        +1, 0, ''),
 ('capital.divest',      'capital',   '资产出售',         0, 0, ''),
 ('capital.convertible', 'capital',   '可转债',           0, 0, ''),
 ('capital.dividend',    'capital',   '分红送转',        +1, 0, ''),
 ('operation.contract',  'operation', '重大合同/中标',   +1, 0, '看占营收比'),
 ('operation.patent',    'operation', '专利/新产品',     +1, 0, ''),
 ('operation.capacity',  'operation', '产能扩张',        +1, 0, ''),
 ('operation.customer',  'operation', '大客户变动',       0, 0, ''),
 ('other.clarify',       'other',     '澄清公告',         0, 0, ''),
 ('other.suspend',       'other',     '停牌/复牌',        0, 0, ''),
 ('other.controller',    'other',     '实控人变更',       0, 0, '');
