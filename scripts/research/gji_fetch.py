"""
GJI 对照 · 拉取 7 只宽基 ETF 日线（量能口径）
GJI 原文用的 7 只：510300 159919 510050 510500 159915 512100 588000
输出：/data/research/raw/etfvol_*.csv
"""
import csv, json, os, sys, time, urllib.request
from pathlib import Path

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
    sys.exit("no token")

OUT = Path("/data/research/raw")
OUT.mkdir(parents=True, exist_ok=True)


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


GJI_ETFS = {
    "510300.SH": "hs300a", "159919.SZ": "hs300b", "510050.SH": "sz50",
    "510500.SH": "zz500", "159915.SZ": "cyb", "512100.SH": "zz1000", "588000.SH": "kc50",
}

print("=== 7 只宽基 ETF 日线（量能口径）===")
for code, tag in GJI_ETFS.items():
    rows = call("fund_daily", {"ts_code": code, "start_date": "20180101", "end_date": "20261102"},
                "ts_code,trade_date,open,high,low,close,vol,amount")
    path = OUT / f"etfvol_{tag}.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ts_code", "trade_date", "open", "high", "low", "close", "vol", "amount"])
        w.writerows(rows)
    print(f"  etfvol_{tag}.csv  {len(rows):>5} rows  ({code})")

print("\nDONE")
