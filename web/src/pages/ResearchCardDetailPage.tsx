/**
 * 研究卡详情页（整页，非弹层）：一张卡的完整档案 + 信号与分析。
 * 布局：顶部 KPI 带；左栏卡片 8 字段与决策历史；主栏证据图表、绩效细分、
 * 信号日历（近 30 天，点日期看当日标的与判定动作）；底部人工迁移操作。
 */
import { useCallback, useEffect, useState } from "react";
import { Badge, Btn, Card, KpiCard } from "../components/ui";
import EChart from "../components/EChart";
import type { EChartsOption } from "echarts";
import {
  ACTION_LABEL, actionBadge, EvidenceBars, MiniTable, Signed, STATE_META, TransitionBox,
} from "../components/cardShared";
import {
  api, BacktestFull, BacktestListItem, CardSignalsResp, OptimizeRunRow, researchApi,
  ResearchCardItem, StatRow,
} from "../api/client";
import { useTheme, ThemeColors } from "../theme";

/** 百分比：正数带 +，空值给 —。回测表里「超额」是核心列，不该被格式化吃掉符号。 */
function pct(v: number | null | undefined): string {
  if (v == null) return "—";
  return `${v >= 0 ? "+" : ""}${(v * 100).toFixed(2)}%`;
}

function pctColor(v: number | null | undefined, colors: ThemeColors): string {
  if (v == null) return colors.text;
  return v >= 0 ? colors.up : colors.down;
}

const TABS = [
  { key: "overview", label: "概览" },
  { key: "backtests", label: "回测" },
  { key: "optimize", label: "参数寻优" },
  { key: "paper", label: "模拟盘" },
  { key: "attribution", label: "归因" },
] as const;
type TabKey = (typeof TABS)[number]["key"];

/** 净值曲线：策略 vs 基准，都正常化到 1.0（绝对值不同量级，不然基准会贴地）。 */
function EquityChart({ detail, colors }: { detail: BacktestFull; colors: ThemeColors }) {
  const pts = detail.equity_curve ?? [];
  if (pts.length < 2) {
    return <div style={{ fontSize: 12, color: colors.muted }}>这条回测没有净值曲线数据。</div>;
  }
  const base = pts[0].equity || 1;
  const b0 = pts[0].benchmark || null;
  const dates = pts.map((p) => p.date.slice(5));
  const eq = pts.map((p) => +(p.equity / base).toFixed(4));
  const bm = pts.map((p) => (p.benchmark && b0 ? +(p.benchmark / b0).toFixed(4) : null));
  const option: EChartsOption = {
    grid: { left: 46, right: 14, top: 26, bottom: 26 },
    tooltip: { trigger: "axis", valueFormatter: (v) => (typeof v === "number" ? v.toFixed(3) : "—") },
    legend: { data: ["策略", "基准"], right: 0, top: 0, itemWidth: 12, itemHeight: 8, textStyle: { fontSize: 10 } },
    xAxis: { type: "category", data: dates, axisLabel: { fontSize: 10, interval: Math.max(0, Math.floor(dates.length / 6)) }, axisLine: { lineStyle: { color: colors.border } } },
    yAxis: { type: "value", scale: true, axisLabel: { fontSize: 10 }, splitLine: { lineStyle: { color: colors.border, type: "dashed" } } },
    series: [
      { name: "策略", type: "line", data: eq, showSymbol: false, lineStyle: { width: 1.8 }, itemStyle: { color: colors.accent } },
      { name: "基准", type: "line", data: bm, showSymbol: false, lineStyle: { width: 1.2 }, itemStyle: { color: colors.muted } },
    ],
  };
  return <EChart option={option} height={260} />;
}

