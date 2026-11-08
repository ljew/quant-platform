/**
 * 基金持续性：追冠军会不会被打脸。
 *
 * 三块内容，对应三个已经验证过的结论：
 *   1) 迁移矩阵 —— 上年各梯队第二年落到哪（Top5% 有 30% 概率掉进后 30%）
 *   2) 梯队 → 次年绝对超额 —— 均值和年际波动一起看，中游组波动最小
 *   3) 经理层 vs 基金层 —— 同一分组下经理层 t 值更低，说明业绩不完全跟人走
 *
 * 刻意不做「经理排行榜」：H1/H2 已证伪按排名筛人抓不到持续性，做成榜单只会被误用。
 * 口径：2015–2025 完整年度；显著性用年度等权序列（n≤10），不用 pooled 观测。
 */
import { useEffect, useMemo, useState } from "react";
import { Btn, Card, PageHeader } from "../components/ui";
import EChart from "../components/EChart";
import { get } from "../api/client";
import { useTheme } from "../theme";

interface Census {
  group: string; n_base: number; n_full10: number; ann_median: number;
  ann_p90: number; ann_p99: number; ann_max: number; n_ge20: number;
  n_ge15: number; pct_ge20: number; hist: number[];
}
interface Tier {
  group?: string; tier: string; label: string; mean: number; t: number;
  p?: number; sd: number; pos_years: number; n_years: number;
}
interface Transition {
  keys: string[]; labels: string[]; matrix: number[][];
  diag: Record<string, number>; top5_to_bot30: number; bot30_stay: number;
  counts: number[][];
}
interface YearRow { year: number; exc: number; n: number }
interface SizeBand {
  band: string; mean: number; t: number; p: number;
  pos_years: number; n_years: number; sd?: number;
}
interface SizeBlock {
  bands: SizeBand[];
  flow_bands: SizeBand[];
  flow_test: {
    top5_median: number; others_median: number; diff_pp: number;
    t: number; p: number; pos_years: number; n_years: number;
  } | null;
}
interface HoldAttr {
  yearly: { year: number; n_top: number; top_hold: number; rest_hold: number;
    top_fund: number; rest_fund: number; cover: number }[];
  hold_diff: number; hold_t: number; hold_pos: number;
  fund_diff: number; fund_t: number; fund_pos: number;
  gap: number; n_years: number; cover_median: number;
}
interface FreqRow {
  freq: string; tier: string; gross: number; cost: number; net: number;
  t_gross: number; t_net: number; n: number; pos: number;
}
interface FreqCost {
  rows: FreqRow[]; n_months: number; n_funds: number;
  cost_per_trade: number; note: string;
}
interface Payload {
  ok: boolean; error?: string; generated_at?: string; period?: string;
  census?: Census[]; tiers?: Tier[]; transition?: Transition;
  manager_tiers?: Tier[]; manager_n?: number; yearly?: Record<string, YearRow[]>;
  size?: SizeBlock; size_backtest?: Record<string, {
    ann: number; excess: number; t: number; p: number; pos: number; tot: number; avg_n: number;
  }>; hold_attr?: HoldAttr;
  hold_lag?: { diff: number; t: number; p: number; pos: number; n: number; years: string };
  freq_cost?: FreqCost;
  cached_at?: string;
}

const GRP_CN: Record<string, string> = { active: "主动权益", enh_quant: "指增 / 量化" };
const TIER_CN: Record<string, string> = {
  top5: "前5%", p5_10: "5-10%", p10_30: "10-30%",
  p30_50: "30-50%", p50_70: "50-70%", bot30: "后30%",
};

function Pct({ v, bold }: { v: number; bold?: boolean }) {
  const { colors } = useTheme();
  return (
    <span style={{ color: v >= 0 ? colors.up : colors.down, fontWeight: bold ? 700 : 600 }}>
      {v > 0 ? "+" : ""}{v.toFixed(2)}%
    </span>
  );
}

