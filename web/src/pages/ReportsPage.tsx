/**
 * 市场资讯报告（今日观点 · 报告 Tab）
 *
 * 用途：WorkBuddy 四时段自动化生成的跨市场资讯简报，落盘后由
 * backend/scripts/ingest_market_report.py 幂等入库（quant_dev.db → market_reports），
 * 这里只读展示：按日期/时段/关键词检索 → 点开直接读全文（HTML 已入库，不依赖宿主机文件）。
 *
 * 数据源：GET /reports（列表/过滤）、/reports/stats（统计）、/reports/{id}/html（渲染）。
 * 不产出判断，报告本身即「事实层 + 观察点」，无裁决区。
 */
import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { Badge, Btn, Card, KpiCard, PageHeader, inputStyle } from "../components/ui";
import { useTheme } from "../theme";
import { get } from "../api/client";

interface ReportRow {
  report_id: string;
  report_date: string;
  slot: string;
  title: string;
  window_start: string | null;
  window_end: string | null;
  data_asof: string | null;
  trade_status: string | null;
  template_version: string | null;
  generated_at: string | null;
  summary: string | null;
  md_path: string | null;
  html_path: string | null;
  news_count: number | null;
  conflict_count: number | null;
  missing_count: number | null;
  tags: string | null;
  ingested_at: string | null;
  updated_at: string | null;
}

interface Stats {
  total: number;
  by_slot: { slot: string; n: number }[];
  by_date: { report_date: string; n: number; news: number | null; conflicts: number | null; missing: number | null }[];
  latest: ReportRow | null;
  index_url: string;
}

const SLOTS = ["", "早间版", "午间版", "收盘版", "晚间版"];
const SLOT_COLOR: Record<string, string> = {
  早间版: "#2456c8",
  午间版: "#b9770e",
  收盘版: "#1a8a48",
  晚间版: "#6d28d9",
};

