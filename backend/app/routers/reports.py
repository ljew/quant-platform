"""市场资讯报告 API（只读）。

表 `market_reports` 由 `backend/scripts/ingest_market_report.py` 创建与写入：
报告由 WorkBuddy 自动化生成到 market_brief/，落盘后入库。这里只读，
避免与生成端、容器内调度器形成 SQLite 双写。

为什么不用已有的 research_cards / doc_document：
- research_cards 是假设卡（hypothesis / evidence / state），字段语义完全不同；
- doc_document 是外部抓取语料（6.3 万行公告/新闻，带 PIT 与转载归并），
  把自产报告混进去会污染语料，且它没有对外查询接口。
报告是独立的「研究产出资产」，故独立建表 + 独立只读接口。
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse, PlainTextResponse
from sqlalchemy import text

from app.database import SessionLocal

router = APIRouter(prefix="/reports", tags=["reports"])

_COLS = (
    "report_id,report_date,slot,title,window_start,window_end,data_asof,"
    "trade_status,template_version,generated_at,summary,md_path,html_path,"
    "news_count,conflict_count,missing_count,tags,ingested_at,updated_at"
)
_ORDER = (
    "report_date DESC, CASE slot WHEN '晚间版' THEN 4 WHEN '收盘版' THEN 3 "
    "WHEN '午间版' THEN 2 ELSE 1 END DESC"
)


def _rows(sql: str, params: Optional[dict] = None) -> list[dict]:
    db = SessionLocal()
    try:
        return [dict(r) for r in db.execute(text(sql), params or {}).mappings().all()]
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=503,
            detail=f"报告库不可用（请先运行 ingest_market_report.py）：{exc}",
        ) from exc
    finally:
        db.close()


@router.get("")
def list_reports(
    date_from: Optional[str] = Query(None, description="起始日期 YYYY-MM-DD"),
    date_to: Optional[str] = Query(None, description="结束日期 YYYY-MM-DD"),
    slot: Optional[str] = Query(None, description="早间版 / 午间版 / 收盘版 / 晚间版"),
    q: Optional[str] = Query(None, description="标题/摘要/全文/标签模糊搜索"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """列出全部报告（不含全文），支持日期、时段、关键词过滤与分页。"""
    where, params = ["1=1"], {}
    if date_from:
        where.append("report_date >= :date_from")
        params["date_from"] = date_from
    if date_to:
        where.append("report_date <= :date_to")
        params["date_to"] = date_to
    if slot:
        where.append("slot = :slot")
        params["slot"] = slot
    if q:
        where.append("(title LIKE :q OR summary LIKE :q OR content_md LIKE :q OR tags LIKE :q)")
        params["q"] = f"%{q}%"
    clause = " AND ".join(where)
    total = _rows(f"SELECT COUNT(*) AS c FROM market_reports WHERE {clause}", params)[0]["c"]
    rows = _rows(
        f"SELECT {_COLS} FROM market_reports WHERE {clause} "
        f"ORDER BY {_ORDER} LIMIT :limit OFFSET :offset",
        {**params, "limit": limit, "offset": offset},
    )
    return {"total": total, "limit": limit, "offset": offset, "reports": rows}


@router.get("/stats")
def stats():
    """总量、时段分布、按日统计、最新一份（供平台首页/健康检查使用）。"""
    total = _rows("SELECT COUNT(*) AS c FROM market_reports")[0]["c"]
    by_slot = _rows("SELECT slot, COUNT(*) AS n FROM market_reports GROUP BY slot ORDER BY n DESC")
    by_date = _rows(
        "SELECT report_date, COUNT(*) AS n, SUM(news_count) AS news, "
        "SUM(conflict_count) AS conflicts, SUM(missing_count) AS missing "
        "FROM market_reports GROUP BY report_date ORDER BY report_date DESC LIMIT 60"
    )
    latest = _rows(f"SELECT {_COLS} FROM market_reports ORDER BY {_ORDER} LIMIT 1")
    return {
        "total": total,
        "by_slot": by_slot,
        "by_date": by_date,
        "latest": latest[0] if latest else None,
        "index_url": "/api/v1/reports/index",
    }


@router.get("/search")
def search(q: str = Query(..., min_length=1, description="关键词"), limit: int = Query(20, ge=1, le=200)):
    """全文搜索（列表接口的语义化别名）。

    必须显式把可选参数传 None：直接调用 list_reports 时，未传的参数会保留
    FastAPI 的 Query(...) 默认值对象，被当成 SQL 绑定值而报错。
    """
    return list_reports(date_from=None, date_to=None, slot=None,
                        q=q, limit=limit, offset=0)


_INDEX_HEAD = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>市场资讯报告 · 全部报告</title><style>
:root{--bg:#f5f6f8;--card:#fff;--ink:#1b1f27;--muted:#6b7280;--line:#e5e7eb;--accent:#1d4ed8}
*{box-sizing:border-box}body{margin:0;padding:18px 16px 40px;background:var(--bg);color:var(--ink);
font:13px/1.5 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}
.wrap{max-width:1180px;margin:0 auto}h1{font-size:19px;margin:0 0 4px}
.sub{color:var(--muted);font-size:12px;margin-bottom:12px}
.bar{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px}
input,select{font:inherit;padding:5px 9px;border:1px solid var(--line);border-radius:6px;background:#fff;color:var(--ink)}
section{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 14px}
table{width:100%;border-collapse:collapse;font-size:12px}
th,td{padding:5px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{background:#f9fafb;color:#4b5563;font-size:11px;white-space:nowrap}
td.n{text-align:right;font-variant-numeric:tabular-nums}td.sum{color:#374151;max-width:520px}
td.lk{white-space:nowrap}a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
.b{background:#eef2ff;color:#3730a3;border-radius:4px;padding:1px 5px;font-size:10px}
footer{color:var(--muted);font-size:11px;text-align:center;padding:12px}
</style></head><body><div class="wrap">
<h1>市场资讯报告 · 全部报告</h1>
<div class="sub">数据源 market_reports（quant_dev.db）· 由 ingest_market_report.py 入库 · <b>__N__</b> 份</div>
<div class="bar"><input id="q" placeholder="搜索标题/摘要/日期…" size="34">
<select id="slot"><option value="">全部时段</option><option>早间版</option><option>午间版</option><option>收盘版</option><option>晚间版</option></select>
<span id="cnt" style="color:var(--muted);font-size:12px;align-self:center"></span></div>
<section><table><thead><tr><th>日期</th><th>时段</th><th>状态</th><th>资讯</th><th>冲突</th><th>待补</th><th>速览摘要</th><th>查看</th></tr></thead>
<tbody id="tb">"""
_INDEX_TAIL = """</tbody></table></section>
<footer>API：GET /api/v1/reports（列表/搜索）· /api/v1/reports/{id}（详情）· /{id}/html · /{id}/markdown · /stats</footer>
</div><script>
const q=document.getElementById('q'),s=document.getElementById('slot'),tb=document.getElementById('tb'),cnt=document.getElementById('cnt');
function f(){const kw=q.value.trim().toLowerCase(),sl=s.value;let n=0;
for(const tr of tb.rows){const ok=(!kw||tr.dataset.text.includes(kw)||tr.cells[0].textContent.includes(kw))&&(!sl||tr.dataset.slot===sl);
tr.style.display=ok?'':'none';if(ok)n++}cnt.textContent='显示 '+n+' / __N__ 份'}
q.addEventListener('input',f);s.addEventListener('change',f);f();
</script></body></html>"""


