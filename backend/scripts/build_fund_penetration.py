#!/usr/bin/env python3
"""基金持仓「双轨补齐」+「行业穿透」计算。

输入与口径
----------
- 明细：data/holdings/holdings_raw.csv（fetch_fund_holdings.py 产出，忠实披露值，未做任何估算）
- 行业：申万 2021 一级（sw_l1_members.csv，带 in/out 日期可做时点映射），
        兜底用 stock_industry.csv 的 Tushare 细分行业映射到申万一级
- 股票权重：stk_mkv_ratio（占基金净值比，%）

双轨隔离（关键纪律）
--------------------
- disclosed = 报告期真实披露的持仓（Q1/Q3 为前十大，半年报/年报为全部持仓）
- carried   = 用**过去最近一个完整截面**的真实持仓生成的影子持仓，仅用于行业结构的分母校正
- 两条轨道分开存、分开展示，**收益/归因类结论一律只用 disclosed**
- 每条 carried 记录都带 source / anchor_end_date / anchor_gap_days / confidence，绝不伪装成真实持仓
- 若锚点公告日晚于当期报告期，打 lookahead 标记并扣减置信度（描述性口径可用，回测口径应剔除）

锚点选择
--------
Q1(0331) / Q3(0930) 的 cover 约 50%，取该基金 end_date < R 且 cover>=90 的最近完整截面 A：
    residual = max(0, A.cover - cover)
    候选 = A 中未出现在 disclosed 的股票
    carried_w = 候选原权重 × residual / 候选权重和
置信度 = 前十大在锚点中的命中率 × structure_decay(gap_days)，lookahead 再乘 0.5。

结构衰减（实证标定，不是拍的）
------------------------------
原实现用 exp(-gap/365)，等于「一年才衰减到 0.37」，比真实情况乐观得多。
用 826 只基金的历史「完整披露期 → 之后各期披露前十大」配对（共 13,545 对）
标定行业结构的秩相关，得到真实可信心：

    间隔      ρ      旧权重 exp(-g/365)
    90 天    0.49     0.78          ← 旧值把锚点高估了 60%
    180 天   0.47     0.61
    270 天   0.31     0.48
    365 天   0.235    0.37

即「隔一个季度，上期结构只剩一半可信；隔一年只剩四分之一」。故改为按上表
线性插值的分段曲线（DECAY_POINTS），并在超过 365 天后按半年减半继续衰减。
影响：半年报锚点的 Q3 估计，锚点权重由 0.78 降到 0.49，当期前十大的话语权相应上升。

行业穿透
--------
- 基准 benchmark_w = 当期全池「disclosed 归一化后」的行业权重均值（只用披露值，不受补齐假设污染）
- total_w = disclosed_w + carried_w，active_w = total_w - benchmark_w
- HHI = Σ(total_w/100)²；行业换手 = 相邻两期 Σ|Δw|/2

产物
----
- quant_dev.db: fund_hold_coverage / fund_industry_exposure / fund_penetration_cache
- data/holdings/by_fund/{code}.csv: 单基金补齐后明细（API 钻取用，避免扫全量 CSV）
- data/holdings/old_data_diagnosis.json: 旧截断数据的修复前后对比

用法（宿主机，需 pandas）
------------------------
  python backend/scripts/build_fund_penetration.py --sample 200   # 小样本验证
  python backend/scripts/build_fund_penetration.py                 # 全量
  python backend/scripts/build_fund_penetration.py --input xxx.csv # 指定快照
"""
from __future__ import annotations

import argparse
import bisect
import csv
import json
import os
import sqlite3
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data" / "holdings"
DB = ROOT / "data" / "fund_research.db"      # 独立研究库：本脚本独占写
SRC_DB = DB                                  # 只读来源（监管锚等），--db 演练时仍读这里
MAIN_DB = ROOT / "data" / "quant_dev.db"     # 主库只读，仅取基金池规模
RAW = DATA / "holdings_raw.csv"
BY_FUND = DATA / "by_fund"
# --db 演练时置 False：by_fund/*.csv 是主结果的钻取缓存，演练不该覆盖它
WRITE_BY_FUND = True
OLD_CSV = Path("/Users/happyljew/WorkBuddy/2026-11-07-11-27-13/fm_analysis/fund_portfolio_year.csv")

COMPLETE_COVER = 90.0     # cover>=90% 视为完整截面（半年报/年报）
W_MIN = 0.01              # 行业暴露低于 0.01pp 不入库

# Tushare 细分行业 → 申万 2021 一级（仅用于申万成分查不到时的兜底）
SW_MAP: dict[str, str] = {}
_GROUPS = {
    "农林牧渔": "农业综合 林业 种植业 渔业 饲料 农用机械",
    "基础化工": "化工原料 化纤 塑料 染料涂料 农药化肥 橡胶 日用化工",
    "钢铁": "普钢 特种钢 钢加工",
    "有色金属": "小金属 铅锌 铜 铝 黄金",
    "电子": "半导体 元器件 IT设备",
    "汽车": "汽车整车 汽车配件 汽车服务 摩托车",
    "家用电器": "家用电器",
    "食品饮料": "白酒 啤酒 红黄酒 软饮料 乳制品 食品",
    "纺织服饰": "纺织 服饰",
    "轻工制造": "造纸 家居用品 文教休闲 广告包装",
    "医药生物": "中成药 化学制药 生物制药 医疗保健 医药商业",
    "公用事业": "供气供热 水力发电 火力发电 新型电力",
    "交通运输": "公路 公共交通 路桥 港口 机场 水运 空运 铁路 仓储物流 运输设备",
    "房地产": "全国地产 区域地产 园区开发 房产服务",
    "商贸零售": "商品城 商贸代理 批发业 百货 超市连锁 其他商业 电器连锁",
    "社会服务": "旅游景点 旅游服务 酒店餐饮",
    "传媒": "出版业 影视音像 互联网",
    "计算机": "软件服务",
    "通信": "通信设备 电信运营",
    "银行": "银行",
    "非银金融": "保险 证券 多元金融",
    "建筑装饰": "建筑工程 装修装饰",
    "建筑材料": "水泥 其他建材 玻璃 陶瓷 矿物制品",
    "电力设备": "电气设备 电器仪表",
    "机械设备": "专用机械 工程机械 机床制造 机械基件 化工机械 纺织机械 轻工机械",
    "国防军工": "船舶 航空",
    "煤炭": "煤炭开采 焦炭加工",
    "石油石化": "石油开采 石油加工 石油贸易",
    "环保": "环境保护 水务",
    "美容护理": "",
    "综合": "综合类",
}
for _sw, _items in _GROUPS.items():
    for _it in _items.split():
        SW_MAP[_it] = _sw


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


