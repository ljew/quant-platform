import { useCallback, useEffect, useMemo, useState } from "react";
import type { EChartsOption } from "echarts";
import EChart from "../components/EChart";
import { useTheme } from "../theme";
import {
  etfFlowApi,
  type EtfFlowDashboard,
  type EtfFlowStatus,
} from "../api/client";

/**
 * 资金流（国家队 ETF 资金看板 · 观察口径）。
 *
 * ## 这个页面和 NSI 信号的关系
 *
 * 平台里已经有 NSI（`docs/NSI-SPEC.md`）——用 5 只核心 ETF 份额算的国家队信号，
 * T+1 开盘执行，由 national_team 策略消费。本页面**不是**信号，只做三件事：
 *
 * 1. 把覆盖面从 5 只扩到 20 只、归成 12 类宽基 → 看护盘重心在哪一类
 * 2. 把粒度压到 1 分钟 → 补 NSI 承认的盲区（盘中痕迹看不见）
 * 3. 把上面两块可视化，并把 NSI 的当日读数并排展示，方便对照
 *
 * **⚠️ 页面上的「盘中量能」不得用于决策**：NSI 规格书 §9.3 实测过「用 ETF 成交量
 * 替代份额，IC 训练 −0.19 / 验证 +0.13，方向整个翻转」。这里的分钟图只是让人看
 * 「今天资金活跃度是否异常」，真正的仓位依据在顶部那张 NSI 卡。
 */

/** hex → "r,g,b"，供热力色阶拼 rgba 用。 */
function rgb(hex: string): string {
  const h = hex.replace("#", "");
  const n = parseInt(h.length === 3 ? h.split("").map((c) => c + c).join("") : h, 16);
  return `${(n >> 16) & 255},${(n >> 8) & 255},${n & 255}`;
}

/** hex + 透明度 → 合法 rgba 字符串。直接拼 hex 后缀会得到 "36,86,20033"，Canvas 无法解析。 */
const rgba = (hex: string, a: number) => `rgba(${rgb(hex)},${a})`;

const SECTIONS = [
  { id: "sec-minute", label: "① 盘中分钟观测" },
  { id: "sec-support", label: "② 托底信号" },
  { id: "sec-daily", label: "③ 日度资金全景" },
];

const jumpTo = (id: string) => {
  const el = document.getElementById(id);
  if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
};

const f1 = (v: number | null | undefined) => (v == null || isNaN(v) ? "—" : v.toFixed(1));
const f2 = (v: number | null | undefined) => (v == null || isNaN(v) ? "—" : v.toFixed(2));
const sgn = (v: number) => (v > 0 ? "+" : "");

export default function EtfFlowPage() {
  const { colors, mode } = useTheme();
  const [data, setData] = useState<EtfFlowDashboard | null>(null);
  const [status, setStatus] = useState<EtfFlowStatus | null>(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState("");
  const [auto, setAuto] = useState(true);
  const [updatedAt, setUpdatedAt] = useState("");
  const [note, setNote] = useState("");

  const load = useCallback(async () => {
    try {
      const d = await etfFlowApi.dashboard();
      setData(d);
      setErr("");
      setUpdatedAt(new Date().toLocaleTimeString("zh-CN", { hour12: false }));
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
    try {
      setStatus(await etfFlowApi.status());
    } catch {
      /* 状态接口失败不影响主视图 */
    }
  }, []);

  useEffect(() => {
    load();
    if (!auto) return;
    const t = setInterval(load, 60_000);
    return () => clearInterval(t);
  }, [load, auto]);

  const doAction = async (kind: "update" | "bootstrap" | "sample" | "minutes") => {
    setBusy(kind);
    try {
      if (kind === "update") await etfFlowApi.update();
      else if (kind === "bootstrap") await etfFlowApi.bootstrap(false);
      else if (kind === "minutes") {
        const r = await etfFlowApi.minutes();
        setNote(r.message || "");
        setTimeout(() => setNote(""), 8000);
      } else await etfFlowApi.sample();
      setTimeout(load, 2500);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setTimeout(() => setBusy(""), 2500);
    }
  };

  // ── 空库引导 ──────────────────────────────────────────────
  if (!data && !err) {
    return (
      <div style={{ padding: 40, textAlign: "center", color: colors.muted, fontSize: 13 }}>
        正在加载资金流数据…
      </div>
    );
  }
  if (!data) {
    return (
      <div style={{ padding: 40, color: colors.muted, fontSize: 13 }}>
        <div style={{ color: colors.up, fontWeight: 600, marginBottom: 8 }}>资金流数据加载失败</div>
        <div style={{ marginBottom: 12 }}>{err}</div>
        <button onClick={() => load()} style={btnStyle(colors.accent)}>重试</button>
      </div>
    );
  }

  const { meta, nsi, minute, support, daily, category, intraday_15m, summary, etf_detail, focus, valuation } = data;
  const empty = meta.universe.length === 0;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      {/* ══ 标题 + 锚点导航 ══ */}
      <div style={{ display: "flex", alignItems: "flex-end", gap: 16, flexWrap: "wrap" }}>
        <div>
          <div style={{ fontSize: 17.5, fontWeight: 700, color: colors.text, letterSpacing: -0.2 }}>
            20 只重点 ETF 资金观测
          </div>
          <div style={{ fontSize: 12, color: colors.muted, marginTop: 3 }}>
            {meta.as_of} · {meta.universe.length} 只宽基 ETF · 日度份额口径 + 分钟量能
          </div>
        </div>
        <div style={{ marginLeft: "auto", display: "flex", gap: 6, flexWrap: "wrap" }}>
          {SECTIONS.map((x) => (
            <button key={x.id} onClick={() => jumpTo(x.id)} style={{
              background: "transparent", border: `1px solid ${colors.border}`,
              color: colors.muted, borderRadius: 6, padding: "5px 10px",
              fontSize: 12, cursor: "pointer", whiteSpace: "nowrap",
            }}>{x.label}</button>
          ))}
        </div>
      </div>

      {/* ══ 状态栏 ══ */}
      <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap", fontSize: 12, color: colors.muted }}>
        <Pill colors={colors} tone={meta.live ? "live" : "idle"}>
          {meta.live
            ? `盘中 ${meta.last_minute}`
            : meta.is_today && meta.bars_source === "sampled"
              ? "今日收盘 · 实时采样"
              : meta.is_today && meta.bars_source === "none"
                ? "今日 · 分钟线待补"
                : `数据日 ${meta.as_of || "—"}`}
        </Pill>
        {!meta.is_today && !meta.live && (
          <Pill colors={colors} tone="warn">最近交易日，非当日</Pill>
        )}
        {meta.is_today && meta.bars_source === "sampled" && !meta.live && (
          <Pill colors={colors} tone="warn">
            官方分钟线未发布 · 仅 {meta.sampled_from} 起 {meta.sampled_count} 个采样点
          </Pill>
        )}
        {meta.is_today && meta.bars_source === "none" && (
          <Pill colors={colors} tone="warn">今日分钟线尚未取到 · 点右侧拉取</Pill>
        )}
        <Pill colors={colors} tone="note">观察口径 · 不构成交易信号</Pill>
        <span style={{ fontVariantNumeric: "tabular-nums" }}>
          更新于 {updatedAt || meta.generated_at}
        </span>
        {note && <span style={{ color: colors.accent }}>{note}</span>}
        <div style={{ marginLeft: "auto", display: "flex", gap: 8 }}>
          <button onClick={() => setAuto((a) => !a)} style={btnStyle(colors.border, colors.text)}>
            {auto ? "暂停自动刷新" : "开启自动刷新"}
          </button>
          <button onClick={() => doAction("sample")} disabled={!!busy} style={btnStyle(colors.border, colors.text)}>
            {busy === "sample" ? "采样中…" : "采一次"}
          </button>
          {/* 只在这一屏确实是「采样碎片」时才出现：收盘后官方分钟线还没发布时，
              点一下就能立刻重试，不用等调度器那 10 分钟一轮 */}
          {meta.is_today && (meta.bars_source === "sampled" || meta.bars_source === "none") && (
            <button onClick={() => doAction("minutes")} disabled={!!busy} style={btnStyle(colors.border, colors.text)}>
              {busy === "minutes" ? "拉取中…" : "拉官方分钟线"}
            </button>
          )}
          <button onClick={() => doAction("update")} disabled={!!busy} style={btnStyle(colors.border, colors.text)}>
            {busy === "update" ? "更新中…" : "更新数据"}
          </button>
          <button onClick={() => load()} style={btnStyle(colors.accent)}>立即刷新</button>
        </div>
      </div>

      {/* ══ 最新解读（置顶） ══ */}
      <SummaryCard summary={summary} colors={colors} />

      {empty && (
        <div style={{ ...cardStyle(colors), color: colors.muted, fontSize: 13 }}>
          样本池为空。点击「更新数据」触发首次全量拉取（约 84 次接口调用、40 秒），
          完成后刷新本页即可。
          {status && <span style={{ marginLeft: 8 }}>当前：{status.universe} 只</span>}
        </div>
      )}

      {/* ═══ 一、NSI 信号（复用既有信号，非本页面计算） ═══ */}
      {nsi.available && <NsiCard nsi={nsi} colors={colors} mode={mode} />}

      {/* ═══ 二、盘中分钟观测 ═══ */}
      <SectionTitle
        id="sec-minute" num="02" title="盘中分钟观测"
        sub="20 只 ETF 每分钟成交额加总，与上证综指同屏对照 · 仅观察"
        colors={colors}
      />
      <MinuteCard meta={meta} minute={minute} colors={colors} mode={mode} />
      <CumCard meta={meta} minute={minute} colors={colors} mode={mode} />
      <TailCard tail={minute.tail} asOf={meta.as_of} colors={colors} />
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 14 }}>
        <SlotCard
          title="历史放量日 · 15 分钟形态"
          tag={`${intraday_15m.surge_samples} 个样本日均值`}
          labels={intraday_15m.labels}
          amount={intraday_15m.surge_amount}
          chg={intraday_15m.surge_chg}
          hot
          colors={colors}
          mode={mode}
          note="放量通常集中在哪几个时段，当时指数走到了哪里。尾盘段明显抬升是典型的国家队节奏。"
        />
        <SlotCard
          title="常态交易日 · 15 分钟形态"
          tag={`${intraday_15m.normal_samples} 个样本日均值`}
          labels={intraday_15m.labels}
          amount={intraday_15m.normal_amount}
          chg={intraday_15m.normal_chg}
          colors={colors}
          mode={mode}
          note="对照组：常态日是标准 U 型——开盘与收盘前半小时最密，中间清淡。偏离这个形状本身就是信号。"
        />
      </div>

      <EtfDetailTable detail={etf_detail} focus={focus} asOf={meta.as_of} colors={colors} />

      {/* ═══ 三、ETF 托底信号 ═══ */}
      <SectionTitle
        id="sec-support" num="03" title="ETF 托底信号"
        sub={`单日净流入 = Σ(份额变化 × 单位净值) · ${support.sample_count} 个历史样本`}
        colors={colors}
      />
      <SupportCard support={support} meta={meta} colors={colors} mode={mode} />
      <ForwardCard forward={support.forward} colors={colors} />
      <ValuationCard v={valuation} colors={colors} />

      {/* ═══ 四、日度资金全景 ═══ */}
      <SectionTitle
        id="sec-daily" num="04" title="日度资金全景"
        sub={`2023 年至今 · ${daily.dates.length} 个交易日`}
        colors={colors}
      />
      <DailyCard daily={daily} colors={colors} mode={mode} />
      <CategoryCard category={category} colors={colors} />

      {/* ═══ 口径说明 ═══ */}
      <div style={{ ...cardStyle(colors), fontSize: 12, color: colors.muted, lineHeight: 1.8 }}>
        <div style={{ fontWeight: 600, color: colors.text, marginBottom: 6 }}>口径与边界</div>
        <div>
          · <b>净流入</b> = Σ(当日份额 − 前日份额) × 单位净值。份额单位万份、净值取基金公告口径。
          <b>份额折算日</b>（份额与净值同时大幅反向变动、两者乘积≈1）单独识别并跳过 —— 否则
          一次 1:2 合并会被算成几百亿的假赎回。
        </div>
        <div>
          · <b>盘中量能</b>：tushare 的 1 分钟线盘中取不到，用 <code>realtime_quote</code> 的累计
          成交额逐分钟差分；收盘后由当日完整 1 分钟线覆盖。此口径已被实测证伪为信号（见
          NSI 规格书 §9.3），<b>仅用于观察</b>。
        </div>
        <div>
          · <b>与 NSI 的分工</b>：NSI 是 5 只核心 ETF 的份额信号（T+1 执行），本页面是 20 只 /
          12 类宽基的观察视图。两者数据同源、口径不同，<b>不可互相替代</b>。
        </div>
        <div>
          · <b>为什么用份额而不是持仓</b>：2026 年起 ETF 前十大持有人数据不再对外披露，
          季报/年报的持仓路径已经断了。<b>每日份额变化是目前唯一能当天（T+1）看到国家队足迹的公开口径</b>，
          也是 2024 年以来各类测算净买入额的通用算法（Δ份额 × 区间平均净值）的日频化版本。
        </div>
        <div>
          · <b>托底之后</b>是同一指数口径的<b>事后统计</b>，不是可交易回测：未扣成本与滑点、未做样本外，
          连续托底日的持有期还会互相重叠（样本不独立）。它只用来校准预期，不构成收益承诺；
          仓位依据仍以顶部 NSI 为准。
        </div>
        <div>
          · <b>尾盘集中度</b> = 14:31–15:00 成交额 ÷ 全天成交额，与过去 20 个交易日均值对比。
          这是「当日净流入」公布之前唯一可用的前置指标，但同样只反映量能，<b>不区分买卖方向</b>。
        </div>
        <div>
          · <b>估值序列</b>来自 tushare <code>daily_basic</code> 的逐股 PE/PB，按日聚合成全市场中位数
          （源表 900 万行 → 聚合表 1900 行），分位取<strong>滚动 5 年</strong>窗口。每日 19:30 后由数据调度器
          自动补齐缺口；源表落后时看板会显示旧分位，<strong>不会报错</strong>，所以看卡片右上角的日期。
        </div>
        <div>
          · 样本池 {meta.universe.length} 只，按类别内「规模排名 × 0.6 + 成交额排名 × 0.4」自动筛选；
          更新于 {meta.generated_at}
          {status && ` · 库内 ${status.share_rows.toLocaleString()} 条份额 / ${status.minute_rows.toLocaleString()} 条分钟`}
        </div>
      </div>
    </div>
  );
}

