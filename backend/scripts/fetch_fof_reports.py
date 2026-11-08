#!/usr/bin/env python3
"""FOF 基金层持仓管道：定期报告 PDF -> 「基金投资明细」-> fund_fof_holdings。

为什么需要这条管道
------------------
Tushare `fund_portfolio` 和天天基金 `FundArchivesDatas?type=jjcc` 对 FOF **只返回股票**：
016146 回 4 只股票、stk_mkv_ratio 合计 100%，但那只是股票仓位内部的 100%，
股票仅占净值 0.17%，基金层完全不可见。
FOF 持有哪只基金只写在定期报告里，是法定披露、免费、可解析。

数据源
------
- 公告清单：https://api.fund.eastmoney.com/f10/JJGG?fundCode=&type=3&pageIndex=&pageSize=
            type=3 = 「定期报告」分类（实测 006580 返回 52 期）
- PDF 直链：https://pdf.dfcfw.com/pdf/H2_{announcementId}_1.pdf
- 法规依据：《公开募集证券投资基金信息披露内容与格式准则第2号》
            中期/年度报告 §7.12.2「所有基金投资明细」
            季度报告   §6.1「前十名基金投资明细」

口径
----
- 份额类别（A/C/E/Y）的定期报告持仓部分完全相同（持仓是基金层面），
  按产品去重只解析代表份额；净值/份额合计等差异不影响本表。
- 只做公募 FOF。债基/货基/私募不覆盖（2026-11-08 决策），
  故底层是债基/货基的行仍照实入库，只是下游拿不到它的持仓。
- 不估算、不补权重，只落原始披露值。

依赖
----
  pdfplumber（表格结构）/ pypdfium2（快速定位章节页，可选）/ urllib（标准库）
  本机可用 /usr/bin/python3 直接跑。

用法
----
  /usr/bin/python3 backend/scripts/fetch_fof_reports.py --status
  /usr/bin/python3 backend/scripts/fetch_fof_reports.py --years 2026 --limit 5 --keep-pdf
  /usr/bin/python3 backend/scripts/fetch_fof_reports.py --years 2023-2026 --workers 4
  /usr/bin/python3 backend/scripts/fetch_fof_reports.py --reparse            # 只重解析已下载
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
from concurrent.futures.process import BrokenProcessPool
import io
import json
import os
import re
import sqlite3
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path

for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)

ROOT = Path(__file__).resolve().parents[2]
DB = ROOT / "data" / "fund_research.db"
MAIN_DB = ROOT / "data" / "quant_dev.db"
DATA = ROOT / "data" / "research"
PDFDIR = ROOT / "data" / "fof_pdf"
LOG = DATA / "fetch_fof.log"
PROGRESS = DATA / "progress_fof.json"

ANN_URL = "https://api.fund.eastmoney.com/f10/JJGG"
PDF_URL = "https://pdf.dfcfw.com/pdf/H2_{}_1.pdf"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Referer": "https://fundf10.eastmoney.com/",
}

# 节标题（注意：全量版与前十版措辞不同）
# 章节标题在不同年份/不同报告类型里写法不一致，实测至少三种：
#   7.12.2 …排序的【所有】基金投资明细（2026 中报）
#   7.12.2 …排序的基金投资明细      （2025 中报，无「所有」）
#   8.12.2 …基金投资明细            （2025 年报，章节号是 8 不是 7）
# 所以用最短的公共子串定位，靠「哪一页抽到的行最多」来排除目录页等误命中。
START_KEY = "基金投资明细"
START_FULL = "所有基金投资明细"
START_TOP = "前十名基金投资明细"
SKIP_KW = ("招募说明书", "基金合同", "托管协议", "上市交易公告书", "发售公告", "澄清公告")
NUM_RE = re.compile(r"-?[\d,]+\.\d{2}%?")   # 有的报告在占净值比后面带 %
CODE_RE = re.compile(r"\d{6}")

DDL = """
CREATE TABLE IF NOT EXISTS fund_fof_reports (
  base_code   TEXT NOT NULL,
  ann_id      TEXT NOT NULL,
  title       TEXT,
  report_type TEXT,              -- Q1/Q2/Q3/Q4/H1/FY
  end_date    TEXT,              -- 报告期 YYYYMMDD
  pub_date    TEXT,
  pdf_url     TEXT,
  n_pages     INTEGER,
  n_rows      INTEGER,
  sum_weight  REAL,              -- Σ 占净值比(%)，应与「基金投资」资产占比接近
  parse_status TEXT,             -- ok / empty / fail
  err         TEXT,
  checks_json TEXT,
  fetched_at  TEXT,
  PRIMARY KEY (base_code, ann_id));