function Stat({ label, value, sub }: { label: string; value: string; sub?: string }) {
  const { colors } = useTheme();
  return (
    <div style={{ background: colors.card, border: `1px solid ${colors.border}`, borderRadius: 12, padding: "14px 16px" }}>
      <div style={{ fontSize: 12, color: colors.muted, marginBottom: 4 }}>{label}</div>
      <div style={{ fontSize: 23, fontWeight: 700, letterSpacing: -0.4, fontVariantNumeric: "tabular-nums" }}>{value}</div>
      {sub && <div style={{ fontSize: 11.5, color: colors.muted, marginTop: 3 }}>{sub}</div>}
    </div>
  );
}

export default function FundPersistencePage() {
  const { colors, mode } = useTheme();
  const [data, setData] = useState<Payload | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const load = () => {
    setBusy(true);
    setError("");
    get<Payload>("/fund/persistence")
      .then((d) => { if (!d.ok) setError(d.error || "接口返回异常"); else setData(d); })
      .catch((e) => setError((e as Error).message))
      .finally(() => setBusy(false));
  };
  useEffect(load, []);

  const act = data?.census?.find((c) => c.group === "active");
  const eq = data?.census?.find((c) => c.group === "enh_quant");
  const tiersA = (data?.tiers || []).filter((t) => t.group === "active");
  const tiersE = (data?.tiers || []).filter((t) => t.group === "enh_quant");
  const mgr = data?.manager_tiers || [];
  const sz = data?.size;
  const sbt = data?.size_backtest;
  const ha = data?.hold_attr;
  const hl = data?.hold_lag;
  const fq = data?.freq_cost;
  const bTiny = sz?.bands.find((b) => b.band === "<2亿");
  const bSweet = sz?.bands.find((b) => b.band === "2-10亿");
  const bBig = sz?.bands.find((b) => b.band === ">80亿");

  const heatOption = useMemo(() => {
    const tr = data?.transition;
    if (!tr) return null;
    const cells: [number, number, number][] = [];
    tr.matrix.forEach((row, i) => row.forEach((v, j) => cells.push([j, i, v])));
    return {
      grid: { left: 76, right: 26, top: 46, bottom: 66 },
      tooltip: {
        formatter: (p: { value: [number, number, number] }) =>
          `上年 ${tr.labels[p.value[1]]} → 次年 ${tr.labels[p.value[0]]}<br/>概率 <b>${p.value[2]}%</b>`,
      },
      xAxis: {
        type: "category", name: "次年", nameLocation: "middle", nameGap: 34,
        nameTextStyle: { fontSize: 11, color: colors.muted },
        data: tr.labels, splitArea: { show: true }, axisLabel: { fontSize: 11 },
      },
      yAxis: {
        type: "category", name: "上年", nameLocation: "middle", nameGap: 70, nameRotate: 90,
        nameTextStyle: { fontSize: 11, color: colors.muted },
        data: tr.labels, inverse: true, splitArea: { show: true }, axisLabel: { fontSize: 11 },
      },
      visualMap: {
        min: 0, max: 36, show: false,
        inRange: {
          color: mode === "dark"
            ? ["#1c2331", "#4a2622", "#8f332b", "#d4553f"]
            : ["#ffffff", "#fbdfd9", "#f0a496", "#c0392b"],
        },
      },
      series: [{
        type: "heatmap",
        data: cells,
        label: {
          show: true, fontSize: 11.5,
          formatter: (p: { value: [number, number, number] }) => p.value[2].toFixed(1),
          color: mode === "dark" ? "#e9edf6" : "#1f2533",
        },
        itemStyle: { borderColor: colors.card, borderWidth: 2 },
        emphasis: { itemStyle: { shadowBlur: 6, shadowColor: "rgba(0,0,0,.18)" } },
      }],
    } as never;
  }, [data, colors, mode]);

  const barOption = useMemo(() => {
    if (!tiersA.length) return null;
    return {
      grid: { left: 56, right: 24, top: 40, bottom: 46 },
      tooltip: {
        trigger: "axis",
        formatter: (ps: { name: string; value: number; dataIndex: number }[]) => {
          const i = ps[0].dataIndex;
          const t = tiersA[i];
          return `${t.label}<br/>年均超额 <b>${t.mean > 0 ? "+" : ""}${t.mean}%</b><br/>` +
            `t = ${t.t}（${t.pos_years}/${t.n_years} 年为正）<br/>年际波动 σ = ${t.sd}%`;
        },
      },
      xAxis: { type: "category", data: tiersA.map((t) => t.label), axisLabel: { fontSize: 11 } },
      yAxis: { type: "value", name: "%", nameTextStyle: { fontSize: 11 }, axisLabel: { fontSize: 11 } },
      series: [{
        type: "bar",
        barWidth: "46%",
        data: tiersA.map((t) => ({
          value: t.mean,
          itemStyle: { color: t.mean >= 0 ? colors.up : colors.down, opacity: 0.85, borderRadius: [3, 3, 0, 0] },
        })),
        label: {
          show: true, position: "top", fontSize: 11,
          formatter: (p: { value: number }) => `${p.value > 0 ? "+" : ""}${p.value}`,
        },
        markLine: {
          silent: true, symbol: "none",
          data: [{ yAxis: 0 }],
          lineStyle: { color: colors.axis, width: 1, type: "solid" },
          label: { show: false },
        },
      }],
    } as never;
  }, [tiersA, colors]);

  const yearOption = useMemo(() => {
    const rows = data?.yearly?.top5 || [];
    if (!rows.length) return null;
    return {
      grid: { left: 52, right: 24, top: 40, bottom: 42 },
      tooltip: {
        trigger: "axis",
        formatter: (ps: { value: number }[]) => `${ps[0].value > 0 ? "+" : ""}${ps[0].value}%`,
      },
      xAxis: { type: "category", data: rows.map((r) => r.year), axisLabel: { fontSize: 11 } },
      yAxis: { type: "value", name: "%", nameTextStyle: { fontSize: 11 }, axisLabel: { fontSize: 11 } },
      series: [{
        type: "bar", barWidth: "52%",
        data: rows.map((r) => ({
          value: r.exc,
          itemStyle: { color: r.exc >= 0 ? colors.up : colors.down, opacity: 0.85, borderRadius: [3, 3, 0, 0] },
        })),
        label: { show: true, position: "top", fontSize: 10.5, formatter: (p: { value: number }) => `${p.value > 0 ? "+" : ""}${Math.round(p.value)}` },
        markLine: { silent: true, symbol: "none", data: [{ yAxis: 0 }], lineStyle: { color: colors.axis, width: 1 }, label: { show: false } },
      }],
    } as never;
  }, [data, colors]);

  const th: React.CSSProperties = {
    textAlign: "right", padding: "8px 10px", fontSize: 11.5,
    color: colors.muted, fontWeight: 500, borderBottom: `1px solid ${colors.border}`,
  };
  const td: React.CSSProperties = { textAlign: "right", padding: "8px 10px", fontSize: 12.5, fontVariantNumeric: "tabular-nums" };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      <PageHeader
        title="基金持续性"
        desc="追冠军会不会被打脸 · 2015–2025 完整年度 · 显著性用年度等权序列（n≤10）"
        actions={<Btn small onClick={load} disabled={busy}>{busy ? "加载中" : "刷新"}</Btn>}
      />

      {error && (
        <div style={{ background: "#fdf2f2", border: `1px solid ${colors.up}`, color: colors.up, borderRadius: 10, padding: "12px 16px", fontSize: 13 }}>
          {error}
        </div>
      )}

      {data?.ok && (
        <>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 12 }}>
            <Stat label="主动权益 · 十年完整样本" value={`${act?.n_full10 ?? "—"} 只`} sub={`2015 年末已成立 ${act?.n_base ?? "—"} 只`} />
            <Stat label="其中十年年化 ≥20%" value={`${act?.n_ge20 ?? "—"} 只`} sub={`占 ${act?.pct_ge20 ?? 0}% · 最高 ${act?.ann_max ?? "—"}%`} />
            <Stat label="十年年化中位数" value={`${act?.ann_median ?? "—"}%`} sub={`P90 ${act?.ann_p90 ?? "—"}% · P99 ${act?.ann_p99 ?? "—"}%`} />
            <Stat label="经理（solo 在管）" value={`${data.manager_n ?? "—"} 位`} sub="用于经理层分组对照" />
          </div>

          <Card
            title="梯队迁移矩阵"
            colors={colors}
            extra={<span style={{ fontSize: 12, color: colors.muted }}>行 = 上年 · 列 = 次年 · 单位 %</span>}
          >
            <div style={{ fontSize: 12.5, color: colors.muted, marginBottom: 8 }}>
              Top5% 只有 <b style={{ color: colors.text }}>{data.transition?.diag?.top5}%</b> 能留在 Top5%，有{" "}
              <b style={{ color: colors.up }}>{data.transition?.top5_to_bot30}%</b> 掉进后 30%。
              而后 30% 的留存率高达 <b style={{ color: colors.text }}>{data.transition?.bot30_stay}%</b>——
              「烂的会继续烂」比「好的会继续好」更稳定。
            </div>
            {heatOption && <EChart option={heatOption} height={380} />}
          </Card>

          <Card
            title="各梯队 → 次年绝对超额"
            colors={colors}
            extra={<span style={{ fontSize: 12, color: colors.muted }}>相对次年全体中位数 · 年度均值</span>}
          >
            <div style={{ fontSize: 12.5, color: colors.muted, marginBottom: 8 }}>
              <b style={{ color: colors.text }}>注意读法：</b>不是看谁最高，是看 t 值和年际波动。
              Top5% 年均 {tiersA[0]?.mean ?? "—"}% 但 σ 高达 {tiersA[0]?.sd ?? "—"}%，t 只有 {tiersA[0]?.t ?? "—"}；
              30–50% 组只有 {tiersA.find((t) => t.tier === "p30_50")?.mean ?? "—"}%，σ 却只有{" "}
              {tiersA.find((t) => t.tier === "p30_50")?.sd ?? "—"}%，t 是全表唯一接近 2 的。
            </div>
            {barOption && <EChart option={barOption} height={300} />}
            <table style={{ width: "100%", borderCollapse: "collapse", marginTop: 10 }}>
              <thead>
                <tr>
                  <th style={{ ...th, textAlign: "left" }}>梯队</th>
                  <th style={th}>年均超额</th>
                  <th style={th}>年际 σ</th>
                  <th style={th}>t</th>
                  <th style={th}>为正年数</th>
                </tr>
              </thead>
              <tbody>
                {tiersA.map((t) => (
                  <tr key={t.tier} style={{ borderBottom: `1px solid ${colors.border}` }}>
                    <td style={{ ...td, textAlign: "left", fontWeight: 600 }}>{t.label}</td>
                    <td style={td}><Pct v={t.mean} bold={Math.abs(t.t) >= 2} /></td>
                    <td style={{ ...td, color: colors.muted }}>{t.sd.toFixed(2)}</td>
                    <td style={{ ...td, fontWeight: Math.abs(t.t) >= 2 ? 700 : 400, color: Math.abs(t.t) >= 2 ? colors.text : colors.muted }}>
                      {t.t > 0 ? "+" : ""}{t.t.toFixed(2)}
                    </td>
                    <td style={{ ...td, color: colors.muted }}>{t.pos_years}/{t.n_years}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Card>

          <Card
            title="经理层 vs 基金层"
            colors={colors}
            extra={<span style={{ fontSize: 12, color: colors.muted }}>同一分组方法 · 谁的预测力更强</span>}
          >
            <div style={{ fontSize: 12.5, color: colors.muted, marginBottom: 8 }}>
              同样分组下，经理层的 t 值普遍低于基金层。说明业绩里有一部分挂在「产品、平台、当期风格」上，
              并不跟着人走——<b style={{ color: colors.text }}>想预测明年，看基金比看人略稳一点</b>，但两者都没到显著。
            </div>
            <table style={{ width: "100%", borderCollapse: "collapse" }}>
              <thead>
                <tr>
                  <th style={{ ...th, textAlign: "left" }}>梯队</th>
                  <th style={th}>基金层 年均超额</th>
                  <th style={th}>基金层 t</th>
                  <th style={th}>经理层 年均超额</th>
                  <th style={th}>经理层 t</th>
                </tr>
              </thead>
              <tbody>
                {tiersA.map((t, i) => {
                  const m = mgr.find((x) => x.tier === t.tier);
                  return (
                    <tr key={t.tier} style={{ borderBottom: `1px solid ${colors.border}` }}>
                      <td style={{ ...td, textAlign: "left", fontWeight: 600 }}>{t.label}</td>
                      <td style={td}><Pct v={t.mean} /></td>
                      <td style={{ ...td, color: colors.muted }}>{t.t > 0 ? "+" : ""}{t.t.toFixed(2)}</td>
                      <td style={td}>{m ? <Pct v={m.mean} /> : "—"}</td>
                      <td style={{ ...td, color: colors.muted }}>{m ? `${m.t > 0 ? "+" : ""}${m.t.toFixed(2)}` : "—"}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </Card>

          <Card
            title="追冠军的分年度超额（波动有多大）"
            colors={colors}
            extra={<span style={{ fontSize: 12, color: colors.muted }}>Top5% 梯队 · 相对同年中位数</span>}
          >
            <div style={{ fontSize: 12.5, color: colors.muted, marginBottom: 8 }}>
              这张图解释为什么「按排名选人」不能做策略：超额方向在年与年之间反复翻转，
              2018→2019 一年摆到 <b style={{ color: colors.down }}>−20.7%</b>，2019→2020 又摆到{" "}
              <b style={{ color: colors.up }}>+23.5%</b>。买卖点无法从这种序列里稳定提取。
            </div>
            {yearOption && <EChart option={yearOption} height={260} />}
          </Card>

          {fq && fq.rows.length > 0 && (
            <Card
              title="换手频率 × 成本：同一信号，三种调仓周期"
              colors={colors}
              extra={<span style={{ fontSize: 12, color: colors.muted }}>日频净值 {fq.n_months} 个月 · {fq.n_funds} 只基金</span>}
            >
              <div style={{ fontSize: 12.5, color: colors.muted, marginBottom: 8 }}>
                同一排名信号，调仓越勤成本越吃收益。月度动量统计上最强（毛{" "}
                {fq.rows.find((r) => r.freq === "月度" && r.tier === "top5")?.gross}%、
                t={fq.rows.find((r) => r.freq === "月度" && r.tier === "top5")?.t_gross}），
                但一年换 12 次要付 {(fq.cost_per_trade * 12).toFixed(2)}%，净超额只剩{" "}
                {fq.rows.find((r) => r.freq === "月度" && r.tier === "top5")?.net}%。
                <b style={{ color: colors.text }}>
                  年度口径下，30–50% 组是唯一扣成本后 t 过 2 的
                </b>
                （净 {fq.rows.find((r) => r.freq === "年度" && r.tier === "p30_50")?.net}%、
                t={fq.rows.find((r) => r.freq === "年度" && r.tier === "p30_50")?.t_net}、
                {fq.rows.find((r) => r.freq === "年度" && r.tier === "p30_50")?.pos}/
                {fq.rows.find((r) => r.freq === "年度" && r.tier === "p30_50")?.n} 年为正）。
              </div>
              <table style={{ width: "100%", borderCollapse: "collapse" }}>
                <thead>
                  <tr>
                    <th style={{ ...th, textAlign: "left" }}>频率</th>
                    <th style={{ ...th, textAlign: "left" }}>梯队</th>
                    <th style={th}>毛超额</th>
                    <th style={th}>成本</th>
                    <th style={th}>净超额</th>
                    <th style={th}>t(净)</th>
                    <th style={th}>为正</th>
                  </tr>
                </thead>
                <tbody>
                  {["月度", "季度", "年度"].map((f) =>
                    fq.rows.filter((r) => r.freq === f).map((r, i) => (
                      <tr key={f + r.tier} style={{ borderBottom: `1px solid ${colors.border}` }}>
                        <td style={{ ...td, textAlign: "left", color: colors.muted }}>{i === 0 ? f : ""}</td>
                        <td style={{ ...td, textAlign: "left", fontWeight: 600 }}>{TIER_CN[r.tier] ?? r.tier}</td>
                        <td style={td}><Pct v={r.gross} /></td>
                        <td style={{ ...td, color: colors.muted }}>−{r.cost.toFixed(2)}</td>
                        <td style={td}><Pct v={r.net} bold={Math.abs(r.t_net) >= 2} /></td>
                        <td style={{ ...td, fontWeight: Math.abs(r.t_net) >= 2 ? 700 : 400,
                          color: Math.abs(r.t_net) >= 2 ? colors.text : colors.muted }}>
                          {r.t_net > 0 ? "+" : ""}{r.t_net.toFixed(2)}
                        </td>
                        <td style={{ ...td, color: colors.muted }}>{r.pos}/{r.n}</td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
              <p className="note" style={{ fontSize: 12, color: colors.muted, marginTop: 8, lineHeight: 1.7 }}>
                成本口径：单次申赎 {fq.cost_per_trade}%（申购 0.15% 打一折 + 赎回 0.5%），月度调仓年化 7.8%、季度 2.6%、年度 0.65%。
                超额均为「相对当期全体中位数」，未计申购限额与时滞。
              </p>
            </Card>
          )}

          <Card title="指增 / 量化：持续性更弱" colors={colors}>
            <div style={{ fontSize: 12.5, color: colors.muted, marginBottom: 8 }}>
              六个梯队全部不显著，Top5% 的 t 只有 {tiersE[0]?.t ?? "—"}。唯一有方向性的是后 30%
              （{tiersE.find((t) => t.tier === "bot30")?.mean ?? "—"}%，t={tiersE.find((t) => t.tier === "bot30")?.t ?? "—"}）——
              这类产品里「差的会继续差」比「好的会继续好」更可靠。
            </div>
            <table style={{ width: "100%", borderCollapse: "collapse" }}>
              <thead>
                <tr>
                  <th style={{ ...th, textAlign: "left" }}>梯队</th>
                  <th style={th}>年均超额</th>
                  <th style={th}>年际 σ</th>
                  <th style={th}>t</th>
                  <th style={th}>为正年数</th>
                </tr>
              </thead>
              <tbody>
                {tiersE.map((t) => (
                  <tr key={t.tier} style={{ borderBottom: `1px solid ${colors.border}` }}>
                    <td style={{ ...td, textAlign: "left", fontWeight: 600 }}>{t.label}</td>
                    <td style={td}><Pct v={t.mean} /></td>
                    <td style={{ ...td, color: colors.muted }}>{t.sd.toFixed(2)}</td>
                    <td style={{ ...td, color: colors.muted }}>{t.t > 0 ? "+" : ""}{t.t.toFixed(2)}</td>
                    <td style={{ ...td, color: colors.muted }}>{t.pos_years}/{t.n_years}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Card>

          {sz && sz.bands.length > 0 && (
            <Card
              title="规模：甜蜜区在 2–10 亿，不是越小越好"
              colors={colors}
              extra={<span style={{ fontSize: 12, color: colors.muted }}>按 t 年末规模分档 · 规模 = 份额 × 单位净值</span>}
            >
              <div style={{ fontSize: 12.5, color: colors.muted, marginBottom: 8 }}>
                2–10 亿组年均超额 <b style={{ color: colors.up }}>{bSweet?.mean ?? "—"}%</b>，
                t={bSweet?.t ?? "—"}、{bSweet?.n_years ? `${bSweet.pos_years}/${bSweet.n_years}` : "—"} 年为正——
                这是全篇唯一 t 过 4 的结论。规模过 80 亿转负（{bBig?.mean ?? "—"}%，
                {bBig?.n_years ? `${bBig.pos_years}/${bBig.n_years}` : "—"} 年为正）。
                <b style={{ color: colors.text }}>但不是「小就是好」：&lt;2 亿反而弱（{bTiny?.mean ?? "—"}%），
                  迷你基金有清盘与建仓期风险。</b>
              </div>
              <table style={{ width: "100%", borderCollapse: "collapse" }}>
                <thead>
                  <tr>
                    <th style={{ ...th, textAlign: "left" }}>规模档</th>
                    <th style={th}>年均超额</th>
                    <th style={th}>年际 σ</th>
                    <th style={th}>t</th>
                    <th style={th}>为正年数</th>
                  </tr>
                </thead>
                <tbody>
                  {sz.bands.map((b) => (
                    <tr key={b.band} style={{ borderBottom: `1px solid ${colors.border}` }}>
                      <td style={{ ...td, textAlign: "left", fontWeight: 600 }}>{b.band}</td>
                      <td style={td}><Pct v={b.mean} bold={Math.abs(b.t) >= 2} /></td>
                      <td style={{ ...td, color: colors.muted }}>{b.sd != null ? b.sd.toFixed(2) : "—"}</td>
                      <td style={{ ...td, fontWeight: Math.abs(b.t) >= 2 ? 700 : 400, color: Math.abs(b.t) >= 2 ? colors.text : colors.muted }}>
                        {b.t > 0 ? "+" : ""}{b.t.toFixed(2)}
                      </td>
                      <td style={{ ...td, color: colors.muted }}>{b.pos_years}/{b.n_years}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {sz.flow_test && (
                <div style={{ marginTop: 10, padding: "10px 12px", background: colors.tableStripe, borderRadius: 8, fontSize: 12.5, lineHeight: 1.7 }}>
                  <b style={{ color: colors.text }}>机制修正：</b>冠军次年份额增速中位 {sz.flow_test.top5_median}%，
                  其余 {sz.flow_test.others_median}%（差值 +{sz.flow_test.diff_pp}pp，t={sz.flow_test.t}）——
                  <b style={{ color: colors.text }}>冠军只是「赎回得少」，并没有出现大量申购</b>。
                  所以「申购冲击导致超额衰减」在 A 股不成立，&gt;80 亿的拖累来自存量规模本身。
                </div>
              )}
              {sbt && sbt["2-10亿"] && sbt[">80亿"] && (
                <div style={{ marginTop: 10, padding: "10px 12px", border: `1px solid ${colors.up}33`, borderLeft: `3px solid ${colors.up}`, borderRadius: 8, fontSize: 12.5, lineHeight: 1.75 }}>
                  <b style={{ color: colors.up }}>但扣成本后就没了。</b>
                  全买 2–10 亿（年均 {sbt["2-10亿"].avg_n} 只）年化 {sbt["2-10亿"].ann}%，
                  超额只剩 <b>+{sbt["2-10亿"].excess}%</b>（t={sbt["2-10亿"].t}，{sbt["2-10亿"].pos}/{sbt["2-10亿"].tot} 年为正）——
                  0.65% 的申赎成本吃掉了大部分。
                  真正能用的是反面：<b>回避 &gt;80 亿</b>，年化 {sbt[">80亿"].ann}%，超额{" "}
                  <b>{sbt[">80亿"].excess}%</b>（t={sbt[">80亿"].t}，只有 {sbt[">80亿"].pos}/{sbt[">80亿"].tot} 年为正）。
                  <b style={{ color: colors.text }}>又一次印证：负面清单比正面名单可靠。</b>
                </div>
              )}
            </Card>
          )}

          {ha && ha.yearly.length > 0 && (
            <Card
              title="持仓归因：超额来自选股，还是交易"
              colors={colors}
              extra={<span style={{ fontSize: 12, color: colors.muted }}>披露持仓加权 · 占净值比中位 {ha.cover_median}%</span>}
            >
              <div style={{ fontSize: 12.5, color: colors.muted, marginBottom: 8 }}>
                上年 Top5% 的<b style={{ color: colors.text }}>持仓组合</b>次年跑赢全池持仓{" "}
                <b style={{ color: colors.up }}>{ha.hold_diff > 0 ? "+" : ""}{ha.hold_diff}%</b>（t={ha.hold_t}，{ha.hold_pos}/{ha.n_years} 年为正）；
                但同一批基金的<b style={{ color: colors.text }}>实际净值</b>只跑赢{" "}
                <b style={{ color: colors.up }}>+{ha.fund_diff}%</b>（t={ha.fund_t}，{ha.fund_pos}/{ha.n_years}）。
                差额 <b style={{ color: colors.down }}>{ha.gap}%</b> 是<b style={{ color: colors.text }}>仓位、交易与择时的合计损耗</b>。
              </div>
              <table style={{ width: "100%", borderCollapse: "collapse" }}>
                <thead>
                  <tr>
                    <th style={{ ...th, textAlign: "left" }}>次年</th>
                    <th style={th}>样本</th>
                    <th style={th}>持仓组合差</th>
                    <th style={th}>实际净值差</th>
                    <th style={th}>差额</th>
                  </tr>
                </thead>
                <tbody>
                  {ha.yearly.map((r) => {
                    const gap = r.top_fund - r.rest_fund - (r.top_hold - r.rest_hold);
                    return (
                      <tr key={r.year} style={{ borderBottom: `1px solid ${colors.border}` }}>
                        <td style={{ ...td, textAlign: "left", fontWeight: 600 }}>{r.year}</td>
                        <td style={{ ...td, color: colors.muted }}>{r.n_top}</td>
                        <td style={td}><Pct v={r.top_hold - r.rest_hold} /></td>
                        <td style={td}><Pct v={r.top_fund - r.rest_fund} /></td>
                        <td style={td}><Pct v={gap} /></td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
              <div style={{ marginTop: 10, padding: "10px 12px", background: colors.tableStripe, borderRadius: 8, fontSize: 12.5, lineHeight: 1.7 }}>
                <b style={{ color: colors.text }}>怎么读：</b>持仓层面的优势（t={ha.hold_t}）比实际兑现的（t={ha.fund_t}）更强也更稳，
                说明「选出来的股票确实更好，但没有完整兑现」。最典型是 2019 年：持仓组合只跑输 1.4%，
                实际净值却跑输 19.9%——<b style={{ color: colors.text }}>差在仓位上，不是选股上</b>。
                这也解释了为什么按排名选基抓不住超额——<b style={{ color: colors.text }}>能力在持仓里，损耗在仓位上。</b>
                {hl && (
                  <>
                    <br />
                    <b style={{ color: colors.text }}>去掉前视再看一遍：</b>年报要次年 3 月底才披露，上面的算法用了 1–3 月的收益。
                    改成「次年 4/15 建仓」后，Top5% 持仓组合仍跑赢{" "}
                    <b style={{ color: colors.up }}>+{hl.diff}%</b>（t={hl.t}，{hl.pos}/{hl.n} 年为正，{hl.years}）——
                    <b style={{ color: colors.text }}>只掉了 0.66pp，前视偏差不大，但样本少一年、仍不显著。</b>
                  </>
                )}
              </div>
            </Card>
          )}

          <div style={{ fontSize: 11.5, color: colors.muted, lineHeight: 1.8, padding: "4px 2px" }}>
            口径：tushare <code>adj_nav</code> 复权净值 · 场外 · 份额去重 · 含清盘基金 · 已剔除纯被动指数 ·
            分组按上年收益分位 · 超额 = 次年收益 − 次年全体中位数 · t 基于年度序列（10 个观测）。
            <br />
            统计功效：10 年样本能可靠否定「明显有效」，不足以断定「一定无效」。
            缓存生成于 {data.generated_at}。
          </div>
        </>
      )}
    </div>
  );
}