/** 库里的时间戳是 UTC（脚本用 datetime.utcnow），展示时转本地。 */
function fmtTime(raw: string | null): string {
  if (!raw) return "—";
  const s = raw.includes("T") ? raw : raw.replace(" ", "T");
  const d = new Date(s.endsWith("Z") || /[+-]\d{2}:?\d{2}$/.test(s) ? s : `${s}Z`);
  if (Number.isNaN(d.getTime())) return raw;
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

export default function ReportsPage() {
  const { colors } = useTheme();
  const [rows, setRows] = useState<ReportRow[]>([]);
  const [total, setTotal] = useState(0);
  const [stats, setStats] = useState<Stats | null>(null);
  const [q, setQ] = useState("");
  const [slot, setSlot] = useState("");
  const [dateFrom, setDateFrom] = useState("");
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [active, setActive] = useState<ReportRow | null>(null);
  const boxRef = useRef<HTMLDivElement | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setErr(null);
    try {
      const p = new URLSearchParams({ limit: "300" });
      if (q.trim()) p.set("q", q.trim());
      if (slot) p.set("slot", slot);
      if (dateFrom) p.set("date_from", dateFrom);
      const [list, st] = await Promise.all([
        get<{ total: number; reports: ReportRow[] }>(`/reports?${p.toString()}`),
        get<Stats>("/reports/stats"),
      ]);
      setRows(list.reports);
      setTotal(list.total);
      setStats(st);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
      setRows([]);
      setTotal(0);
    } finally {
      setLoading(false);
    }
  }, [q, slot, dateFrom]);

  // 输入防抖，避免每敲一个字打一次库
  useEffect(() => {
    const t = setTimeout(() => void load(), 260);
    return () => clearTimeout(t);
  }, [load]);

  const maxMissing = useMemo(
    () => rows.reduce((m, r) => Math.max(m, r.missing_count ?? 0), 0),
    [rows],
  );

  const open = (r: ReportRow) => {
    setActive(r);
    setTimeout(() => boxRef.current?.scrollIntoView({ behavior: "smooth", block: "start" }), 60);
  };

  const latest = stats?.latest ?? null;

  return (
    <div>
      <PageHeader
        title="市场资讯报告"
        desc="四时段（06:45 / 12:00 / 15:30 / 21:00）跨市场简报 · 覆盖股票 / 债券 / 利率 / 黄金 / 汇率 · 已入库可检索"
        actions={
          <>
            <Btn small kind="ghost" onClick={() => void load()}>刷新</Btn>
            <a href="/api/v1/reports/index" target="_blank" rel="noreferrer"
              style={{ fontSize: 12.5, color: colors.accent, textDecoration: "none" }}>
              纯 HTML 索引 ↗
            </a>
          </>
        }
      />

      {/* —— 入库状态：让「报告到底进没进库」一眼可见 —— */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(158px,1fr))", gap: 10, marginBottom: 12 }}>
        <KpiCard label="库中报告" value={String(stats?.total ?? 0)} sub="market_reports" colors={colors} />
        <KpiCard
          label="最新一份"
          value={latest ? `${latest.report_date.slice(5)} ${latest.slot}` : "—"}
          sub={latest ? `入库 ${fmtTime(latest.ingested_at)}` : "尚无报告"}
          tone="accent"
          colors={colors}
        />
        <KpiCard
          label="覆盖交易日"
          value={String(stats?.by_date?.length ?? 0)}
          sub={`按日统计 ${stats?.by_date?.[0]?.report_date ?? "—"} 起`}
          colors={colors}
        />
        <KpiCard
          label="当前筛选"
          value={`${total} 份`}
          sub={maxMissing > 0 ? `最高待补 ${maxMissing} 项` : "无待补标记"}
          colors={colors}
        />
      </div>

      {/* —— 筛选 —— */}
      <Card colors={colors} pad={12} style={{ marginBottom: 12 }}>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
          <input
            placeholder="搜索标题 / 摘要 / 全文 / 标签（如：黄金、美联储、涨停）"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            style={{ ...inputStyle(colors), flex: "1 1 300px", width: "auto" }}
          />
          <select value={slot} onChange={(e) => setSlot(e.target.value)}
            style={{ ...inputStyle(colors), width: 120 }}>
            {SLOTS.map((s) => <option key={s} value={s}>{s || "全部时段"}</option>)}
          </select>
          <input type="date" value={dateFrom} onChange={(e) => setDateFrom(e.target.value)}
            title="起始日期" style={{ ...inputStyle(colors), width: 148 }} />
          {(q || slot || dateFrom) && (
            <Btn small kind="ghost" onClick={() => { setQ(""); setSlot(""); setDateFrom(""); }}>清空</Btn>
          )}
          <span style={{ fontSize: 12, color: colors.muted }}>
            {loading ? "读取中…" : `命中 ${total} / 库中 ${stats?.total ?? 0} 份`}
          </span>
        </div>
      </Card>

      {err && (
        <Card colors={colors} style={{ marginBottom: 12 }}>
          <div style={{ color: colors.up, fontSize: 13 }}>
            报告库读取失败：{err}
            <div style={{ color: colors.muted, marginTop: 6, fontSize: 12 }}>
              先确认后端已加载 /api/v1/reports 路由，再跑一次入库脚本：
              <code style={{ marginLeft: 6 }}>python backend/scripts/ingest_market_report.py --dir &lt;market_brief 目录&gt;</code>
            </div>
          </div>
        </Card>
      )}

      {/* —— 列表 —— */}
      <Card
        colors={colors}
        pad={0}
        title={`报告列表${slot ? ` · ${slot}` : ""}`}
        extra={<span style={{ fontSize: 12, color: colors.muted }}>点击任意行直接读全文</span>}
      >
        <div style={{ overflowX: "auto" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12.5 }}>
            <thead>
              <tr style={{ background: colors.tableStripe, color: colors.muted, fontSize: 11.5 }}>
                {["日期", "时段", "状态", "资讯", "冲突", "待补", "版本", "入库时间", "速览摘要"].map((h) => (
                  <th key={h} style={{ padding: "8px 10px", textAlign: "left", fontWeight: 600, whiteSpace: "nowrap" }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => {
                const on = active?.report_id === r.report_id;
                const rest = (r.trade_status ?? "").includes("休市");
                return (
                  <tr
                    key={r.report_id}
                    onClick={() => open(r)}
                    style={{
                      borderTop: `1px solid ${colors.border}`,
                      background: on ? `${colors.accent}12` : undefined,
                      cursor: "pointer",
                    }}
                  >
                    <td style={{ padding: "7px 10px", whiteSpace: "nowrap", fontVariantNumeric: "tabular-nums" }}>{r.report_date}</td>
                    <td style={{ padding: "7px 10px", whiteSpace: "nowrap" }}>
                      <Badge text={r.slot} color={SLOT_COLOR[r.slot] ?? colors.accent} soft />
                    </td>
                    <td style={{ padding: "7px 10px", whiteSpace: "nowrap", color: rest ? colors.muted : colors.text }}>
                      {rest ? "休市" : "交易日"}
                    </td>
                    <td style={{ ...numCell, color: (r.news_count ?? 0) < 8 ? colors.up : colors.text }}>{r.news_count ?? 0}</td>
                    <td style={{ ...numCell, color: (r.conflict_count ?? 0) > 0 ? "#b9770e" : colors.muted }}>{r.conflict_count ?? 0}</td>
                    <td style={{ ...numCell, color: (r.missing_count ?? 0) > 3 ? "#b9770e" : colors.muted }}>{r.missing_count ?? 0}</td>
                    <td style={{ padding: "7px 10px", whiteSpace: "nowrap", color: colors.muted, fontSize: 11.5 }}>{r.template_version ?? "—"}</td>
                    <td style={{ padding: "7px 10px", whiteSpace: "nowrap", color: colors.muted, fontSize: 11.5 }}>{fmtTime(r.ingested_at)}</td>
                    <td style={{ padding: "7px 10px", color: colors.muted, maxWidth: 620 }}>{r.summary ?? "—"}</td>
                  </tr>
                );
              })}
              {!loading && rows.length === 0 && (
                <tr>
                  <td colSpan={9} style={{ padding: "26px 12px", textAlign: "center", color: colors.muted }}>
                    没有匹配的报告。换个关键词，或清空日期筛选。
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </Card>

      {/* —— 阅读区 —— */}
      {active && (
        <div ref={boxRef} style={{ marginTop: 12 }}>
          <Card
            colors={colors}
            pad={0}
            title={
              <span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
                {active.title}
                <Badge text={active.slot} color={SLOT_COLOR[active.slot] ?? colors.accent} soft />
              </span>
            }
            extra={
              <span style={{ display: "inline-flex", gap: 12, alignItems: "center", fontSize: 12 }}>
                <span style={{ color: colors.muted }}>{active.report_date} · 生成 {fmtTime(active.generated_at)}</span>
                <a href={`/api/v1/reports/${encodeURIComponent(active.report_id)}/html`} target="_blank" rel="noreferrer"
                  style={{ color: colors.accent, textDecoration: "none" }}>新窗口 ↗</a>
                <a href={`/api/v1/reports/${encodeURIComponent(active.report_id)}/markdown`} target="_blank" rel="noreferrer"
                  style={{ color: colors.accent, textDecoration: "none" }}>Markdown</a>
                <button onClick={() => setActive(null)}
                  style={{ border: 0, background: "transparent", color: colors.muted, cursor: "pointer", fontSize: 12 }}>
                  收起 ✕
                </button>
              </span>
            }
          >
            <iframe
              key={active.report_id}
              title={active.title}
              src={`/api/v1/reports/${encodeURIComponent(active.report_id)}/html`}
              style={{ width: "100%", height: "82vh", border: 0, display: "block", background: "#fff" }}
            />
          </Card>
        </div>
      )}

      <div style={{ marginTop: 10, fontSize: 11.5, color: colors.muted, lineHeight: 1.7 }}>
        表 <code>market_reports</code>（quant_dev.db）· 全文（MD + HTML）已入库，容器内直接读库，不依赖宿主机文件路径 ·
        入库脚本 <code>backend/scripts/ingest_market_report.py</code>（report_id 幂等，每小时兜底扫描）。
        <br />
        报告性质：信息收集整理与事实核对，<b>不含方向预测与买卖建议，无裁决区</b>；观察点均为 if-then 可验证条件。
      </div>
    </div>
  );
}

const numCell: CSSProperties = {
  padding: "7px 10px",
  textAlign: "right",
  fontVariantNumeric: "tabular-nums",
  whiteSpace: "nowrap",
};