CREATE TABLE IF NOT EXISTS fund_fof_holdings (
  base_code   TEXT NOT NULL,     -- FOF 自身 6 位代码
  end_date    TEXT NOT NULL,
  report_type TEXT NOT NULL,     -- 同一报告期 H1 与 Q2 并存（全量 vs 前十），主键必须带上
  ann_id      TEXT,
  seq         INTEGER,
  sub_code    TEXT NOT NULL,     -- 底层基金 6 位代码
  sub_ts_code TEXT,              -- 尽力映射到 xxx.OF / xxx.SZ
  sub_name    TEXT,
  operate_mode TEXT,
  shares      REAL,              -- 持有份额（份）
  mkv         REAL,              -- 公允价值（元）
  weight_pct  REAL,              -- 占基金资产净值比例(%)
  related_party INTEGER,         -- 是否管理人及关联方所管
  PRIMARY KEY (base_code, end_date, report_type, sub_code));

CREATE INDEX IF NOT EXISTS ix_ffh_sub ON fund_fof_holdings(sub_code);
CREATE INDEX IF NOT EXISTS ix_ffh_end ON fund_fof_holdings(end_date);
CREATE INDEX IF NOT EXISTS ix_ffr_ann ON fund_fof_reports(ann_id);

-- 份额类别映射：A/C/E/Y 的持仓完全一样，只解析代表份额，
-- 这张表让下游用任意份额代码都能找到解析结果。
CREATE TABLE IF NOT EXISTS fund_fof_share_map (
  share_code TEXT PRIMARY KEY,
  rep_code   TEXT NOT NULL,
  name       TEXT,
  is_rep     INTEGER);