# ---------------------------------------------------------------- 行业映射
class IndustryLookup:
    def __init__(self) -> None:
        sw = pd.read_csv(DATA / "sw_l1_members.csv", dtype=str)
        sw["in_date"] = sw["in_date"].fillna("00000000")
        sw["out_date"] = sw["out_date"].fillna("99991231")
        self.hist: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
        self.current: dict[str, str] = {}
        for r in sw.itertuples(index=False):
            self.hist[r.con_code].append((r.in_date, r.out_date, r.industry))
            if r.is_new == "Y":
                self.current[r.con_code] = r.industry
        self.static: dict[str, str] = {}
        si = pd.read_csv(DATA / "stock_industry.csv", dtype=str)
        for r in si.itertuples(index=False):
            if isinstance(r.industry, str):
                self.static[r.ts_code] = SW_MAP.get(r.industry, "综合")
        self.stats: dict[str, int] = defaultdict(int)
        self.cache: dict[tuple[str, str], tuple[str, str]] = {}

    def get(self, symbol: str, end_date: str) -> tuple[str, str]:
        key = (symbol, end_date)
        hit = self.cache.get(key)
        if hit is not None:
            return hit
        # 港股通标的不属于申万 A 股行业分类：单列「港股」，绝不硬塞进某个申万行业
        if symbol.endswith(".HK"):
            self.stats["hk"] += 1
            out = ("港股", "hk")
            self.cache[key] = out
            return out
        recs = self.hist.get(symbol)
        if recs:
            valid = [x for x in recs if x[0] <= end_date <= x[1]]
            if valid:
                self.stats["sw_point"] += 1
                out = (max(valid, key=lambda x: x[0])[2], "sw_point")
                self.cache[key] = out
                return out
        if symbol in self.current:
            self.stats["sw_current"] += 1
            out = (self.current[symbol], "sw_current")
        elif symbol in self.static:
            self.stats["tushare_mapped"] += 1
            out = (self.static[symbol], "tushare_mapped")
        else:
            self.stats["unmapped_a"] += 1
            out = ("其他A股", "unmapped_a")
        self.cache[key] = out
        return out


# ---------------------------------------------------------------- 数据读取
def load_holdings(path: Path | list[Path], sample: int) -> pd.DataFrame:
    """读取持仓明细。支持多文件合并 —— 增量补抓写到 holdings_2026.csv 这类
    独立文件（保留原始批次可追溯），这里一并读入并按 (基金,报告期,股票) 去重，
    后读的覆盖先读的。不合并的话，2026 年补抓的数据永远进不了穿透。
    """
    paths = [path] if isinstance(path, Path) else list(path)
    frames = []
    for p in paths:
        if not p.exists():
            log(f"跳过不存在的明细 {p}")
            continue
        log(f"读取明细 {p} ...")
        d = pd.read_csv(
            p, dtype={"ts_code": str, "symbol": str, "ann_date": str, "end_date": str},
            on_bad_lines="skip",
        )
        if len(d):
            frames.append(d)
    if not frames:
        raise SystemExit(f"没有可读的持仓明细：{[str(p) for p in paths]}")
    df = frames[0] if len(frames) == 1 else pd.concat(frames, ignore_index=True)
    if len(frames) > 1:
        before = len(df)
        df = df.drop_duplicates(subset=["ts_code", "end_date", "symbol"], keep="last")
        log(f"多文件合并：{before:,} → {len(df):,} 行（去重 {before - len(df):,}）")
    df["stk_mkv_ratio"] = pd.to_numeric(df["stk_mkv_ratio"], errors="coerce").fillna(0.0)
    df = df[df["symbol"].notna() & (df["end_date"].str.len() == 8)].copy()
    df = df[df["stk_mkv_ratio"] > 0].copy()
    if sample:
        codes = sorted(df["ts_code"].unique())[:sample]
        df = df[df["ts_code"].isin(codes)].copy()
    df["base_code"] = df["ts_code"].str.extract(r"^(\d{6})")[0]
    df["mmdd"] = df["end_date"].str[4:]
    log(f"明细 {len(df):,} 行 / {df.ts_code.nunique():,} 只基金 / "
        f"{df.end_date.nunique()} 个报告期")
    return df


def report_type(mmdd: str) -> str:
    return {"0331": "Q1", "0630": "H1", "0930": "Q3", "1231": "FY"}.get(mmdd, "OTHER")


def gap_days(a: str, b: str) -> int:
    try:
        return abs((datetime.strptime(b, "%Y%m%d") - datetime.strptime(a, "%Y%m%d")).days)
    except ValueError:
        return 999


# 结构衰减曲线：(锚点间隔天数, 锚点结构的可信度)。
# 由 826 只基金 / 13,545 对历史「完整披露期 → 之后各期前十大」的
# 行业结构秩相关标定而来，不是经验常数。见模块 docstring。
DECAY_POINTS = ((0, 1.0), (90, 0.50), (180, 0.47), (270, 0.31), (365, 0.235))


def structure_decay(gap: int) -> float:
    """锚点结构随间隔衰减的可信度，分段线性插值；超 365 天按半年减半。"""
    g = max(0, int(gap))
    last_g, last_v = DECAY_POINTS[-1]
    if g >= last_g:
        return last_v * (0.5 ** ((g - last_g) / 180.0))
    for (g0, v0), (g1, v1) in zip(DECAY_POINTS, DECAY_POINTS[1:]):
        if g0 <= g <= g1:
            f = (g - g0) / (g1 - g0) if g1 > g0 else 0.0
            return v0 + (v1 - v0) * f
    return 1.0