@router.get("/index")
def index_page():
    """可直接用浏览器打开的全部报告索引（从库动态生成，不依赖前端构建产物）。"""
    from html import escape as esc

    rows = _rows(f"SELECT {_COLS} FROM market_reports ORDER BY {_ORDER}")
    trs = []
    for r in rows:
        rid = esc(str(r["report_id"]))
        status = "休市" if "休市" in (r["trade_status"] or "") else "交易日"
        links = []
        if r["html_path"]:
            links.append(f'<a href="/api/v1/reports/{rid}/html">HTML</a>')
        links += [f'<a href="/api/v1/reports/{rid}/markdown">MD</a>',
                  f'<a href="/api/v1/reports/{rid}">JSON</a>']
        text = esc((str(r["title"]) + " " + str(r["summary"] or "")).lower())
        trs.append(
            f'<tr data-slot="{esc(str(r["slot"]))}" data-text="{text}">'
            f'<td>{esc(str(r["report_date"]))}</td><td>{esc(str(r["slot"]))}</td>'
            f'<td><span class="b">{status}</span></td>'
            f'<td class="n">{r["news_count"]}</td><td class="n">{r["conflict_count"]}</td>'
            f'<td class="n">{r["missing_count"]}</td>'
            f'<td class="sum">{esc(str(r["summary"] or "")[:160])}</td>'
            f'<td class="lk">{" · ".join(links)}</td></tr>'
        )
    page = (_INDEX_HEAD + "".join(trs) + _INDEX_TAIL).replace("__N__", str(len(rows)))
    return HTMLResponse(page)


@router.get("/{report_id}")
def get_report(report_id: str):
    """单份报告详情，含 Markdown 全文。"""
    rows = _rows(
        f"SELECT {_COLS}, content_md, content_hash FROM market_reports WHERE report_id=:rid",
        {"rid": report_id},
    )
    if not rows:
        raise HTTPException(status_code=404, detail=f"报告不存在：{report_id}")
    return rows[0]


@router.get("/{report_id}/html")
def report_html(report_id: str):
    """直接渲染报告 HTML（全文已入库，容器内不依赖宿主机路径）。"""
    rows = _rows("SELECT content_html FROM market_reports WHERE report_id=:rid", {"rid": report_id})
    if not rows:
        raise HTTPException(status_code=404, detail=f"报告不存在：{report_id}")
    if not rows[0]["content_html"]:
        raise HTTPException(status_code=404, detail=f"该报告无 HTML 版本：{report_id}")
    return HTMLResponse(rows[0]["content_html"])


@router.get("/{report_id}/markdown")
def report_markdown(report_id: str):
    """返回原始 Markdown（供其它程序消费）。"""
    rows = _rows("SELECT content_md FROM market_reports WHERE report_id=:rid", {"rid": report_id})
    if not rows:
        raise HTTPException(status_code=404, detail=f"报告不存在：{report_id}")
    return PlainTextResponse(rows[0]["content_md"], media_type="text/markdown; charset=utf-8")
