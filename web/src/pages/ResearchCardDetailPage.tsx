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

      {/* —— 回测 / 验证方式 ——
          老刘 2026-11-09：「点进去也看不到具体回测的情况」。
          策略卡关联 registry 里同名策略的最近一次真实回测；
          因子卡本来就没有回测，这里正面说清它靠什么验证，而不是留空白。 */}
      {isFactor ? (
        <Card title="这张卡怎么验证（没有「回测」）" colors={colors}>
          <div style={{ fontSize: 12.5, lineHeight: 1.85, color: colors.text }}>
            因子卡验证的是<b>信号的预测力</b>，不是「能不能赚钱」：下面三根柱子是同一信号在
            240 / 120 / 90 日窗口的平均超额，t 值是「这不是运气」的可信度。
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
    </div>
  );
}