/** 回测页签：左列表右详情（曲线 + 关键指标）。 */
function BacktestsTab({ list, detail, sel, onSelect, colors }: {
  list: BacktestListItem[] | null;
  detail: BacktestFull | null;
  sel: number | null;
  onSelect: (id: number) => void;
  colors: ThemeColors;
}) {
  if (list === null) return <div style={{ fontSize: 12, color: colors.muted }}>加载中…</div>;
  if (list.length === 0) {
    return <div style={{ fontSize: 12.5, color: colors.muted, lineHeight: 1.8 }}>
      这个策略还没有回测记录。去「回测寻优」跑一次，结果会出现在这里。
    </div>;
  }
  return (
    <div style={{ display: "grid", gridTemplateColumns: "minmax(300px, 1fr) minmax(0, 1.6fr)", gap: 16, alignItems: "start" }}>
      <Card title={`全部回测（${list.length}）`} colors={colors}
            extra={<span style={{ fontSize: 11, color: colors.muted }}>点一行看曲线</span>}>
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
          <thead>
            <tr style={{ color: colors.muted }}>
              {["#", "区间", "超额", "回撤"].map((h) => (
                <th key={h} style={{ textAlign: "left", padding: "6px 6px", fontWeight: 500 }}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {list.map((b) => (
              <tr key={b.id} onClick={() => onSelect(b.id)}
                  style={{
                    borderTop: `1px solid ${colors.border}`, cursor: "pointer",
                    background: sel === b.id ? `${colors.accent}12` : "transparent",
                  }}>
                <td style={{ padding: "7px 6px", color: colors.muted }}>{b.id}</td>
                <td style={{ padding: "7px 6px" }}>
                  {b.start_date.slice(0, 7)} ~ {b.end_date.slice(0, 7)}
                </td>
                <td style={{ padding: "7px 6px", fontWeight: 600, color: pctColor(b.excess_return, colors) }}>
                  {pct(b.excess_return)}
                </td>
                <td style={{ padding: "7px 6px", color: colors.muted }}>{pct(b.max_drawdown)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>
      <Card title={detail ? `#${detail.id} 净值曲线（正常化到 1.0）` : "净值曲线"} colors={colors}>
        {!detail ? <div style={{ fontSize: 12, color: colors.muted }}>加载中…</div> : (
          <>
            <EquityChart detail={detail} colors={colors} />
            <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 10, marginTop: 10 }}>
              {([
                ["总收益", detail.total_return], ["基准", detail.benchmark_total_return],
                ["超额", detail.excess_return], ["最大回撤", detail.max_drawdown],
              ] as [string, number | null][]).map(([k, v]) => (
                <div key={k} style={{ border: `1px solid ${colors.border}`, borderRadius: 8, padding: "7px 9px" }}>
                  <div style={{ fontSize: 10.5, color: colors.muted }}>{k}</div>
                  <div style={{ fontSize: 13, fontWeight: 700, color: pctColor(v, colors), fontVariantNumeric: "tabular-nums" }}>
                    {pct(v)}
                  </div>
                </div>
              ))}
            </div>
          </>
        )}
      </Card>
    </div>
  );
}

/** 模拟盘页签：该策略的任务 + 净值曲线。 */
function PaperTab({ strategyKey, colors }: { strategyKey: string; colors: ThemeColors }) {
  const [tasks, setTasks] = useState<{ id: number; name: string; equity: number | null; pnl_pct: number | null; positions_count: number | null; last_bar_date: string | null; last_run_at: string | null; enabled: boolean }[] | null>(null);
  const [points, setPoints] = useState<{ date: string; equity: number }[] | null>(null);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const all = await api.paperTasks();
        if (!alive) return;
        const mine = (all as unknown as { id: number; name: string; strategy_key: string }[])
          .filter((t) => t.strategy_key === strategyKey);
        setTasks(mine as never);
        if (mine.length > 0) {
          const snap = await api.paperSnapshots(mine[0].id, 500);
          if (alive) setPoints(snap.points);
        } else {
          setPoints([]);
        }
      } catch {
        if (alive) { setTasks([]); setPoints([]); }
      }
    })();
    return () => { alive = false; };
  }, [strategyKey]);

  if (tasks === null) return <div style={{ fontSize: 12, color: colors.muted }}>加载中…</div>;
  if (tasks.length === 0) {
    return <div style={{ fontSize: 12.5, color: colors.muted, lineHeight: 1.8 }}>
      这个策略还没有模拟盘任务。可以在「回测寻优」页跑一次回测后建盘，
      或让智能体用 <code>create_paper_task</code> 发起（走审批）。
    </div>;
  }
  const t = tasks[0];
  const cnt = points?.length ?? 0;
  const option: EChartsOption | null = cnt > 1 ? {
    grid: { left: 52, right: 14, top: 20, bottom: 26 },
    tooltip: { trigger: "axis" },
    xAxis: { type: "category", data: points!.map((p) => p.date.slice(2)), axisLabel: { fontSize: 10, interval: Math.max(0, Math.floor(cnt / 6)) } },
    yAxis: { type: "value", scale: true, axisLabel: { fontSize: 10 }, splitLine: { lineStyle: { color: colors.border, type: "dashed" } } },
    series: [{ name: "净值", type: "line", data: points!.map((p) => p.equity), showSymbol: false, lineStyle: { width: 1.8 }, itemStyle: { color: colors.accent } }],
  } : null;

  return (
    <div>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 12, marginBottom: 14 }}>
        {([
          ["净值", t.equity == null ? "—" : t.equity.toLocaleString("zh-CN", { maximumFractionDigits: 0 })],
          ["收益率", t.pnl_pct == null ? "—" : `${t.pnl_pct >= 0 ? "+" : ""}${(t.pnl_pct * 100).toFixed(2)}%`],
          ["持仓", t.positions_count == null ? "—" : String(t.positions_count)],
          ["净值日期", t.last_bar_date ?? "—"],
        ] as [string, string][]).map(([k, v]) => (
          <div key={k} style={{ background: colors.card, border: `1px solid ${colors.border}`, borderRadius: 10, padding: "10px 12px" }}>
            <div style={{ fontSize: 11, color: colors.muted }}>{k}</div>
            <div style={{ fontSize: 16, fontWeight: 700, fontVariantNumeric: "tabular-nums" }}>{v}</div>
          </div>
        ))}
      </div>
      <Card title={`${t.name} · 净值走势（${cnt} 个交易日）`} colors={colors}
            extra={<span style={{ fontSize: 11, color: colors.muted }}>最后运行 {(t.last_run_at ?? "").replace("T", " ").slice(0, 16)}</span>}>
        {option ? <EChart option={option} height={280} />
          : <div style={{ fontSize: 12, color: colors.muted, lineHeight: 1.8 }}>
              还没有足够的净值快照。任务每天收盘后跑一次，攒够两天就能画。
            </div>}
      </Card>
    </div>
  );
}

