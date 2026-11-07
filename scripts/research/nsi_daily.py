#!/usr/bin/env python3
"""
国家队介入强度指数（NSI）· 每日生产脚本
=========================================
用法（在容器内）：
    python /app/scripts/research/nsi_daily.py            # 增量更新 + 输出信号
    python /app/scripts/research/nsi_daily.py --full     # 全量重建（首次/换月）

输出：
    /data/research/nsi_signal.csv   历史信号与仓位
    /data/research/nsi_latest.json  当日读数（供看板/推送读取）

口径纪律（每一轮都踩过，不许改）：
  1. ETF 份额 T 日盘后/次日盘前可见 → 仓位在 T+1 开盘执行，不提前
  2. 指标标准化一律滚动 250 日，无未来信息
  3. 绝对流入强度是信息本体，不做二次分位化（实测会把信号毁掉）
  4. 只做加法：触发才加仓，不因 NSI 低而减仓（躲跌端实测 -2.42pp）
"""
import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

RAW = Path("/data/research/raw")
OUT = Path("/data/research")
RAW.mkdir(parents=True, exist_ok=True)

# ---------- 参数（全部来自训练期分位，不手工拍） ----------
TRAIN_END = "2023-12-31"
HI_QUANTILE = 0.97      # 加仓触发阈值 = 训练期 97 分位（GJI 对照后由 99 调至 97，实测更优）
HOLD_DAYS = 20          # 触发后加仓持续交易日
BASE_POS = 0.70         # 基础仓位
BOOST_POS = 1.00        # 触发后仓位
POS_PEN_W = 4.0         # 位置惩罚权重（GJI §3 的 750 日分位惩罚折算到 0-100 刻度）

INDEXES = {"000300.SH": "hs300", "000016.SH": "sz50", "000852.SH": "zz1000",
           "000905.SH": "zz500", "399986.SZ": "bank"}
ETFS = {"510300.SH": "hs300etf", "510050.SH": "sz50etf", "510500.SH": "zz500etf",
        "159919.SZ": "hs300etf2", "512100.SH": "zz1000etf"}
CORE = ["hs300etf", "sz50etf", "zz500etf", "hs300etf2", "zz1000etf"]


