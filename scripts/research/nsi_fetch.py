"""
国家队介入强度指数（NSI）· 第 1 步：数据拉取
数据源：tushare（token 从 /data/.env 或环境变量读）
输出：/data/research/raw/*.csv
"""
import json, os, sys, time, urllib.request
from pathlib import Path
import csv

TOKEN = os.environ.get("QUANT_TUSHARE_TOKEN")
if not TOKEN:
    for p in ("/app/.env", "/data/.env", ".env"):
        if os.path.exists(p):
            for line in open(p, encoding="utf-8", errors="ignore"):
                if line.startswith("QUANT_TUSHARE_TOKEN="):
                    TOKEN = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
        if TOKEN:
            break
if not TOKEN:
    sys.exit("no tushare token")

OUT = Path("/data/research/raw")
OUT.mkdir(parents=True, exist_ok=True)
START, END = "20180101", "20261102"


def call(api, params, fields=""):
    body = json.dumps({"api_name": api, "token": TOKEN, "params": params, "fields": fields}).encode()
    for attempt in range(3):
        try:
            req = urllib.request.Request("http://api.tushare.pro", data=body,
                                        headers={"Content-Type": "application/json"})
            d = json.load(urllib.request.urlopen(req, timeout=60))
            if d.get("code") != 0:
                raise RuntimeError(f"{api}: {d.get('msg')}")
            dd = d.get("data") or {}
            return dd.get("items") or [], dd.get("fields") or []
        except Exception as e:
            if attempt == 2:
                raise
            time.sleep(1.5 * (attempt + 1))


def save(name, rows, header):
    path = OUT / f"{name}.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    print(f"  {name}.csv  {len(rows):>6} rows")
    return path


# ---------- 1. 指数日线 ----------
INDEXES = {
    "000300.SH": "hs300", "000016.SH": "sz50", "000852.SH": "zz1000",
    "000905.SH": "zz500", "399986.SZ": "bank", "000001.SH": "sse",
}
print("=== 指数日线 ===")
for code, tag in INDEXES.items():
    rows, _ = call("index_daily", {"ts_code": code, "start_date": START, "end_date": END},
                   "trade_date,open,high,low,close,pre_close,pct_chg,vol,amount")
    save(f"idx_{tag}", rows, ["trade_date", "open", "high", "low", "close", "pre_close", "pct_chg", "vol", "amount"])

# ---------- 2. 宽基 ETF 份额 ----------
ETFS = {
    "510300.SH": "hs300etf", "510050.SH": "sz50etf", "510500.SH": "zz500etf",
    "159919.SZ": "hs300etf2", "588000.SH": "kc50etf", "159915.SZ": "cybetf",
    "512100.SH": "zz1000etf", "510880.SH": "divetf",
}
print("=== ETF 份额 ===")
for code, tag in ETFS.items():
    acc = []
    for off in range(0, 8000, 2000):
        rows, _ = call("fund_share", {"ts_code": code, "start_date": START, "end_date": END,
                                      "offset": off, "limit": 2000}, "trade_date,fd_share")
        if not rows:
            break
        acc += rows
        if len(rows) < 2000:
            break
    save(f"sh_{tag}", acc, ["trade_date", "fd_share"])

# ---------- 3. ETF 净值（算金额口径的资金流） ----------
print("=== ETF 净值 ===")
for code, tag in ETFS.items():
    rows, _ = call("fund_nav", {"ts_code": code, "start_date": START, "end_date": END},
                   "nav_date,unit_nav,accum_nav")
    # 净值数据可能分段返回，只取前 6000
    save(f"nav_{tag}", rows, ["nav_date", "unit_nav", "accum_nav"])

# ---------- 4. 沪深300 30min 分钟数据（尾盘强度） ----------
print("=== 30min 分钟数据 ===")
mins = []
for y in range(2018, 2027):
    rows, _ = call("stk_mins", {"ts_code": "000300.SH", "freq": "30min",
                                "start_date": f"{y}-01-01 09:00:00", "end_date": f"{y}-12-31 15:00:00"},
                   "trade_time,open,high,low,close,vol,amount")
    mins += rows
    print(f"  {y}: {len(rows)}")
mins.sort(key=lambda r: r[0])
save("min_hs300_30m", mins, ["trade_time", "open", "high", "low", "close", "vol", "amount"])

# ---------- 5. 全市场成交额（流动性/情绪） ----------
print("=== 全市场日线成交额 ===")
# 用指数成交额代替（hs300 + zz1000 已有），另外拉两市成交用 daily_info
try:
    rows, _ = call("daily_info", {"trade_date": "", "start_date": START, "end_date": END},
                   "trade_date,ts_code,ts_name,com_count,amount,vol")
    save("daily_info", rows, ["trade_date", "ts_code", "ts_name", "com_count", "amount", "vol"])
except Exception as e:
    print("  daily_info skipped:", str(e)[:80])

print("\nDONE ->", OUT)
