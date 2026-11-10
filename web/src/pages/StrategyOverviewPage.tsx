/**
 * 策略研究 · 总览（策略研究的门面，设计文档 09 节）。
 *
 * 这个 hub 只回答一个问题：**我有几条策略线、各自走到哪、今天该处理什么。**
 * 核心不是展示，是推进 —— 所以第一屏是「待处理」，不是图表。
 *
 * 三块：今日编排（原「自动研究」搬来）· 待办清单 · 研究管线。
 * 管线的价值在于把 8 个分散阶段压成一行，一眼看出谁卡在⑤（没寻优）、谁卡在⑥（没模拟盘）。
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { Badge, Btn, Card } from "../components/ui";
import {
  api, agentApi, AgentState, BacktestListItem, FrameworkHealth, OptimizeRunRow,
  researchApi, ResearchCardItem,
} from "../api/client";
import { useTheme } from "../theme";

type LiteTask = { id: number; strategy_key: string; name: string; kind: string; last_bar_date: string | null };

const STAGES = ["①想法", "②因子", "③写策略", "④回测", "⑤寻优", "⑥模拟盘", "⑦归因", "⑧决策"];

function stagesOf(card: ResearchCardItem, bts: BacktestListItem[],
                  tasks: LiteTask[], runs: OptimizeRunRow[]): Set<number> {
  const s = new Set<number>([1]);
  if (card.card_type === "factor" || card.evidence?.ic_mean != null) s.add(2);
  if (card.strategy_key) s.add(3);
  if (card.strategy_key && bts.some((b) => b.strategy_key === card.strategy_key)) s.add(4);
  if (card.strategy_key && runs.some((r) => r.strategy_key === card.strategy_key && r.status === "done")) s.add(5);
  if (card.strategy_key && tasks.some((t) => t.strategy_key === card.strategy_key)) s.add(6);
  // 归因：只有跑过带明细的回测才算走完（round_trips>0 才谈得上分解）
  if (card.strategy_key && bts.some((b) => b.strategy_key === card.strategy_key && (b.trade_count ?? 0) > 0)) s.add(7);
  if (card.state === "enabled" && s.has(6)) s.add(8);
  return s;
}

export default function StrategyOverviewPage() {
  const { colors } = useTheme();
  const [cards, setCards] = useState<ResearchCardItem[] | null>(null);
  const [bts, setBts] = useState<BacktestListItem[]>([]);
  const [tasks, setTasks] = useState<LiteTask[]>([]);
  const [runs, setRuns] = useState<OptimizeRunRow[]>([]);
  const [agent, setAgent] = useState<AgentState | null>(null);
  const [health, setHealth] = useState<FrameworkHealth | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [c, b, t, r, a, h] = await Promise.all([
        researchApi.cards(),
        api.strategyBacktests(50),
        api.paperTasks(),
        api.optimizeRuns(undefined, 30),
        agentApi.state(),
        api.frameworkHealth(),
      ]);
      setCards((c as { cards: ResearchCardItem[] }).cards ?? []);
      setBts(b);
      setTasks(t as unknown as LiteTask[]);
      setRuns(r.runs);
      setAgent(a);
      setHealth(h);
      setErr(null);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const stats = useMemo(() => {
    const list = cards ?? [];
    const active = list.filter((c) => ["enabled", "candidate", "downgraded"].includes(c.state));
    const paperKeys = new Set(tasks.map((t) => t.strategy_key));
    const withPaper = active.filter((c) => c.strategy_key && paperKeys.has(c.strategy_key));
    const excs = active.map((c) => c.evidence?.exc_10).filter((v): v is number => typeof v === "number");
    const median = excs.length
      ? excs.slice().sort((a, b) => a - b)[Math.floor(excs.length / 2)]
      : null;
    return { total: list.length, active: active.length, withPaper: withPaper.length, median, excs: excs.length };
  }, [cards, tasks]);

  /** 待办：只出「能立刻动手」的，最多 3 条 —— 多了说明该批量处理，不是该翻页。 */
  const todos = useMemo(() => {
    const list = cards ?? [];
    const out: { tone: string; title: string; detail: string; action: string; tab: string; id?: number }[] = [];
    const paperKeys = new Set(tasks.map((t) => t.strategy_key));

    for (const c of list) {
      if (c.state !== "enabled" || !c.strategy_key) continue;
      const bt = bts.filter((b) => b.strategy_key === c.strategy_key)
        .sort((a, b) => (b.end_date ?? "").localeCompare(a.end_date ?? ""))[0];
      if (bt && (bt.excess_return ?? 0) < 0) {
        out.push({
          tone: colors.up,
          title: `${c.name}：卡片是「启用」，但回测超额 ${((bt.excess_return ?? 0) * 100).toFixed(2)}%`,
          detail: `#${bt.id} · ${bt.start_date} ~ ${bt.end_date} · ${bt.trade_count ?? 0} 笔成交。`
            + `要么退役，要么补一段不同市场环境重测。`,
          action: "看回测", tab: "cards", id: c.id,
        });
      }
    }
    for (const c of list) {
      if (c.state !== "candidate") continue;
      const t = c.evidence?.t_10;
      if (typeof t === "number" && Math.abs(t) >= 2) {
        out.push({
          tone: colors.down,
          title: `${c.name}：证据已够，可以升级`,
          detail: `t=${t.toFixed(2)}（|t|≥2）· 90 日超额 ${c.evidence?.exc_10 ?? "—"}%`,
          action: "看卡片", tab: "cards", id: c.id,
        });
      }
    }
    for (const c of list) {
      if (c.state !== "enabled" || !c.strategy_key) continue;
      if (!paperKeys.has(c.strategy_key)) {
        const poolMode = c.crowd_note?.includes("signal_union");
        out.push({
          tone: colors.muted,
          title: `${c.name}：还没有模拟盘`,
          detail: poolMode
            ? "signal_union 池要加载全市场面板，模拟盘已改子进程隔离，但内存仍吃紧。"
            : "回测达标但没在跟，等于只有结论没有跟踪。",
          action: "看卡片", tab: "cards", id: c.id,
        });
      }
    }
    return out.slice(0, 3);
  }, [cards, bts, tasks, colors]);

  const pipeline = useMemo(() => {
    return (cards ?? [])
      .filter((c) => ["enabled", "candidate", "downgraded"].includes(c.state))
      .map((c) => {
        const s = stagesOf(c, bts, tasks, runs);
        const bt = bts.filter((b) => b.strategy_key === c.strategy_key)
          .sort((a, b) => (b.end_date ?? "").localeCompare(a.end_date ?? ""))[0];
        const hasRun = runs.some((r) => r.strategy_key === c.strategy_key && r.status === "done");
        const hasPaper = tasks.some((t) => t.strategy_key === c.strategy_key);
        const next = !s.has(4) ? "跑回测" : !hasRun ? "做参数寻优"
          : !hasPaper ? "建模拟盘" : c.state === "enabled" ? "跟踪中" : "等决策";
        return { c, s, bt, next };
      })
      .sort((a, b) => b.s.size - a.s.size);
  }, [cards, bts, tasks, runs]);

  if (err) {
    return <div style={{ color: colors.up, fontSize: 13 }}>加载失败：{err}
      <Btn small kind="ghost" onClick={load}>重试</Btn></div>;
  }
  if (cards === null) return <div style={{ color: colors.muted, fontSize: 13 }}>加载中…</div>;

  const lr = agent?.latest_run ?? null;
  const dur = lr?.started_at && lr.finished_at
    ? Math.round((new Date(lr.finished_at).getTime() - new Date(lr.started_at).getTime()) / 1000)
    : null;

  return (
    <div>
      <div style={{ display: "flex", alignItems: "baseline", gap: 12, marginBottom: 14, flexWrap: "wrap" }}>
        <h2 style={{ margin: 0, fontSize: 19 }}>策略研究</h2>
        <span style={{ fontSize: 12, color: colors.muted }}>
          {stats.active} 条策略线在跟踪 · {stats.withPaper} 个有模拟盘 · {bts.length} 条回测记录
        </span>
      </div>

      {/* 今日编排横条 */}
      <div style={{
        background: colors.card, border: `1px solid ${colors.border}`, borderRadius: 10,
        padding: "11px 14px", marginBottom: 14, display: "flex", alignItems: "center", gap: 14,
        flexWrap: "wrap",
      }}>
        <b style={{ fontSize: 13 }}>今日编排</b>
        {lr ? (
          <>
            <Badge text={lr.status} color={lr.status === "SUCCESS" ? colors.down : colors.up} soft />
            <span style={{ fontSize: 12, color: colors.muted }}>
              run #{lr.id} · {dur != null ? `${Math.floor(dur / 60)}'${dur % 60}"` : "—"}
              {" · "}{(lr.started_at ?? "").replace("T", " ").slice(5, 16)}
            </span>
            <span style={{ fontSize: 12, color: colors.muted, flex: 1, minWidth: 200 }}>
              {lr.gate_note || ""}
            </span>
          </>
        ) : (
          <span style={{ fontSize: 12, color: colors.muted }}>今天还没跑过编排</span>
        )}
        <span style={{ fontSize: 11.5, color: colors.muted }}>
          下次 {agent?.scheduler?.next_run?.replace("T", " ").slice(5, 16) ?? "—"}
        </span>
      </div>

      {/* 四个数字 */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 12, marginBottom: 18 }}>
        {([
          ["在跟踪", `${stats.active}`, `${cards.filter((c) => c.state === "enabled").length} 启用 · ${cards.filter((c) => c.state === "downgraded").length} 降级`, colors.text],
          ["有模拟盘", `${stats.withPaper} / ${stats.active}`, stats.withPaper < stats.active ? `${stats.active - stats.withPaper} 条还没验证执行` : "全覆盖", stats.withPaper < stats.active ? "#b9770e" : colors.down],
          ["本周超额（中位）", stats.median == null ? "—" : `${stats.median > 0 ? "+" : ""}${stats.median}%`, `基于 ${stats.excs} 张卡的 90 日 T+10`, stats.median == null ? colors.text : (stats.median >= 0 ? colors.up : colors.down)],
          ["待处理", `${todos.length}`, todos.length ? "见下" : "无", todos.length ? colors.up : colors.down],
        ] as [string, string, string, string][]).map(([k, v, s, tone]) => (
          <div key={k} style={{ background: colors.card, border: `1px solid ${colors.border}`, borderRadius: 10, padding: "12px 14px" }}>
            <div style={{ fontSize: 11, color: colors.muted }}>{k}</div>
            <div style={{ fontSize: 22, fontWeight: 700, color: tone, fontVariantNumeric: "tabular-nums", lineHeight: 1.3 }}>{v}</div>
            <div style={{ fontSize: 10.5, color: colors.muted }}>{s}</div>
          </div>
        ))}
      </div>

      {/* 今天该处理的 */}
      <Card title={`今天该处理的（${todos.length}）`} colors={colors}
            extra={<span style={{ fontSize: 11, color: colors.muted }}>都是能直接动手的</span>}>
        {todos.length === 0 ? (
          <div style={{ fontSize: 12.5, color: colors.muted }}>没有待处理项 —— 各条线都在正常推进。</div>
        ) : todos.map((t, i) => (
          <div key={i} style={{
            display: "flex", gap: 10, padding: "9px 0",
            borderTop: i ? `1px solid ${colors.border}` : "none",
          }}>
            <span style={{ color: t.tone, fontWeight: 700, fontSize: 14, lineHeight: 1.5 }}>●</span>
            <div style={{ flex: 1 }}>
              <div style={{ fontSize: 12.5, fontWeight: 600 }}>{t.title}</div>
              <div style={{ fontSize: 11.5, color: colors.muted, lineHeight: 1.7, marginTop: 2 }}>{t.detail}</div>
            </div>
            <Btn small kind="ghost"
                onClick={() => {
                  if (t.tab === "cards" && t.id) window.location.hash = `cards/${t.id}`;
                }}>
              {t.action} →
            </Btn>
          </div>
        ))}
      </Card>

      {/* 策略体检 */}
      <Card title="策略体检" colors={colors} style={{ marginTop: 14 }}
            extra={<span style={{ fontSize: 11, color: colors.muted }}>研发期自检，一周看一次</span>}>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 12 }}>
          {([
            ["五层健康", health?.worst ?? "—", health?.worst === "OK" ? colors.down : colors.up],
            ["回测区间跨度", bts.length ? `${(bts.map((b) => b.start_date).sort()[0] ?? "").slice(0, 7)} ~ ${(bts.map((b) => b.end_date).sort().reverse()[0] ?? "").slice(0, 7)}` : "—", colors.text],
            ["过拟合风险", `${pipeline.filter((p) => p.bt && (p.bt.trade_count ?? 0) < 20).length} 条样本偏薄`, "#b9770e"],
            ["成本假设", "双边 0.2%", colors.text],
          ] as [string, string, string][]).map(([k, v, tone]) => (
            <div key={k} style={{ border: `1px solid ${colors.border}`, borderRadius: 8, padding: "9px 11px" }}>
              <div style={{ fontSize: 10.5, color: colors.muted }}>{k}</div>
              <div style={{ fontSize: 13, fontWeight: 700, color: tone, marginTop: 2 }}>{v}</div>
            </div>
          ))}
        </div>
      </Card>

      {/* 研究管线 */}
      <Card title="研究管线" colors={colors} style={{ marginTop: 14 }}
            extra={<span style={{ fontSize: 11, color: colors.muted }}>
              亮 = 已走过 · 暗 = 没做
            </span>}>
        <div style={{ overflowX: "auto" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
            <thead>
              <tr style={{ color: colors.muted }}>
                <th style={{ textAlign: "left", padding: "6px 8px", fontWeight: 500, minWidth: 130 }}>策略</th>
                <th style={{ textAlign: "left", padding: "6px 8px", fontWeight: 500, width: 60 }}>状态</th>
                {STAGES.map((s) => (
                  <th key={s} style={{ textAlign: "center", padding: "6px 4px", fontWeight: 500, width: 52, fontSize: 10.5 }}>{s}</th>
                ))}
                <th style={{ textAlign: "right", padding: "6px 8px", fontWeight: 500, width: 84 }}>回测超额</th>
                <th style={{ textAlign: "left", padding: "6px 8px", fontWeight: 500, width: 100 }}>下一步</th>
              </tr>
            </thead>
            <tbody>
              {pipeline.map(({ c, s, bt, next }) => (
                <tr key={c.id} style={{ borderTop: `1px solid ${colors.border}` }}>
                  <td style={{ padding: "7px 8px" }}>
                    <div style={{ fontSize: 12.5 }}>{c.name}</div>
                    <div style={{ fontSize: 10.5, color: colors.muted }}>{c.strategy_key || "未封装"}</div>
                  </td>
                  <td style={{ padding: "7px 8px" }}>
                    <Badge text={c.state === "enabled" ? "启用" : c.state === "candidate" ? "候选" : "降级"}
                           color={c.state === "enabled" ? colors.down : c.state === "candidate" ? colors.accent : "#b9770e"} soft />
                  </td>
                  {STAGES.map((_, i) => {
                    const on = s.has(i + 1);
                    return (
                      <td key={i} style={{ padding: "7px 4px", textAlign: "center" }}>
                        <span style={{
                          display: "inline-block", width: 9, height: 9, borderRadius: 5,
                          background: on ? colors.accent : colors.border,
                        }} title={STAGES[i]} />
                      </td>
                    );
                  })}
                  <td style={{
                    padding: "7px 8px", textAlign: "right", fontWeight: 600,
                    color: bt?.excess_return == null ? colors.muted
                      : (bt.excess_return >= 0 ? colors.up : colors.down),
                  }}>
                    {bt?.excess_return == null ? "—"
                      : `${bt.excess_return >= 0 ? "+" : ""}${(bt.excess_return * 100).toFixed(2)}%`}
                  </td>
                  <td style={{ padding: "7px 8px", fontSize: 11.5, color: colors.muted }}>{next}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div style={{ marginTop: 8, fontSize: 11, color: colors.muted }}>
          「走到哪」用亮暗区分 —— 一眼看出谁卡在⑤（寻优没做）、谁卡在⑥（没模拟盘）。
        </div>
      </Card>

      <div style={{ marginTop: 12, fontSize: 11, color: colors.muted }}>
        刷新于 {new Date().toLocaleTimeString("zh-CN")} ·{" "}
        <a href="#cards" style={{ color: colors.accent }}>去策略库 →</a>
      </div>
    </div>
  );
}