/** 归因页签：行业集中度 + 对冲后指标 + 逐笔明细可用性。 */
function AttributionTab({ detail, colors }: { detail: BacktestFull | null; colors: ThemeColors }) {
  if (!detail) return <div style={{ fontSize: 12, color: colors.muted }}>先选一条回测（在「回测」页签点一行）</div>;
  const ind = detail.industry_distribution ?? {};
  const top = Object.entries(ind).sort((a, b) => b[1] - a[1]).slice(0, 10);
  const max = top.length ? top[0][1] : 1;
  const noTrades = typeof detail.round_trips === "number" && detail.round_trips === 0;
  return (
    <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1.2fr) minmax(0, 1fr)", gap: 16, alignItems: "start" }}>
      <Card title="行业分布（前 10）" colors={colors}
            extra={<span style={{ fontSize: 11, color: colors.muted }}>#{detail.id}</span>}>
        {top.length === 0
          ? <div style={{ fontSize: 12, color: colors.muted }}>这条回测没记录行业分布。</div>
          : top.map(([name, w]) => (
            <div key={name} style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
              <span style={{ width: 76, fontSize: 11.5, color: colors.muted }}>{name}</span>
              <div style={{ flex: 1, height: 8, background: colors.border, borderRadius: 4, overflow: "hidden" }}>
                <div style={{ width: `${(w / max) * 100}%`, height: "100%", background: colors.accent }} />
              </div>
              <span style={{ width: 46, textAlign: "right", fontSize: 11.5, fontVariantNumeric: "tabular-nums" }}>
                {(w * 100).toFixed(1)}%
              </span>
            </div>
          ))}
      </Card>
      <div>
        <Card title="对冲与风险" colors={colors}>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 10 }}>
            {([
              ["对冲后收益", detail.hedged_total_return],
              ["对冲后夏普", detail.hedged_sharpe],
              ["对冲后回撤", detail.hedged_max_drawdown],
            ] as [string, number | null][]).map(([k, v]) => (
              <div key={k} style={{ border: `1px solid ${colors.border}`, borderRadius: 8, padding: "8px 10px" }}>
                <div style={{ fontSize: 10.5, color: colors.muted }}>{k}</div>
                <div style={{ fontSize: 14, fontWeight: 700, fontVariantNumeric: "tabular-nums" }}>
                  {v == null ? "—" : k.includes("夏普") ? v.toFixed(2) : pct(v)}
                </div>
              </div>
            ))}
          </div>
        </Card>
        <Card title="逐笔与持仓" colors={colors} style={{ marginTop: 14 }}>
          <div style={{ fontSize: 12.5, lineHeight: 1.9, color: colors.text }}>
            持仓 <b>{detail.holdings?.length ?? 0}</b> 只 · 成交 <b>{detail.trade_count ?? 0}</b> 笔
          </div>
          {noTrades && (
            <div style={{ marginTop: 8, fontSize: 11.5, color: "#b9770e", lineHeight: 1.8 }}>
              这条回测没有存逐笔明细（`round_trips=0`），所以算不了胜率分解、
              持有期分布这类归因。要看得重跑一次带明细的回测。
            </div>
          )}
        </Card>
      </div>
    </div>
  );
}

function Field({ label, children, colors }: { label: string; children: React.ReactNode; colors: ThemeColors }) {
  return (
    <div style={{ marginBottom: 14 }}>
      <div style={{ fontSize: 11.5, color: colors.muted, marginBottom: 3 }}>{label}</div>
      <div style={{ fontSize: 13, lineHeight: 1.7 }}>{children}</div>
    </div>
  );
}

