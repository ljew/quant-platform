/**
 * 今日观点（默认首页）：打开即有观点。
 * 聚合原「信号中心」每日动作 + 「资讯中心」快讯 + 指数概览 + 流水线状态，
 * 信号行点击展开 K 线抽屉（轻量版行情看板）。
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { Badge, Card, PageHeader } from "../components/ui";
import EChart from "../components/EChart";
import HotBriefCard from "../components/HotBriefCard";
import TodayRobustnessCard from "../components/TodayRobustnessCard";
import { agentApi, api, get, newsApi, AgentState, KlineBar, NewsFeed } from "../api/client";
import { localDate } from "../utils/date";
import { formatPrice2, klineTooltip } from "../utils/klineTooltip";
import { useTheme } from "../theme";

interface ActionItem {
  symbol: string; name: string | null; action: string; streak: number;
  resonance: number; strategies: string; quality_tier: string;
  close: number | null; last_price: number | null; reason: string;
}

const ACTION_META: Record<string, { cn: string; color: string }> = {
  BUY_STRONG: { cn: "重点买入", color: "#c0392b" },
  WATCH: { cn: "关注", color: "#b9770e" },
  NEW: { cn: "新出现", color: "#2471a3" },
  REDUCE: { cn: "减仓", color: "#1e8449" },
  SELL: { cn: "卖出", color: "#1e8449" },
  EXIT: { cn: "移出", color: "#888780" },
};
/** 汇总卡：把 6 种动作归并为 4 张卡。 */
const CARD_DEFS = [
  { key: "buy", label: "重点买入", acts: ["BUY_STRONG"], color: "#c0392b" },
  { key: "watch", label: "关注 / 新出现", acts: ["WATCH", "NEW"], color: "#b9770e" },
  { key: "reduce", label: "减仓", acts: ["REDUCE"], color: "#1e8449" },
  { key: "sell", label: "卖出 / 移出", acts: ["SELL", "EXIT"], color: "#2f8a4e" },
];

/** 体检状态色：绿=正常 橙=警告 红=失败 */

const INDICES = [
  { symbol: "sh000001", name: "上证指数" },
  { symbol: "sz399001", name: "深证成指" },
  { symbol: "sz399006", name: "创业板指" },
  { symbol: "sh000300", name: "沪深300" },
];