/* ══════════════════════════════════════════════════════════════════
   部件
   ══════════════════════════════════════════════════════════════════ */

function btnStyle(bg: string, fg = "#fff"): React.CSSProperties {
  return {
    background: bg, color: fg, border: "none", borderRadius: 7,
    padding: "6px 13px", fontSize: 12.5, cursor: "pointer", fontWeight: 500,
  };
}

function cardStyle(colors: ReturnType<typeof useTheme>["colors"]): React.CSSProperties {
  return {
    background: colors.card, border: `1px solid ${colors.border}`,
    borderRadius: 10, padding: "14px 16px",
  };
}

function Pill({
  children, colors, tone,
}: {
  children: React.ReactNode;
  colors: ReturnType<typeof useTheme>["colors"];
  tone: "live" | "idle" | "warn" | "note";
}) {
  const map = {
    live: { bg: `${rgba(colors.up,0.10)}`, fg: colors.up, bd: `${rgba(colors.up,0.25)}` },
    idle: { bg: colors.tableStripe, fg: colors.muted, bd: colors.border },
    warn: { bg: "#b9770e1a", fg: "#b9770e", bd: "#b9770e40" },
    note: { bg: `${rgba(colors.accent,0.07)}`, fg: colors.accent, bd: `${rgba(colors.accent,0.20)}` },
  }[tone];
  return (
    <span style={{
      background: map.bg, color: map.fg, border: `1px solid ${map.bd}`,
      borderRadius: 6, padding: "2px 8px", fontSize: 11.5, fontWeight: 500,
      fontVariantNumeric: "tabular-nums", whiteSpace: "nowrap",
    }}>{children}</span>
  );
}

function SectionTitle({
  num, title, sub, colors, id,
}: {
  num: string; title: string; sub: string;
  colors: ReturnType<typeof useTheme>["colors"];
  id?: string;
}) {
  return (
    <div id={id} style={{
      display: "flex", alignItems: "baseline", gap: 10,
      borderBottom: `1px solid ${colors.border}`, paddingBottom: 7, marginTop: 6,
    }}>
      <span style={{
        fontSize: 10.5, fontWeight: 700, color: colors.accent,
        background: `${rgba(colors.accent,0.08)}`, borderRadius: 4, padding: "1px 6px",
      }}>{num}</span>
      <span style={{ fontSize: 14.5, fontWeight: 700, color: colors.text }}>{title}</span>
      <span style={{ fontSize: 12, color: colors.muted }}>{sub}</span>
    </div>
  );
}

function KPI({
  label, value, unit, sub, tone, colors,
}: {
  label: string; value: string; unit?: string; sub?: React.ReactNode;
  tone?: "up" | "down" | "plain";
  colors: ReturnType<typeof useTheme>["colors"];
}) {
  const color = tone === "up" ? colors.up : tone === "down" ? colors.down : colors.text;
  return (
    <div style={{ padding: "0 14px", borderLeft: `1px solid ${colors.border}`, flex: 1, minWidth: 0 }}>
      <div style={{ fontSize: 11.5, color: colors.muted, marginBottom: 3, whiteSpace: "nowrap" }}>{label}</div>
      <div style={{
        fontSize: 20, fontWeight: 700, color, fontVariantNumeric: "tabular-nums",
        lineHeight: 1.2, whiteSpace: "nowrap",
      }}>
        {value}{unit && <span style={{ fontSize: 12, fontWeight: 500, color: colors.muted, marginLeft: 2 }}>{unit}</span>}
      </div>
      {sub && <div style={{ fontSize: 11.5, color: colors.muted, marginTop: 3 }}>{sub}</div>}
    </div>
  );
}

/* ── 置顶：最新解读 ─────────────────────────────────────────────── */
function SummaryCard({
  summary, colors,
}: {
  summary: EtfFlowDashboard["summary"] | undefined;
  colors: ReturnType<typeof useTheme>["colors"];
}) {
  if (!summary) return null;
  const tone = {
    up: { fg: colors.up, bd: rgba(colors.up, 0.35), bg: rgba(colors.up, 0.045) },
    down: { fg: colors.down, bd: rgba(colors.down, 0.35), bg: rgba(colors.down, 0.045) },
    warn: { fg: "#b9770e", bd: "#b9770e55", bg: "#b9770e0c" },
    calm: { fg: colors.muted, bd: colors.border, bg: colors.tableStripe },
  }[summary.tone] ?? { fg: colors.muted, bd: colors.border, bg: colors.tableStripe };

  const tcol = (t: string) => (t === "up" ? colors.up : t === "down" ? colors.down : colors.text);

  return (
    <div style={{
      background: tone.bg, border: `1px solid ${tone.bd}`,
      borderRadius: 11, padding: "15px 18px 14px",
    }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 9 }}>
        <span style={{
          fontSize: 10.5, fontWeight: 700, color: tone.fg,
          background: rgba(tone.fg === colors.muted ? colors.border : tone.fg, 0.13),
          borderRadius: 4, padding: "2px 7px", letterSpacing: 0.3,
        }}>最新解读</span>
        <span style={{ fontSize: 11.5, color: colors.muted, fontVariantNumeric: "tabular-nums" }}>
          {summary.as_of}
          {summary.live ? " · 盘中实时" : " · 收盘数据"}
        </span>
      </div>

      <div style={{ fontSize: 18.5, fontWeight: 700, color: tone.fg, lineHeight: 1.45, marginBottom: 13 }}>
        {summary.headline}
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 10, marginBottom: 13 }}>
        {summary.metrics.map((m) => (
          <div key={m.label} style={{
            background: colors.card, border: `1px solid ${colors.border}`,
            borderRadius: 8, padding: "9px 12px", minWidth: 0,
          }}>
            <div style={{ fontSize: 11, color: colors.muted, marginBottom: 2, whiteSpace: "nowrap" }}>{m.label}</div>
            <div style={{
              fontSize: 19, fontWeight: 700, color: tcol(m.tone),
              fontVariantNumeric: "tabular-nums", lineHeight: 1.2, whiteSpace: "nowrap",
            }}>
              {m.value}
              {m.unit && <span style={{ fontSize: 11.5, fontWeight: 500, color: colors.muted, marginLeft: 2 }}>{m.unit}</span>}
            </div>
            <div style={{
              fontSize: 11, color: colors.muted, marginTop: 3,
              overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
            }}>{m.sub}</div>
          </div>
        ))}
      </div>

      <div style={{ display: "flex", flexDirection: "column", gap: 7 }}>
        {summary.points.map((pt) => (
          <div key={pt.tag} style={{ display: "flex", gap: 9, alignItems: "flex-start" }}>
            <span style={{
              flex: "none", fontSize: 10.5, fontWeight: 600, color: tone.fg,
              background: rgba(tone.fg === colors.muted ? colors.border : tone.fg, 0.12),
              borderRadius: 3, padding: "1px 6px", marginTop: 2,
            }}>{pt.tag}</span>
            <span style={{ fontSize: 12.5, lineHeight: 1.75, color: colors.text }}>{pt.text}</span>
          </div>
        ))}
      </div>

      <div style={{
        fontSize: 11, color: colors.muted, marginTop: 10, paddingTop: 8,
        borderTop: `1px dashed ${colors.border}`, lineHeight: 1.65,
      }}>{summary.caveat}</div>
    </div>
  );
}