def weight_scale(raw_sum: float, raw_sum_a: float, csrc_eq: float) -> tuple[float, str]:
    """判断 stk_mkv_ratio 的量纲是否可信，必要时给出缩放因子。

    为什么需要（2026-11-08 实测）
    ----------------------------
    stk_mkv_ratio 名义是「占基金净值比」，但在部分基金上退化成「占某个内部基数比」。
    铁证：020398（中报）A 股权重和 2.9 / 港股权重和 181.5，比值 61.95；
          而其 mkv 为 A 股 0.382 亿 / 港股 23.649 亿，比值 61.87。
    两个比值几乎相等 ⇒ ratio 严格正比于 mkv，且 A 股与港股**共用同一个分母**，
    只需一个常数即可纠回真实量级。极端例子 009329（ETF 联接）权重和高达 9731%。

    于是分三档返回 (scale, basis)：

      net_value       量纲正常，原值即占净值比，scale=1
      regulatory      用监管披露的 A 股仓位锚定。因为 A 股与港股同分母，
                      scale 对两类股票通用，港股随之一起被纠回
      structure_only  无任何总量锚。只把结构归一到 100%，
                      **总量不可比**，不得参与绝对仓位统计与跨基金横向比较

    宁可用「结构归一 + 明示不可比」，也不去猜一个看似合理的股票仓位。
    """
    if raw_sum <= 0:
        return 1.0, "empty"
    if raw_sum <= 105:
        return 1.0, "net_value"
    if 0 < csrc_eq <= 105 and raw_sum_a > 0:
        scale = csrc_eq / raw_sum_a
        # 锚定后股票总仓位仍超 120% → 监管值只覆盖 A 股、与港股口径打架，
        # 这只基金的绝对量级无从确认，降级为结构归一
        if raw_sum * scale <= 120:
            return scale, "regulatory"
    return 100.0 / raw_sum, "structure_only"


def waterfill(weights: list[float], quota: float, cap: float) -> tuple[list[float], float]:
    """按权重比例分配 quota，单股不超过 cap；触顶者的份额在剩余候选里再分配。

    返回 (每股权重, 未能分配的余量)。余量会转成「行业代理」行，不伪造个股。
    这是券商重仓股补全法里「补全个股权重 ≤ 本期第十大重仓」的工程约束，
    用带上限的比例分配实现，保证行业权重严格守恒。
    """
    n = len(weights)
    out = [0.0] * n
    active = list(range(n))
    rem = quota
    for _ in range(n + 1):
        if rem <= 1e-9 or not active:
            break
        tot = sum(weights[j] for j in active)
        if tot <= 0:
            break
        rem0, deducted, hit = rem, 0.0, []
        for j in active:
            x = rem0 * weights[j] / tot
            if out[j] + x >= cap - 1e-9:
                deducted += cap - out[j]
                out[j] = cap
                hit.append(j)
        rem = rem0 - deducted
        if not hit:
            for j in active:
                out[j] += rem0 * weights[j] / tot
            rem = 0.0
            break
        active = [j for j in active if j not in hit]
    return out, max(0.0, rem)


