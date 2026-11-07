#!/usr/bin/env python3
"""市场资讯报告入库器（WorkBuddy -> quant-platform）。

报告由 WorkBuddy 自动化生成到 **docs/market_brief/**（仓库内，随 git 版本化），
本脚本把它落进平台 SQLite，并在 data/reports/ 留一份文件备份与本地索引；
对外索引页由 API 动态生成。

    python ingest_market_report.py --file docs/market_brief/市场资讯报告_20261107_早间版.md
    python ingest_market_report.py --dir  docs/market_brief        # 批量补录（默认即此目录）
    python ingest_market_report.py --dir  docs/market_brief --force  # 正文未变也重算字段
    python ingest_market_report.py --list
    python ingest_market_report.py --rebuild-index

设计要点：
- **幂等**：report_id = YYYYMMDD_时段；content_hash 未变则跳过，不产生重复行。
- **自动归档**：入库成功后把 docs/market_brief/ 的变更 commit + push 到 dev，
  无需人工提交。只在 dev 分支执行；commit 用 pathspec 限定路径，
  不会带走工作区里别人的在制品；`--no-git` 可关闭。
- **自包含**：MD 与 HTML 全文都进库，容器内 API 不依赖宿主机文件路径。
- **单写者**：用 WAL + busy_timeout 排队，不碰 SQLAlchemy 模型，避免与容器内
  调度器抢锁；表由本脚本创建，后端 router 只读。
- **文件备份**：data/reports/（稳定 bind mount）。web/dist/ 会被前端构建整目录
  清空且 inode 变化后容器看不到，不放报告；对外索引 = GET /api/v1/reports/index。
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]          # .../quant-platform
DEFAULT_SRC_DIR = ROOT / "docs" / "market_brief"   # 报告与模板都在仓库内，随 git 版本化
SLOTS = ("早间版", "午间版", "收盘版", "晚间版")
SLOT_ORDER = {"早间版": 1, "午间版": 2, "收盘版": 3, "晚间版": 4}

DDL = """
CREATE TABLE IF NOT EXISTS market_reports (
    report_id         TEXT PRIMARY KEY,
    report_date       TEXT NOT NULL,
    slot              TEXT NOT NULL,
    title             TEXT NOT NULL,
    window_start      TEXT,
    window_end        TEXT,
    data_asof         TEXT,
    trade_status      TEXT,
    template_version  TEXT,
    generated_at      TEXT,
    summary           TEXT,
    content_md        TEXT NOT NULL,
    content_html      TEXT,
    content_hash      TEXT NOT NULL,
    md_path           TEXT,
    html_path         TEXT,
    news_count        INTEGER DEFAULT 0,
    conflict_count    INTEGER DEFAULT 0,
    missing_count     INTEGER DEFAULT 0,
    tags              TEXT DEFAULT '',
    ingested_at       TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_mr_date  ON market_reports(report_date DESC);
CREATE INDEX IF NOT EXISTS ix_mr_slot  ON market_reports(slot);
CREATE INDEX IF NOT EXISTS ix_mr_hash  ON market_reports(content_hash);
"""


def resolve_db() -> Path:
    """宿主机跑用 ROOT/data；容器里跑用 /data（但静态目录只读，会跳过同步）。"""
    env = os.environ.get("QUANT_REPORT_DB")
    if env and Path(env).exists():
        return Path(env)
    for p in (Path("/data/quant_dev.db"), ROOT / "data" / "quant_dev.db"):
        if p.exists():
            return p
    return ROOT / "data" / "quant_dev.db"


def resolve_static() -> Path:
    """报告文件备份目录。

    不能放 web/dist/：前端 vite build 会整目录清空，且 bind mount 的 inode
    变化后容器看不到新文件（2026-11-07 实测 /reports/ 变 404）。data/ 是稳定的
    bind mount，不受构建影响；对外索引页由 API /api/v1/reports/index 动态生成。
    """
    return ROOT / "data" / "reports"


def clean_md(s: str) -> str:
    s = re.sub(r"`([^`]*)`", r"\1", s)
    s = re.sub(r"\*\*([^*]*)\*\*", r"\1", s)
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)
    return s.strip()


def parse_report(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    m = re.search(r"市场资讯报告_(\d{8})_(" + "|".join(SLOTS) + r")\.md$", path.name)
    if not m:
        raise ValueError(f"文件名不符合规范（须为 市场资讯报告_YYYYMMDD_时段.md）：{path.name}")
    ymd, slot = m.group(1), m.group(2)
    date_iso = f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}"
    report_id = f"{ymd}_{slot}"

    def field(label: str) -> str:
        mm = re.search(r"^\|\s*" + re.escape(label) + r"\s*\|\s*(.+?)\s*\|", text, re.M)
        return clean_md(mm.group(1)) if mm else ""

    window = field("覆盖窗口")
    dates = re.findall(r"\d{4}-\d{2}-\d{2}", window)
    status = field("交易日状态")
    generated = field("生成时点")
    gm = re.search(r"\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}", generated)
    # 头部字段常带长说明，入库只留可检索的短值
    status_brief = "休市" if "休市" in status else "交易日"
    rm = re.search(r"[（(]([^）)]*)[）)]", status)
    if rm:
        status_brief += f"（{rm.group(1)}）"
    vm = re.search(r"v\d+\.\d+", field("模板版本"))
    version_brief = vm.group(0) if vm else field("模板版本")[:32]

    # 第 1 节速览 -> summary（去掉项目符号与溯源标签，保留事实）
    sec1 = re.search(r"^## 1\..*?$(.*?)(?=^## 2\.)", text, re.M | re.S)
    bullets = []
    if sec1:
        for line in sec1.group(1).splitlines():
            if line.strip().startswith("- "):
                b = clean_md(re.sub(r"^-\s*", "", line.strip()))
                b = re.sub(r"\[(?:通达信|tushare|neodata|腾讯|中债|公开媒体)[^\]]*\]", "", b)
                bullets.append(b.strip())
    summary = " | ".join(bullets)[:1500]

    # 第 8 节资讯条数（只数主表 | 1 | 形式的行）
    sec8 = re.search(r"^## 8\..*?$(.*?)(?=^## 9\.)", text, re.M | re.S)
    news_count = len(re.findall(r"^\|\s*\d+\s*\|", sec8.group(1), re.M)) if sec8 else 0

    # 兼容「冲突字段：6」与「冲突字段数：6」两种写法（模板第 6 节用的是后者）
    cm = re.search(r"冲突字段(?:数)?[：:]\s*(\d+)", text)
    conflict_count = int(cm.group(1)) if cm else text.count("[源冲突⚠]")
    mm = re.search(r"计数[：:]\s*(\d+)\s*项", text)
    missing_count = int(mm.group(1)) if mm else 0

    tags = ["市场资讯", slot, "休市" if "休市" in status else "交易日"]
    for kw, tag in (("股票", "股票"), ("债券", "债券"), ("利率", "利率"),
                    ("黄金", "黄金"), ("汇率", "汇率"), ("原油", "大宗"),
                    ("海外", "海外"), ("A 股", "A股"), ("源冲突", "源冲突")):
        if kw in text and tag not in tags:
            tags.append(tag)

    html_path = path.with_suffix(".html")
    content_html = html_path.read_text(encoding="utf-8") if html_path.exists() else ""
    title = f"市场资讯报告 · {slot} · {date_iso}"

    return dict(
        report_id=report_id, report_date=date_iso, slot=slot, title=title,
        window_start=dates[0] if dates else date_iso,
        window_end=dates[-1] if dates else date_iso,
        data_asof=field("数据时点"), trade_status=status_brief,
        template_version=version_brief,
        generated_at=gm.group(0) if gm else generated,
        summary=summary, content_md=text, content_html=content_html,
        content_hash=hashlib.sha1(text.encode("utf-8")).hexdigest(),
        md_path=str(path), html_path=str(html_path) if content_html else "",
        news_count=news_count, conflict_count=conflict_count,
        missing_count=missing_count, tags=",".join(tags),
    )


def upsert(conn: sqlite3.Connection, rec: dict, force: bool = False) -> str:
    old = conn.execute("SELECT content_hash FROM market_reports WHERE report_id=?",
                       (rec["report_id"],)).fetchone()
    now = datetime.now().isoformat(timespec="seconds")
    # force=True 时即使正文未变也重算字段（解析逻辑改进后需要回填）
    if old and old[0] == rec["content_hash"] and not force:
        return "skipped"
    rec = dict(rec)
    rec["ingested_at"] = now
    rec["updated_at"] = now
    cols = list(rec)
    sql = (
        f"INSERT INTO market_reports ({','.join(cols)}) VALUES ({','.join(':' + c for c in cols)}) "
        f"ON CONFLICT(report_id) DO UPDATE SET " +
        ",".join(f"{c}=excluded.{c}" for c in cols if c != "report_id")
    )
    conn.execute(sql, rec)
    return "updated" if old else "inserted"


def build_index(conn: sqlite3.Connection, static_dir: Path | None) -> str:
    if static_dir is None:
        return ""
    static_dir.mkdir(parents=True, exist_ok=True)
    rows = conn.execute(
        "SELECT report_id,report_date,slot,title,summary,trade_status,"
        "news_count,conflict_count,missing_count,md_path,html_path "
        "FROM market_reports ORDER BY report_date DESC, slot"
    ).fetchall()
    rows = sorted(rows, key=lambda r: (r[1], SLOT_ORDER.get(r[2], 0)), reverse=True)

    # 同步静态副本（库里的路径可能是宿主机路径；容器内只读时由外侧跳过）
    for r in rows:
        for p in (r[9], r[10]):
            if p and Path(p).exists() and Path(p).parent != static_dir:
                try:
                    shutil.copy2(p, static_dir / Path(p).name)
                except OSError:
                    pass

    def esc(s):
        return html.escape(str(s or ""))

    trs = []
    for r in rows:
        rid, date_, slot, title, summary, status, news, conf, miss, _, hp = r
        has_html = bool(hp)
        links = []
        if has_html:
            links.append(f'<a href="/api/v1/reports/{esc(rid)}/html">HTML</a>')
        links.append(f'<a href="/api/v1/reports/{esc(rid)}/markdown">MD</a>')
        links.append(f'<a href="/api/v1/reports/{esc(rid)}">JSON</a>')
        badge = "休市" if "休市" in (status or "") else "交易日"
        trs.append(
            f'<tr data-slot="{esc(slot)}" data-text="{esc((title + " " + summary).lower())}">'
            f'<td>{esc(date_)}</td><td>{esc(slot)}</td><td><span class="b">{badge}</span></td>'
            f'<td class="n">{news}</td><td class="n">{conf}</td><td class="n">{miss}</td>'
            f'<td class="sum">{esc(summary[:160])}</td>'
            f'<td class="lk">{" · ".join(links)}</td></tr>'
        )

    page = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>市场资讯报告 · 全部报告</title>
<style>
:root{{--bg:#f5f6f8;--card:#fff;--ink:#1b1f27;--muted:#6b7280;--line:#e5e7eb;--accent:#1d4ed8}}
*{{box-sizing:border-box}}body{{margin:0;padding:18px 16px 40px;background:var(--bg);color:var(--ink);
font:13px/1.5 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}}
.wrap{{max-width:1180px;margin:0 auto}}h1{{font-size:19px;margin:0 0 4px}}
.sub{{color:var(--muted);font-size:12px;margin-bottom:12px}}
.bar{{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px}}
input,select{{font:inherit;padding:5px 9px;border:1px solid var(--line);border-radius:6px;background:#fff;color:var(--ink)}}
section{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 14px}}
table{{width:100%;border-collapse:collapse;font-size:12px}}
th,td{{padding:5px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}}
th{{background:#f9fafb;color:#4b5563;font-size:11px;white-space:nowrap}}
td.n{{text-align:right;font-variant-numeric:tabular-nums}}
td.sum{{color:#374151;max-width:520px}}
td.lk{{white-space:nowrap}}a{{color:var(--accent);text-decoration:none}}a:hover{{text-decoration:underline}}
.b{{background:#eef2ff;color:#3730a3;border-radius:4px;padding:1px 5px;font-size:10px}}
footer{{color:var(--muted);font-size:11px;text-align:center;padding:12px}}
</style></head><body><div class="wrap">
<h1>市场资讯报告 · 全部报告</h1>
<div class="sub">共 <b>{len(rows)}</b> 份 · 数据源 market_reports（quant_dev.db）· 由 ingest_market_report.py 自动同步 · 更新时间 {esc(datetime.now().strftime('%Y-%m-%d %H:%M'))}</div>
<div class="bar"><input id="q" placeholder="搜索标题/摘要/日期…" size="34">
<select id="slot"><option value="">全部时段</option>{''.join(f'<option>{s}</option>' for s in SLOTS)}</select>
<span id="cnt" style="color:var(--muted);font-size:12px;align-self:center"></span></div>
<section><table><thead><tr><th>日期</th><th>时段</th><th>状态</th><th>资讯</th><th>冲突</th><th>待补</th><th>速览摘要</th><th>查看</th></tr></thead>
<tbody id="tb">{''.join(trs)}</tbody></table></section>
<footer>API：GET /api/v1/reports（列表/搜索）· /reports/&#123;id&#125;（详情）· /reports/&#123;id&#125;/html · /reports/stats</footer>
</div><script>
const q=document.getElementById('q'),s=document.getElementById('slot'),tb=document.getElementById('tb'),cnt=document.getElementById('cnt');
function f(){{const kw=q.value.trim().toLowerCase(),sl=s.value;let n=0;
for(const tr of tb.rows){{const ok=(!kw||tr.dataset.text.includes(kw)||tr.cells[0].textContent.includes(kw))&&(!sl||tr.dataset.slot===sl);
tr.style.display=ok?'':'none';if(ok)n++}}cnt.textContent='显示 '+n+' / {len(rows)} 份'}}
q.addEventListener('input',f);s.addEventListener('change',f);f();
</script></body></html>"""
    # data/reports/ 里还有 auto_review / strategy_verify 等平台自有报告，
    # 不用泛化的 index.html，避免被误认为覆盖全部报告。
    out = static_dir / "market_reports_index.html"
    out.write_text(page, encoding="utf-8")
    return str(out)


def git_sync_reports(repo: Path, rel_paths: list[str], message: str) -> dict:
    """把报告目录的变更提交并推送到 dev（无人值守归档用）。

    任何一步失败都只记录、不抛异常 —— 归档失败不能影响入库本身。

    安全约束（都踩过或评估过）：
    - **只在 dev 分支执行**：其他分支跳过，避免自动化直接往 main 写提交。
    - **pathspec 限定**：先 `git add -- <路径>`，再 `git commit -m .. -- <路径>`。
      用 pathspec 而不是直接 commit，是为了不把暂存区里别人的改动一起带走
      （老刘的工作区常有别的在制品）。
    - **无变更不产生空提交**：先看 `status --porcelain`。
    - **push 失败不回滚**：本地提交保留，下次运行会把它一起推上去。
    """
    res: dict = {"attempted": False, "committed": False, "pushed": False,
                 "branch": "", "reason": ""}

    def run(*a: str, timeout: int = 120) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(repo), *a],
                              capture_output=True, text=True, timeout=timeout)

    if not (repo / ".git").exists():
        res["reason"] = "非 git 仓库"
        return res

    try:
        br = run("rev-parse", "--abbrev-ref", "HEAD")
        res["branch"] = br.stdout.strip()
        if br.returncode != 0:
            res["reason"] = f"读取分支失败: {(br.stderr or '').strip()[:80]}"
            return res
        if res["branch"] != "dev":
            res["reason"] = f"当前分支 {res['branch']}，仅在 dev 自动归档"
            return res

        st = run("status", "--porcelain", "--", *rel_paths)
        if st.returncode != 0:
            res["reason"] = f"status 失败: {(st.stderr or '').strip()[:80]}"
            return res
        if not st.stdout.strip():
            res["reason"] = "无变更"
            return res

        res["attempted"] = True
        add = run("add", "--", *rel_paths)
        if add.returncode != 0:
            res["reason"] = f"add 失败: {(add.stderr or '').strip()[:80]}"
            return res

        cm = run("commit", "-m", message, "--", *rel_paths)
        if cm.returncode != 0:
            res["reason"] = f"commit 失败: {(cm.stderr or cm.stdout).strip()[:100]}"
            return res
        res["committed"] = True

        ps = run("push", "origin", "dev", timeout=180)
        if ps.returncode == 0:
            res["pushed"] = True
        else:
            res["reason"] = f"已提交未推送: {(ps.stderr or '').strip()[:100]}"
    except Exception as e:  # noqa: BLE001
        res["reason"] = f"{type(e).__name__}: {e}"[:120]
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description="市场资讯报告入库")
    ap.add_argument("--file", type=Path, help="单份 Markdown 报告")
    ap.add_argument("--dir", type=Path, help="批量扫描目录（默认 market_brief/）")
    ap.add_argument("--list", action="store_true", help="列出库中报告")
    ap.add_argument("--rebuild-index", action="store_true", help="只重建静态索引")
    ap.add_argument("--force", action="store_true",
                    help="正文未变也重算字段（解析逻辑改进后回填用）")
    ap.add_argument("--no-git", action="store_true",
                    help="只入库，不自动提交归档（默认入库后会把 docs/market_brief "
                         "的变更 commit + push 到 dev）")
    args = ap.parse_args()

    db = resolve_db()
    conn = sqlite3.connect(str(db), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(DDL)
    conn.commit()

    if args.list:
        rows = conn.execute("SELECT report_id,report_date,slot,news_count,conflict_count,"
                            "missing_count,updated_at FROM market_reports "
                            "ORDER BY report_date DESC, slot").fetchall()
        print(json.dumps({"total": len(rows), "db": str(db),
                          "reports": [dict(r) for r in rows]}, ensure_ascii=False, indent=2))
        return 0

    pat = re.compile(r"市场资讯报告_\d{8}_(?:早间版|午间版|收盘版|晚间版)\.md$")

    def collect(d: Path) -> list[Path]:
        if not d.is_dir():
            return []
        return [p for p in sorted(d.glob("市场资讯报告_*.md")) if pat.search(p.name)]

    files: list[Path] = []
    if args.file:
        files = [args.file]
    elif args.dir or args.rebuild_index:
        files = collect(args.dir or DEFAULT_SRC_DIR)
    else:
        files = collect(DEFAULT_SRC_DIR)

    stat = {"inserted": 0, "updated": 0, "skipped": 0, "failed": 0}
    touched = []
    for f in files:
        try:
            rec = parse_report(f)
            r = upsert(conn, rec, force=args.force)
            stat[r] += 1
            if r != "skipped":
                touched.append(rec["report_id"])
        except Exception as e:  # noqa: BLE001
            stat["failed"] += 1
            print(f"[WARN] {f.name}: {e}", file=sys.stderr)
    conn.commit()

    static_dir = resolve_static()
    idx = build_index(conn, static_dir)
    total = conn.execute("SELECT COUNT(*) FROM market_reports").fetchone()[0]
    conn.close()

    # —— 自动归档：入库成功后把 docs/market_brief 的变更 commit + push 到 dev ——
    # 只在 dev 分支执行，pathspec 限定路径，失败不影响入库结果（见 git_sync_reports）
    if args.no_git:
        git_res = {"attempted": False, "committed": False, "pushed": False,
                   "reason": "已用 --no-git 跳过"}
    else:
        if touched:
            ids = "、".join(touched[:3]) + (f" 等 {len(touched)} 份" if len(touched) > 3 else "")
            msg = f"chore(reports): 自动归档 {ids}"
        else:
            msg = f"chore(reports): 自动归档报告目录 {datetime.now():%Y-%m-%d %H:%M}"
        git_res = git_sync_reports(ROOT, ["docs/market_brief"], msg)

    out = {"ok": stat["failed"] == 0, "db": str(db), "scanned": len(files),
           **stat, "total_in_db": total, "report_ids": touched,
           "index_path": idx,
           "index_url": "http://127.0.0.1:8000/api/v1/reports/index",
           "api": "http://127.0.0.1:8000/api/v1/reports",
           "git": git_res}
    print(json.dumps(out, ensure_ascii=False))
    return 0 if stat["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