/* ── 单只 ETF 当日表现 ──────────────────────────────────────────── */
function EtfDetailTable({
  detail, focus, asOf, colors,
}: {
  detail: EtfFlowDashboard["etf_detail"] | undefined;
  focus: EtfFlowDashboard["focus"] | undefined;
  asOf: string;
  colors: ReturnType<typeof useTheme>["colors"];
}) {
  const rows = detail ?? [];
  if (!rows.length) return null;
  return (
    <div style={cardStyle(colors)}>
      <CardHead
        title="单只 ETF 当日表现"
        tag={`${asOf} · 按成交额降序`}
        colors={colors}
      />

      {focus && focus.today.length > 0 && (
        <div style={{
          display: "flex", gap: 16, flexWrap: "wrap", alignItems: "baseline",
          fontSize: 12, color: colors.muted, margin: "2px 0 8px",
          paddingBottom: 8, borderBottom: `1px solid ${colors.border}`,
        }}>
          <span>
            今日焦点 <b style={{ color: colors.text }}>{focus.today[0].name}</b>
            <span style={{ color: colors.axis }}> {focus.today[0].amount} 亿 · {focus.today[0].share}%</span>
          </span>
          <span>
            {focus.top1_changed
              ? `较 ${focus.prev_date.slice(5)} 换标的（原为 ${focus.prev[0]?.name ?? "—"}）`
              : "与上一交易日同一焦点"}
          </span>
          <span>
            top3 重合 <b style={{ color: focus.overlap >= 2 ? colors.text : colors.down }}>
              {focus.overlap}/3
            </b>
          </span>
          <span>
            集中度 HHI <b style={{ color: colors.text }}>{focus.hhi == null ? "—" : focus.hhi.toFixed(3)}</b>
            {focus.hhi_prev != null && (
              <span style={{ color: colors.axis }}>（前日 {focus.hhi_prev.toFixed(3)}）</span>
            )}
          </span>
        </div>
      )}

      <div style={{ overflowX: "auto", marginTop: 8 }}>
        <table style={{ borderCollapse: "collapse", width: "100%", fontSize: 12 }}>
          <thead>
            <tr style={{ color: colors.axis, fontSize: 10.5 }}>
              <th style={{ textAlign: "left", padding: "0 8px 6px 2px", fontWeight: 500, width: 84 }}>代码</th>
              <th style={{ textAlign: "left", padding: "0 8px 6px", fontWeight: 500 }}>名称</th>
              <th style={{ textAlign: "left", padding: "0 8px 6px", fontWeight: 500, width: 78 }}>跟踪指数</th>
              <th style={{ textAlign: "right", padding: "0 8px 6px", fontWeight: 500, width: 92 }}>成交额(亿)</th>
              <th style={{ textAlign: "left", padding: "0 8px 6px", fontWeight: 500, width: 150 }}>占比</th>
              <th style={{ textAlign: "right", padding: "0 2px 6px 8px", fontWeight: 500, width: 78 }}>涨跌幅</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((d) => {
              const chg = d.pct_chg;
              const c = chg == null ? colors.muted : chg > 0 ? colors.up : chg < 0 ? colors.down : colors.muted;
              return (
                <tr key={d.symbol} style={{ borderTop: `1px solid ${colors.border}` }}>
                  <td style={{ padding: "6px 8px 6px 2px", color: colors.muted, fontVariantNumeric: "tabular-nums" }}>{d.symbol}</td>
                  <td style={{ padding: "6px 8px", color: colors.text }}>{d.name}</td>
                  <td style={{ padding: "6px 8px", color: colors.muted }}>{d.category}</td>
                  <td style={{ padding: "6px 8px", textAlign: "right", fontWeight: 600, fontVariantNumeric: "tabular-nums", color: colors.text }}>
                    {d.amount.toFixed(1)}
                  </td>
                  <td style={{ padding: "6px 8px" }}>
                    <div style={{ display: "flex", alignItems: "center", gap: 7 }}>
                      <div style={{ flex: 1, height: 5, background: colors.tableStripe, borderRadius: 3, overflow: "hidden" }}>
                        <div style={{ width: `${Math.max(1, d.rel * 100)}%`, height: "100%", background: colors.accent, opacity: 0.55, borderRadius: 3 }} />
                      </div>
                      <span style={{ width: 42, textAlign: "right", color: colors.muted, fontVariantNumeric: "tabular-nums", fontSize: 11.5 }}>
                        {d.share.toFixed(1)}%
                      </span>
                    </div>
                  </td>
                  <td style={{ padding: "6px 2px 6px 8px", textAlign: "right", fontWeight: 600, color: c, fontVariantNumeric: "tabular-nums" }}>
                    {chg == null ? "—" : `${chg > 0 ? "+" : ""}${chg.toFixed(2)}%`}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <Note colors={colors} label="这张表在讲什么">
        上面两张分钟图看的是 20 只 ETF 的<strong>整体合力</strong>，这张表把它拆开、看<strong>到底是谁在放量</strong>。
        顶部的「焦点切换」是另一层信息：国家队不盯着一只买，经常今天买 A、明天买 B，
        所以 <strong>top3 每天大幅换人</strong>本身就是它的痕迹；反过来连续几天压在同一只上，
        更像是单一渠道的持续申购。
        成交额最大的一只往往就是当天资金的焦点，占比直接读出它一家占了多少权重；涨跌幅则告诉你资金是
        「越跌越买」还是「追涨」——两者组合起来，比单看总量更能判断这波资金是防御性的托底，
        还是进攻性的抢筹。
      </Note>
    </div>
  );
}

/* ── 尾盘集中度 ─────────────────────────────────────────────────── */
function TailCard({
  tail, asOf, colors,
}: {
  tail: EtfFlowDashboard["minute"]["tail"];
  asOf: string;
  colors: ReturnType<typeof useTheme>["colors"];
}) {
  const opt = useMemo<EChartsOption>(() => {
    const hist = tail.series.map((x) => ({ label: x.date.slice(5), ratio: x.ratio, hot: false }));
    const pts = tail.today != null
      ? [...hist, { label: "今日", ratio: tail.today, hot: true }]
      : hist;
    return {
      grid: { left: 42, right: 12, top: 14, bottom: 24 },
      tooltip: {
        trigger: "axis",
        backgroundColor: colors.card, borderColor: colors.border,
        textStyle: { color: colors.text, fontSize: 12 },
        formatter: (ps) => {
          const arr = ps as unknown as { name: string; value: number }[];
          const a = arr[0];
          return a ? `${a.name}｜尾盘占比 ${Number(a.value).toFixed(2)}%` : "";
        },
      },
      xAxis: {
        type: "category",
        data: pts.map((x) => x.label),
        axisLine: { lineStyle: { color: colors.border } },
        axisTick: { show: false },
        axisLabel: {
          color: colors.axis, fontSize: 9.5, hideOverlap: true,
          interval: (i: number, _v: string) => i % 4 === 0 || i === pts.length - 1,
        },
      },
      yAxis: {
        type: "value",
        axisLabel: { color: colors.axis, fontSize: 9.5, formatter: "{value}%" },
        splitLine: { lineStyle: { color: colors.border } },
        splitNumber: 3,
      },
      series: [{
        type: "bar", barWidth: "58%",
        data: pts.map((x) => ({
          value: x.ratio,
          itemStyle: {
            color: x.hot ? colors.accent : rgba(colors.accent, 0.26),
            borderRadius: [2, 2, 0, 0],
          },
        })),
        markLine: tail.avg20 != null ? {
          silent: true, symbol: "none", animation: false,
          lineStyle: { color: colors.muted, type: "dashed", width: 1 },
          label: { show: false },
          data: [{ yAxis: tail.avg20 }],
        } : undefined,
      }],
    };
  }, [tail, colors]);

  const mult = tail.mult;
  const tone = mult == null ? colors.text : mult >= 1.3 ? colors.up : mult <= 0.8 ? colors.down : colors.text;

  return (
    <div style={cardStyle(colors)}>
      <CardHead
        title="尾盘集中度"
        tag={`${tail.window} 成交额 ÷ 全天 · 虚线为 20 日均值`}
        colors={colors}
      />
      <div style={{
        display: "grid", gridTemplateColumns: "232px 1fr",
        gap: 18, alignItems: "center", marginTop: 2,
      }}>
        <div>
          <div style={{ fontSize: 11, color: colors.muted, marginBottom: 2 }}>当日尾盘占比</div>
          <div style={{
            fontSize: 30, fontWeight: 700, lineHeight: 1.1, color: tone,
            fontVariantNumeric: "tabular-nums",
          }}>
            {tail.today == null ? "—" : `${tail.today.toFixed(2)}%`}
          </div>
          <div style={{ fontSize: 12, color: colors.muted, marginTop: 7, lineHeight: 1.7 }}>
            20 日均值 {tail.avg20 == null ? "—" : `${tail.avg20.toFixed(2)}%`}
            {mult != null && (
              <> · <b style={{ color: tone, fontSize: 13 }}>{mult.toFixed(2)}×</b></>
            )}
          </div>
          <div style={{ fontSize: 11.5, color: colors.muted, marginTop: 3 }}>
            {asOf} · 前 {tail.series.length} 个交易日对照
          </div>
        </div>
        <EChart option={opt} height={168} />
      </div>
      <Note colors={colors}>
        份额要等 T+1 才有，盘中唯一能当天就用的就是量能结构。国家队进场的典型形态是
        <strong>尾盘一根放量柱</strong> —— 最后 30 分钟的成交额占比会明显抬升。
        这里把它压成一个数：<strong>14:31–15:00 成交额 ÷ 全天成交额</strong>，再和过去 20 个交易日的均值比。
        倍数明显大于 1 说明资金在收盘前集中进场；明显小于 1 说明尾盘在缩，连护盘痕迹都不明显。
        这是「当日净流入」公布之前，唯一能替代它的前置指标。
      </Note>
    </div>
  );
}

/* ── 托底之后：T+n 前瞻收益 ─────────────────────────────────────── */
function ForwardCard({
  forward, colors,
}: {
  forward: EtfFlowDashboard["support"]["forward"];
  colors: ReturnType<typeof useTheme>["colors"];
}) {
  const opt = useMemo<EChartsOption>(() => {
    const pts = forward.points.filter((x) => x.t20 != null);
    return {
      grid: { left: 54, right: 20, top: 22, bottom: 34 },
      tooltip: {
        backgroundColor: colors.card, borderColor: colors.border,
        textStyle: { color: colors.text, fontSize: 12 },
        formatter: (ps) => {
          const arr = ps as unknown as { data: { _p?: EtfFlowDashboard["support"]["forward"]["points"][number] } }[];
          const x = arr[0]?.data?._p;
          if (!x) return "";
          const c20 = (x.t20 ?? 0) >= 0 ? colors.up : colors.down;
          const c60 = (x.t60 ?? 0) >= 0 ? colors.up : colors.down;
          return `<div style="font-size:11px;color:${colors.muted};margin-bottom:3px">${x.date}</div>`
            + `净流入 <b>${f1(x.net_inflow)}</b> 亿 · 当日指数 ${sgn(x.index_chg)}${f2(x.index_chg)}%<br/>`
            + `T+20 <b style="color:${c20}">${sgn(x.t20 ?? 0)}${f2(x.t20)}%</b>`
            + (x.t60 != null ? ` · T+60 <b style="color:${c60}">${sgn(x.t60)}${f2(x.t60)}%</b>` : "");
        },
      },
      xAxis: {
        type: "value",
        name: "净流入（亿元）", nameLocation: "middle", nameGap: 22,
        nameTextStyle: { color: colors.axis, fontSize: 10 },
        axisLabel: { color: colors.axis, fontSize: 9.5 },
        axisLine: { lineStyle: { color: colors.border } },
        splitLine: { show: false },
      },
      yAxis: {
        type: "value",
        name: "T+20 收益 %", nameLocation: "end", nameGap: 8,
        nameTextStyle: { color: colors.axis, fontSize: 10, align: "left" },
        axisLabel: { color: colors.axis, fontSize: 9.5, formatter: "{value}%" },
        splitLine: { lineStyle: { color: colors.border } },
        splitNumber: 4,
      },
      series: [{
        type: "scatter", symbolSize: 8,
        data: pts.map((x) => ({
          value: [x.net_inflow, x.t20],
          _p: x,
          itemStyle: {
            color: (x.t20 ?? 0) >= 0 ? rgba(colors.up, 0.8) : rgba(colors.down, 0.8),
            borderColor: colors.card, borderWidth: 0.5,
          },
        })),
        markLine: {
          silent: true, symbol: "none", animation: false,
          lineStyle: { color: colors.muted, type: "dashed", width: 1, opacity: 0.7 },
          label: { show: false },
          data: [{ yAxis: 0 }],
        },
      }],
    };
  }, [forward, colors]);

  const hs = forward.horizons;
  const corr20 = forward.corr["20"] ?? forward.corr[20 as unknown as string];

  return (
    <div style={cardStyle(colors)}>
      <CardHead
        title={`托底之后：${forward.points.length} 个托底日的后市`}
        tag="同一指数口径的事后统计 · 非可交易回测"
        colors={colors}
      />

      <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 10, margin: "2px 0 12px" }}>
        {hs.map((h) => {
          const st = forward.stats[String(h)];
          if (!st || !st.n) return null;
          const tone = st.win >= 55 ? colors.up : st.win <= 45 ? colors.down : colors.text;
          return (
            <div key={h} style={{
              background: colors.card, border: `1px solid ${colors.border}`,
              borderRadius: 8, padding: "9px 12px",
            }}>
              <div style={{ fontSize: 11, color: colors.muted, marginBottom: 3 }}>
                托底后 {h} 个交易日
              </div>
              <div style={{
                fontSize: 20, fontWeight: 700, color: tone,
                fontVariantNumeric: "tabular-nums", lineHeight: 1.2,
              }}>
                {st.win.toFixed(1)}%
                <span style={{ fontSize: 11, fontWeight: 500, color: colors.muted, marginLeft: 3 }}>
                  上涨
                </span>
              </div>
              <div style={{ fontSize: 11.5, color: colors.muted, marginTop: 4, lineHeight: 1.6 }}>
                中位 <b style={{ color: st.median >= 0 ? colors.up : colors.down }}>{sgn(st.median)}{f2(st.median)}%</b>
                {" · "}均值 {sgn(st.mean)}{f2(st.mean)}%
                <br />
                区间 {f2(st.worst)}% ~ {sgn(st.best)}{f2(st.best)}% · n={st.n}
              </div>
            </div>
          );
        })}
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 236px", gap: 16, alignItems: "center" }}>
        <EChart option={opt} height={252} />
        <div style={{ fontSize: 12, color: colors.muted, lineHeight: 1.9 }}>
          <div style={{ fontWeight: 600, color: colors.text, marginBottom: 4 }}>
            流入规模重要吗？
          </div>
          与 T+20 收益的相关系数
          <b style={{ color: colors.text, marginLeft: 4 }}>
            {corr20 == null ? "—" : corr20.toFixed(2)}
          </b>
          <div style={{ marginTop: 2 }}>
            {corr20 == null
              ? "样本不足，无法判断。"
              : Math.abs(corr20) < 0.2
                ? "几乎无关 —— 托底金额大小不预示涨幅，别按「买得多就涨得多」推。"
                : Math.abs(corr20) < 0.45
                  ? "弱相关 —— 流入大的那次后市略好，但解释力很有限。"
                  : "明显相关 —— 流入规模可作为后市强弱的参考。"}
          </div>
          <div style={{ marginTop: 8, color: colors.axis, fontSize: 11.5 }}>
            横轴 = 当日净流入，纵轴 = T+20 收益。虚线是 0 轴，点在虚线上方即为上涨。
          </div>
        </div>
      </div>

      <Note colors={colors}>
        这张图回答的是「托底之后会怎样」，而前面几张只回答「今天像不像托底」。
        三个横截面各有含义：<strong>T+5 看情绪</strong>（通常还没走出来）、
        <strong>T+20 看资金落地</strong>、<strong>T+60 看趋势是否成立</strong>。
        横截面越长，样本越少（最近的托底日还没有 60 个交易日的后市数据），所以每格都标了 n。
        <br />
        <span style={{ color: colors.axis }}>
          注意这是<strong>同一指数口径的事后统计</strong>，不是可交易策略回测：没扣成本、没算滑点、
          没做样本外；连续托底日的持有期还会互相重叠，样本并不独立。
          它的用途是校准预期，不能当收益承诺，更不该据此定仓位 —— 那件事归 NSI。
        </span>
      </Note>
    </div>
  );
}

/* ── 估值 × 资金流 ──────────────────────────────────────────────── */
function ValuationCard({
  v, colors,
}: {
  v: EtfFlowDashboard["valuation"] | undefined;
  colors: ReturnType<typeof useTheme>["colors"];
}) {
  const opt = useMemo<EChartsOption>(() => {
    const dates = v?.dates ?? [];
    const pe = v?.pe_series ?? [];
    const idx = new Map(dates.map((d, i) => [d, i] as const));
    const marks = (v?.support_marks ?? [])
      .map((m) => {
        const i = idx.get(m.date);
        return i == null || pe[i] == null ? null : { value: [i, pe[i]], _m: m };
      })
      .filter((x): x is { value: number[]; _m: { date: string; pct: number } } => x != null);
    return {
      grid: { left: 48, right: 16, top: 20, bottom: 26 },
      tooltip: {
        trigger: "axis",
        backgroundColor: colors.card, borderColor: colors.border,
        textStyle: { color: colors.text, fontSize: 12 },
        formatter: (ps) => {
          const arr = ps as unknown as { axisValue?: string; seriesName?: string; value: number | number[] }[];
          const a = arr?.[0];
          if (!a) return "";
          const d = a.axisValue ?? "";
          const i = idx.get(d);
          const p = i == null ? null : v?.pct_series?.[i];
          let out = `<div style="font-size:11px;color:${colors.muted};margin-bottom:3px">${d}</div>`;
          out += `PE 中位数 <b>${f2(pe[i ?? -1] ?? null)}</b>`;
          if (p != null) out += ` · 5 年分位 <b>${p.toFixed(1)}%</b>`;
          const mk = arr.find((x) => x.seriesName === "托底日");
          if (mk) out += `<br/><span style="color:${colors.up}">● 当日为历史托底日</span>`;
          return out;
        },
      },
      xAxis: {
        type: "category",
        data: dates,
        axisLine: { lineStyle: { color: colors.border } },
        axisTick: { show: false },
        axisLabel: {
          color: colors.axis, fontSize: 9.5, hideOverlap: true,
          formatter: (val: string) => val.slice(0, 7),
        },
      },
      yAxis: {
        type: "value", scale: true,
        axisLabel: { color: colors.axis, fontSize: 9.5 },
        splitLine: { lineStyle: { color: colors.border } },
        splitNumber: 4,
      },
      series: [
        {
          name: "PE 中位数", type: "line", data: pe, showSymbol: false,
          lineStyle: { color: colors.accent, width: 1.4 },
          itemStyle: { color: colors.accent },
          areaStyle: {
            color: {
              type: "linear", x: 0, y: 0, x2: 0, y2: 1,
              colorStops: [
                { offset: 0, color: rgba(colors.accent, 0.16) },
                { offset: 1, color: rgba(colors.accent, 0) },
              ],
            },
          },
        },
        {
          name: "托底日", type: "scatter", data: marks, symbolSize: 7, z: 5,
          itemStyle: { color: rgba(colors.up, 0.85), borderColor: colors.card, borderWidth: 0.5 },
        },
      ],
    };
  }, [v, colors]);

  if (!v || !v.available) return null;
  const sp = v.support_pct;

  const blocks = [
    {
      label: "当前估值分位",
      main: v.pct == null ? "—" : `${v.pct.toFixed(1)}%`,
      tone: v.pct == null ? colors.text
        : v.pct <= 30 ? colors.down : v.pct >= 70 ? colors.up : colors.text,
      sub: `PE ${f2(v.pe)} · PB 分位 ${v.pct_pb == null ? "—" : v.pct_pb.toFixed(0) + "%"}`,
      tail: `${v.start} 起 ${v.count} 个交易日`,
    },
    {
      label: "托底日的中位分位",
      main: sp ? `${sp.median.toFixed(1)}%` : "—",
      tone: sp && sp.median >= 50 ? colors.up : colors.text,
      sub: sp ? `样本 n=${sp.n} · 四分位 ${sp.p25.toFixed(0)}% ~ ${sp.p75.toFixed(0)}%` : "样本不足",
      tail: sp ? `低估值区(≤20%) 占 ${sp.low_share.toFixed(0)}% · 高位区(≥80%) 占 ${sp.high_share.toFixed(0)}%` : "",
    },
    {
      label: "托底时的估值跨度",
      main: sp ? `${sp.p25.toFixed(0)}–${sp.p75.toFixed(0)}%` : "—",
      tone: colors.text,
      sub: sp && sp.p75 - sp.p25 > 50 ? "跨度极大，不存在统一门槛" : "分布相对集中",
      tail: (v.support_yearly ?? []).length
        ? `年度中位：${(v.support_yearly ?? []).map((y) => `${y.year} ${y.median.toFixed(0)}%`).join(" · ")}`
        : "",
    },
  ];

  return (
    <div style={cardStyle(colors)}>
      <CardHead
        title="估值 × 资金流"
        tag={`全 A 股 PE(TTM) 中位数 · 滚动 5 年分位 · ${v.as_of}`}
        colors={colors}
      />

      <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 10, margin: "2px 0 12px" }}>
        {blocks.map((b) => (
          <div key={b.label} style={{
            background: colors.card, border: `1px solid ${colors.border}`,
            borderRadius: 8, padding: "9px 12px", minWidth: 0,
          }}>
            <div style={{ fontSize: 11, color: colors.muted, marginBottom: 3 }}>{b.label}</div>
            <div style={{
              fontSize: 20, fontWeight: 700, color: b.tone,
              fontVariantNumeric: "tabular-nums", lineHeight: 1.2,
            }}>{b.main}</div>
            <div style={{ fontSize: 11.5, color: colors.muted, marginTop: 4, lineHeight: 1.6 }}>{b.sub}</div>
            <div style={{
              fontSize: 11, color: colors.axis, marginTop: 2, lineHeight: 1.55,
            }}>{b.tail}</div>
          </div>
        ))}
      </div>

      <EChart option={opt} height={260} />

      <Note colors={colors}>
        蓝线是全 A 股 PE(TTM) 中位数（剔除亏损与 300 倍以上的极端值），
        <span style={{ color: colors.up }}>红点</span>是 {sp?.n ?? 0} 个历史托底日。
        它要回答的是一个容易被说反的问题：<strong>国家队真的是「跌到低估值才出手」吗？</strong>
        <br />
        按年看中位分位反而更清楚 —— {sp && sp.p75 - sp.p25 > 50
          ? <span style={{ color: colors.text }}>
              托底横跨了估值谱系的两端：2024 年是极端低估时的救市（中位个位数分位），
              2025–2026 年则在估值中高位持续买入，更像是<strong>常态化护盘</strong>而不是抄底。
              四分之一落在 20% 分位以下、另有近两成落在 80% 分位以上，没有一个统一的估值门槛。
            </span>
          : <span style={{ color: colors.text }}>
              托底集中在相对集中的估值区间，说明存在某种隐性的估值门槛。
            </span>}
        <br />
        <span style={{ color: colors.axis }}>
          口径提醒：这里是<strong>全 A 股 PE 中位数的 5 年滚动分位</strong>，与市面上按中证 800
          或等权口径算的「估值星级」并不等价，绝对值不可直接对照；看趋势与相对位置即可。
          估值只说明「贵不贵」，不说明「会不会买」—— 与资金流叠加才有意义。
        </span>
      </Note>
    </div>
  );
}