export default function TodayPage() {
  const { colors } = useTheme();
  const [date, setDate] = useState("");
  const [dates, setDates] = useState<{ date: string; symbols: number }[]>([]);
  const [actions, setActions] = useState<ActionItem[]>([]);
  const [agent, setAgent] = useState<AgentState | null>(null);
  // 注意：这个 feed 仍要拉 —— 右栏「指数概览」用的是它的 global_quotes。
  // 首页已不再展示原始快讯列表（2026-11-07 挪到「全部资讯」页）。
  const [feed, setFeed] = useState<NewsFeed | null>(null);
  const [filter, setFilter] = useState<string>("buy");
  const [idxQuotes, setIdxQuotes] = useState<{ name: string; price: number; pct: number }[]>([]);
  // K 线抽屉
  const [drawer, setDrawer] = useState<{ symbol: string; name: string } | null>(null);
  const [bars, setBars] = useState<KlineBar[]>([]);
  const [drawerLoading, setDrawerLoading] = useState(false);

  useEffect(() => {
    newsApi.feed(false).then(setFeed).catch(() => {});
    get<{ date: string; symbols: number }[]>("/signal/dates").then((d) => {
      setDates(d);
      if (d[0]) setDate(d[0].date);
    }).catch(() => {});
    agentApi.state().then(setAgent).catch(() => {});
  }, []);

  useEffect(() => {
    if (!date) return;
    get<{ date: string | null; items: ActionItem[] }>(`/signal/actions?date=${date}`)
      .then((d) => setActions(d.items)).catch(() => setActions([]));
  }, [date]);

  // 指数概览：优先实时快照（批量接口），失败或不完整回退资讯里的全球行情
  useEffect(() => {
    (async () => {
      try {
        const qs = await get<{ symbol: string; name: string; price: number; change_pct: number }[]>(
          `/market/quote?symbols=${INDICES.map((i) => i.symbol).join(",")}`);
        // 双保险：四个指数必须按代码全部命中才采用，名称一律取本地 INDICES。
        // 后端若只回 1 条、或把 sh000001 串成个股 sz000001，这里会拒绝展示并回退，
        // 避免 2×2 网格冒出「平安银行」这种错卡（2026-11-07）。
        const bySym = new Map(qs.map((q) => [q.symbol?.toLowerCase(), q]));
        const matched = INDICES.map((i) => {
          const q = bySym.get(i.symbol);
          return q ? { name: i.name, price: q.price, pct: q.change_pct } : null;
        });
        if (matched.every((x) => x !== null)) {
          setIdxQuotes(matched as { name: string; price: number; pct: number }[]);
          return;
        }
      } catch { /* 回退到资讯快照 */ }
      if (feed) {
        // 按 INDICES 的代码与顺序对齐，不依赖后端列表顺序（原实现 slice(0,4)
        // 会在缺创业板指时把第 3 张卡换成中证800）。缺项才用其它行情补位。
        const byCode = new Map(feed.global_quotes.map((q) => [q.code.toLowerCase(), q]));
        const aligned: { name: string; price: number; pct: number }[] = [];
        for (const i of INDICES) {
          const q = byCode.get(i.symbol);
          if (q) aligned.push({ name: i.name, price: q.price, pct: q.change_pct });
        }
        for (const q of feed.global_quotes) {
          if (aligned.length >= INDICES.length) break;
          if (INDICES.some((i) => i.symbol === q.code.toLowerCase())) continue;
          aligned.push({ name: q.name, price: q.price, pct: q.change_pct });
        }
        setIdxQuotes(aligned.slice(0, INDICES.length));
      }
    })();
  }, [feed]);

  const openDrawer = useCallback((it: ActionItem) => {
    setDrawer({ symbol: it.symbol, name: it.name || it.symbol });
    setDrawerLoading(true);
    api.kline(it.symbol, localDate(400), localDate(), 500)
      .then(setBars).catch(() => setBars([]))
      .finally(() => setDrawerLoading(false));
  }, []);

  const counts = useMemo(() => {
    const m: Record<string, ActionItem[]> = {};
    CARD_DEFS.forEach((c) => { m[c.key] = actions.filter((a) => c.acts.includes(a.action)); });
    return m;
  }, [actions]);

  /** 一句话观点：由当日信号自动拼出来。 */
  const verdict = useMemo(() => {
    if (!actions.length) return "当日暂无信号产出——可切换日期回看，或到「数据中心」检查每日流水线状态。";
    const parts: string[] = [];
    const buys = counts.buy;
    if (buys.length) {
      const names = buys.slice(0, 3).map((x) => `「${x.name || x.symbol}」`).join("");
      parts.push(`重点买入 ${buys.length} 只：${names}${buys.length > 3 ? " 等" : ""}`);
      const hot = [...buys].sort((a, b) => b.streak - a.streak)[0];
      if (hot && hot.streak >= 2) parts.push(`「${hot.name || hot.symbol}」连续 ${hot.streak} 日命中（${hot.strategies}）`);
    }
    const exits = [...counts.reduce, ...counts.sell];
    if (exits.length) {
      parts.push(`减仓/卖出 ${exits.length} 只：${exits.slice(0, 3).map((x) => `「${x.name || x.symbol}」`).join("")}${exits.length > 3 ? " 等" : ""}`);
    }
    if (!parts.length && counts.watch.length) parts.push(`无买卖动作，${counts.watch.length} 只进入观察名单`);
    return `今日信号 ${actions.length} 条。` + parts.join("；") + "。";
  }, [actions, counts]);

  /** 流水线状态徽章。 */
  const pipeChip = useMemo(() => {
    const run = agent?.latest_run;
    if (!run) return { text: "流水线状态未知", color: colors.muted };
    if (agent?.orchestrator.running) return { text: "● 流水线运行中…", color: colors.accent };
    if (run.gate_status === "pending") return { text: "● 待人工审查（Risk 闸门）", color: "#b9770e" };
    if (run.status === "failed" || run.error) return { text: "● 流水线异常", color: colors.up };
    return { text: `● 流水线已完成${run.data_date ? `（${run.data_date}）` : ""}`, color: colors.down };
  }, [agent, colors]);

  const shown = counts[filter] || [];
  const klineOption = useMemo(() => ({
    tooltip: klineTooltip(bars, colors),
    grid: { left: 64, right: 16, top: 16, bottom: 44 },
    xAxis: { type: "category", data: bars.map((b) => b.date), boundaryGap: true, axisLabel: { fontSize: 10 } },
    yAxis: { scale: true, axisLabel: { fontSize: 10, formatter: formatPrice2 } },
    dataZoom: [
      { type: "inside", start: 55, end: 100 },
      { type: "slider", height: 16, bottom: 6, start: 55, end: 100 },
    ],
    series: [{
      name: "K线", type: "candlestick",
      data: bars.map((b) => [b.open, b.close, b.low, b.high]),
      itemStyle: { color: colors.up, color0: colors.down, borderColor: colors.up, borderColor0: colors.down },
    }],
  }), [bars, colors]);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      <PageHeader
        title="今日观点"
        desc={`${date || "…"} · 信号由每日流水线自动产出 · T+1 开盘建仓口径`}
        actions={
          <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
            <button
              onClick={() => window.dispatchEvent(new CustomEvent("quant-nav", { detail: { tab: "cockpit" } }))}
              style={{
                border: `1px solid ${colors.border}`, background: colors.card, color: pipeChip.color,
                borderRadius: 999, padding: "5px 14px", fontSize: 12, cursor: "pointer", fontWeight: 600,
              }}
              title="点击查看研究驾驶舱"
            >
              {pipeChip.text}
            </button>
            <select value={date} onChange={(e) => setDate(e.target.value)}
              style={{ padding: "6px 10px", borderRadius: 8, border: `1px solid ${colors.border}`, background: colors.card, color: colors.text }}>
              {dates.map((d) => <option key={d.date} value={d.date}>{d.date}（{d.symbols} 只）</option>)}
            </select>
          </div>
        }
      />

      {/* —— 一句话观点 —— */}
      <div style={{
        background: `linear-gradient(135deg, ${colors.accent}14, ${colors.card})`,
        border: `1px solid ${colors.accent}44`, borderRadius: 12, padding: "14px 18px",
        fontSize: 14.5, lineHeight: 1.8, fontWeight: 500,
      }}>
        {verdict}
      </div>

      {/* —— W46-3 稳健摘要（轻量）：新鲜度 + 最近评估徽章 + OOS/成本入口 —— */}
      <TodayRobustnessCard colors={colors} />

      {/* —— 每小时热点简报（非结构化资讯 → LLM 判断）——
          放在「一句话观点」下面：两个都是判断，先读完观点再看可执行清单。
          两列排布是为了不把「信号明细」挤出首屏。 */}
      <HotBriefCard columns={2} dense />

      {/* —— 动作汇总卡 —— */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(4,1fr)", gap: 12 }}>
        {CARD_DEFS.map((c) => (
          <button key={c.key} onClick={() => setFilter(c.key)} style={{
            background: colors.card, border: `1px solid ${filter === c.key ? colors.accent : colors.border}`,
            borderLeft: `3px solid ${c.color}`, borderRadius: 12, padding: "10px 16px",
            cursor: "pointer", textAlign: "left", color: colors.text,
          }}>
            <div style={{ fontSize: 24, fontWeight: 700, color: c.color, fontVariantNumeric: "tabular-nums" }}>
              {counts[c.key]?.length ?? 0}
            </div>
            <div style={{ fontSize: 12, color: colors.muted }}>{c.label}</div>
          </button>
        ))}
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 330px", gap: 14, alignItems: "start" }}>
        {/* —— 信号明细 —— */}
        <Card colors={colors} pad={8} title={
          <span>信号明细
            <span style={{ color: colors.muted, fontSize: 12, fontWeight: 400, marginLeft: 8 }}>
              {CARD_DEFS.find((c) => c.key === filter)?.label} · {shown.length} 只 · 点击行查看 K 线
            </span>
          </span>
        }>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
            <thead>
              <tr>
                {["标的", "操作", "现价", "连续", "共振", "命中策略", "理由"].map((h) => (
                  <th key={h} style={{ color: colors.muted, fontWeight: 500, textAlign: "left", padding: "7px 10px", borderBottom: `1px solid ${colors.border}`, fontSize: 12 }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {shown.map((it) => {
                const meta = ACTION_META[it.action] || { cn: it.action, color: colors.muted };
                const sel = drawer?.symbol === it.symbol;
                return (
                  <tr key={it.symbol} onClick={() => openDrawer(it)} style={{
                    cursor: "pointer", background: sel ? `${colors.accent}1f` : "transparent",
                  }}>
                    <td style={{ padding: "8px 10px", borderBottom: `1px solid ${colors.border}` }}>
                      <b>{it.name || it.symbol}</b> <span style={{ color: colors.muted, fontSize: 11 }}>{it.symbol}</span>
                    </td>
                    <td style={{ padding: "8px 10px", borderBottom: `1px solid ${colors.border}` }}>
                      <Badge text={meta.cn} color={meta.color} soft />
                    </td>
                    <td style={{ padding: "8px 10px", borderBottom: `1px solid ${colors.border}` }}>
                      {(() => {
                        // 优先展示信号日的未复权实际收盘价（与同花顺口径一致）；
                        // 库里无原始 K 线时才退回策略用的复权价。
                        const p = it.last_price ?? it.close;
                        return p == null ? "—" : p.toFixed(2);
                      })()}
                    </td>
                    <td style={{ padding: "8px 10px", borderBottom: `1px solid ${colors.border}` }}>
                      {it.streak > 0 ? <span style={{ color: meta.color, fontWeight: 600 }}>{it.streak} 日</span> : "—"}
                    </td>
                    <td style={{ padding: "8px 10px", borderBottom: `1px solid ${colors.border}` }}>{it.resonance || "—"}</td>
                    <td style={{ padding: "8px 10px", borderBottom: `1px solid ${colors.border}`, fontSize: 12 }}>{it.strategies}</td>
                    <td style={{ padding: "8px 10px", borderBottom: `1px solid ${colors.border}`, fontSize: 12, color: colors.muted, maxWidth: 220 }}>{it.reason}</td>
                  </tr>
                );
              })}
              {!shown.length && (
                <tr><td colSpan={7} style={{ padding: 18, color: colors.muted, textAlign: "center" }}>该类别当日无信号</td></tr>
              )}
            </tbody>
          </table>

          {/* —— K 线抽屉 —— */}
          {drawer && (
            <div style={{ borderTop: `1px solid ${colors.border}`, marginTop: 8, padding: "10px 8px 4px" }}>
              <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 6 }}>
                {drawer.name}（{drawer.symbol}）日 K · 近一年
                {drawerLoading && <span style={{ color: colors.muted, fontWeight: 400, marginLeft: 8 }}>加载中…</span>}
                <span style={{ float: "right", display: "flex", gap: 8 }}>
                  <button
                    onClick={() => window.dispatchEvent(new CustomEvent("quant-nav", { detail: { tab: "market" } }))}
                    style={{ border: 0, background: "transparent", color: colors.accent, cursor: "pointer", fontSize: 12 }}>
                    完整行情看板 →
                  </button>
                  <button onClick={() => setDrawer(null)}
                    style={{ border: 0, background: "transparent", color: colors.muted, cursor: "pointer", fontSize: 12 }}>
                    收起 ×
                  </button>
                </span>
              </div>
              {bars.length > 0 && <EChart height={300} option={klineOption as never} />}
              {!bars.length && !drawerLoading && <div style={{ color: colors.muted, fontSize: 12.5 }}>暂无 K 线数据</div>}
            </div>
          )}
        </Card>

        {/* —— 右侧：指数概览 —— */}
        <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
          <Card colors={colors} title="指数概览" pad={10}>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
              {idxQuotes.map((q) => (
                <div key={q.name} style={{ border: `1px solid ${colors.border}`, borderRadius: 8, padding: "8px 12px" }}>
                  <div style={{ fontSize: 11.5, color: colors.muted }}>{q.name}</div>
                  <div style={{ display: "flex", alignItems: "baseline", gap: 8 }}>
                    <b style={{ fontSize: 15, fontVariantNumeric: "tabular-nums" }}>
                      {q.price.toLocaleString(undefined, { maximumFractionDigits: 2 })}
                    </b>
                    <span style={{ fontSize: 12, fontWeight: 600, color: q.pct >= 0 ? colors.up : colors.down }}>
                      {q.pct >= 0 ? "+" : ""}{q.pct.toFixed(2)}%
                    </span>
                  </div>
                </div>
              ))}
              {!idxQuotes.length && <div style={{ color: colors.muted, fontSize: 12.5, gridColumn: "1/-1" }}>加载中…</div>}
            </div>
          </Card>

        </div>
      </div>
    </div>
  );
}
