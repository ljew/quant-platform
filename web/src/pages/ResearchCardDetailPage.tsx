/**
 * 研究卡详情页（整页，非弹层）：一张卡的完整档案 + 信号与分析。
 * 布局：顶部 KPI 带；左栏卡片 8 字段与决策历史；主栏证据图表、绩效细分、
 * 信号日历（近 30 天，点日期看当日标的与判定动作）；底部人工迁移操作。
 */
import { useCallback, useEffect, useState } from "react";
import { Badge, Btn, Card, KpiCard } from "../components/ui";
import EChart from "../components/EChart";
import {
  ACTION_LABEL, actionBadge, EvidenceBars, MiniTable, Signed, STATE_META, TransitionBox,
} from "../components/cardShared";
import { CardSignalsResp, researchApi, ResearchCardItem, StatRow } from "../api/client";
import { useTheme, ThemeColors } from "../theme";

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

export default function ResearchCardDetailPage({ cardId, onBack }: { cardId: number; onBack: () => void }) {
  const { colors } = useTheme();
  const [card, setCard] = useState<ResearchCardItem | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setCard(await researchApi.card(cardId));
      setErr(null);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  }, [cardId]);

  useEffect(() => { load(); }, [load]);

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
    </div>
  );
}