/* ── NSI 信号卡 ─────────────────────────────────────────────────── */
function NsiCard({
  nsi, colors, mode,
}: {
  nsi: NonNullable<EtfFlowDashboard["nsi"]>;
  colors: ReturnType<typeof useTheme>["colors"];
  mode: string;
}) {
  const hi = nsi.hi_threshold ?? 71.34;
  const opt = useMemo<EChartsOption>(() => {
    const h = nsi.history ?? [];
    const dates = h.map((p) => p.date);
    const sc = h.map((p) => p.score);
    const pos = h.map((p) => (p.position ?? 0.7) * 100);
    return {
      grid: [
        { left: 48, right: 20, top: 24, height: 150 },
        { left: 48, right: 20, top: 208, height: 48 },
      ],
      tooltip: {
        trigger: "axis",
        backgroundColor: colors.card,
        borderColor: colors.border,
        textStyle: { color: colors.text, fontSize: 12 },
        formatter: (ps) => {
          const arr = ps as { dataIndex: number }[];
          const i = arr[0]?.dataIndex ?? 0;
          return `${dates[i]}<br/>score <b>${f2(sc[i])}</b>（阈值 ${f2(hi)}）<br/>建议仓位 <b>${f2((pos[i] ?? 0) / 100)}</b>`;
        },
      },
      axisPointer: { link: [{ xAxisIndex: "all" }] },
      xAxis: [
        {
          type: "category", data: dates, gridIndex: 0,
          axisLabel: { show: false }, axisLine: { lineStyle: { color: colors.border } },
          axisTick: { show: false },
        },
        {
          type: "category", data: dates, gridIndex: 1,
          axisLabel: { color: colors.axis, fontSize: 10, interval: Math.ceil(dates.length / 8) },
          axisLine: { lineStyle: { color: colors.border } }, axisTick: { show: false },
        },
      ],
      yAxis: [
        {
          gridIndex: 0, type: "value",
          axisLabel: { color: colors.axis, fontSize: 10 },
          splitLine: { lineStyle: { color: colors.border } },
        },
        {
          gridIndex: 1, type: "value", min: 60, max: 105,
          axisLabel: { color: colors.axis, fontSize: 10, formatter: "{value}%" },
          splitLine: { show: false },
        },
      ],
      series: [
        {
          name: "score", type: "line", data: sc, xAxisIndex: 0, yAxisIndex: 0,
          showSymbol: false, lineStyle: { color: colors.accent, width: 1.6 },
          areaStyle: {
            color: {
              type: "linear", x: 0, y: 0, x2: 0, y2: 1,
              colorStops: [
                { offset: 0, color: `${rgba(colors.accent,0.20)}` },
                { offset: 1, color: `${rgba(colors.accent,0)}` },
              ],
            },
          },
          markLine: {
            silent: true, symbol: "none",
            lineStyle: { color: colors.up, width: 1, type: [5, 4] },
            label: {
              formatter: `触发阈值 ${f2(hi)}`, position: "insideEndTop",
              color: colors.up, fontSize: 10,
            },
            data: [{ yAxis: hi }],
          },
        },
        {
          name: "建议仓位", type: "line", data: pos, xAxisIndex: 1, yAxisIndex: 1,
          showSymbol: false, step: "end",
          lineStyle: { color: colors.up, width: 1.4 },
          areaStyle: { color: `${rgba(colors.up,0.2)}` },
        },
      ],
    };
  }, [nsi, colors, hi]);

  const pos = nsi.position ?? 0.7;
  const base = nsi.base_position ?? 0.7;
  const boosting = pos > base + 1e-6;

  return (
    <div style={cardStyle(colors)}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 12 }}>
        <span style={{ fontSize: 14, fontWeight: 700, color: colors.text }}>
          国家队介入强度 NSI
        </span>
        <Pill colors={colors} tone="note">信号口径 · 5 只核心 ETF 份额</Pill>
        <span style={{ fontSize: 11.5, color: colors.muted }}>
          as_of {nsi.as_of} · {nsi.regime}
        </span>
        {nsi.data_warning && <Pill colors={colors} tone="warn">{nsi.data_warning}</Pill>}
      </div>

      <div style={{ display: "flex", gap: 0, marginBottom: 14 }}>
        <KPI
          label="score（含位置惩罚）" value={f2(nsi.score)} colors={colors}
          tone={(nsi.score ?? 0) >= hi ? "up" : "plain"}
          sub={`阈值 ${f2(hi)} · 历史分位 ${f1(nsi.score_percentile)}%`}
        />
        <KPI
          label="建议仓位" value={`${Math.round(pos * 100)}%`} colors={colors}
          tone={boosting ? "up" : "plain"}
          sub={`基础 ${Math.round(base * 100)}%${boosting ? " · 加仓中" : " · 未触发"}`}
        />
        <KPI
          label="已加仓" value={String(nsi.holding_days ?? 0)} unit="个交易日" colors={colors}
          sub={`上限 20 日 · 上次变动 ${nsi.last_change ?? "—"}`}
        />
        <KPI
          label="750 日分位" value={f2(nsi.p750)} colors={colors}
          sub={`位置惩罚 ${sgn(nsi.pos_penalty ?? 0)}${f1(nsi.pos_penalty)}`}
        />
        <KPI
          label="资金痕迹 FLOW" value={f2(nsi.flow)} colors={colors}
          sub={nsi.buyer_type ? `${nsi.buyer_type}${nsi.hhi != null ? ` · HHI ${f2(nsi.hhi)}` : ""}` : "—"}
        />
      </div>

      <EChart option={opt} height={272} />
      <div style={{ fontSize: 11.5, color: colors.muted, marginTop: 6, lineHeight: 1.7 }}>
        上：score 与触发阈值（{f2(hi)}）；下：建议仓位。NSI 只做加法——score 低不会触发减仓，
        加仓窗口在最后一次触发后自然滚动 20 个交易日。{nsi.note}
      </div>
    </div>
  );
}