"""

_lock = threading.Lock()
_local = threading.local()
_pdfium_lock = threading.Lock()


def tconn() -> sqlite3.Connection:
    """每线程复用一个连接；SQLite 连接不可跨线程共享。"""
    c = getattr(_local, "c", None)
    if c is None:
        c = sqlite3.connect(DB, timeout=60)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=60000")
        _local.c = c
    return c


def log(msg: str) -> None:
    line = f"[{datetime.now():%H:%M:%S}] {msg}"
    with _lock:
        print(line, flush=True)
        DATA.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB, timeout=30)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=30000")
    return c


def init_db() -> None:
    with _conn() as c:
        c.executescript(DDL)


def _get(url: str, timeout: int = 40) -> bytes:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


# 源站对并发很敏感：一口气拉到 1100 份左右会开始 connection refused。
# 全局 0.25s 最小间隔（≈4 req/s）+ 指数退避，能把成功率拉到可接受水平。
_dl_lock = threading.Lock()
_last_dl = [0.0]


def _download(url: str, attempts: int = 4) -> bytes:
    last: Exception | None = None
    for k in range(attempts):
        try:
            with _dl_lock:
                wait = 0.25 - (time.time() - _last_dl[0])
                if wait > 0:
                    time.sleep(wait)
                _last_dl[0] = time.time()
            data = _get(url, timeout=90)
            if not data[:4].startswith(b"%PDF"):
                raise RuntimeError(f"非 PDF 响应 {len(data)}B")
            return data
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep([3, 9, 25, 60][min(k, 3)])
    raise last  # type: ignore[misc]


def _get_json(url: str) -> dict:
    return json.loads(_get(url).decode("utf-8", "ignore"))


# ---------------------------------------------------------------- 基金池
def product_key(name: str) -> str:
    n = re.sub(r"[-－—]?\s*[A-ZＡ-Ｚ]$", "", name.strip())
    n = re.sub(r"[（(]\s*FOF\s*[）)]", "(FOF)", n)
    return n


def em_fund_list(max_age_days: int = 7) -> list[list[str]]:
    """东财全量基金名单（28305 条）。缓存到本地，避免每次拉 3.2MB。"""
    cache = DATA / "em_fund_list.json"
    DATA.mkdir(parents=True, exist_ok=True)
    if cache.exists() and (time.time() - cache.stat().st_mtime) < max_age_days * 86400:
        return json.loads(cache.read_text(encoding="utf-8"))
    url = "https://fund.eastmoney.com/js/fundcode_search.js"
    raw = _get(url, timeout=60).decode("utf-8-sig", "ignore")
    arr = json.loads(re.search(r"\[.*\]", raw, re.S).group(0))
    cache.write_text(json.dumps(arr, ensure_ascii=False), encoding="utf-8")
    log(f"刷新东财基金名单：{len(arr)} 条")
    return arr


def universe(dedupe: bool = True, source: str = "em") -> list[tuple[str, str]]:
    """公募 FOF 代表份额列表 [(base_code, name)]。"""
    if source == "master":
        con = sqlite3.connect(f"file:{MAIN_DB}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "SELECT base_code, name FROM fund_master WHERE name LIKE '%FOF%' AND ts_code LIKE '%.OF'"
            ).fetchall()
        finally:
            con.close()
    else:
        # 东财分类里 FOF-稳健型/均衡型/进取型 才是目标；QDII-FOF 也纳入
        rows = [(x[0], x[2]) for x in em_fund_list() if "FOF" in (x[3] + x[2]).upper()]
    rows = sorted(rows)
    if not dedupe:
        return rows

    best: dict[str, tuple[str, str]] = {}
    for code, name in rows:
        k = product_key(name)
        cur = best.get(k)
        if cur is None:
            best[k] = (code, name)
            continue
        # 优先 A 类（同名产品持仓一致，A 类历史最长）
        if re.search(r"[-－—]?A$", name) and not re.search(r"[-－—]?A$", cur[1]):
            best[k] = (code, name)
        elif code < cur[0]:
            best[k] = (code, name)
    return sorted(best.values())


# ---------------------------------------------------------------- 公告清单
def classify(title: str) -> tuple[str | None, str | None]:
    if any(k in title for k in SKIP_KW):
        return None, None
    m = re.search(r"(20\d{2})\s*年", title)
    if not m:
        return None, None
    y = int(m.group(1))
    if "第1季度报告" in title:
        return "Q1", f"{y}0331"
    if "第2季度报告" in title:
        return "Q2", f"{y}0630"
    if "第3季度报告" in title:
        return "Q3", f"{y}0930"
    if "第4季度报告" in title:
        return "Q4", f"{y}1231"
    if "中期报告" in title or "半年度报告" in title:
        return "H1", f"{y}0630"
    if "年度报告" in title:
        return "FY", f"{y}1231"
    return None, None


def list_reports(code: str, pages: int = 4) -> list[dict]:
    out: list[dict] = []
    for p in range(1, pages + 1):
        url = f"{ANN_URL}?fundCode={code}&pageIndex={p}&pageSize=50&type=3"
        try:
            d = _get_json(url)
        except Exception as e:  # noqa: BLE001
            log(f"公告清单失败 {code} p{p}: {e!r}")
            break
        rows = d.get("Data") or []
        if not rows:
            break
        for a in rows:
            rt, end = classify(a.get("TITLE") or "")
            if not rt:
                continue
            out.append(
                {
                    "ann_id": a["ID"],
                    "title": a["TITLE"],
                    "report_type": rt,
                    "end_date": end,
                    "pub_date": (a.get("PUBLISHDATEDesc") or "")[:10].replace("-", ""),
                }
            )
        if len(rows) < 50:
            break
        time.sleep(0.15)
    # 同一报告期可能重复（更正公告保留最新）
    uniq: dict[tuple[str, str], dict] = {}
    for r in out:
        uniq[(r["report_type"], r["end_date"])] = r
    return sorted(uniq.values(), key=lambda x: x["end_date"], reverse=True)


# ---------------------------------------------------------------- PDF 定位与解析
def text_pages(src) -> tuple[list[str], int]:
    """逐页纯文本。pypdfium2 比 pdfminer 快一个量级，用于定位章节。

    src 可以是本地路径，也可以是 PDF 字节流——管道默认在内存里解析，不落盘。
    pdfium 不是线程安全的：多线程同时解析不同 PDF 会在 CPDF_StreamParser 里
    直接 SIGTRAP 崩进程（实测 2026-11-08）。解析走进程池隔离，本函数内再加锁兜底。
    """
    data = str(src) if isinstance(src, Path) else src
    try:
        import pypdfium2 as pdfium

        with _pdfium_lock:
            pdf = pdfium.PdfDocument(data)
            n = len(pdf)
            texts = [pdf[i].get_textpage().get_text_range() for i in range(n)]
            try:
                pdf.close()
            except Exception:  # noqa: BLE001
                pass
        return texts, n
    except Exception:  # noqa: BLE001
        import pdfplumber

        with pdfplumber.open(io.BytesIO(data) if isinstance(data, (bytes, bytearray)) else data) as pdf:
            return [(pg.extract_text() or "") for pg in pdf.pages], len(pdf.pages)


def _nz(t: str) -> str:
    """去掉所有空白：PDF 标题常被折行切断，不归一化就会漏匹配。"""
    return re.sub(r"\s+", "", t)


def _is_toc(t: str) -> bool:
    """目录页特征：大量省略号点线。目录里也印着章节标题，必须先排除。"""
    return t.count("...") >= 3 or t.count("…") >= 3


def candidate_starts(texts: list[str]) -> list[int]:
    out: list[int] = []
    for i, t in enumerate(texts):
        if _is_toc(t):
            continue
        if START_KEY in _nz(t):
            out.append(i)
    return out


def section_pages(start: int, texts: list[str]) -> list[int]:
    """章节候选页：从命中页往后最多 14 页。

    不用关键字截断——季报的「基金份额持有人信息」往往和明细表同页出现，
    关键字会误杀。改由「连续两页都抽不到行」来收尾。
    """
    return list(range(start, min(start + 14, len(texts))))


def parse_text_rows(text: str, cut_heading: bool = False) -> list[dict]:
    """文本兜底：表格抽取在章节首页会因表头碎片化漏行，这里按行首「序号 + 6 位代码」切段。

    行尾恒为 `持有份额 公允价值 占净值比 [是|否]`，因此取段内最后三个数值即可。
    cut_heading 用来跳过章节标题之前的债券投资明细（债券代码同样是 6 位）。
    """
    t = re.sub(r"\s+", " ", text)
    if cut_heading:
        idx = max(t.find(START_FULL), t.find(START_TOP), t.find(START_KEY))
        if idx > 0:
            t = t[idx:]
    starts = [(m.start(), int(m.group(1)), m.group(2))
              for m in re.finditer(r"(?<![\d.])(\d{1,3}) (\d{6}) ", t)]
    out: list[dict] = []
    for k, (pos, seq, code) in enumerate(starts):
        end = starts[k + 1][0] if k + 1 < len(starts) else len(t)
        chunk = t[pos:end].rstrip()
        related = None
        if chunk.endswith("是") or chunk.endswith("否"):
            related = 1 if chunk.endswith("是") else 0
            chunk = chunk[:-1]
        nums = re.findall(r"[\d,]+\.\d{2}", chunk)
        if len(nums) != 3:  # 多于 3 个说明切段跨了多行，宁缺毋滥
            continue
        if _f(nums[-1]) > 100:  # 单只基金占净值比不可能超 100%
            continue
        out.append(
            {
                "seq": seq,
                "sub_code": code,
                "sub_name": "",
                "operate_mode": "",
                "shares": _f(nums[-3]),
                "mkv": _f(nums[-2]),
                "weight_pct": _f(nums[-1]),
                "related_party": related,
            }
        )
    return out


def _f(x: str) -> float:
    return float(x.replace(",", "").replace("%", ""))


def parse_row(cells) -> dict | None:
    cs = [("" if c is None else str(c)).replace("\n", "").replace("\u3000", " ").strip() for c in cells]
    ci = None
    for i, c in enumerate(cs):
        if re.fullmatch(r"\d{6}", c):
            ci = i
            break
    if ci is None:
        return None
    code = cs[ci]
    rest = [c for c in cs[ci + 1:] if c]
    if not rest:
        return None
    related = None
    if rest[-1] in ("是", "否"):
        related = 1 if rest[-1] == "是" else 0
        rest = rest[:-1]
    nums = [c for c in rest if NUM_RE.fullmatch(c)]
    if len(nums) < 3:
        return None
    shares, mkv, w = nums[-3], nums[-2], nums[-1]
    name = rest[0] if rest else ""
    mode = rest[1] if len(rest) > 1 and not NUM_RE.fullmatch(rest[1]) else ""
    seq = None
    pre = [c for c in cs[:ci] if c]
    if pre and re.fullmatch(r"\d{1,4}", pre[0]):
        seq = int(pre[0])
    return {
        "seq": seq,
        "sub_code": code,
        "sub_name": name,
        "operate_mode": mode,
        "shares": _f(shares),
        "mkv": _f(mkv),
        "weight_pct": _f(w),
        "related_party": related,
    }


def parse_pdf(src) -> tuple[list[dict], int, str]:
    import pdfplumber

    data = str(src) if isinstance(src, Path) else src
    texts, n_pages = text_pages(data)
    cands = candidate_starts(texts)
    if not cands:
        # 有的 QDII-FOF 章节标题写法完全不同，但表头一定有「基金名称 + 占基金…比例」。
        # 这类报告底层是境外基金、没有 6 位代码，与债基/货基一样不在覆盖范围。
        for t in texts:
            z = _nz(t)
            if "基金名称" in z and "占基金" in z:
                return [], n_pages, "no_code"
        return [], n_pages, "empty"
    best: list[dict] = []
    saw_fund_table = False
    with pdfplumber.open(io.BytesIO(data) if isinstance(data, (bytes, bytearray)) else data) as pdf:
        for c in cands:
            rows: list[dict] = []
            seen: set[str] = set()
            next_seq = 1
            empty_streak = 0
            for i in section_pages(c, texts):
                if i >= len(pdf.pages):
                    break
                page = pdf.pages[i]
                # 首页上，基金投资明细标题之上还有「债券投资明细」，其行也有 6 位代码，
                # 必须按纵向位置切掉，否则会把 019792 之类当成基金。
                ycut = 0.0
                if i == c:
                    for pat in (START_FULL, START_TOP, "基金投资明细"):
                        hits = page.search(pat) or []
                        if hits:
                            ycut = max(h["bottom"] for h in hits) - 1
                            break
                page_rows: list[dict] = []
                for tb in page.find_tables():
                    if tb.bbox[1] < ycut:
                        continue
                    ex = tb.extract()
                    if ex and any("基金名称" in (c or "") for c in ex[0]):
                        saw_fund_table = True
                    for r in ex:
                        d = parse_row(r)
                        if d:
                            page_rows.append(d)
                table_codes = {d["sub_code"] for d in page_rows}
                # 文本兜底只补表格漏掉的代码（章节首页表头碎片化是主要漏行来源）
                for d in parse_text_rows(texts[i], cut_heading=(i == c)):
                    if d["sub_code"] not in table_codes:
                        page_rows.append(d)
                # 顺序号必须单调递增：能挡掉同一页上其它表格的残留行
                kept: list[dict] = []
                for d in sorted(page_rows, key=lambda x: (x["seq"] is None, x["seq"] or 0)):
                    if d["sub_code"] in seen:
                        continue
                    if d["seq"] is not None and d["seq"] < next_seq:
                        continue
                    kept.append(d)
                if not kept:
                    empty_streak += 1
                    if rows and empty_streak >= 2:
                        break
                    continue
                empty_streak = 0
                for d in kept:
                    rows.append(d)
                    seen.add(d["sub_code"])
                    if d["seq"] is not None:
                        next_seq = max(next_seq, d["seq"] + 1)
            if len(rows) > len(best):
                best = rows
            if len(best) >= 8:  # 命中真章节，不必再试其它候选页
                break
    # 物理约束兜底：单只基金占净值比 ≤100%、份额与市值必须为正
    best = [d for d in best
            if 0 <= (d["weight_pct"] or 0) <= 100 and d["shares"] > 0 and d["mkv"] > 0]
    dedup: dict[str, dict] = {}
    for d in best:
        dedup.setdefault(d["sub_code"], d)
    out = sorted(dedup.values(), key=lambda x: (x["seq"] is None, x["seq"] or 0))
    if out:
        return out, n_pages, "ok"
    # 页面上确实有基金明细表，但没有基金代码列——QDII-FOF 的底层是境外基金，
    # 与债基/货基一样不在本链路覆盖范围，单独标记，别当成解析失败。
    if not saw_fund_table:
        for t in texts:
            z = _nz(t)
            if "基金名称" in z and "占基金" in z:
                saw_fund_table = True
                break
    return [], n_pages, ("no_code" if saw_fund_table else "empty")


def _parse_one(data: bytes):
    """子进程入口：单份 PDF 单进程解析。

    pdfium 的 native heap 在多线程场景下会崩（CPDF_StreamParser SIGTRAP），
    锁也挡不住，所以解析走进程池——崩了只死一个子进程，重试即可。
    """
    return parse_pdf(data)


class Parser:
    def __init__(self, workers: int = 3):
        self.workers = max(1, workers)
        self._ex = None
        self._lk = threading.Lock()

    def _ensure(self):
        if self._ex is None:
            self._ex = cf.ProcessPoolExecutor(max_workers=self.workers)
        return self._ex

    def _drop(self):
        with self._lk:
            ex, self._ex = self._ex, None
        if ex is not None:
            try:
                ex.shutdown(wait=False, cancel_futures=True)
            except Exception:  # noqa: BLE001
                pass

    def parse(self, data: bytes):
        for _ in range(3):
            with self._lk:
                ex = self._ensure()
            try:
                return ex.submit(_parse_one, data).result(timeout=240)
            except BrokenProcessPool:
                log("解析子进程崩溃，重建进程池")
                self._drop()
            except Exception as e:  # noqa: BLE001
                return [], 0, f"fail: {e!r}"
        return [], 0, "fail: 进程池连续崩溃"


PARSER: "Parser | None" = None


# ---------------------------------------------------------------- 落库
def upsert_report(c: sqlite3.Connection, base: str, meta: dict, n_pages: int,
                  rows: list[dict], status: str, err: str) -> None:
    seqs = [r["seq"] for r in rows if r["seq"]]
    sum_w = round(sum(r["weight_pct"] or 0 for r in rows), 2)
    checks = {
        "n": len(rows),
        "sum_weight": sum_w,
        "seq_max": max(seqs) if seqs else None,
        "seq_gap": (max(seqs) - len(seqs)) if seqs else None,
        "exceeds_100": sum_w > 100.5,
        "no_related_col": all(r["related_party"] is None for r in rows) if rows else None,
    }
    c.execute(
        """INSERT OR REPLACE INTO fund_fof_reports
           (base_code,ann_id,title,report_type,end_date,pub_date,pdf_url,
            n_pages,n_rows,sum_weight,parse_status,err,checks_json,fetched_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            base, meta["ann_id"], meta["title"], meta["report_type"], meta["end_date"],
            meta["pub_date"], PDF_URL.format(meta["ann_id"]), n_pages, len(rows), sum_w,
            status, err, json.dumps(checks, ensure_ascii=False),
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )
    if rows:
        c.execute(
            "DELETE FROM fund_fof_holdings WHERE base_code=? AND end_date=? AND report_type=?",
            (base, meta["end_date"], meta["report_type"]),
        )
        c.executemany(
            """INSERT OR REPLACE INTO fund_fof_holdings
               (base_code,end_date,report_type,ann_id,seq,sub_code,sub_ts_code,sub_name,
                operate_mode,shares,mkv,weight_pct,related_party)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                (
                    base, meta["end_date"], meta["report_type"], meta["ann_id"], r["seq"],
                    r["sub_code"], r["sub_code"] + ".OF", r["sub_name"], r["operate_mode"],
                    r["shares"], r["mkv"], r["weight_pct"], r["related_party"],
                )
                for r in rows
            ],
        )


def existing(c: sqlite3.Connection) -> set[tuple[str, str]]:
    return {(a, b) for a, b in c.execute("SELECT base_code, ann_id FROM fund_fof_reports")}


def _suffix(code: str) -> str:
    """把 6 位代码补成 ts_code 后缀。ETF/LOF 在交易所，其余是场外。"""
    if code.startswith(("15", "16")):
        return ".SZ"
    if code.startswith(("50", "51", "52", "56", "58", "59")):
        return ".SH"
    return ".OF"


def build_share_map() -> None:
    """把每个份额代码映射到实际解析的代表份额。"""
    reps = {product_key(name): code for code, name in universe(dedupe=True)}
    arr = em_fund_list()
    rows = []
    for x in arr:
        code, name = x[0], x[2]
        if "FOF" not in (x[3] + name).upper():
            continue
        rep = reps.get(product_key(name))
        if not rep:
            continue
        rows.append((code, rep, name, 1 if code == rep else 0))
    con = _conn()
    con.execute("DELETE FROM fund_fof_share_map")
    con.executemany(
        "INSERT OR REPLACE INTO fund_fof_share_map (share_code,rep_code,name,is_rep) VALUES (?,?,?,?)",
        rows,
    )
    con.commit()
    con.close()
    log(f"份额映射：{len(rows)} 个份额 -> {len({r[1] for r in rows})} 个代表份额")


def fixmeta() -> None:
    """补全名称与 ts_code：文本兜底解析出的行没有名称，另外后缀统一按代码段判定。"""
    names = {x[0]: x[2] for x in em_fund_list()}
    con = _conn()
    con.execute("PRAGMA busy_timeout=60000")
    n_name = n_code = 0
    rows = con.execute(
        "SELECT base_code, end_date, sub_code, sub_name, sub_ts_code FROM fund_fof_holdings"
    ).fetchall()
    for base, end, code, name, ts in rows:
        new_name = name or names.get(code, "")
        new_ts = (code + _suffix(code)) if not ts or ts.endswith(".OF") else ts
        if new_name != name or new_ts != ts:
            con.execute(
                "UPDATE fund_fof_holdings SET sub_name=?, sub_ts_code=? "
                "WHERE base_code=? AND end_date=? AND sub_code=?",
                (new_name, new_ts, base, end, code),
            )
            n_name += (new_name != name)
            n_code += (new_ts != ts)
    con.commit()
    con.close()
    log(f"fixmeta：补名称 {n_name} 行、改 ts_code {n_code} 行")
    build_share_map()


def handle(c: sqlite3.Connection, base: str, meta: dict, keep_pdf: bool) -> str:
    try:
        data = _download(PDF_URL.format(meta["ann_id"]))
        if len(data) < 20_000:
            raise RuntimeError(f"PDF 过小 size={len(data)}")
        rows, n_pages, status = PARSER.parse(data) if PARSER else parse_pdf(data)
        if keep_pdf:
            PDFDIR.mkdir(parents=True, exist_ok=True)
            (PDFDIR / f"{base}_{meta['end_date']}_{meta['report_type']}.pdf").write_bytes(data)
        c2 = tconn()
        with _lock:
            upsert_report(c2, base, meta, n_pages, rows, status, "")
            c2.commit()
        return f"{base} {meta['end_date']} {meta['report_type']}: {status} n={len(rows)}"
    except Exception as e:  # noqa: BLE001
        c2 = tconn()
        with _lock:
            upsert_report(c2, base, meta, 0, [], "fail", repr(e)[:400])
            c2.commit()
        return f"{base} {meta['end_date']} {meta['report_type']}: FAIL {e!r}"


# ---------------------------------------------------------------- 状态
def show_status() -> None:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        print("—— 报告解析状态 ——")
        for row in con.execute(
            """SELECT report_type, parse_status, COUNT(*), ROUND(AVG(n_rows),1),
                      ROUND(AVG(sum_weight),2), ROUND(MIN(sum_weight),2), ROUND(MAX(sum_weight),2)
               FROM fund_fof_reports GROUP BY 1,2 ORDER BY 1,2"""
        ):
            print("  {:<3} {:<6} n={:<5} rows_avg={:<6} sumW_avg={:<7} min={:<7} max={}".format(*row))
        n_rep, n_fund, n_hold = [
            con.execute(q).fetchone()[0]
            for q in (
                "SELECT COUNT(*) FROM fund_fof_reports",
                "SELECT COUNT(DISTINCT base_code) FROM fund_fof_reports",
                "SELECT COUNT(*) FROM fund_fof_holdings",
            )
        ]
        print(f"  报告 {n_rep} 份 / FOF {n_fund} 只 / 明细 {n_hold} 行")
        print("—— 各报告期覆盖 ——")
        for row in con.execute(
            """SELECT end_date, COUNT(DISTINCT base_code), SUM(n_rows)
               FROM fund_fof_reports WHERE parse_status='ok'
               GROUP BY 1 ORDER BY 1 DESC LIMIT 12"""
        ):
            print("  {} FOF={:<4} 明细={}".format(*row))
    finally:
        con.close()


# ---------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", default="2023-2026", help="起始-结束年，如 2023-2026；单年写 2026")
    ap.add_argument("--kinds", default="H1,FY,Q1,Q2,Q3,Q4", help="只跑这些报告类型，如 H1,FY")
    ap.add_argument("--universe", default="em", choices=("em", "master"),
                    help="em=东财全量名单(1439 份额)；master=主库 FOF 子集(595 份额)")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 只 FOF（调试）")
    ap.add_argument("--only", default="", help="只跑指定代码，逗号分隔")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--no-dedupe", action="store_true", help="不按产品去重份额类别")
    ap.add_argument("--keep-pdf", action="store_true")
    ap.add_argument("--reparse", action="store_true", help="忽略已完成记录，重下重解析")
    ap.add_argument("--retry-fail", action="store_true", help="把此前 fail 的记录清掉重试")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--fixmeta", action="store_true", help="补全名称与 ts_code 后退出")
    args = ap.parse_args()
    if args.fixmeta:
        init_db()
        fixmeta()
        show_status()
        return

    if args.status:
        show_status()
        return

    init_db()
    if args.retry_fail:
        con = _conn()
        n = con.execute(
            "DELETE FROM fund_fof_reports WHERE parse_status IN ('fail','empty')").rowcount
        con.commit()
        con.close()
        log(f"清掉 {n} 条失败记录，准备重试")
    if "-" in args.years:
        y0, y1 = (int(x) for x in args.years.split("-"))
    else:
        y0 = y1 = int(args.years)
    funds = universe(dedupe=not args.no_dedupe, source=args.universe)
    kinds = {k.strip().upper() for k in args.kinds.split(",") if k.strip()}
    if args.only:
        want = {x.strip() for x in args.only.split(",") if x.strip()}
        funds = [f for f in funds if f[0] in want]
    if args.limit:
        funds = funds[: args.limit]
    log(f"启动 FOF 管道：{len(funds)} 只代表份额（universe={args.universe}），"
        f"报告期 {y0}-{y1}，类型 {sorted(kinds)}，workers={args.workers}")

    c = _conn()
    done = set() if args.reparse else existing(c)
    c.close()

    jobs: list[tuple[str, dict]] = []
    for i, (code, name) in enumerate(funds, 1):
        reps = list_reports(code)
        for r in reps:
            y = int(r["end_date"][:4])
            if y0 <= y <= y1 and r["report_type"] in kinds and (code, r["ann_id"]) not in done:
                jobs.append((code, r))
        if i % 50 == 0:
            log(f"清单 {i}/{len(funds)}，待处理报告 {len(jobs)}")
        time.sleep(0.05)

    log(f"待处理报告 {len(jobs)} 份")
    if not jobs:
        show_status()
        return

    t0 = time.time()
    ok = fail = 0
    global PARSER
    PARSER = Parser(args.workers)
    with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(handle, None, code, meta, args.keep_pdf): (code, meta) for code, meta in jobs}
        for n, fut in enumerate(cf.as_completed(futs), 1):
            msg = fut.result()
            if "FAIL" in msg or "empty" in msg:
                fail += 1
            else:
                ok += 1
            if n % 25 == 0 or n == len(futs):
                el = time.time() - t0
                log(f"进度 {n}/{len(futs)} ok={ok} bad={fail} 用时 {el/60:.1f}min 预计剩 {el/n*(len(futs)-n)/60:.0f}min")
    log(f"完成：ok={ok} bad={fail}，用时 {(time.time()-t0)/60:.1f}min")
    show_status()


if __name__ == "__main__":
    main()