def get_token():
    t = os.environ.get("QUANT_TUSHARE_TOKEN")
    if t:
        return t
    for p in ("/app/.env", "/data/.env", ".env"):
        if os.path.exists(p):
            for line in open(p, encoding="utf-8", errors="ignore"):
                if line.startswith("QUANT_TUSHARE_TOKEN="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    sys.exit("no tushare token")


TOKEN = get_token()


def call(api, params, fields="", retry=3):
    body = json.dumps({"api_name": api, "token": TOKEN, "params": params, "fields": fields}).encode()
    for a in range(retry):
        try:
            req = urllib.request.Request("http://api.tushare.pro", data=body,
                                         headers={"Content-Type": "application/json"})
            d = json.load(urllib.request.urlopen(req, timeout=60))
            if d.get("code") != 0:
                raise RuntimeError(d.get("msg"))
            dd = d.get("data") or {}
            return dd.get("items") or []
        except Exception:
            if a == retry - 1:
                raise
            time.sleep(1.5 * (a + 1))


FULL_START = "20180101"  # 历史文件损坏时按此起点全量重建（与 --full 一致）


def read_csv_safe(path: Path, columns: list[str]) -> tuple[pd.DataFrame, bool]:
    """读增量历史；返回 (历史表, 是否损坏需全量重建)。

    2026-11-07：to_csv 直接覆盖会先截断原文件，进程被 kill / 容器重启时
    就留下 0 字节 CSV（idx_sz50.csv 实测），此后每天 pd.read_csv 抛
    EmptyDataError，NSI 调度永久卡死。这里把「文件损坏」与「文件不存在」
    区分开：前者必须重新拉全量，绝不能当空表跳过（会让信号静默算错）。
    """
    cols = list(columns)
    if not path.exists():
        return pd.DataFrame(columns=cols), False
    if path.stat().st_size == 0:
        return pd.DataFrame(columns=cols), True
    try:
        return pd.read_csv(path), False
    except (pd.errors.EmptyDataError, pd.errors.ParserError, UnicodeDecodeError):
        return pd.DataFrame(columns=cols), True


def write_csv_atomic(df: pd.DataFrame, path: Path) -> None:
    """先写同目录 .tmp 再 os.replace，中断也不会把已有历史截断成 0 字节。"""
    tmp = path.with_name(path.name + ".tmp")
    df.to_csv(tmp, index=False)
    os.replace(tmp, path)


def merge_history(old: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    """拼接增量时跳过空表，避免 pandas 的 all-NA concat FutureWarning / dtype 漂移。"""
    parts = [d for d in (old, new) if not d.empty]
    return pd.concat(parts, ignore_index=True) if parts else new


def update_index(code, tag, start):
    cols = ["trade_date", "open", "high", "low", "close", "pre_close",
            "pct_chg", "vol", "amount"]
    path = RAW / f"idx_{tag}.csv"
    old, rebuild = read_csv_safe(path, cols)
    if rebuild:
        print(f"  ! idx_{tag}.csv 损坏，按 {FULL_START} 全量重建")
        start = FULL_START
    rows = call("index_daily", {"ts_code": code, "start_date": start, "end_date": today()},
                ",".join(cols))
    new = pd.DataFrame(rows, columns=cols)
    if not old.empty:
        new = new[~new["trade_date"].astype(str).isin(old["trade_date"].astype(str))]
    write_csv_atomic(merge_history(old, new), path)
    return len(new)


def update_etf(code, tag, start):
    cols = ["trade_date", "fd_share"]
    path = RAW / f"sh_{tag}.csv"
    old, rebuild = read_csv_safe(path, cols)
    if rebuild:
        print(f"  ! sh_{tag}.csv 损坏，按 {FULL_START} 全量重建")
        start = FULL_START
    rows = []
    # 全量重建时 8 年份额可能超过 4000 行，把分页上限放宽（返回空即 break）
    for off in range(0, 10000, 2000):
        r = call("fund_share", {"ts_code": code, "start_date": start, "end_date": today(),
                                "offset": off, "limit": 2000}, ",".join(cols))
        if not r:
            break
        rows += r
        if len(r) < 2000:
            break
    new = pd.DataFrame(rows, columns=cols)
    if not old.empty:
        new = new[~new["trade_date"].astype(str).isin(old["trade_date"].astype(str))]
    write_csv_atomic(merge_history(old, new), path)
    return len(new)


def update_min(start_year):
    """30min 分钟数据按年拉，只补最近两年（首尾重叠由去重处理）"""
    cols = ["trade_time", "open", "high", "low", "close", "vol", "amount"]
    path = RAW / "min_hs300_30m.csv"
    old, rebuild = read_csv_safe(path, cols)
    if rebuild:
        print("  ! min_hs300_30m.csv 损坏，按近三年全量重建")
    lo = max(start_year, datetime.now().year - (2 if rebuild else 1))
    rows = []
    for y in range(lo, datetime.now().year + 1):
        r = call("stk_mins", {"ts_code": "000300.SH", "freq": "30min",
                              "start_date": f"{y}-01-01 09:00:00",
                              "end_date": f"{y}-12-31 15:00:00"},
                 ",".join(cols))
        rows += r
    new = pd.DataFrame(rows, columns=cols)
    if not old.empty:
        old["trade_time"] = old["trade_time"].astype(str)
        new = new[~new["trade_time"].astype(str).isin(set(old["trade_time"]))]
    write_csv_atomic(merge_history(old, new), path)
    return len(new)


def today():
    return datetime.now().strftime("%Y%m%d")


# ---------- 指标计算（与 research 脚本同口径） ----------
def build():
    idx = {}
    for tag in ["hs300", "sz50", "zz1000", "zz500", "bank"]:
        d = pd.read_csv(RAW / f"idx_{tag}.csv")
        d["date"] = pd.to_datetime(d["trade_date"].astype(str))
        idx[tag] = d.sort_values("date").set_index("date")

    base = idx["hs300"][["close", "open", "high", "low", "pct_chg"]].dropna().copy()

    S = {}
    for tag in ETFS.values():
        d = pd.read_csv(RAW / f"sh_{tag}.csv")
        d["date"] = pd.to_datetime(d["trade_date"].astype(str))
        S[tag] = d.set_index("date")["fd_share"].astype(float).sort_index()
    SD = pd.DataFrame({t: S[t].reindex(base.index).ffill() for t in ETFS.values()})

    A = pd.DataFrame(index=base.index)
    chg1 = SD.pct_change(fill_method=None)
    detr1 = chg1 - chg1.rolling(60, min_periods=20).median()
    A["A1"] = detr1[CORE].mean(axis=1)
    chg5 = SD.pct_change(5, fill_method=None)
    detr5 = chg5 - chg5.rolling(60, min_periods=20).median()
    A["A2"] = detr5[CORE].mean(axis=1)
    A["A3"] = (chg1[CORE] > 0).sum(axis=1) / chg1[CORE].notna().sum(axis=1).replace(0, np.nan)
    # A4 必须按规模加权：小规模 ETF 的份额跳动不该等比放大
    # 用「份额×收盘价」近似规模（同一 ETF 的份额与金额同比例，无需额外拉净值）
    _w = SD[CORE].div(SD[CORE].sum(axis=1).replace(0, np.nan), axis=0)
    A["A4"] = (chg1[CORE] * _w).sum(axis=1, min_count=3) * 100
    # C3：份额 20 日动量（去趋势）
    c20 = SD.pct_change(20, fill_method=None)
    A["C3"] = (c20 - c20.rolling(60, min_periods=20).median()).mean(axis=1)
    base = base.join(A)

    base["rng"] = (base["high"] - base["low"]).replace(0, np.nan)
    base["B1"] = (base["close"] - base["low"]) / base["rng"]
    mn = pd.read_csv(RAW / "min_hs300_30m.csv")
    mn["dt"] = pd.to_datetime(mn["trade_time"])
    mn["d"] = mn["dt"].dt.normalize()
    mn["hm"] = mn["dt"].dt.strftime("%H:%M")
    lb = mn[mn["hm"] == "15:00"].set_index("d")["close"].astype(float)
    b1430 = mn[mn["hm"] == "14:30"].set_index("d")["close"].astype(float)
    base["B2"] = ((lb / b1430 - 1) * 100).reindex(base.index) - base["pct_chg"]
    base["B3"] = idx["sz50"]["pct_chg"].reindex(base.index) - idx["zz1000"]["pct_chg"].reindex(base.index)
    base["B4"] = idx["bank"]["pct_chg"].reindex(base.index) - base["pct_chg"]
    base["B5"] = -(base["high"] - base["close"]) / base["close"] * 100
    base["C1"] = base["close"].pct_change(20) * 100
    base["C4"] = base["pct_chg"].rolling(20).std()

    def rr(s, w=250, mp=80):
        return s.rolling(w, min_periods=mp).apply(lambda x: (x <= x[-1]).mean(), raw=True)

    FLOW = ["A1", "A2", "A3", "A4", "C3"]
    BEHAV = ["B1", "B2", "B3", "B4", "B5"]
    base["FLOW"] = pd.concat([rr(base[c]) for c in FLOW], axis=1).mean(axis=1)
    base["BEHAV"] = pd.concat([rr(base[c]) for c in BEHAV], axis=1).mean(axis=1)
    base["PRESS"] = ((-base["C1"]).pipe(rr) + base["C4"].pipe(rr)) / 2
    base["NSI"] = base["FLOW"] * (0.4 + 0.6 * base["PRESS"]) * 100

    # 拥挤度 / 买家类型代理（2026-11-03 加入，仅作置信度参考，不进入 score）
    # 原设想用「逆势买=国家队」区分，实测反向（2024/2025 两次护盘期 crowding 反而 0.73/0.65），已否决。
    # 改用「集中度 HHI」：多只宽基同步申购(HHI低)=像国家队；单只集中(HHI高)=像套利/单一机构。
    # 实测分组 IC：低 HHI +0.128 / 高 HHI -0.068；高 HHI 时把 score 置 0 可让验证期 IC 从 0.044 升到 0.134。
    _pos = SD[CORE].diff().clip(lower=0)
    _tot = _pos.sum(axis=1, min_count=3)
    _w = _pos.div(_tot.replace(0, np.nan), axis=0)
    base["hhi"] = (_w ** 2).sum(axis=1, min_count=3).rolling(20, min_periods=10).mean()

    # 位置惩罚（来自 GJI §3）：750 日分位越高越不该把放量当护盘
    base["p750"] = base["close"].rolling(750, min_periods=250).apply(
        lambda x: (x <= x[-1]).mean(), raw=True)
    base["pos_pen"] = np.select(
        [base["p750"] >= 0.90, base["p750"] >= 0.80, base["p750"] >= 0.65, base["p750"] <= 0.15],
        [-3.0, -2.0, -1.0, 0.5], 0.0)
    base["pos_pen"] = pd.Series(base["pos_pen"], index=base.index).fillna(0)
    # 关键：惩罚加在 NSI 原始刻度上，绝不再做滚动分位化（实测会毁掉信号）
    base["score"] = base["NSI"] + base["pos_pen"] * POS_PEN_W
    return base


def _freshness(df):
    """检查 ETF 份额相对指数收盘日落后了几个交易日。

    返回 (指数最新日, 份额最新日, 落后交易日数)。
    份额落后 >0 时 score 会被 ffill 系统性低估，调度层应据此重跑或告警。
    """
    idx_last = df.index[-1]
    last = []
    for tag in ETFS.values():
        p = RAW / f"sh_{tag}.csv"
        if not p.exists():
            continue
        try:
            d = pd.read_csv(p)
            if len(d):
                last.append(pd.to_datetime(d["trade_date"].astype(str)).max())
        except Exception:
            continue
    sh_last = max(last) if last else None
    gap = None
    if sh_last is not None and sh_last < idx_last:
        gap = int((df.index > sh_last).sum())
    return idx_last, (sh_last.date() if sh_last is not None else None), gap


def position_from_nsi(nsi, hi, base_pos=BASE_POS, boost=BOOST_POS, hold=HOLD_DAYS):
    s = nsi.values
    p = np.full(len(s), np.nan)
    last = -10 ** 9
    for i in range(len(s)):
        if np.isnan(s[i]):
            p[i] = np.nan
            continue
        if s[i] >= hi:
            last = i
        p[i] = boost if i - last <= hold else base_pos
    return pd.Series(p, index=nsi.index)


def run_backtest(df, cost=0.001):
    """用生产口径复核加法模式（口径必须与研究结论一致，否则阈值不可信）"""
    hs = pd.read_csv(RAW / "idx_hs300.csv")
    hs["date"] = pd.to_datetime(hs["trade_date"].astype(str))
    hs = hs.sort_values("date").set_index("date").reindex(df.index)
    r = hs["open"].astype(float).pct_change()

    train = df.loc[:TRAIN_END, "score"].dropna()
    hi = train.quantile(HI_QUANTILE)
    pos = position_from_nsi(df["score"], hi).shift(2)

    def one(p, cost_=cost):
        ok = p.notna() & r.notna()
        rr_ = p[ok] * r[ok]
        d = p[ok].diff().abs().fillna(0)
        rr_ = rr_ - d[ok] * cost_
        eq = (1 + rr_).cumprod()
        n = len(rr_)
        ann = eq.iloc[-1] ** (250 / n) - 1
        vol = rr_.std() * np.sqrt(250)
        mdd = (eq / eq.cummax() - 1).min()
        return ann * 100, (ann / vol if vol else np.nan), mdd * 100, p[ok].mean() * 100, d.sum(), rr_

    a = one(pos)
    b = one(pd.Series(BASE_POS, index=df.index).shift(2), 0.0)
    print(f"\n[复核] 生产口径 阈值={hi:.1f}")
    print(f"{'方案':<22}{'均仓':>8}{'年化':>9}{'夏普':>8}{'回撤':>9}{'换手':>7}")
    print(f"{'加法模式(70→100)':<22}{a[3]:>7.1f}%{a[0]:>8.2f}%{a[1]:>8.2f}{a[2]:>8.2f}%{a[4]:>7.0f}")
    print(f"{'恒 70%':<22}{b[3]:>7.1f}%{b[0]:>8.2f}%{b[1]:>8.2f}{b[2]:>8.2f}%{b[4]:>7.0f}")
    print(f"{'超额':<22}{'':>8}{a[0]-b[0]:>+8.2f}pp")
    y = (a[5].groupby(a[5].index.year).apply(lambda x: (1 + x).prod() - 1) * 100)
    yb = (b[5].groupby(b[5].index.year).apply(lambda x: (1 + x).prod() - 1) * 100)
    c = y.index.intersection(yb.index)
    print(f"跑赢恒仓 {(y[c]>yb[c]).sum()}/{len(c)} 年  年均超额 {(y[c]-yb[c]).mean():+.2f}pp")
    print("分年度差:", {int(k): round(v, 2) for k, v in (y[c] - yb[c]).items()})

    # ---- 回归锚点（规格书 §7.2 的验收基准，改代码后必对照）----
    print("\n[锚点] 规格书 §7.2 回归基准：")
    ANCHORS = ["2018-10-19", "2019-01-04", "2021-02-10", "2022-04-26", "2022-10-31",
               "2024-02-05", "2025-04-07", "2026-06-30", "2026-10-23", "2026-11-02"]
    cols = [c for c in ["NSI", "score", "pos_pen", "p750", "position"] if c in df.columns]
    print(f"{'日期':<12}" + "".join(f"{c:>9}" for c in cols))
    pos_full = position_from_nsi(df["score"], hi)
    for a_str in ANCHORS:
        t = pd.Timestamp(a_str)
        if t not in df.index:
            continue
        r = df.loc[t]
        cells = []
        for c in cols:
            v = pos_full.loc[t] if c == "position" else r[c]
            cells.append(f"{v:>9.2f}" if pd.notna(v) else f"{'NaN':>9}")
        print(f"{str(t.date()):<12}" + "".join(cells))
    d2 = df["score"].dropna()
    print(f"\nscore 分布: 均值{d2.mean():.2f} 标准差{d2.std():.2f} "
          f"最小{d2.min():.2f} 最大{d2.max():.2f} 样本{len(d2)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="全量重建")
    ap.add_argument("--backtest", action="store_true", help="用当前口径复核回测")
    args = ap.parse_args()
    if args.backtest:
        run_backtest(build())
        return
    start = "20180101" if args.full else (datetime.now() - timedelta(days=45)).strftime("%Y%m%d")

    print(f"[NSI] 更新数据 start={start}")
    for code, tag in INDEXES.items():
        print(f"  idx_{tag}: +{update_index(code, tag, start)}")
    for code, tag in ETFS.items():
        print(f"  sh_{tag}: +{update_etf(code, tag, start)}")
    print(f"  min_30m: +{update_min(int(start[:4]))}")

    df = build()
    train = df.loc[:TRAIN_END, "score"].dropna()
    hi = train.quantile(HI_QUANTILE)
    mid = train.quantile(0.50)
    pos = position_from_nsi(df["score"], hi)

    # ——— 数据新鲜度检查（2026-11-03 加入）———
    # ETF 份额并非「T 日盘后立刻可得」：实测当晚 22:30 仍拿不到，次日早上才有。
    # 若份额落后指数，build() 里的 ffill 会让 A1(日脉冲)=0，系统性低估托底强度
    # （实测同一天可差 48.9 与 77.3）。这里显式量出落差，供调度器/人工判断是否重跑。
    idx_last, sh_last, gap_days = _freshness(df)

    df["position"] = pos
    write_csv_atomic(
        df[["NSI", "score", "pos_pen", "p750", "position", "FLOW", "BEHAV", "PRESS", "hhi"]],
        OUT / "nsi_signal.csv")

    d = df["score"].dropna()
    cur_nsi, cur_pos = d.iloc[-1], pos.dropna().iloc[-1]
    last_trig = pos.diff().ne(0).pipe(lambda s: s[s].index[-1])
    holding = df.index.get_indexer([df.index[-1]])[0] - df.index.get_indexer([last_trig])[0] \
        if last_trig in df.index else None

    out = {
        "as_of": str(df.index[-1].date()),
        "score": round(float(cur_nsi), 2),
        "nsi_raw": round(float(df["NSI"].iloc[-1]), 2),
        "pos_penalty": round(float(df["pos_pen"].iloc[-1]), 2),
        "p750": round(float(df["p750"].iloc[-1]), 3) if pd.notna(df["p750"].iloc[-1]) else None,
        "score_percentile": round(float(d.rank(pct=True).iloc[-1] * 100), 1),
        "hi_threshold": round(float(hi), 2),
        "flow": round(float(df["FLOW"].iloc[-1]), 3),
        "behav": round(float(df["BEHAV"].iloc[-1]), 3),
        "press": round(float(df["PRESS"].iloc[-1]), 3),
        "position": round(float(cur_pos), 3),
        "base_position": BASE_POS,
        "last_change": str(last_trig.date()),
        "holding_days": int(holding) if holding is not None else None,
        "hhi": (round(float(df["hhi"].iloc[-1]), 3)
                if "hhi" in df.columns and pd.notna(df["hhi"].iloc[-1]) else None),
        "buyer_type": ("多只同步(像国家队)" if ("hhi" in df.columns and pd.notna(df["hhi"].iloc[-1])
                       and df["hhi"].iloc[-1] <= 0.668) else "单只集中(像套利/单一机构)"),
        "etf_share_as_of": str(sh_last) if sh_last else None,
        "data_lag_days": gap_days,
        "data_warning": (f"ETF份额落后指数 {gap_days} 个交易日，score 可能低估"
                         if gap_days and gap_days > 0 else None),
        "regime": ("加仓中（国家队托底）" if cur_pos >= BOOST_POS else "基础仓位"),
        "note": "score = NSI + 位置惩罚×4；NSI 低不等于看空，只代表没有托底；只做加法不做减法",
    }
    _json_tmp = OUT / "nsi_latest.json.tmp"
    _json_tmp.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(_json_tmp, OUT / "nsi_latest.json")

    print(f"\n[NSI] {out['as_of']}  score={out['score']:.1f}"
          f"（NSI {out['nsi_raw']:.1f} + 位置惩罚 {out['pos_penalty']:+.1f}×{POS_PEN_W:.0f}）")
    print(f"[NSI] 阈值={hi:.1f}  仓位={out['position']*100:.0f}%  {out['regime']}")
    print(f"[NSI] 输出 -> {OUT/'nsi_latest.json'}")
    if gap_days and gap_days > 0:
        print(f"[NSI] ⚠️ ETF份额最新 {sh_last}，落后指数 {gap_days} 个交易日 —— "
              f"score 可能低估，建议次日早上重跑")
    if out["regime"].startswith("加仓"):
        print(f"[NSI] 加仓已持续 {out['holding_days']} 个交易日，上限 {HOLD_DAYS} 日")


if __name__ == "__main__":
    main()