/* ── 分钟成交额 × 指数 ──────────────────────────────────────────── */
function MinuteCard({
  meta, minute, colors, mode,
}: {
  meta: EtfFlowDashboard["meta"];
  minute: EtfFlowDashboard["minute"];
  colors: ReturnType<typeof useTheme>["colors"];
  mode: string;
}) {
  const opt = useMemo<EChartsOption>(() => {
    const n = minute.count || 0;
    const labels = minute.labels.slice(0, n);
    const hot = minute.today_amount.map((v, i) =>
      v != null && minute.avg20_amount[i] > 0 && v / minute.avg20_amount[i] > 2.6);
    return {
      grid: [
        { left: 54, right: 56, top: 30, height: 150 },
        { left: 54, right: 56, top: 212, height: 132 },
      ],
      tooltip: {
        trigger: "axis",
        backgroundColor: colors.card, borderColor: colors.border,
        textStyle: { color: colors.text, fontSize: 12 },
        formatter: (ps) => {
          const arr = ps as { dataIndex: number }[];
          const i = arr[0]?.dataIndex ?? 0;
          const a = minute.today_amount[i];
          const avg = minute.avg20_amount[i];
          const mult = a != null && avg > 0 ? (a / avg).toFixed(1) : "—";
          return `${labels[i]}<br/>上证 <b>${f2(minute.today_index[i])}</b>`
            + `<br/>本分钟成交 <b>${f1(a)}</b> 亿（均量 ${mult}×）`
            + `<br/>累计 <b>${f1(minute.today_cum[i])}</b> 亿`;
        },
      },
      axisPointer: { link: [{ xAxisIndex: "all" }] },
      xAxis: [
        {
          type: "category", data: labels, gridIndex: 0,
          axisLabel: { show: false }, axisLine: { lineStyle: { color: colors.border } },
          axisTick: { show: false },
        },
        {
          type: "category", data: labels, gridIndex: 1,
          axisLabel: {
            color: colors.axis, fontSize: 10,
            interval: (i: number, v: string) => i === 0 || v.endsWith(":00") || v.endsWith(":30"),
          },
          axisLine: { lineStyle: { color: colors.border } }, axisTick: { show: false },
        },
      ],
      yAxis: [
        {
          gridIndex: 0, scale: true,
          axisLabel: { color: colors.axis, fontSize: 10 },
          splitLine: { lineStyle: { color: colors.border } },
        },
        {
          gridIndex: 1,
          axisLabel: { color: colors.axis, fontSize: 10 },
          splitLine: { lineStyle: { color: colors.border } },
        },
      ],
      series: [
        {
          name: "上证综指", type: "line", data: minute.today_index,
          xAxisIndex: 0, yAxisIndex: 0, showSymbol: false, connectNulls: true,
          lineStyle: { color: colors.accent, width: 1.7 },
          areaStyle: {
            color: {
              type: "linear", x: 0, y: 0, x2: 0, y2: 1,
              colorStops: [
                { offset: 0, color: `${rgba(colors.accent,0.15)}` },
                { offset: 1, color: `${rgba(colors.accent,0)}` },
              ],
            },
          },
          markLine: {
            silent: true, symbol: "none",
            lineStyle: { color: colors.muted, width: 1, type: [5, 4] },
            label: { formatter: `昨收 ${f2(minute.prev_close)}`, color: colors.muted, fontSize: 10 },
            data: [{ yAxis: minute.prev_close ?? 0 }],
          },
        },
        {
          name: "分钟成交额", type: "bar", xAxisIndex: 1, yAxisIndex: 1,
          barWidth: "72%",
          data: minute.today_amount.map((v, i) => ({
            value: v,
            itemStyle: { color: hot[i] ? colors.up : `${rgba(colors.up,0.30)}`, borderRadius: [1, 1, 0, 0] },
          })),
        },
        {
          name: "20 日同期均量", type: "line", data: minute.avg20_amount,
          xAxisIndex: 1, yAxisIndex: 1, showSymbol: false,
          lineStyle: { color: colors.muted, width: 1 },
        },
      ],
    };
  }, [minute, colors]);

  const tag = meta.partial_from != null
    ? `采样自 ${minute.labels[meta.partial_from]}，此前无数据`
    : meta.count ? `${minute.labels[0]} – ${minute.labels[meta.count - 1]}`
      : (meta.is_today ? "今日暂无分钟数据" : "等待盘中数据");

  return (
    <div style={cardStyle(colors)}>
      <CardHead title="分钟成交额 × 上证综指" tag={tag} colors={colors} />
      {minute.count === 0 ? (
        <Empty colors={colors}>
          暂无分钟数据 —— 交易时段内服务会自动逐分钟采集；
          已收盘的话点状态栏「拉官方分钟线」立即补
        </Empty>
      ) : <EChart option={opt} height={372} />}
      <Note colors={colors}>
        一天里 20 只重点 ETF 每一分钟在二级市场的真实成交，加总成一根根柱子；折线是上证综指同步点位，虚线是昨收。
        它回答的是「今天资金有没有在动」——柱子突然拔高、又伴随指数向上，往往是资金进场；柱子放量但指数不动或下跌，
        则更像托底或对倒。深红色标记的是该分钟成交额超过 20 日同期均量 2.6 倍的时刻，是全天最值得留意的脉冲。
      </Note>
    </div>
  );
}