/** 信号日历 + 当日标的明细（点日期切换）。 */
function SignalExplorer({ card, colors }: { card: ResearchCardItem; colors: ThemeColors }) {
  const [data, setData] = useState<CardSignalsResp | null>(null);
  const [dayDetail, setDayDetail] = useState<CardSignalsResp | null>(null);
  const [selDate, setSelDate] = useState<string | null>(null);
  const [loadingDay, setLoadingDay] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    researchApi.cardSignals(card.id)
      .then((d) => {
        if (!alive) return;
        if (d.ok) { setData(d); setDayDetail(d); setSelDate(d.latest_date ?? null); }
        else setErr(d.error || "不可用");
      })
      .catch((e) => { if (alive) setErr(e instanceof Error ? e.message : String(e)); });
    return () => { alive = false; };
  }, [card.id]);

  const pickDay = async (date: string) => {
    if (date === selDate) return;
    setSelDate(date);
    setLoadingDay(true);
    try {
      const d = await researchApi.cardSignals(card.id, date);
      if (d.ok) setDayDetail(d);
    } finally {
      setLoadingDay(false);
    }
  };

  if (err) return <div style={{ fontSize: 12.5, color: colors.muted }}>{err}</div>;
  if (!data) return <div style={{ fontSize: 12.5, color: colors.muted }}>加载中（首次约几秒钟）…</div>;

  const p = data.perf || {};
  const months = (data.by_month ?? []).filter((r) => r.n_10 != null);
  const streaks = (data.by_streak ?? []).filter((r) => r.n_10 != null);
  const resos = (data.by_resonance ?? []).filter((r) => r.n_10 != null);
  const weekly = (card.evidence?.weekly ?? []).slice(-26);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      {/* 周度证据趋势（复盘积累后更有用） */}
      {weekly.length > 0 && (
        <Card title="每周复盘证据趋势（90 日滚动超额）" colors={colors}>
          <EChart height={180} option={{
            grid: { left: 48, right: 16, top: 16, bottom: 24 },
            xAxis: { type: "category", data: weekly.map((w) => w.date.slice(5)), axisLabel: { color: colors.axis } },
            yAxis: { type: "value", axisLabel: { color: colors.axis, formatter: "{value}%" }, splitLine: { lineStyle: { color: colors.border } } },
            series: [{
              type: "bar",
              data: weekly.map((w) => ({
                value: w.exc_10,
                itemStyle: { color: (w.exc_10 ?? 0) >= 0 ? colors.up : colors.down },
              })),
            }],
            tooltip: { trigger: "axis" },
          }} />
          {weekly.length < 4 && (
            <div style={{ fontSize: 11, color: colors.muted, marginTop: 4 }}>
              每周六复盘后自动积累一个点，攒几周后趋势就有意义了
            </div>
          )}
        </Card>
      )}

      {/* 按月超额 */}
      {months.length > 0 && (
        <Card title="按月超额（识别是否只靠某段行情）" colors={colors}>
          <EChart height={200} option={{
            grid: { left: 48, right: 16, top: 16, bottom: 24 },
            xAxis: { type: "category", data: months.map((m) => m.month as string), axisLabel: { color: colors.axis } },
            yAxis: { type: "value", axisLabel: { color: colors.axis, formatter: "{value}%" }, splitLine: { lineStyle: { color: colors.border } } },
            series: [{
              type: "bar",
              data: months.map((m) => ({
                value: m.exc_10,
                itemStyle: { color: ((m.exc_10 as number) ?? 0) >= 0 ? colors.up : colors.down },
              })),
            }],
            tooltip: { trigger: "axis" },
          }} />
          <div style={{ marginTop: 10 }}>
            <MiniTable colors={colors} head={["月份", "信号数", "T+10 超额", "胜率", "t 值"]}
              rows={months.map((m: StatRow) => [
                m.month as string, (m.n_10 as number)?.toLocaleString(),
                <Signed v={m.exc_10 as number} />, `${m.win_10 ?? "—"}%`,
                typeof m.t_10 === "number" ? m.t_10.toFixed(2) : "—",
              ])} />
          </div>
        </Card>
      )}

      {/* 连续天数 / 共振细分 */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))", gap: 16 }}>
        {streaks.length > 0 && (
          <Card title="按连续在榜天数" colors={colors}>
            <MiniTable colors={colors} head={["连续天数", "信号数", "T+10 超额", "胜率"]}
              rows={streaks.map((r: StatRow) => [
                r.streak_bucket as string, (r.n_10 as number)?.toLocaleString(),
                <Signed v={r.exc_10 as number} />, `${r.win_10 ?? "—"}%`,
              ])} />
            <div style={{ fontSize: 11, color: colors.muted, marginTop: 6 }}>
              平台的建仓规则要求连续 ≥9 天——「9-15天」「15天+」两档才是实际可建仓的样本
            </div>
          </Card>
        )}
        {resos.length > 0 && (
          <Card title="按同日共振策略数" colors={colors}>
            <MiniTable colors={colors} head={["共振", "信号数", "T+10 超额", "胜率"]}
              rows={resos.map((r: StatRow) => [
                r.reso_bucket as string, (r.n_10 as number)?.toLocaleString(),
                <Signed v={r.exc_10 as number} />, `${r.win_10 ?? "—"}%`,
              ])} />
          </Card>
        )}
      </div>

      {/* 信号日历 */}
      {(data.recent_days?.length ?? 0) > 0 && (
        <Card title={`信号日历（近 ${data.recent_days!.length} 个有信号的交易日，点日期看明细）`} colors={colors}>
          <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
            {data.recent_days!.map((d) => {
              const on = d.date === selDate;
              return (
                <button key={d.date} onClick={() => pickDay(d.date)} style={{
                  fontSize: 11.5, border: `1px solid ${on ? colors.accent : colors.border}`, borderRadius: 6,
                  padding: "3px 9px", cursor: "pointer",
                  background: on ? `${colors.accent}18` : "transparent",
                  color: on ? colors.accent : colors.muted,
                }}>
                  {d.date.slice(5)} · <b>{d.count}</b> 只
                </button>
              );
            })}
          </div>
        </Card>
      )}

      {/* 当日标的与判定 */}
      {dayDetail && (dayDetail.latest_actions?.length ?? 0) > 0 && (
        <Card title={`${dayDetail.latest_date} 信号标的与判定（共 ${dayDetail.latest_total} 只${(dayDetail.latest_total ?? 0) > 100 ? `，按动作优先级显示前 100` : ""}）`}
          colors={colors} pad={8}
          extra={loadingDay ? <span style={{ fontSize: 12, color: colors.muted }}>加载中…</span> : undefined}>
          <div style={{ maxHeight: 420, overflowY: "auto" }}>
            <MiniTable colors={colors} head={["标的", "判定", "连续", "共振", "收盘", "理由"]}
              rows={dayDetail.latest_actions!.map((a) => [
                <span>{a.name ?? ""} <span style={{ color: colors.muted, fontSize: 11 }}>{a.symbol}</span></span>,
                actionBadge(a.action, colors),
                a.streak ?? "—",
                a.resonance ?? "—",
                a.close != null ? a.close.toFixed(2) : "—",
                <span style={{ color: colors.muted, fontSize: 11, textAlign: "left", display: "block" }}>{a.reason ?? "—"}</span>,
              ])} />
          </div>
        </Card>
      )}

      {/* 绩效总览（兜底摘要，放最后作参考） */}
      <div style={{ fontSize: 12, color: colors.muted }}>
        近 {data.window_days} 日汇总：信号 {Number(p.n_10 ?? 0).toLocaleString()} 条 · T+10 超额 <Signed v={p.exc_10 as number} /> ·
        T+5 <Signed v={p.exc_5 as number} /> · 胜率 {p.win_10 ?? "—"}% · t 值 {typeof p.t_10 === "number" ? p.t_10.toFixed(2) : "—"}
        （口径：T+1 开盘建仓、T+N 收盘卖出、超额 vs 全市场等权）
      </div>
    </div>
  );
}