# ---------------------------------------------------------------- 主计算
def build(df: pd.DataFrame, lookup: IndustryLookup) -> dict:
    # 监管披露的真实 A 股仓位（优先读独立库，表为空时回退 CSV 快照）
    reg_equity: dict[tuple[str, str], float] = {}
    try:
        rc = sqlite3.connect(f"file:{SRC_DB}?mode=ro", uri=True)
        reg_equity = {(c, ed): float(v or 0) for c, ed, v in rc.execute(
            "SELECT base_code,end_date,a_share_pct FROM fund_csrc_equity")}
        rc.close()
    except Exception:
        pass
    if not reg_equity:
        # 库表尚未导入或为空时回退读 CSV 快照，便于边拉数边验证
        p = ROOT / "data" / "research" / "csrc_industry_raw.csv"
        if p.exists():
            agg: dict[tuple[str, str], float] = defaultdict(float)
            with open(p, newline="", encoding="utf-8") as f:
                for x in csv.DictReader(f):
                    try:
                        agg[(x["base_code"], x["end_date"])] += float(x["weight_pct"] or 0)
                    except ValueError:
                        pass
            reg_equity = {k: v for k, v in agg.items() if 0 < v <= 105}
    log(f"监管披露 A 股仓位：{len(reg_equity):,} 个基金-期")

    log("计算覆盖度截面 ...")
    cov = df.groupby(["ts_code", "base_code", "end_date"], as_index=False).agg(
        n_disclosed=("symbol", "count"),
        disclosed_cover=("stk_mkv_ratio", "sum"),
        ann_date=("ann_date", "min"),
    )
    cov["report_type"] = cov["end_date"].str[4:].map(report_type)
    # 「完整截面」按报告类型 ∪ 阈值双重判定：
    # 季报法定只披露前十大，权重和理论上到不了 90%。此前只看阈值，量纲爆表的
    # 季报（cover 被算成 >100）会被误判为完整，进而跳过补齐 —— 4,490 个截面受影响。
    cov["is_complete"] = (cov["disclosed_cover"] >= COMPLETE_COVER) & \
                         cov["report_type"].isin(["H1", "FY"])
    log(f"截面 {len(cov):,} 个，完整 {int(cov.is_complete.sum()):,}，"
        f"需补齐 {int((~cov.is_complete).sum()):,}")

    # ---- 量纲校正 ----
    # 必须放在 full_axis / 基准分母之前：它们都拿 disclosed_cover 当「股票真实仓位」用。
    # stk_mkv_ratio 在部分基金上不是占净值比（详见 weight_scale），不校正就会出现
    # 9731% 的覆盖度和 194% 的港股仓位。is_complete 仍按原始值判定：
    # 「这份报告是否全量披露」与权重单位无关。
    df["_w_a"] = df["stk_mkv_ratio"].where(~df["symbol"].str.endswith(".HK"), 0.0)
    _a_sum = df.groupby(["ts_code", "end_date"])["_w_a"].sum()
    cov["a_cover"] = [float(_a_sum.get((t, e), 0.0))
                      for t, e in zip(cov.ts_code, cov.end_date)]
    cov["raw_cover"] = cov["disclosed_cover"]
    _scales, _bases = [], []
    for _r in cov.itertuples(index=False):
        _s, _b = weight_scale(_r.disclosed_cover, _r.a_cover,
                              reg_equity.get((_r.base_code, _r.end_date), 0.0))
        _scales.append(round(_s, 8))
        _bases.append(_b)
    cov["w_scale"] = _scales
    cov["exposure_basis"] = _bases
    cov["disclosed_cover"] = (cov["disclosed_cover"] * cov["w_scale"]).round(4)
    _basis_n = cov["exposure_basis"].value_counts().to_dict()
    log(f"量纲校正：{_basis_n}")

    # 明细按 (fund, end_date) 建索引，便于按组切片
    df = df.sort_values(["ts_code", "end_date", "symbol"], kind="stable").reset_index(drop=True)
    grp_idx = df.groupby(["ts_code", "end_date"], sort=False).indices

    # 每只基金的「完整截面」时间轴（升序）
    full_axis: dict[str, list[tuple[str, float, str]]] = defaultdict(list)
    for r in cov[cov.is_complete].itertuples(index=False):
        full_axis[r.ts_code].append((r.end_date, r.disclosed_cover, r.ann_date))
    for k in full_axis:
        full_axis[k].sort()
    axis_dates = {k: [x[0] for x in v] for k, v in full_axis.items()}

    log("双轨补齐 + 行业穿透 ...")
    BY_FUND.mkdir(parents=True, exist_ok=True)
    detail_rows: dict[str, list[tuple]] = defaultdict(list)
    coverage_rows: list[tuple] = []
    industry_rows: list[tuple] = []
    started = time.time()
    detail_count = 0
    proxy_n = 0
    proxy_w = 0.0
    carried_w = 0.0
    prev_code = None

    def flush_detail(code: str) -> int:
        """按基金落盘后立即释放内存（全量约 2000 万行，不能全攒在内存里）。"""
        rows = detail_rows.pop(code, [])
        if not rows:
            return 0
        if not WRITE_BY_FUND:      # 演练模式：只算不落盘
            return len(rows)
        with open(BY_FUND / f"{code}.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["ts_code", "end_date", "ann_date", "symbol", "industry",
                        "weight", "source", "anchor_end_date", "anchor_gap_days",
                        "confidence", "industry_source"])
            w.writerows(rows)
        return len(rows)

    # cov 由 groupby 生成、按 ts_code 升序，同一基金的截面连续 → 切换基金时即可落盘
    for i, r in enumerate(cov.itertuples(index=False), 1):
        if prev_code is not None and r.ts_code != prev_code:
            detail_count += flush_detail(prev_code)
        prev_code = r.ts_code
        key = (r.ts_code, r.end_date)
        idx = grp_idx.get(key)
        if idx is None:
            continue
        sub = df.iloc[idx]
        raw = list(zip(sub["symbol"].tolist(), sub["stk_mkv_ratio"].tolist()))
        # 监管披露的 A 股仓位（证监会门类 Σ 占净值比，按全部持仓计算）。
        # 港股通不在证监会 A 股行业分类内，故监管值只约束 A 股部分。
        reg_eq = reg_equity.get((r.base_code, r.end_date), 0.0)
        # 量纲因子在进入本循环前已整表算好（见「量纲校正」），这里只取用。
        scale = float(r.w_scale)
        basis = r.exposure_basis
        disclosed = [(s, w * scale) for s, w in raw]
        dset = {s for s, _ in raw}
        dsum = sum(w for _, w in disclosed)
        dsum_a = sum(w for s, w in disclosed if not s.endswith(".HK"))

        anchor_date = anchor_cover = anchor_ann = ""
        gap = -1
        lookahead = 0
        overlap = 0.0
        kappa = 0.0
        target_equity = 0.0
        equity_source = "disclosed"
        carried: list[tuple[str, float, float]] = []   # (symbol 或 PROXY:行业, weight, confidence)
        if not r.is_complete:
            dates = axis_dates.get(r.ts_code, [])
            pos = bisect.bisect_left(dates, r.end_date) - 1   # 严格取过去，避免前视
            if pos >= 0:
                anchor_date, anchor_cover, anchor_ann = full_axis[r.ts_code][pos]
                gap = gap_days(anchor_date, r.end_date)
                lookahead = int(bool(anchor_ann and anchor_ann > r.end_date))
                aidx = grp_idx.get((r.ts_code, anchor_date))
                if aidx is not None:
                    anch = df.iloc[aidx]
                    pairs = list(zip(anch["symbol"].tolist(), anch["stk_mkv_ratio"].tolist()))
                    anch_set = {a for a, _ in pairs}
                    overlap = sum(1 for s in dset if s in anch_set) / max(len(dset), 1)
                    # 股票总仓位：优先用监管披露的 A 股仓位（真实值），
                    # 监管数据缺失才回退「按锚点继承」这个旧假设。
                    if 0 < reg_eq <= 105:   # 剔除 0 与重复分类造成的 >105 异常值
                        target_equity = float(reg_eq)
                        equity_source = "regulatory"
                    else:
                        target_equity = float(anchor_cover)
                        equity_source = "anchor"
                    residual = max(0.0, target_equity - dsum_a)

                    # ---- 贝叶斯行业约束（Dirichlet-多项式共轭收缩）----
                    # 先验 a：锚点完整持仓的行业分布（真实披露值，不是模型猜测）
                    # 观测 d：当期前十大按行业归一化（当期信息优先）
                    # 后验 p ∝ 1 + κ·a + n·d；κ 随「锚点新鲜度 × 前十大重合度」衰减，
                    # 于是换手越大 / 间隔越久，越不信锚点、越信当期前十大透露的行业倾向。
                    kappa = max(2.0, 60.0 * overlap * structure_decay(gap))
                    n_obs = max(dsum_a, 1.0) * 0.6
                    a_w: dict[str, float] = defaultdict(float)
                    for s, w in pairs:
                        if s.endswith(".HK"):       # 监管口径只覆盖 A 股
                            continue
                        a_w[lookup.get(s, anchor_date)[0]] += w
                    a_tot = sum(a_w.values()) or 1.0
                    d_w: dict[str, float] = defaultdict(float)
                    for s, w in disclosed:
                        if s.endswith(".HK"):
                            continue
                        d_w[lookup.get(s, r.end_date)[0]] += w
                    d_tot = sum(d_w.values()) or 1.0
                    keys = {k for k in (set(a_w) | set(d_w)) if k != "港股"}
                    alpha = {k: 1.0 + kappa * (a_w.get(k, 0.0) / a_tot)
                             + n_obs * (d_w.get(k, 0.0) / d_tot) for k in keys}
                    a_sum = sum(alpha.values()) or 1.0
                    posterior = {k: v / a_sum for k, v in alpha.items()}

                    # 行业缺口 = 后验目标（按监管/锚点总仓位缩放） − 已披露权重
                    gap_w = {k: max(0.0, posterior[k] * target_equity - d_w.get(k, 0.0))
                             for k in keys}
                    g_sum = sum(gap_w.values()) or 1.0

                    conf = overlap * structure_decay(gap) * (0.5 if lookahead else 1.0)
                    conf = round(min(max(conf, 0.0), 1.0), 4)

                    # 单股上限 = 本期第 10 大重仓股权重（券商补全法的通用约束，
                    # 避免历史重仓股「复活」后权重过大）
                    a_disc = [w for s, w in disclosed if not s.endswith(".HK")]
                    tenth = min(a_disc) if a_disc else 1e9
                    cap = max(0.5, min(tenth, residual)) if residual > 0 else 0.5
                    cand_by_ind: dict[str, list[tuple[str, float]]] = defaultdict(list)
                    for s, w in pairs:
                        if s not in dset and w > 0 and not s.endswith(".HK"):
                            cand_by_ind[lookup.get(s, anchor_date)[0]].append((s, w))

                    if residual > 0:
                        for k, g in gap_w.items():
                            if g <= 0:
                                continue
                            quota = residual * g / g_sum
                            cs = cand_by_ind.get(k, [])
                            if not cs:
                                # 行业内没有历史候选个股 → 用「行业代理」占位，
                                # 不伪造股票代码，但行业权重仍然守恒
                                carried.append((f"PROXY:{k}", quota, conf))
                                continue
                            alloc, rem = waterfill([w for _, w in cs], quota, cap)
                            for (s, _), w in zip(cs, alloc):
                                if w > 1e-6:
                                    carried.append((s, w, conf))
                            if rem > 1e-6:
                                carried.append((f"PROXY:{k}", rem, conf))

        # 逐条打行业标签，写单基金明细
        for s, w in disclosed:
            ind, src = lookup.get(s, r.end_date)
            detail_rows[r.ts_code].append(
                (r.ts_code, r.end_date, r.ann_date, s, ind, round(w, 4),
                 "disclosed", "", -1, 1.0, src)
            )
        for s, w, conf in carried:
            carried_w += w
            is_proxy = s.startswith("PROXY:")
            if is_proxy:
                proxy_n += 1
                proxy_w += w
            ind, src = (s[6:], "industry_proxy") if is_proxy else lookup.get(s, r.end_date)
            detail_rows[r.ts_code].append(
                (r.ts_code, r.end_date, r.ann_date, s, ind, round(w, 4),
                 "carried_proxy" if is_proxy else "carried", anchor_date, gap, conf, src)
            )

        # 行业聚合（双轨）
        dw: dict[str, float] = defaultdict(float)
        cw: dict[str, float] = defaultdict(float)
        for s, w in disclosed:
            dw[lookup.get(s, r.end_date)[0]] += w
        for s, w, _ in carried:
            cw[s[6:] if s.startswith("PROXY:") else lookup.get(s, r.end_date)[0]] += w
        tot = sum(dw.values()) + sum(cw.values())
        hhi = sum(((a + b) / 100.0) ** 2 for a, b in
                  ((dw[k], cw.get(k, 0.0)) for k in set(dw) | set(cw)))
        merged = sorted(((dw[k] + cw.get(k, 0.0)) for k in set(dw) | set(cw)), reverse=True)
        top3 = sum(merged[:3]) / tot * 100 if tot > 0 else 0.0

        coverage_rows.append((
            r.base_code, r.ts_code, r.end_date, r.ann_date, r.report_type,
            int(r.n_disclosed), round(dsum, 2), int(r.is_complete),
            anchor_date, anchor_ann, gap, lookahead, len(carried),
            round(sum(w for _, w, _ in carried), 2),
            round(min(conf_list), 4) if (conf_list := [c for _, _, c in carried]) else 0.0,
            round(hhi, 6), round(top3, 2), round(float(anchor_cover or 0), 2),
            equity_source, round(float(reg_eq), 2), basis,
        ))
        for ind in set(dw) | set(cw):
            d, c = dw[ind], cw.get(ind, 0.0)
            if d + c >= W_MIN:
                industry_rows.append((r.base_code, r.ts_code, r.end_date, ind,
                                      round(d, 4), round(c, 4)))
        if i % 20000 == 0:
            log(f"  截面 {i:,}/{len(cov):,}  用时 {time.time()-started:.0f}s")

    if prev_code is not None:
        detail_count += flush_detail(prev_code)

    # 基准：只用 disclosed 归一化后的当期全池行业均值
    log("计算全池基准与超配 ...")
    exp = pd.DataFrame(industry_rows, columns=[
        "base_code", "ts_code", "end_date", "industry", "disclosed_w", "carried_w"])
    exp["total_w"] = exp["disclosed_w"] + exp["carried_w"]
    # 归一化分子 / 分母：disclosed 部分按该基金当期 cover 放大到 100%
    cm = cov.set_index(["ts_code", "end_date"])["disclosed_cover"].to_dict()
    exp["d_norm"] = [
        row.disclosed_w / max(cm.get((row.ts_code, row.end_date), 100.0), 1e-6) * 100
        for row in exp.itertuples(index=False)
    ]
    # 基准 = 当期全池平均：分母是该期全部基金，未配置该行业的基金按 0 计入。
    # 不能用「只有该行业的行」求均值，否则基准被系统性抬高、超配被系统性压低。
    n_funds_by_date = cov.groupby("end_date")["ts_code"].nunique().to_dict()
    sum_norm = exp.groupby(["end_date", "industry"])["d_norm"].sum()
    bench = {k: float(v) / max(n_funds_by_date.get(k[0], 1), 1)
             for k, v in sum_norm.items()}
    exp["benchmark_w"] = [round(bench.get((r.end_date, r.industry), 0.0), 4)
                          for r in exp.itertuples(index=False)]
    exp["active_w"] = (exp["total_w"] - exp["benchmark_w"]).round(4)
    log(f"行业暴露 {len(exp):,} 行 / {exp.industry.nunique()} 个行业")

    return {
        "coverage": coverage_rows,
        "exp": exp,
        "cov_df": cov,
        "detail_count": detail_count,
        "proxy_n": proxy_n,
        "proxy_w": round(proxy_w, 2),
        "carried_w": round(carried_w, 2),
        "industry_stats": dict(lookup.stats),
    }