/* ── 累计成交额同期对比 ─────────────────────────────────────────── */
function CumCard({
  meta, minute, colors, mode,
}: {
  meta: EtfFlowDashboard["meta"];
  minute: EtfFlowDashboard["minute"];
  colors: ReturnType<typeof useTheme>["colors"];
  mode: string;
}) {
  const opt = useMemo<EChartsOption>(() => {
    const n = minute.count || 0;
    const labels = minute.labels.slice(0, n);
    // 采样起点：这之前没有分钟数据，曲线是「接」上去的而不是从 0 涨上来的。
    // 不标出来的话，会误读成「14:51 那一分钟成交了 700 多亿」。
    const partial = meta.partial_from != null;
    const startLbl = partial ? minute.labels[meta.partial_from as number] : null;
    return {
      grid: { left: 54, right: 76, top: 34, bottom: 30 },
      legend: {
        top: 2, right: 6, itemWidth: 14, itemHeight: 2, itemGap: 14,
        textStyle: { color: colors.muted, fontSize: 11 },
      },
      tooltip: {
        trigger: "axis",
        backgroundColor: colors.card, borderColor: colors.border,
        textStyle: { color: colors.text, fontSize: 12 },
      },
      xAxis: {
        type: "category", data: labels, boundaryGap: false,
        axisLabel: {
          color: colors.axis, fontSize: 10,
          interval: (i: number, v: string) => i === 0 || v.endsWith(":00") || v.endsWith(":30"),
        },
        axisLine: { lineStyle: { color: colors.border } }, axisTick: { show: false },
      },
      yAxis: {
        type: "value",
        axisLabel: { color: colors.axis, fontSize: 10 },
        splitLine: { lineStyle: { color: colors.border } },
      },
      series: [
        {
          name: "当日累计", type: "line", data: minute.today_cum, showSymbol: false,
          connectNulls: true, smooth: true, smoothMonotone: "x",
          lineStyle: { color: colors.up, width: 2.2 },
          itemStyle: { color: colors.up },
          // 采样模式下不铺面积：只有尾部一小段时，面积会变成一根从 0 拉到 700 多的
          // 竖条，看起来像「一分钟内涨了 700 亿」，反而误导
          areaStyle: partial ? undefined : {
            color: {
              type: "linear", x: 0, y: 0, x2: 0, y2: 1,
              colorStops: [
                { offset: 0, color: `${rgba(colors.up,0.15)}` },
                { offset: 1, color: `${rgba(colors.up,0)}` },
              ],
            },
          },
          markLine: partial ? {
            silent: true, symbol: "none", animation: false,
            lineStyle: { color: colors.axis, type: "dashed", width: 1, opacity: 0.85 },
            // 不写字：位置在右上角，会和图例撞在一起；含义由卡片 tag「采样自 14:51」
            // 和下方说明承担，虚线只负责标出「数据从这一格开始」
            label: { show: false },
            data: [{ xAxis: startLbl as string }],
          } : undefined,
          endLabel: {
            show: true, formatter: `${f1(minute.today_cum[n - 1])}`,
            color: colors.up, fontSize: 10.5, offset: [4, 0],
          },
        },
        {
          name: "上一交易日同期", type: "line", data: minute.prev_cum, showSymbol: false,
          smooth: true, smoothMonotone: "x",
          lineStyle: { color: colors.muted, width: 1.3, type: [6, 4] },
          itemStyle: { color: colors.muted },
        },
        {
          name: "20 日同期均值", type: "line", data: minute.avg20_cum, showSymbol: false,
          smooth: true, smoothMonotone: "x",
          lineStyle: { color: colors.axis, width: 1.2, opacity: 0.55 },
          itemStyle: { color: colors.axis },
        },
      ],
    };
  }, [minute, colors]);

  return (
    <div style={cardStyle(colors)}>
      <CardHead
        title="累计成交额 · 同期对比"
        tag={meta.partial_from != null ? `采样自 ${minute.labels[meta.partial_from]}` : `截至 ${meta.last_minute}`}
        colors={colors}
      />
      {minute.count === 0 ? (
        <Empty colors={colors}>暂无数据</Empty>
      ) : <EChart option={opt} height={286} />}
      <Note colors={colors}>
        {meta.partial_from != null && (
          <span style={{ color: colors.axis }}>
            今天服务是从 {minute.labels[meta.partial_from]} 才开始采样的，所以红线只覆盖右侧那一小段；
            起点的高度是那一刻的<strong>真实累计成交额</strong>，之后逐分钟累加，不是从 0 涨上来的。{" "}
          </span>
        )}
        把上一张的分钟成交额从开盘一路累加，得到「到这一刻为止今天一共成交了多少」。红线是当日，深灰虚线是上一交易日，
        浅灰是近 20 个交易日的同期平均。它回答的是「今天整体算不算放量」——曲线明显骑在均值线之上说明全天量能偏大，
        反过来一直在均值线下就是缩量。看的是总量节奏，不是某一分钟的脉冲。
      </Note>
    </div>
  );
}

/* ── 15 分钟形态 ────────────────────────────────────────────────── */
function SlotCard({
  title, tag, labels, amount, chg, hot, colors, mode, note,
}: {
  title: string; tag: string; labels: string[]; amount: number[];
  chg: number[]; hot?: boolean;
  colors: ReturnType<typeof useTheme>["colors"];
  mode: string; note: string;
}) {
  const opt = useMemo<EChartsOption>(() => ({
    grid: { left: 44, right: 42, top: 30, bottom: 26 },
    tooltip: {
      trigger: "axis",
      backgroundColor: colors.card, borderColor: colors.border,
      textStyle: { color: colors.text, fontSize: 12 },
    },
    xAxis: {
      type: "category", data: labels.map((s) => s.slice(6)),
      axisLabel: { color: colors.axis, fontSize: 9.5, interval: 1 },
      axisLine: { lineStyle: { color: colors.border } }, axisTick: { show: false },
    },
    yAxis: [
      {
        type: "value",
        axisLabel: { color: colors.axis, fontSize: 10 },
        splitLine: { lineStyle: { color: colors.border } }, splitNumber: 3,
      },
      {
        type: "value",
        axisLabel: { color: colors.axis, fontSize: 10, formatter: "{value}%" },
        splitLine: { show: false }, splitNumber: 3,
      },
    ],
    series: [
      {
        name: "成交额", type: "bar", data: amount, barWidth: "58%",
        itemStyle: {
          color: hot ? colors.up : colors.muted,
          opacity: hot ? 0.85 : 0.45,
          borderRadius: [2, 2, 0, 0],
        },
      },
      {
        name: "累计涨跌", type: "line", yAxisIndex: 1, data: chg,
        showSymbol: true, symbol: "circle", symbolSize: 3.5,
        lineStyle: { color: colors.accent, width: 1.6 },
        itemStyle: { color: colors.accent }, smooth: 0.25,
        markLine: {
          silent: true, symbol: "none",
          lineStyle: { color: colors.border, width: 1 },
          label: { show: false }, data: [{ yAxis: 0 }],
        },
      },
    ],
  }), [labels, amount, chg, hot, colors]);

  return (
    <div style={cardStyle(colors)}>
      <CardHead title={title} tag={tag} colors={colors} />
      <EChart option={opt} height={252} />
      <Note colors={colors}>{note}</Note>
    </div>
  );
}