/** 证据明细：把 evidence 里「详情页不认识」的结构化数据渲染出来。
 *
 * 为什么需要（2026-11-10 老刘：「基金规模与次年超额，这些卡片点开后，
 * 里面没有啥内容」）：原来详情页只认一种形状 —— `w240/w120/w60`（三窗口超额）
 * 和 `ic_mean/icir`。但三张基金卡的 evidence 各是各的结构：
 *   #18 levels（分档：<2亿 / 2-10亿 / … 各自的 exc、t、pos）
 *   #23 monthly / quarterly / annual（各含 gross/net/cost/t）
 *   #26 1m / 3m / 12m（留存概率）
 * 于是一个字段都对不上，整页空白。
 *
 * 这里做通用渲染：标量 → 键值行；对象（值是标量）→ 单行表；
 * 对象（值是对象）→ 多行表。列取并集，缺的留空。
 */
type EvTable = { title: string; headers: string[]; rows: { label: string; cells: unknown[] }[] };

function buildEvidenceTables(ev: Record<string, unknown>) {
  const scalars: [string, string][] = [];
  const tables: EvTable[] = [];
  const others: [string, unknown][] = [];
  const isScalar = (v: unknown) =>
    typeof v === "number" || typeof v === "string" || typeof v === "boolean";

  for (const [k, v] of Object.entries(ev || {})) {
    if (v == null || v === "") continue;
    if (isScalar(v)) { scalars.push([k, String(v)]); continue; }
    if (typeof v !== "object" || Array.isArray(v)) { others.push([k, v]); continue; }

    const obj = v as Record<string, unknown>;
    const vals = Object.values(obj);
    if (vals.every(isScalar)) {
      tables.push({
        title: k, headers: Object.keys(obj),
        rows: [{ label: "—", cells: Object.values(obj) }],
      });
      continue;
    }
    // 值是对象 → 多行表
    const subKeys = Object.keys(obj);
    const cols = [...new Set(subKeys.flatMap((sk) => {
      const cell = obj[sk];
      return cell && typeof cell === "object" && !Array.isArray(cell)
        ? Object.keys(cell as Record<string, unknown>) : [];
    }))];
    if (!cols.length) { others.push([k, v]); continue; }
    tables.push({
      title: k, headers: cols,
      rows: subKeys.map((sk) => {
        const cell = (obj[sk] || {}) as Record<string, unknown>;
        return { label: sk, cells: cols.map((c) => cell[c]) };
      }),
    });
  }
  return { scalars, tables, others };
}