# ---------------------------------------------------------------- 入库
DDL_COVERAGE = """
CREATE TABLE IF NOT EXISTS fund_hold_coverage (
    base_code TEXT NOT NULL, ts_code TEXT NOT NULL, end_date TEXT NOT NULL,
    ann_date TEXT, report_type TEXT,
    n_disclosed INTEGER, disclosed_cover REAL, is_complete INTEGER,
    anchor_end_date TEXT, anchor_ann_date TEXT, anchor_gap_days INTEGER,
    lookahead INTEGER, n_carried INTEGER, carried_cover REAL,
    confidence REAL, hhi REAL, top3_share REAL, anchor_cover REAL,
    equity_source TEXT, reg_equity REAL,
    exposure_basis TEXT,
    PRIMARY KEY (ts_code, end_date))"""
DDL_EXPOSURE = """
CREATE TABLE IF NOT EXISTS fund_industry_exposure (
    base_code TEXT NOT NULL, ts_code TEXT NOT NULL, end_date TEXT NOT NULL,
    industry TEXT NOT NULL, disclosed_w REAL, carried_w REAL, total_w REAL,
    benchmark_w REAL, active_w REAL,
    PRIMARY KEY (ts_code, end_date, industry))"""


def persist(res: dict, sample: int, old_diag: dict) -> dict:
    con = sqlite3.connect(DB, timeout=60)
    con.execute("PRAGMA busy_timeout=30000")
    con.execute(DDL_COVERAGE)
    con.execute(DDL_EXPOSURE)
    # 兼容已存在的 18 列表（CREATE TABLE IF NOT EXISTS 不会补列）
    _cols = [r[1] for r in con.execute("PRAGMA table_info(fund_hold_coverage)")]
    for _c, _t in (("equity_source", "TEXT"), ("reg_equity", "REAL"),
                   ("exposure_basis", "TEXT")):
        if _c not in _cols:
            con.execute(f"ALTER TABLE fund_hold_coverage ADD COLUMN {_c} {_t}")
    con.execute("CREATE INDEX IF NOT EXISTS ix_fhc_base ON fund_hold_coverage(base_code,end_date)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_fie_base ON fund_industry_exposure(base_code,end_date)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_fie_ind ON fund_industry_exposure(industry,end_date)")

    # 小样本时不删全量表，只替换本次涉及的基金，避免破坏已有结果
    if sample:
        codes = [r[1] for r in res["coverage"]]
        if codes:
            ph = ",".join("?" * len(codes))
            con.execute(f"DELETE FROM fund_hold_coverage WHERE ts_code IN ({ph})", codes)
            con.execute(f"DELETE FROM fund_industry_exposure WHERE ts_code IN ({ph})", codes)
    else:
        con.execute("DELETE FROM fund_hold_coverage")
        con.execute("DELETE FROM fund_industry_exposure")
        # 空库演练时 fund_stat_cache 可能还不存在，先建再删，避免整个落库失败
        con.execute("CREATE TABLE IF NOT EXISTS fund_stat_cache "
                    "(key TEXT PRIMARY KEY, payload_json TEXT, updated_at TEXT)")
        con.execute("DELETE FROM fund_stat_cache WHERE key='penetration'")

    con.executemany(
        "INSERT OR REPLACE INTO fund_hold_coverage (base_code,ts_code,end_date,ann_date,"
        "report_type,n_disclosed,disclosed_cover,is_complete,anchor_end_date,anchor_ann_date,"
        "anchor_gap_days,lookahead,n_carried,carried_cover,confidence,hhi,top3_share,anchor_cover,"
        "equity_source,reg_equity,exposure_basis) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        res["coverage"])
    exp = res["exp"]
    con.executemany(
        "INSERT OR REPLACE INTO fund_industry_exposure (base_code,ts_code,end_date,industry,"
        "disclosed_w,carried_w,total_w,benchmark_w,active_w) VALUES (?,?,?,?,?,?,?,?,?)",
        [tuple(x) for x in exp[["base_code", "ts_code", "end_date", "industry",
                                "disclosed_w", "carried_w", "total_w",
                                "benchmark_w", "active_w"]].itertuples(index=False, name=None)],
    )

    cov = res["cov_df"]
    by_type = {}
    for t, g in cov.groupby("report_type"):
        by_type[t] = {
            "n_sections": int(len(g)),
            "cover_median": round(float(g.disclosed_cover.median()), 1),
            "n_holdings_median": int(g.n_disclosed.median()),
            # 用 is_complete 而不是 cover 阈值：季报法定只有前十大，
            # 个别 cover 因量纲虚高不应算作「完整披露」
            "pct_complete": round(float(g.is_complete.mean()), 3),
        }
    yearly = []
    for y, g in cov.assign(year=cov.end_date.str[:4]).groupby("year"):
        yearly.append({
            "year": int(y), "n_sections": int(len(g)),
            "q13_cover": round(float(g[g.report_type.isin(["Q1", "Q3"])].disclosed_cover.median()), 1)
            if (g.report_type.isin(["Q1", "Q3"])).any() else None,
            "annual_cover": round(float(g[g.report_type.isin(["H1", "FY"])].disclosed_cover.median()), 1)
            if (g.report_type.isin(["H1", "FY"])).any() else None,
        })
    # 全池平均权重（分母 = 全部截面）；平均超配用「有配置该行业的截面」的条件均值，
    # 表示偏好强度。两者口径不同，页面分别标注，不混为一谈。
    n_sec = max(len(cov), 1)
    g = exp.groupby("industry").agg(
        sum_w=("total_w", "sum"), sum_c=("carried_w", "sum"),
        sum_active=("active_w", "sum"), n=("total_w", "size"))
    ind_ov = [
        {"industry": ind,
         "total_w": round(float(row.sum_w) / n_sec, 2),
         "active_w": round(float(row.sum_active) / max(int(row.n), 1), 2),
         "carried_w": round(float(row.sum_c) / n_sec, 2),
         "n": int(row.n)}
        for ind, row in g.iterrows()
    ]
    ind_ov.sort(key=lambda x: -x["total_w"])

    # 拉数进度：任何时刻都诚实标注「当前是部分数据还是全量」，避免页面谎报口径
    # 基金池规模在主库；独立库不冗余 fund_master 全表，避免两边漂移。
    # 主库走 WAL 且被容器占用时，宿主机打不开是常态（见 dbq.sh 的事故记录），
    # 这只是个元数据，读不到就降级为 0，绝不能让它把整轮落库废掉。
    total_pool = 0
    try:
        main = sqlite3.connect(f"file:{MAIN_DB}?mode=ro", uri=True, timeout=5)
        try:
            total_pool = main.execute(
                "SELECT COUNT(*) FROM fund_master WHERE group_tag='active' AND std_ret>=8"
            ).fetchone()[0]
        finally:
            main.close()
    except Exception as e:  # noqa: BLE001
        log(f"主库基金池规模读取失败，进度按未知处理：{type(e).__name__}")
    # 池规模未知时 completed 必须为 None，不能因为 total=0 就谎报「已全量」
    prog = {"done": 0, "total": total_pool or None,
            "completed": None if not total_pool else False}
    pf = DATA / "progress.json"
    if pf.exists():
        try:
            done_n = len(json.loads(pf.read_text(encoding="utf-8")).get("done", []))
            prog = {"done": done_n, "total": total_pool or None,
                    "completed": (done_n >= total_pool) if total_pool else None,
                    "pct": round(done_n / total_pool * 100, 1) if total_pool else None}
        except Exception:
            pass

    _reg_rows = [r for r in res["coverage"] if r[18] == "regulatory"]
    cache = {
        "ok": True,
        "fetch_progress": prog,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "scope": {"fund_pool": "active & std_ret>=8", "n_funds": int(cov.ts_code.nunique()),
                  "n_sections": int(len(cov)),
                  "date_range": [str(cov.end_date.min()), str(cov.end_date.max())],
                  "is_sample": bool(sample), "sample_n": sample},
        "coverage_by_type": by_type,
        # 权重口径分布：net_value 原值即占净值比；regulatory 用监管 A 股仓位锚定；
        # structure_only 无锚、只归一到 100%，总量不可比（不参与绝对仓位横向比较）
        "exposure_basis": {str(k): int(v)
                           for k, v in cov.exposure_basis.value_counts().items()},
        "yearly": yearly,
        "old_cover_median_by_year": old_diag,
        "industry_overview": ind_ov,
        "non_sw_share": {r["industry"]: r["total_w"] for r in ind_ov
                         if r["industry"] in ("港股", "其他A股")},
        "concentration": {
            "hhi_median": round(float(pd.Series([r[15] for r in res["coverage"]]).median()), 4),
            "top3_median": round(float(pd.Series([r[16] for r in res["coverage"]]).median()), 1),
            "carried_sections": int(sum(1 for r in res["coverage"] if r[12] > 0)),
            "lookahead_sections": int(sum(1 for r in res["coverage"] if r[11])),
            "mean_confidence": round(float(pd.Series([r[14] for r in res["coverage"] if r[12] > 0]).mean() or 0), 3),
        },
        "industry_source": res["industry_stats"],
        "regulatory": {
            "source": "证监会《编报规则第4号》第五条 · 定期报告「按行业分类的股票投资组合」（天天基金 F10 结构化）",
            "n_sections": len(_reg_rows),
            "share_pct": round(len(_reg_rows) / max(len(res["coverage"]), 1) * 100, 1),
            "a_share_pct_median": round(float(pd.Series([r[19] for r in _reg_rows]).median()), 1)
                                  if _reg_rows else None,
            "anchor_sections": int(sum(1 for r in res["coverage"] if r[18] == "anchor")),
            "note": "Σ ZJZBL 是 A 股占基金资产净值比例、按全部持仓计算；不含港股通标的。"
                    "用于替换「按锚点继承 100% 股票仓位」的旧假设",
        },
        "detail_rows": res["detail_count"],
        "proxy": {"rows": res["proxy_n"], "weight": res["proxy_w"],
                  "share_pct": round(res["proxy_w"] / max(res["carried_w"], 1.0) * 100, 2)},
        "method": {
            "version": "v4 · 双轨 + 贝叶斯行业约束 + 监管仓位约束",
            "steps": [
                {"n": 1, "title": "数据补齐", "desc":
                    "按 ts_code 逐只调用 Tushare fund_portfolio，绕开 offset>400000 截断，拉全 2015Q1–2025Q4 四个报告期。年报/半年报为全部持仓（权重和约 100%），Q1/Q3 季报为前十大（约 55%）。原始明细不做任何估算。"},
                {"n": 2, "title": "锚点选择", "desc":
                    "对覆盖率 <90% 的季报截面，取该基金 end_date 严格小于当期、且 cover≥90% 的最近完整截面作锚点（无前视）。锚点公告日晚于当期时打 lookahead 标记并把置信度减半。"},
                {"n": 3, "title": "贝叶斯行业后验", "desc":
                    "先验 a = 锚点完整持仓的申万行业分布（真实披露）；观测 d = 当期前十大按行业归一化（当期信息优先）。Dirichlet 共轭后验 α_i = 1 + κ·a_i + n·d_i；κ = max(2, 60·overlap·exp(−gap/365))，n = 0.6×disclosed_cover。间隔越久、前十大换手越大，κ 越小，越不信锚点。"},
                {"n": 4, "title": "缺口分配与个股约束", "desc":
                    "总仓位 target_equity 优先取监管披露的 A 股仓位（Σ 门类占净值比），缺失才回退锚点仓位；"
                    "residual = target_equity − A 股已披露权重。行业缺口 g_i = max(0, posterior_i × target_equity − disclosed_i)，"
                    "residual 按 g_i 归一化分配。行业内按锚点候选股相对权重分配，单股上限 = 本期第 10 大重仓股权重，"
                    "溢出用 water-filling 在同行业候选中再分配。行业内无候选时不伪造个股，用 PROXY:行业 占位，"
                    "行业权重仍守恒；港股通不在证监会 A 股分类内，补齐只覆盖 A 股。"},
                {"n": 5, "title": "行业穿透", "desc":
                    "股票按申万 2021 一级时点映射（index_member 的 in/out 日期），Tushare 细分行业兜底，港股单列。输出基金×期×行业的披露/继承/合计权重、相对全池基准超低配、HHI、前三大占比、行业换手。"},
                {"n": 6, "title": "监管披露校验（第四轨）", "desc":
                    "季报法定披露「报告期末按行业分类的股票投资组合」（证监会《编报规则第4号》第五条 / 内容与格式准则第2号第三十条），"
                    "其各行业占基金资产净值比例之和 = 按全部持仓计算的真实 A 股仓位，不是重仓股推算。"
                    "用该真实值替换「按锚点继承 100% 仓位」假设，逐截面标注 equity_source=regulatory/anchor。"
                    "证监会门类只有约 19 类（制造业常占 60%+），与申万一级 31 类没有官方一一映射，"
                    "故门类分布只作独立展示与总量约束，不做强制映射。"},
            ],
            "confidence": "overlap × exp(−gap/365) × (0.5 if lookahead else 1)",
            "benchmark": "当期全池 disclosed 归一化后的行业均值，分母为该期全部基金（未配置按 0）；只用披露值，不受补齐假设污染",
            "discipline": "disclosed 与 carried 分存分展示；收益/归因类结论一律只用 disclosed，carried 仅用于行业结构分母校正",
            "not_done": "净值回归第三轨由 build_fund_regression.py 追加（步骤 7）；仍未做的是 FOF 多层嵌套穿透、用回归结果反推个股级持仓",
        },
        "discipline": "carried 仅用于行业结构分母校正；收益/归因结论一律只用 disclosed",
    }
    con.execute(
        "CREATE TABLE IF NOT EXISTS fund_stat_cache (key TEXT PRIMARY KEY, payload_json TEXT, updated_at TEXT)")
    con.execute("INSERT OR REPLACE INTO fund_stat_cache VALUES (?,?,?)",
                ("penetration", json.dumps(cache, ensure_ascii=False, default=float),
                 datetime.now().isoformat(timespec="seconds")))
    con.commit()
    n1 = con.execute("SELECT COUNT(*) FROM fund_hold_coverage").fetchone()[0]
    n2 = con.execute("SELECT COUNT(*) FROM fund_industry_exposure").fetchone()[0]
    con.close()
    log(f"入库完成：coverage {n1:,} 行 / exposure {n2:,} 行")
    return cache


def diagnose_old() -> dict:
    """旧截断数据的体检结论，写进页面作为「修复前后」对照。"""
    if not OLD_CSV.exists():
        return {}
    agg: dict[tuple[str, str], list] = defaultdict(lambda: [0, 0.0])
    with open(OLD_CSV, newline="") as f:
        rd = csv.reader(f)
        next(rd)
        for r in rd:
            a = agg[(r[0], r[2])]
            a[0] += 1
            try:
                a[1] += float(r[6] or 0)
            except ValueError:
                pass
    out = {}
    for y in range(2015, 2026):
        cs = [v[1] for (c, ed), v in agg.items() if ed.startswith(str(y)) and ed[4:] == "1231"]
        if cs:
            cs.sort()
            out[str(y)] = round(cs[len(cs) // 2], 1)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=0)
    ap.add_argument("--input", type=str, action="append", default=None,
                    help="指定明细 CSV，可多次传；缺省读 holdings_raw.csv + 所有增量批次")
    ap.add_argument("--db", type=str, default=None,
                    help="目标库路径，默认 data/fund_research.db。演练时指向临时库，避免污染生产数据")
    args = ap.parse_args()
    if args.db:
        # 演练用：只改写入目标，读取的监管锚等仍来自研究库
        global DB, WRITE_BY_FUND
        DB = Path(args.db)
        # by_fund/*.csv 是主结果的钻取缓存（API 单基金详情直接读它）。
        # 演练若照写，会出现「临时库 + 生产明细」的错位，所以演练只算不落盘。
        WRITE_BY_FUND = False
        log(f"写入目标库：{DB}（演练模式，不覆盖 by_fund 明细）")
    if args.input:
        paths = [Path(p) for p in args.input]
    else:
        # 基础全量 + 所有增量批次（holdings_2026.csv 之类）。
        # 排序保证基础文件先读、增量后读（去重时 keep="last" 生效）。
        paths = [RAW]
        paths += sorted(p for p in DATA.glob("holdings_*.csv") if p.name != RAW.name)
    lookup = IndustryLookup()
    df = load_holdings(paths, args.sample)
    res = build(df, lookup)
    diag = diagnose_old()
    cache = persist(res, args.sample, diag)

    (DATA / "penetration_summary.json").write_text(
        json.dumps(cache, ensure_ascii=False, indent=2, default=float), encoding="utf-8")
    log("=" * 66)
    log(f"基金 {cache['scope']['n_funds']} / 截面 {cache['scope']['n_sections']} / "
        f"明细 {cache['detail_rows']:,} 行")
    log(f"覆盖度：{json.dumps(cache['coverage_by_type'], ensure_ascii=False)}")
    log(f"行业映射来源：{cache['industry_source']}")
    log(f"旧数据 cover 中位（按年）：{json.dumps(diag, ensure_ascii=False)}")


if __name__ == "__main__":
    main()