/* ── 托底信号 ───────────────────────────────────────────────────── */
function SupportCard({
  support, meta, colors, mode,
}: {
  support: EtfFlowDashboard["support"];
  meta: EtfFlowDashboard["meta"];
  colors: ReturnType<typeof useTheme>["colors"];
  mode: string;
}) {
  const days = support.days;
  const avgIn = days.length ? days.reduce((a, b) => a + b.net_inflow, 0) / days.length : 0;
  const turns = days.map((d) => d.turnover).sort((a, b) => a - b);
  const est = support.today_estimate || support.today_turnover;
  const below = turns.filter((v) => v < est).length;
  const q = turns.length ? (below / turns.length) * 100 : 0;
  const last = days[days.length - 1];

  const barOpt = useMemo<EChartsOption>(() => ({
    grid: { left: 50, right: 20, top: 26, bottom: 28 },
    tooltip: {
      trigger: "axis", axisPointer: { type: "shadow" },
      backgroundColor: colors.card, borderColor: colors.border,
      textStyle: { color: colors.text, fontSize: 12 },
      formatter: (ps) => {
        const arr = ps as { dataIndex: number }[];
        const d = days[arr[0]?.dataIndex ?? 0];
        if (!d) return "";
        return `${d.date}<br/>净流入 <b>${f1(d.net_inflow)}</b> 亿`
          + `<br/>20ETF 成交 <b>${f1(d.turnover)}</b> 亿`
          + `<br/>上证 ${f2(d.index)}（${sgn(d.index_chg)}${f2(d.index_chg)}%）`;
      },
    },
    xAxis: {
      type: "category", data: days.map((d) => d.date),
      axisLabel: {
        color: colors.axis, fontSize: 9.5,
        interval: (i: number, _v: string) =>
          i === 0 || (i > 0 && days[i].date.slice(2, 4) !== days[i - 1].date.slice(2, 4)) || i % 10 === 0,
        formatter: (v: string) => v.slice(2, 7),
      },
      axisLine: { lineStyle: { color: colors.border } }, axisTick: { show: false },
    },
    yAxis: {
      type: "value",
      axisLabel: { color: colors.axis, fontSize: 10 },
      splitLine: { lineStyle: { color: colors.border } },
    },
    series: [{
      type: "bar", barMaxWidth: 8,
      data: days.map((d) => ({
        value: d.net_inflow,
        itemStyle: { color: d.index_chg >= 0 ? colors.up : colors.down, opacity: 0.82, borderRadius: [1.5, 1.5, 0, 0] },
      })),
      markLine: {
        silent: true, symbol: "none",
        lineStyle: { color: colors.muted, width: 1, type: [5, 4] },
        label: { formatter: `均值 ${f1(avgIn)}`, color: colors.muted, fontSize: 10 },
        data: [{ yAxis: avgIn }],
      },
    }],
  }), [days, avgIn, colors]);

  const cdfOpt = useMemo<EChartsOption>(() => {
    const cdf = turns.map((v, i) => [v, ((i + 1) / turns.length) * 100]);
    if (turns.length) cdf.unshift([turns[0] * 0.94, 0]);
    return {
      grid: { left: 44, right: 30, top: 26, bottom: 30 },
      tooltip: {
        trigger: "axis", backgroundColor: colors.card, borderColor: colors.border,
        textStyle: { color: colors.text, fontSize: 12 },
        formatter: (ps) => {
          const arr = ps as unknown as { value: [number, number] }[];
          const v = arr[0]?.value;
          return v && v.length === 2 ? `成交额 ${f1(v[0])} 亿 → 分位 ${v[1].toFixed(0)}%` : "";
        },
      },
      xAxis: {
        type: "value", name: "亿元", nameTextStyle: { color: colors.axis, fontSize: 10 },
        axisLabel: { color: colors.axis, fontSize: 10 },
        splitLine: { lineStyle: { color: colors.border } },
      },
      yAxis: {
        type: "value", min: 0, max: 100, name: "累计占比 %",
        nameTextStyle: { color: colors.axis, fontSize: 10 },
        axisLabel: { color: colors.axis, fontSize: 10 },
        splitLine: { lineStyle: { color: colors.border } },
      },
      series: [{
        type: "line", data: cdf, showSymbol: false, smooth: 0.1,
        lineStyle: { color: colors.accent, width: 2 },
        areaStyle: { color: `${rgba(colors.accent,0.12)}` },
        markLine: {
          silent: true, symbol: "none",
          lineStyle: { color: colors.up, width: 1.3, type: [5, 4] },
          label: { formatter: `${f1(est)} 亿`, color: colors.up, fontSize: 10, position: "end" },
          data: [{ xAxis: est }],
        },
        markPoint: {
          symbol: "circle", symbolSize: 9,
          itemStyle: { color: colors.up, borderColor: colors.card, borderWidth: 2 },
          label: {
            formatter: `${q.toFixed(0)}%`, position: "left", color: colors.up,
            fontSize: 11.5, fontWeight: 700,
            backgroundColor: colors.card, padding: [2, 5], borderRadius: 4,
          },
          data: [{ coord: [est, q], name: "当前" }],
        },
      }],
    };
  }, [turns, est, q, colors]);

  return (
    <>
      <div style={{ ...cardStyle(colors), display: "flex", alignItems: "center", padding: "12px 0" }}>
        <KPI label="单日净流入门槛" value={String(support.threshold)} unit="亿" colors={colors}
          sub="进入托底日序列的最低金额" />
        <KPI label="历史托底日" value={String(support.sample_count)} unit="个" colors={colors}
          sub={`原始 ${support.raw_count} 个，剔除 ${support.raw_count - support.sample_count} 个事件窗口`} />
        <KPI label="托底日平均净流入" value={f1(avgIn)} unit="亿" colors={colors}
          sub={days.length ? `区间 ${f1(Math.min(...days.map((d) => d.net_inflow)))} – ${f1(Math.max(...days.map((d) => d.net_inflow)))} 亿` : "—"} />
        <KPI label="最近一次托底日" value={last?.date?.slice(5) ?? "—"} colors={colors}
          tone="up"
          sub={last ? `净流入 ${f1(last.net_inflow)} 亿 · 指数 ${sgn(last.index_chg)}${f2(last.index_chg)}%` : "—"} />
        <KPI
          label={meta.live ? "今日成交额分位" : "当日成交额分位"}
          value={q.toFixed(0)} unit="%"
          colors={colors}
          sub={`高于 ${below} / ${turns.length} 个托底日`}
        />
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1.4fr 1fr", gap: 14 }}>
        <div style={cardStyle(colors)}>
          <CardHead title={`${support.sample_count} 个历史托底日的净流入`} tag="单日净流入 > 100 亿" colors={colors} />
          <EChart option={barOpt} height={296} />
          <Note colors={colors}>
            每根柱是一个「单日净申购超过 100 亿」的交易日，颜色跟随当日指数涨跌。它回答的是
            「历史托底都发生在什么位置、量级多大」——把今天的读数放进来横向比对，就知道眼下这波够不够格
            进入托底日序列。看的是「钱」的横向水位。
          </Note>
        </div>
        <div style={cardStyle(colors)}>
          <CardHead title="成交额在托底日中的位置" tag={`分母 ${support.sample_count} 个样本`} colors={colors} />
          <EChart option={cdfOpt} height={296} />
          <Note colors={colors}>
            先看量、后看钱。这张累积分布图把 66 个托底日的成交额从低到高排开，红线是当日的预估位置，
            圆点是它落在的分位。成交额只是「钱来了」的旁证，真正确认要看份额——份额盘后才公布，
            盘中只能用成交额做前置判断。两张图一个看量、一个看方向，配起来才算完整。
          </Note>
        </div>
      </div>

      <div style={{ ...cardStyle(colors), background: colors.tableStripe }}>
        <CardHead title="样本剔除口径" tag="为何不是全部 > 100 亿的日子" colors={colors} />
        <div style={{ display: "flex", flexDirection: "column", gap: 6, marginTop: 8 }}>
          {support.excluded.map((e) => (
            <div key={e.start} style={{
              display: "grid", gridTemplateColumns: "190px 1fr", gap: 12,
              background: colors.card, border: `1px solid ${colors.border}`,
              borderRadius: 7, padding: "7px 11px", fontSize: 12.5,
            }}>
              <b style={{ color: colors.text, fontVariantNumeric: "tabular-nums" }}>{e.start} → {e.end}</b>
              <span style={{ color: colors.muted }}>{e.reason}</span>
            </div>
          ))}
        </div>
        <Note colors={colors} label="这张表在讲什么">
          这几段窗口里份额变化由事件驱动，单日净流入量级会失真。留在样本里会抬高「托底日」门槛，
          反而看不出真正的托底。
        </Note>
      </div>
    </>
  );
}