function EvidenceDetail({ ev, colors }: { ev: Record<string, unknown>; colors: ThemeColors }) {
  const { scalars, tables, others } = buildEvidenceTables(ev);
  if (!scalars.length && !tables.length && !others.length) return null;

  const fmt = (v: unknown) => {
    if (v == null || v === "") return "—";
    if (typeof v === "number") return Number.isInteger(v) ? v.toLocaleString() : v.toFixed(2);
    return String(v);
  };

  return (
    <Card title="证据明细（卡片里存的原始数据）" colors={colors}>
      {scalars.length > 0 && (
        <div style={{ display: "flex", flexWrap: "wrap", gap: "6px 20px", marginBottom: 12 }}>
          {scalars.map(([k, v]) => (
            <div key={k} style={{ fontSize: 12 }}>
              <span style={{ color: colors.muted }}>{k}：</span>
              <b style={{ color: colors.text }}>{v}</b>
            </div>
          ))}
        </div>
      )}
      {tables.map((t) => (
        <div key={t.title} style={{ marginBottom: 14 }}>
          <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 5 }}>{t.title}</div>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 11.5 }}>
            <thead>
              <tr style={{ color: colors.muted }}>
                <th style={{ textAlign: "left", padding: "4px 8px", fontWeight: 500 }}></th>
                {t.headers.map((h) => (
                  <th key={h} style={{ textAlign: "right", padding: "4px 8px", fontWeight: 500 }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {t.rows.map((r) => (
                <tr key={r.label} style={{ borderTop: `1px solid ${colors.border}` }}>
                  <td style={{ padding: "4px 8px", color: colors.muted }}>{r.label}</td>
                  {r.cells.map((c, i) => (
                    <td key={i} style={{
                      padding: "4px 8px", textAlign: "right",
                      fontVariantNumeric: "tabular-nums",
                      color: typeof c === "number" && c < 0 ? colors.down : colors.text,
                    }}>{fmt(c)}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ))}
      {others.map(([k, v]) => (
        <div key={k} style={{ fontSize: 11.5, marginBottom: 6 }}>
          <span style={{ color: colors.muted }}>{k}：</span>
          <code style={{ fontSize: 11 }}>{JSON.stringify(v)}</code>
        </div>
      ))}
    </Card>
  );
}

export default function ResearchCardDetailPage({ cardId, onBack, initialTab }: { cardId: number; onBack: () => void; initialTab?: string }) {
  const { colors } = useTheme();
  const [card, setCard] = useState<ResearchCardItem | null>(null);
  const [err, setErr] = useState<string | null>(null);
  // 回测全部历史 + 寻优落库记录：设计文档 09 节要求「回测列全部而不是最近一条」
  const [backtests, setBacktests] = useState<BacktestListItem[] | null>(null);
  const [runs, setRuns] = useState<OptimizeRunRow[] | null>(null);
  const [tab, setTab] = useState<TabKey>((TABS.some((t) => t.key === initialTab) ? initialTab : "overview") as TabKey);
  const [btId, setBtId] = useState<number | null>(null);
  const [btDetail, setBtDetail] = useState<BacktestFull | null>(null);

  const load = useCallback(async () => {
    try {
      setCard(await researchApi.card(cardId));
      setErr(null);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  }, [cardId]);

  useEffect(() => { load(); }, [load]);

  const strategyKey = card?.strategy_key ?? "";
  useEffect(() => {
    if (!strategyKey) return;
    let alive = true;
    (async () => {
      try {
        const [bs, rs] = await Promise.all([
          api.strategyBacktests(20),
          api.optimizeRuns(strategyKey, 10),
        ]);
        if (!alive) return;
        const mine = bs.filter((b) => b.strategy_key === strategyKey);
        setBacktests(mine);
        setRuns(rs.runs);
        if (mine.length > 0) setBtId(mine[0].id);
      } catch {
        if (alive) { setBacktests([]); setRuns([]); }
      }
    })();
    return () => { alive = false; };
  }, [strategyKey]);

  useEffect(() => {
    if (!btId) { setBtDetail(null); return; }
    let alive = true;
    api.backtestFull(btId)
      .then((d) => { if (alive) setBtDetail(d); })
      .catch(() => { if (alive) setBtDetail(null); });
    return () => { alive = false; };
  }, [btId]);

  if (err) return <div style={{ color: colors.up }}>加载失败：{err} <Btn small kind="ghost" onClick={onBack}>返回</Btn></div>;
  if (!card) return <div style={{ color: colors.muted }}>加载中…</div>;

  const meta = STATE_META[card.state] ?? STATE_META.watch;
  const ev = card.evidence || {};
  const isFactor = card.card_type === "factor";

  return (
    <div>
      {/* 头部 */}
      <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 18, flexWrap: "wrap" }}>
        <Btn small kind="ghost" onClick={onBack}>← 返回卡片墙</Btn>
        <span style={{ fontSize: 20, fontWeight: 700 }}>{card.name}</span>
        <span style={{ color: colors.muted, fontSize: 12.5 }}>{card.key}</span>
        <Badge text={isFactor ? "因子卡" : "策略卡"} color="#6c5ce7" soft />
        <Badge text={meta.label} color={meta.color} soft />
        <span style={{ fontSize: 12, color: colors.muted }}>{meta.desc}</span>
      </div>

      {/* 页签：概览=原有档案；其余四页是设计文档 09 节要求的「枢纽」 */}
      <div style={{ display: "flex", gap: 2, borderBottom: `1px solid ${colors.border}`, marginBottom: 16 }}>
        {TABS.map((t) => {
          const n = t.key === "backtests" ? backtests?.length
            : t.key === "optimize" ? runs?.length : undefined;
          const on = tab === t.key;
          return (
            <button key={t.key} onClick={() => setTab(t.key)}
              style={{
                padding: "7px 13px", fontSize: 13, cursor: "pointer",
                border: "none", background: "transparent",
                color: on ? colors.accent : colors.muted,
                borderBottom: `2px solid ${on ? colors.accent : "transparent"}`,
                marginBottom: -1, fontWeight: on ? 700 : 400,
              }}>
              {t.label}{n != null ? `（${n}）` : ""}
            </button>
          );
        })}
      </div>

      {tab === "overview" && (<>
      {/* KPI 带 */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(140px, 1fr))", gap: 12, marginBottom: 18 }}>
        {!isFactor && <>
          <KpiCard label="90 日 T+10 超额" value={ev.exc_10 != null ? `${ev.exc_10 > 0 ? "+" : ""}${ev.exc_10}%` : "—"}
            tone={ev.exc_10 != null ? (ev.exc_10 >= 0 ? "up" : "down") : "neutral"} colors={colors}
            sub={`T+5 ${ev.exc_5 != null ? `${ev.exc_5 > 0 ? "+" : ""}${ev.exc_5}%` : "—"}`} />
          <KpiCard label="t 值（|t|≥2 不是运气）" value={ev.t_10 != null ? ev.t_10.toFixed(2) : "—"}
            tone={Math.abs(ev.t_10 ?? 0) >= 2 ? (ev.t_10! >= 0 ? "up" : "down") : "neutral"} colors={colors} />
          <KpiCard label="样本量 n" value={(ev.n_10 ?? ev.n)?.toLocaleString() ?? "—"} colors={colors}
            sub={`胜率 ${ev.win_10 ?? "—"}%`} />
        </>}
        {isFactor && <>
          <KpiCard label="IC" value={ev.ic_mean?.toFixed(4) ?? "—"}
            tone={(ev.ic_mean ?? 0) >= 0 ? "up" : "down"} colors={colors} />
          <KpiCard label="ICIR" value={ev.icir?.toFixed(2) ?? "—"} colors={colors} />
          <KpiCard label="评级" value={ev.rating ?? "—"} colors={colors} />
        </>}
        {card.state === "downgraded" && (
          <KpiCard label="退役倒计时（负周数）" value={`${ev.negative_weeks ?? 0} / 4`}
            tone={(ev.negative_weeks ?? 0) >= 3 ? "up" : "neutral"} colors={colors}
            sub="满 4 周系统建议退役；转正自动清零" />
        )}
        <KpiCard label="证据更新于" value={ev.evidence_updated_at ?? card.updated_at?.slice(0, 10) ?? "—"} colors={colors}
          sub="每周六复盘自动刷新" />
      </div>

      {/* —— 回测 / 验证方式 ——
          老刘 2026-11-09：「点进去也看不到具体回测的情况」。
          策略卡关联 registry 里同名策略的最近一次真实回测；
          因子卡本来就没有回测，这里正面说清它靠什么验证，而不是留空白。 */}
      {isFactor ? (
        <>
        <Card title="这张卡怎么验证（没有「回测」）" colors={colors}>
          <div style={{ fontSize: 12.5, lineHeight: 1.85, color: colors.text }}>
            因子卡验证的是<b>信号的预测力</b>，不是「能不能赚钱」。
            不同卡片的证据形式不一样：策略类看三窗口超额（240/120/90 日），
            基金类看分档统计、留存概率这类截面数据 ——
            <b>下面「证据明细」里就是这张卡实际存的数据</b>，原样展开。
            要变成可交易策略，得先注册进策略注册表（带上持仓规则、成本、调仓周期），
            那时才会有净值曲线、回撤和成交明细。
          </div>
          {(card.crowd_note || "").includes("不是可交易信号") && (
            <div style={{
              marginTop: 10, padding: "8px 12px", borderRadius: 8,
              background: "#b9770e14", border: "1px solid #b9770e44",
              fontSize: 12, color: "#b9770e", lineHeight: 1.7,
            }}>
              ⚠️ 这张卡自己就写明了 —— {card.crowd_note}
              <div style={{ color: colors.muted, marginTop: 4 }}>
                （所以它出现在「启用」里是被误标的，按内容应该归档为工程记录）
              </div>
            </div>
          )}
        </Card>
        <EvidenceDetail ev={card.evidence as unknown as Record<string, unknown>} colors={colors} />
        </>
      ) : (
        <Card
          title="最近回测"
          colors={colors}
          extra={card.strategy_key
            ? <span style={{ fontSize: 11, color: colors.muted }}>策略 {card.strategy_key}</span>
            : <span style={{ fontSize: 11, color: "#b9770e" }}>未封装成策略</span>}
        >
          {card.backtest ? (
            <>
              <div style={{ display: "grid", gridTemplateColumns: "repeat(7, 1fr)", gap: 10 }}>
                {([
                  ["总收益", card.backtest.total_return, "pct"],
                  ["基准", card.backtest.benchmark_total_return, "pct"],
                  ["超额", card.backtest.excess_return, "pct"],
                  ["夏普", card.backtest.sharpe, "num2"],
                  ["最大回撤", card.backtest.max_drawdown, "pct"],
                  ["成交笔数", card.backtest.trade_count, "int"],
                  ["胜率", card.backtest.win_rate, "plain"],
                ] as [string, number | null, string][]).map(([label, v, kind]) => {
                  const txt = v == null ? "—"
                    : kind === "pct" ? `${v >= 0 ? "+" : ""}${(v * 100).toFixed(2)}%`
                    : kind === "plain" ? `${(v * 100).toFixed(2)}%`   // 胜率：不该有正负号
                    : kind === "num2" ? v.toFixed(2)
                    : Math.round(v).toLocaleString();
                  // 胜率是「比例」不是「涨跌」，不能染红绿 —— 90% 胜率不该是红色
                  const tone = v == null || kind === "int" || kind === "plain" ? colors.text
                    : (v >= 0 ? colors.up : colors.down);
                  return (
                    <div key={label} style={{
                      border: `1px solid ${colors.border}`, borderRadius: 8, padding: "8px 10px",
                    }}>
                      <div style={{ fontSize: 11, color: colors.muted }}>{label}</div>
                      <div style={{ fontSize: 15, fontWeight: 700, color: tone, fontVariantNumeric: "tabular-nums" }}>
                        {txt}
                      </div>
                    </div>
                  );
                })}
              </div>
              <div style={{ marginTop: 10, display: "flex", alignItems: "center", gap: 12, fontSize: 11.5, color: colors.muted }}>
                <span>#{card.backtest.id} · {card.backtest.start} ~ {card.backtest.end}</span>
                <span style={{ flex: 1 }} />
                <Btn small kind="ghost"
                  onClick={() => window.dispatchEvent(new CustomEvent("quant-nav", { detail: { tab: "btopt" } }))}>
                  查看回测详情 →
                </Btn>
              </div>
            </>
          ) : card.strategy_key ? (
            <div style={{ fontSize: 12.5, color: colors.muted, lineHeight: 1.8 }}>
              对应策略 <b style={{ color: colors.text }}>{card.strategy_key}</b> 还没跑过回测。
              <span style={{ marginLeft: 8 }}>
                <Btn small kind="ghost"
                  onClick={() => window.dispatchEvent(new CustomEvent("quant-nav", { detail: { tab: "btopt" } }))}>
                  去跑一次 →
                </Btn>
              </span>
            </div>
          ) : (
            <div style={{ fontSize: 12.5, color: colors.muted, lineHeight: 1.8 }}>
              这张卡策略还没封装进注册表 —— 它的信号每天在算（见下方信号日历），
              但没有持仓规则/成本模型，所以跑不了组合回测。
              要做成可跟踪的策略，需要在 registry 里加一条 <code>{card.key}</code> 的注册项。
            </div>
          )}
        </Card>
      )}

      <div style={{ display: "grid", gridTemplateColumns: "minmax(260px, 1fr) minmax(0, 2.2fr)", gap: 18, alignItems: "start" }}>
        {/* 左栏：档案 */}
        <Card title="卡片档案" colors={colors}>
          <Field label="① 假设（它凭什么赚钱）" colors={colors}>{card.hypothesis || "—"}</Field>
          <Field label="② 数据与口径" colors={colors}>{card.data_spec || "—"}</Field>
          {!isFactor && (
            <Field label="③ 证据（三窗口超额）" colors={colors}>
              <div style={{ maxWidth: 280 }}><EvidenceBars ev={ev} colors={colors} /></div>
            </Field>
          )}
          <Field label="④ 成本" colors={colors}>{card.cost_note || "—"}</Field>
          <Field label="⑤ 容量与拥挤" colors={colors}>{card.crowd_note || "—"}</Field>
          <Field label="⑥ 失效条件（什么情况算它死了）" colors={colors}>
            <span style={{ color: "#b9770e" }}>{card.fail_condition || "—"}</span>
          </Field>
          <Field label={`⑦ 决策历史（${card.history.length} 次）`} colors={colors}>
            <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              {[...card.history].reverse().map((h, i) => (
                <div key={i} style={{ borderLeft: `2px solid ${colors.border}`, paddingLeft: 10 }}>
                  <div style={{ fontSize: 12 }}>
                    {h.at ?? h.date} · {h.from ? `${STATE_META[h.from]?.label ?? h.from} → ` : ""}{STATE_META[h.to]?.label ?? h.to}
                    <span style={{ color: colors.muted }}>
                      （{h.actor === "human" ? "人工" : h.actor === "auto-mining" ? "自动挖矿" : h.actor === "auto-review" ? "每周复盘" : "系统"}）
                    </span>
                  </div>
                  <div style={{ fontSize: 12, color: colors.muted }}>{h.reason}</div>
                </div>
              ))}
            </div>
          </Field>
        </Card>

        {/* 主栏：信号与分析 */}
        <div>
          {isFactor ? (
            <Card title="因子说明" colors={colors}>
              <div style={{ fontSize: 13, color: colors.muted, lineHeight: 1.8 }}>
                因子卡的截面分析（IC 衰减 / 分组单调性 / 多空收益）在「策略研究 → 因子实验室」。
                此页管生命周期：证据、状态、决策历史。
              </div>
            </Card>
          ) : (
            <SignalExplorer card={card} colors={colors} />
          )}
        </div>
      </div>

      {/* 底部：人工迁移 */}
      <Card title="人工状态迁移（下一次跑批生效，理由自动留痕）" colors={colors} style={{ marginTop: 18 }}>
        <TransitionBox cardId={card.id} current={card.state} colors={colors} onDone={load} />
      </Card>
      </>)}

      {tab === "backtests" && (
        <BacktestsTab list={backtests} detail={btDetail} sel={btId}
          onSelect={setBtId} colors={colors} />
      )}

      {tab === "optimize" && (
        <Card title={`参数寻优（${runs?.length ?? "…"}）`} colors={colors}
              extra={<span style={{ fontSize: 11, color: colors.muted }}>已落库 · 跨重启可回看</span>}>
          {runs === null ? (
            <div style={{ fontSize: 12, color: colors.muted }}>加载中…</div>
          ) : runs.length === 0 ? (
            <div style={{ fontSize: 12.5, color: colors.muted, lineHeight: 1.8 }}>
              还没有寻优记录。去「回测寻优」跑一次，结果会自动落库并出现在这里。
            </div>
          ) : (
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
              <thead>
                <tr style={{ color: colors.muted }}>
                  {["时间", "状态", "组数", "最优夏普", "最优参数", "耗时"].map((h) => (
                    <th key={h} style={{ textAlign: "left", padding: "6px 8px", fontWeight: 500 }}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {runs.map((r) => {
                  const tone = r.status === "done" ? colors.down
                    : r.status === "failed" ? colors.up
                    : r.status === "running" ? colors.accent : colors.muted;
                  const secs = r.started_at && r.finished_at
                    ? Math.round((new Date(r.finished_at).getTime() - new Date(r.started_at).getTime()) / 1000)
                    : null;
                  return (
                    <tr key={r.id} style={{ borderTop: `1px solid ${colors.border}` }}>
                      <td style={{ padding: "7px 8px", color: colors.muted }}>
                        {(r.finished_at ?? r.started_at ?? "").replace("T", " ").slice(5, 16)}
                      </td>
                      <td style={{ padding: "7px 8px", color: tone, fontWeight: 600 }}>{r.status}</td>
                      <td style={{ padding: "7px 8px" }}>{r.total}</td>
                      <td style={{ padding: "7px 8px", fontWeight: 600 }}>
                        {r.best_sharpe == null ? "—" : r.best_sharpe.toFixed(2)}
                      </td>
                      <td style={{ padding: "7px 8px", color: colors.muted, fontSize: 11.5 }}>
                        {Object.keys(r.best_params ?? {}).length === 0
                          ? "—"
                          : Object.entries(r.best_params ?? {}).map(([k, v]) => `${k}=${v}`).join(" / ")}
                      </td>
                      <td style={{ padding: "7px 8px", color: colors.muted }}>
                        {secs == null ? "—" : `${secs}s`}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </Card>
      )}

      {tab === "paper" && <PaperTab strategyKey={strategyKey} colors={colors} />}
      {tab === "attribution" && <AttributionTab detail={btDetail} colors={colors} />}
    </div>
  );
}