/* ── 日度全景 ───────────────────────────────────────────────────── */
function DailyCard({
  daily, colors, mode,
}: {
  daily: EtfFlowDashboard["daily"];
  colors: ReturnType<typeof useTheme>["colors"];
  mode: string;
}) {
  const opt = useMemo<EChartsOption>(() => {
    const n = daily.dates.length;
    return {
      grid: [
        { left: 52, right: 20, top: 26, height: 92 },
        { left: 52, right: 20, top: 154, height: 74 },
        { left: 52, right: 20, top: 262, height: 74 },
      ],
      tooltip: {
        trigger: "axis", backgroundColor: colors.card, borderColor: colors.border,
        textStyle: { color: colors.text, fontSize: 12 },
        formatter: (ps) => {
          const arr = ps as { dataIndex: number }[];
          const i = arr[0]?.dataIndex ?? 0;
          return `${daily.dates[i]}<br/>上证 <b>${f2(daily.index[i])}</b>（${sgn(daily.chg[i])}${f2(daily.chg[i])}%）`
            + `<br/>20ETF 成交 <b>${f1(daily.turnover[i])}</b> 亿`
            + `<br/>净流入 <b>${sgn(daily.net_inflow[i] ?? 0)}${f1(daily.net_inflow[i])}</b> 亿`;
        },
      },
      axisPointer: { link: [{ xAxisIndex: "all" }] },
      dataZoom: [
        { type: "inside", xAxisIndex: [0, 1, 2], start: 0, end: 100 },
        {
          type: "slider", xAxisIndex: [0, 1, 2], height: 16, bottom: 8,
          start: 0, end: 100,
          borderColor: "transparent", fillerColor: `${rgba(colors.accent,0.10)}`,
          handleStyle: { color: colors.card, borderColor: colors.border },
          textStyle: { color: colors.axis, fontSize: 9 },
        },
      ],
      xAxis: [
        { type: "category", data: daily.dates, gridIndex: 0, axisLabel: { show: false }, axisLine: { lineStyle: { color: colors.border } }, axisTick: { show: false } },
        { type: "category", data: daily.dates, gridIndex: 1, axisLabel: { show: false }, axisLine: { lineStyle: { color: colors.border } }, axisTick: { show: false } },
        {
          type: "category", data: daily.dates, gridIndex: 2,
          axisLabel: { color: colors.axis, fontSize: 10, hideOverlap: true },
          axisLine: { lineStyle: { color: colors.border } }, axisTick: { show: false },
        },
      ],
      yAxis: [
        {
          gridIndex: 0, scale: true,
          min: (v: { min: number }) => Math.floor(v.min / 200) * 200,
          max: (v: { max: number }) => Math.ceil(v.max / 200) * 200,
          axisLabel: { color: colors.axis, fontSize: 10 },
          splitLine: { lineStyle: { color: colors.border } }, splitNumber: 2,
        },
        {
          gridIndex: 1, scale: true, min: 0,
          max: (v: { max: number }) => Math.ceil(v.max / 500) * 500,
          axisLabel: { color: colors.axis, fontSize: 10 },
          splitLine: { lineStyle: { color: colors.border } }, splitNumber: 2,
        },
        {
          gridIndex: 2,
          axisLabel: { color: colors.axis, fontSize: 10 },
          splitLine: { lineStyle: { color: colors.border } }, splitNumber: 3,
        },
      ],
      series: [
        {
          name: "上证综指", type: "line", data: daily.index,
          xAxisIndex: 0, yAxisIndex: 0, showSymbol: false,
          lineStyle: { color: colors.accent, width: 1.4 },
        },
        {
          name: "成交额", type: "bar", data: daily.turnover,
          xAxisIndex: 1, yAxisIndex: 1, barWidth: "70%",
          itemStyle: { color: colors.muted, opacity: 0.42, borderRadius: [1, 1, 0, 0] },
        },
        {
          name: "净流入", type: "bar",
          xAxisIndex: 2, yAxisIndex: 2, barWidth: "70%",
          data: daily.net_inflow.map((v) => ({
            value: v,
            itemStyle: {
              color: (v ?? 0) >= 0 ? `${rgba(colors.up,0.77)}` : `${rgba(colors.down,0.77)}`,
              borderRadius: [1, 1, 0, 0],
            },
          })),
          markLine: {
            silent: true, symbol: "none",
            lineStyle: { color: colors.border, width: 1 },
            label: { show: false }, data: [{ yAxis: 0 }],
          },
        },
      ],
    };
  }, [daily, colors]);

  return (
    <div style={cardStyle(colors)}>
      <CardHead title="20 只重点 ETF 日度资金全景" tag={`${daily.dates.length} 个交易日`} colors={colors} />
      <EChart option={opt} height={420} />
      <Note colors={colors}>
        三段共享同一时间轴：指数在上、成交额在中、净流入在下。它回答的是「资金活跃度和方向在长周期里怎么变」——
        成交额持续抬升说明交投转热，净流入连续几天为正才是持续托底，而不是单日脉冲。区间可以拖拽缩放，
        适合拿来当趋势判断的底图。
      </Note>
    </div>
  );
}

/* ── 12 类宽基矩阵 ──────────────────────────────────────────────── */
function CategoryCard({
  category, colors,
}: {
  category: EtfFlowDashboard["category"];
  colors: ReturnType<typeof useTheme>["colors"];
}) {
  const HMAX = 26;
  const heat = (v: number) => {
    if (Math.abs(v) < 0.05) return { bg: "transparent", fg: colors.muted };
    const a = Math.min(1, Math.abs(v) / HMAX);
    const alpha = 0.07 + 0.5 * a;
    return v > 0
      ? { bg: `rgba(${rgb(colors.up)},${alpha})`, fg: alpha > 0.4 ? "#fff" : colors.up }
      : { bg: `rgba(${rgb(colors.down)},${alpha})`, fg: alpha > 0.4 ? "#fff" : colors.down };
  };

  // 月份分组：上层按月份 colspan 合并，下层是日号——比斜排 45° 好读，也省横向空间
  const groups = useMemo(() => {
    const g: { m: string; n: number }[] = [];
    category.dates.forEach((d) => {
      const m = d.slice(0, 7);
      const last = g[g.length - 1];
      if (last && last.m === m) last.n++;
      else g.push({ m, n: 1 });
    });
    return g;
  }, [category.dates]);

  if (!category.labels.length) {
    return (
      <div style={cardStyle(colors)}>
        <CardHead title="宽基 ETF 分指数净申赎矩阵" tag="最近 22 个交易日" colors={colors} />
        <Empty colors={colors}>分类数据尚未生成，点击「更新数据」后重试</Empty>
      </div>
    );
  }

  return (
    <div style={cardStyle(colors)}>
      <CardHead
        title="宽基 ETF 分指数净申赎矩阵"
        tag={`最近 ${category.dates.length} 个交易日 · 单位：亿元`}
        colors={colors}
      />
      <div style={{ overflowX: "auto", marginTop: 8 }}>
        <table style={{ borderCollapse: "separate", borderSpacing: 0, width: "100%", fontSize: 11.5 }}>
          <thead>
            <tr>
              <th style={{ width: 96 }} />
              {groups.map((g, i) => (
                <th key={i} colSpan={g.n} style={{
                  fontSize: 10, color: colors.axis, fontWeight: 500,
                  borderBottom: `1px solid ${colors.border}`, paddingBottom: 3,
                }}>
                  {Number(g.m.slice(5))}月
                </th>
              ))}
              <th style={{ width: 62 }} />
              <th style={{ width: 62 }} />
            </tr>
            <tr>
              <th />
              {category.dates.map((d) => (
                <th key={d} style={{
                  fontSize: 10, color: colors.axis, fontWeight: 500,
                  paddingBottom: 4, fontVariantNumeric: "tabular-nums",
                }}>{d.slice(8)}</th>
              ))}
              <th style={{ fontSize: 10, color: colors.axis, fontWeight: 500, textAlign: "left", paddingLeft: 8 }}>月合计</th>
              <th style={{ fontSize: 10, color: colors.axis, fontWeight: 500, textAlign: "right", paddingRight: 2 }}>规模</th>
            </tr>
          </thead>
          <tbody>
            {category.labels.map((name, r) => {
              const tot = category.month_total[r];
              return (
                <tr key={name}>
                  <th style={{
                    textAlign: "left", fontSize: 12.5, fontWeight: 500, color: colors.text,
                    paddingRight: 8, borderRight: `1px solid ${colors.border}`,
                    whiteSpace: "nowrap",
                  }}>{name}</th>
                  {category.matrix[r].map((v, c) => {
                    const s = heat(v);
                    const zero = Math.abs(v) < 0.05;
                    return (
                      <td key={c} title={`${name} · ${category.dates[c]} · ${v > 0 ? "净申购" : "净赎回"} ${Math.abs(v).toFixed(1)} 亿`}
                        style={{
                          background: s.bg, color: s.fg, textAlign: "center",
                          fontVariantNumeric: "tabular-nums", fontSize: 11,
                          height: 27, minWidth: 38, borderRadius: 3,
                        }}>
                        {zero ? "·" : `${sgn(v)}${v.toFixed(1)}`}
                      </td>
                    );
                  })}
                  <td style={{
                    textAlign: "left", paddingLeft: 8, fontWeight: 700,
                    fontVariantNumeric: "tabular-nums", fontSize: 11.5,
                    color: tot > 0 ? colors.up : tot < 0 ? colors.down : colors.muted,
                    borderLeft: `1px solid ${colors.border}`,
                  }}>{sgn(tot)}{f1(tot)}</td>
                  <td style={{
                    textAlign: "right", paddingRight: 2, fontSize: 11,
                    color: colors.muted, fontVariantNumeric: "tabular-nums",
                    borderLeft: `1px solid ${colors.border}`,
                  }}>{category.scale[r].toLocaleString()}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 9, fontSize: 11, color: colors.muted }}>
        <span>净赎回</span>
        <span style={{
          display: "block", width: 130, height: 8, borderRadius: 2,
          border: `1px solid ${colors.border}`,
          background: `linear-gradient(90deg, rgba(${rgb(colors.down)},.72) 0%, rgba(${rgb(colors.down)},.16) 42%, transparent 50%, rgba(${rgb(colors.up)},.16) 58%, rgba(${rgb(colors.up)},.72) 100%)`,
        }} />
        <span>净申购</span>
        <span style={{ marginLeft: "auto", color: colors.axis }}>颜色深浅 = 金额大小，非涨跌</span>
      </div>
      <Note colors={colors}>
        把全市场所有宽基 ETF 按跟踪指数分成 12 类，看每一类每天净申购还是净赎回。它回答的是
        「钱在宽基内部往哪个方向搬家」——某天沪深300 大幅净申购、同时中证1000 净赎回，说明资金从中小盘切向大盘；
        反之则是在博弹性。红色是钱进来、绿色是钱出去，颜色越深代表当天变动越大。单日大额往往是脉冲，
        连续多日同向才值得跟。
      </Note>
    </div>
  );
}

/* ── 通用小件 ───────────────────────────────────────────────────── */
function CardHead({
  title, tag, colors,
}: {
  title: string; tag: string;
  colors: ReturnType<typeof useTheme>["colors"];
}) {
  return (
    <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12, marginBottom: 6 }}>
      <div style={{ fontSize: 13.5, fontWeight: 600, color: colors.text }}>{title}</div>
      <div style={{
        fontSize: 11, color: colors.muted, background: colors.tableStripe,
        border: `1px solid ${colors.border}`, borderRadius: 5, padding: "2px 7px",
        whiteSpace: "nowrap", fontVariantNumeric: "tabular-nums",
      }}>{tag}</div>
    </div>
  );
}

function Note({
  children, colors, label = "这张图在讲什么",
}: {
  children: React.ReactNode;
  colors: ReturnType<typeof useTheme>["colors"];
  label?: string;
}) {
  return (
    <div style={{
      marginTop: 8, paddingTop: 9, borderTop: `1px dashed ${colors.border}`,
      fontSize: 11.5, lineHeight: 1.7, color: colors.muted,
    }}>
      <span style={{
        display: "inline-block", fontSize: 10, fontWeight: 600, color: colors.accent,
        background: `${rgba(colors.accent,0.08)}`, borderRadius: 3,
        padding: "0 5px", marginRight: 7,
      }}>{label}</span>
      {children}
    </div>
  );
}

function Empty({
  children, colors,
}: {
  children: React.ReactNode;
  colors: ReturnType<typeof useTheme>["colors"];
}) {
  return (
    <div style={{
      height: 160, display: "flex", alignItems: "center", justifyContent: "center",
      color: colors.muted, fontSize: 12.5, background: colors.tableStripe,
      borderRadius: 8, marginTop: 6,
    }}>{children}</div>
  );
}
