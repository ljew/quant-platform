import { useCallback, useEffect, useMemo, useState } from "react";
import type { EChartsOption } from "echarts";
import EChart from "../components/EChart";
import DocDrawer from "../components/DocDrawer";
import { useTheme } from "../theme";
// 设计说明的真源在 docs/（与 NSI-SPEC.md 同级），构建时以原文打进包里，
// 保证「页面上看到的」与「仓库里的文档」永远是同一份，不会各写一版。
import specMarkdown from "../../../docs/ETF-FLOW-SPEC.md?raw";
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
  // 支持 ?doc=1#etfflow 直接展开说明，便于把「设计说明」单独收藏成一个链接
  const [docOpen, setDocOpen] = useState(
    () => typeof window !== "undefined"
      && new URLSearchParams(window.location.search).get("doc") === "1",
  );
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

  const { meta, nsi, minute, support, daily, category, intraday_15m, summary, etf_detail, focus, valuation, elasticity } = data;
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
          {/* 分析逻辑随时可查：浮层打开，不打断看板的滚动位置与图表缩放 */}
          <button onClick={() => setDocOpen(true)} title="每张卡怎么算、阈值怎么来的、不能用来做什么" style={{
            background: "transparent", border: `1px solid ${colors.accent}`,
            color: colors.accent, borderRadius: 6, padding: "5px 11px",
            fontSize: 12, cursor: "pointer", whiteSpace: "nowrap", fontWeight: 600,
          }}>设计说明</button>
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
        sub="20 只 ETF 分钟量能、累计成交与上证综指同屏双轴对照 · 仅观察"
        colors={colors}
      />
      <MinuteCard meta={meta} minute={minute} colors={colors} mode={mode} />
      <TailCard tail={minute.tail} asOf={meta.as_of} colors={colors} />
      <ElasticityCard e={elasticity} colors={colors} />
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
      <ValuationCard v={valuation} t={data?.threshold_compare} colors={colors} />

      {/* ═══ 四、日度资金全景 ═══ */}
      <SectionTitle
        id="sec-daily" num="04" title="日度资金全景"
        sub={`2023 年至今 · ${daily.dates.length} 个交易日`}
        colors={colors}
      />
      <DailyCard daily={daily} colors={colors} mode={mode} />
      <CategoryCard category={category} colors={colors} />

      <ConsistencyCard data={data} colors={colors} />

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

      {/* 分析逻辑说明（浮层，来源 docs/ETF-FLOW-SPEC.md） */}
      <DocDrawer
        open={docOpen}
        onClose={() => setDocOpen(false)}
        title="国家队 ETF 资金看板 · 设计说明"
        subtitle="每张卡怎么算 · 阈值怎么来的 · 哪些事不能拿它做"
        markdown={specMarkdown}
      />
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
              <th style={{ textAlign: "right", padding: "0 8px 6px", fontWeight: 500, width: 96 }}>放量倍数</th>
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
                    {d.mult20 != null && d.mult20 >= 1.6 && !d.enough && (
                      <span style={{ fontSize: 9.5, fontWeight: 400, color: colors.axis, marginLeft: 4 }}>
                        量级不足
                      </span>
                    )}
                  </td>
                  <td style={{
                    padding: "6px 8px", textAlign: "right", fontVariantNumeric: "tabular-nums",
                    fontWeight: d.mult20 != null && d.mult20 >= 1.6 ? 700 : 500,
                    color: d.mult20 == null ? colors.muted
                      : d.mult20 >= 2.2 ? colors.up
                        : d.mult20 >= 1.6 ? "#b9770e"
                          : d.mult20 >= 1.0 ? colors.text : colors.muted,
                  }}>
                    {d.mult20 == null ? "—" : `${d.mult20.toFixed(2)}×`}
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
  const valLow = (forward.by_valuation ?? []).find((g) => g.label.startsWith("低"))?.t60;
  const valHigh = (forward.by_valuation ?? []).find((g) => g.label.startsWith("高"))?.t60;

  return (
    <div style={cardStyle(colors)}>
      <CardHead
        title={`托底之后：${forward.points.length} 个托底日的后市`}
        tag="一档等权净值口径 · 非可交易回测 · 三基准对照"
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

      {forward.benchmark && forward.index_stats && forward.core_stats && (
        <div style={{
          margin: "0 0 12px", padding: "9px 11px",
          background: rgba(colors.accent, 0.04),
          border: `1px solid ${colors.border}`, borderRadius: 8,
        }}>
          <div style={{ fontSize: 11.5, fontWeight: 600, color: colors.text, marginBottom: 5 }}>
            收益基准对照 · 同一批托底日，换个基准结论就变
          </div>
          <table style={{ borderCollapse: "collapse", width: "100%", fontSize: 11 }}>
            <thead>
              <tr style={{ color: colors.axis, fontSize: 10.5 }}>
                <th style={{ textAlign: "left", padding: "2px 8px 4px 0", fontWeight: 500 }}>基准</th>
                {hs.map((h) => (
                  <th key={h} style={{ textAlign: "right", padding: "2px 8px 4px", fontWeight: 500 }}>
                    T+{h} 胜率
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {[
                { label: forward.benchmark.main, data: forward.stats, main: true },
                { label: forward.benchmark.core, data: forward.core_stats, main: false },
                { label: forward.benchmark.index, data: forward.index_stats, main: false },
              ].map((r, i) => (
                <tr key={i} style={{ borderTop: `1px solid ${colors.border}` }}>
                  <td style={{
                    padding: "4px 8px 4px 0",
                    color: r.main ? colors.text : colors.muted,
                    fontWeight: r.main ? 700 : 400,
                  }}>
                    {r.label}
                    {r.main && <span style={{ color: colors.accent, fontSize: 10, marginLeft: 5 }}>主</span>}
                  </td>
                  {hs.map((h) => {
                    const s = r.data[String(h)];
                    const w = s?.win ?? 0;
                    return (
                      <td key={h} style={{
                        textAlign: "right", padding: "4px 8px",
                        color: w >= 55 ? colors.up : w <= 45 ? colors.down : colors.text,
                        fontVariantNumeric: "tabular-nums",
                      }}>
                        {s && s.n ? `${w.toFixed(1)}%` : "—"}
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
          <div style={{ fontSize: 10.5, color: colors.axis, marginTop: 6, lineHeight: 1.6 }}>
            {forward.benchmark.note}
          </div>
        </div>
      )}

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

      {(() => {
        const gs = (forward.by_valuation ?? []).filter((g) => g.t60 && g.t60.n > 0);
        if (!gs.length) return null;
        return (
          <div style={{ marginTop: 14, paddingTop: 12, borderTop: `1px solid ${colors.border}` }}>
            <div style={{
              display: "flex", alignItems: "baseline", gap: 10, marginBottom: 9, flexWrap: "wrap",
            }}>
              <span style={{ fontSize: 12.5, fontWeight: 600, color: colors.text }}>
                按托底时的估值分位分组 · 看 T+60
              </span>
              <span style={{ fontSize: 11, color: colors.muted }}>
                方向有差，但换基准后差距收窄、未通过显著性检验
              </span>
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: 7 }}>
              {gs.map((g) => {
                const st = g.t60!;
                const tone = st.win >= 60 ? colors.up : st.win <= 40 ? colors.down : colors.muted;
                return (
                  <div key={g.label} style={{
                    display: "grid",
                    gridTemplateColumns: "128px 1fr 66px 96px 76px",
                    alignItems: "center", gap: 10, fontSize: 12,
                  }}>
                    <span style={{ color: colors.text, whiteSpace: "nowrap" }}>{g.label}</span>
                    <div style={{
                      height: 8, background: colors.tableStripe,
                      borderRadius: 4, overflow: "hidden",
                    }}>
                      <div style={{
                        width: `${Math.max(2, st.win)}%`, height: "100%",
                        background: tone, opacity: 0.75, borderRadius: 4,
                      }} />
                    </div>
                    <span style={{
                      textAlign: "right", fontWeight: 700, color: tone,
                      fontVariantNumeric: "tabular-nums",
                    }}>{st.win.toFixed(0)}%</span>
                    <span style={{
                      textAlign: "right", fontVariantNumeric: "tabular-nums",
                      color: st.median >= 0 ? colors.up : colors.down,
                    }}>中位 {sgn(st.median)}{f2(st.median)}%</span>
                    <span style={{ textAlign: "right", color: colors.axis, fontSize: 11 }}>
                      n={st.n}
                    </span>
                  </div>
                );
              })}
            </div>
            <div style={{
              marginTop: 9, fontSize: 11.5, color: colors.muted, lineHeight: 1.7,
            }}>
              估值的意义不在「决定会不会托底」（中位分位 62%，两头都有），而在
              <strong style={{ color: colors.text }}>决定托底之后值不值得跟</strong>：
              低估值组的 T+60 胜率与中位收益都明显高于高估值组。
              中段样本很少（n 见右列），只作参考，重点看两端。
            </div>

            {(() => {
              const dd = forward.dedup;
              if (!dd || !dd.low || !dd.high) return null;
              return (
                <div style={{
                  marginTop: 7, padding: "7px 10px",
                  background: colors.tableStripe, borderRadius: 6,
                  fontSize: 11.5, lineHeight: 1.75, color: colors.muted,
                }}>
                  <b style={{ color: colors.text }}>稳健性检验 · 按托底段去重</b>
                  <span style={{ color: colors.axis }}>
                    （{dd.segments} 段，连续多日只取首日，避免同一段行情被重复计数）
                  </span>
                  <br />
                  低估值 <b style={{ color: colors.up }}>{dd.low.win.toFixed(0)}%</b>
                  <span style={{ color: colors.axis }}>（n={dd.low.n}，中位 {sgn(dd.low.median)}{f2(dd.low.median)}%）</span>
                  {" · "}
                  高估值 <b style={{ color: colors.down }}>{dd.high.win.toFixed(0)}%</b>
                  <span style={{ color: colors.axis }}>（n={dd.high.n}，中位 {sgn(dd.high.median)}{f2(dd.high.median)}%）</span>
                  <br />
                  <span style={{ color: colors.text }}>
                    方向不变，但样本掉到 {dd.low.n}/{dd.high.n}，两者 95% 置信区间
                    <strong>已经重叠</strong> —— 这只能当假设看，还不能当规则用。
                  </span>
                </div>
              );
            })()}
          </div>
        );
      })()}

      <Note colors={colors}>
        这张图回答的是「托底之后会怎样」，而前面几张只回答「今天像不像托底」。
        三个横截面各有含义：<strong>T+5 看情绪</strong>（通常还没走出来）、
        <strong>T+20 看资金落地</strong>、<strong>T+60 看趋势是否成立</strong>。
        横截面越长，样本越少（最近的托底日还没有 60 个交易日的后市数据），所以每格都标了 n。
        <br />
        <span style={{ color: colors.text }}>
          要特别说明：上面 T+60 的 {forward.stats["60"]?.win?.toFixed(1) ?? "—"}% 是
          <strong>两拨方向不同的样本平均出来的</strong>——
          低估值组 {valLow?.win?.toFixed(0) ?? "—"}%、高估值组 {valHigh?.win?.toFixed(0) ?? "—"}%。
          但换成一档等权净值口径后差距已明显收窄，段去重后 Fisher 精确检验 p≈0.24，
          <strong>未通过显著性检验</strong>。这张分组只能当方向提示，不能当独立依据。
        </span>
        <br />
        <span style={{ color: colors.axis }}>
          主基准是<strong>一档等权净值</strong>（实际被托底的宽基组合），上证综指与 510300 见上方对照表；
          同一批托底日换个基准，T+20 胜率能差 4–11pp，所以别只记住一个数字。
          注意这是<strong>事后统计</strong>，不是可交易策略回测：没扣成本、没算滑点、
          没做样本外；连续托底日的持有期还会互相重叠，样本并不独立。
          它的用途是校准预期，不能当收益承诺，更不该据此定仓位 —— 那件事归 NSI。
        </span>
      </Note>
    </div>
  );
}

/* ── 估值 × 资金流 ──────────────────────────────────────────────── */
function ValuationCard({
  v, t, colors,
}: {
  v: EtfFlowDashboard["valuation"] | undefined;
  t: EtfFlowDashboard["threshold_compare"];
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

      {t && (
        <div style={{
          margin: "2px 0 12px", padding: "9px 11px",
          background: "rgba(185,119,14,0.06)",
          border: `1px solid ${colors.border}`,
          borderLeft: "3px solid #b9770e", borderRadius: 8,
        }}>
          <div style={{ fontSize: 11.5, fontWeight: 600, color: colors.text, marginBottom: 5 }}>
            门槛口径对照 · 100 亿是固定金额，但全池成交额三年涨了 3.2 倍
          </div>
          <table style={{ borderCollapse: "collapse", width: "100%", fontSize: 11 }}>
            <thead>
              <tr style={{ color: colors.axis, fontSize: 10.5 }}>
                <th style={{ textAlign: "left", padding: "2px 8px 4px 0", fontWeight: 500 }}>口径</th>
                <th style={{ textAlign: "right", padding: "2px 8px 4px", fontWeight: 500 }}>天数</th>
                <th style={{ textAlign: "right", padding: "2px 8px 4px", fontWeight: 500 }}>估值分位中位</th>
                <th style={{ textAlign: "right", padding: "2px 8px 4px", fontWeight: 500 }}>≤20% 占比</th>
                <th style={{ textAlign: "right", padding: "2px 8px 4px", fontWeight: 500 }}>≥80% 占比</th>
                <th style={{ textAlign: "right", padding: "2px 8px 4px", fontWeight: 500 }}>T+20 胜率</th>
              </tr>
            </thead>
            <tbody>
              {[
                { label: "绝对 · 净流入 > 100 亿", d: t.absolute, main: true },
                { label: `相对 · 净流入/20日成交额中位 > ${t.ratio}`, d: t.relative, main: false },
              ].map((r, i) => (
                <tr key={i} style={{ borderTop: `1px solid ${colors.border}` }}>
                  <td style={{
                    padding: "4px 8px 4px 0",
                    color: r.main ? colors.text : colors.muted,
                    fontWeight: r.main ? 700 : 400,
                  }}>
                    {r.label}{r.main && <span style={{ color: colors.accent, fontSize: 10, marginLeft: 5 }}>主</span>}
                  </td>
                  <td style={{ padding: "4px 8px", textAlign: "right", color: colors.text,
                               fontVariantNumeric: "tabular-nums" }}>{r.d.count}</td>
                  <td style={{ padding: "4px 8px", textAlign: "right", fontWeight: 600,
                               color: "#b9770e", fontVariantNumeric: "tabular-nums" }}>
                    {r.d.pct_median == null ? "—" : `${r.d.pct_median.toFixed(1)}%`}
                  </td>
                  <td style={{ padding: "4px 8px", textAlign: "right", color: colors.muted,
                               fontVariantNumeric: "tabular-nums" }}>
                    {r.d.low_share == null ? "—" : `${r.d.low_share.toFixed(1)}%`}
                  </td>
                  <td style={{ padding: "4px 8px", textAlign: "right", color: colors.muted,
                               fontVariantNumeric: "tabular-nums" }}>
                    {r.d.high_share == null ? "—" : `${r.d.high_share.toFixed(1)}%`}
                  </td>
                  <td style={{ padding: "4px 8px", textAlign: "right", color: colors.muted,
                               fontVariantNumeric: "tabular-nums" }}>
                    {r.d.fwd["20"] == null ? "—" : `${r.d.fwd["20"].toFixed(1)}%`}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <div style={{ marginTop: 6, fontSize: 10.5, color: colors.axis, lineHeight: 1.65 }}>
            2026 年占比 {t.absolute.by_year.find((y) => y.year === "2026")?.share ?? 0}%
            → {t.relative.by_year.find((y) => y.year === "2026")?.share ?? 0}%
            ｜ 2024 年 {t.absolute.by_year.find((y) => y.year === "2024")?.share ?? 0}%
            → {t.relative.by_year.find((y) => y.year === "2024")?.share ?? 0}%
            <br />{t.note}
          </div>
          <div style={{ marginTop: 6, fontSize: 11, fontWeight: 600, color: "#b9770e", lineHeight: 1.65 }}>
            ⚠️ {t.verdict}
          </div>
        </div>
      )}

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

/* ── 一档弹性判据 ──────────────────────────────────────────────── */
function ElasticityCard({
  e, colors,
}: {
  e: EtfFlowDashboard["elasticity"] | undefined;
  colors: ReturnType<typeof useTheme>["colors"];
}) {
  const members = e?.members ?? [];
  const j = e?.judge;
  const sf = e?.sign_flag;
  const av = e?.availability;
  const avState = av?.state ?? "ok";
  const avDisabled = avState === "disabled";
  const avDegraded = avState === "degraded";
  const curBand = (e?.bands ?? []).find((b) => b.label === j?.band);
  const pitTop = (e?.bands ?? []).find((b) => b.label === ">2.2×");
  const sampleTop = (e?.bands_sample ?? []).find((b) => b.label === ">2.2×");
  const pit = e?.pit;

  const opt = useMemo<EChartsOption>(() => {
    const pts = e?.recent ?? [];
    return {
      grid: { left: 38, right: 10, top: 18, bottom: 22 },
      tooltip: {
        backgroundColor: colors.card, borderColor: colors.border,
        textStyle: { color: colors.text, fontSize: 12 },
        formatter: (ps) => {
          const arr = ps as unknown as { dataIndex: number }[];
          const i = arr[0]?.dataIndex;
          const x = pts[i];
          if (!x) return "";
          return `<div style="font-size:11px;color:${colors.muted};margin-bottom:3px">${x.date}</div>`
            + `成交额 <b>${f1(x.amount)}</b> 亿 · 基准 ${x.base == null ? "—" : f1(x.base)} 亿<br/>`
            + `倍数 <b>${x.mult == null ? "—" : x.mult.toFixed(2) + "×"}</b>`
            + (x.net == null ? "" : ` · 净申购 <b>${sgn(x.net)}${f1(x.net)}</b> 亿`);
        },
      },
      xAxis: {
        type: "category",
        data: pts.map((x) => x.date.slice(5)),
        axisLine: { lineStyle: { color: colors.border } },
        axisTick: { show: false },
        axisLabel: { color: colors.axis, fontSize: 9, hideOverlap: true,
                     interval: (i: number) => i % 4 === 0 || i === pts.length - 1 },
      },
      yAxis: {
        type: "value", min: 0,
        axisLabel: { color: colors.axis, fontSize: 9, formatter: "{value}×" },
        splitLine: { lineStyle: { color: colors.border } },
        splitNumber: 3,
      },
      series: [{
        type: "bar", barWidth: "58%",
        data: pts.map((x) => ({
          value: x.mult,
          itemStyle: {
            color: x.mult == null ? rgba(colors.axis, 0.25)
              : x.mult >= 2.2 ? colors.up
                : x.mult >= 1.6 ? rgba(colors.up, 0.72)
                  : x.mult >= 1.0 ? rgba(colors.up, 0.34)
                    : rgba(colors.axis, 0.45),
            borderRadius: [2, 2, 0, 0],
          },
        })),
        markLine: {
          silent: true, symbol: "none", animation: false,
          lineStyle: { color: colors.axis, type: "dashed", width: 1, opacity: 0.55 },
          label: { show: false },
          data: [{ yAxis: 1.0 }, { yAxis: 2.2 }],
        },
      }],
    };
  }, [e, colors]);

  const trendOpt = useMemo<EChartsOption>(() => {
    const pts = e?.trend ?? [];
    return {
      grid: { left: 36, right: 12, top: 12, bottom: 20 },
      tooltip: {
        trigger: "axis",
        backgroundColor: colors.card, borderColor: colors.border,
        textStyle: { color: colors.text, fontSize: 12 },
        formatter: (ps) => {
          const arr = ps as unknown as { axisValue: string; value: number }[];
          const a0 = arr?.[0];
          return a0 ? `${a0.axisValue}　弹性 b = <b>${Number(a0.value).toFixed(2)}</b>` : "";
        },
      },
      xAxis: {
        type: "category",
        data: pts.map((x) => x.date.slice(2, 7)),
        axisLine: { lineStyle: { color: colors.border } },
        axisTick: { show: false },
        axisLabel: { color: colors.axis, fontSize: 9, hideOverlap: true,
                     interval: (i: number) => i % 30 === 0 || i === pts.length - 1 },
      },
      yAxis: {
        type: "value", min: -1, max: 1,
        axisLabel: { color: colors.axis, fontSize: 9 },
        splitLine: { lineStyle: { color: colors.border } },
        splitNumber: 2,
      },
      series: [{
        type: "line", data: pts.map((x) => x.b),
        showSymbol: false, smooth: false,
        lineStyle: { color: colors.accent, width: 1.5 },
        itemStyle: { color: colors.accent },
        // 不铺面积：曲线穿过 0 轴时，面积会变成「线下全是填充」，
        // 视觉上像是负值区更强，实际没有含义
        markLine: {
          silent: true, symbol: "none", animation: false,
          label: { show: false },
          data: [
            { yAxis: 0, lineStyle: { color: colors.muted, type: "solid", width: 1 } },
            { yAxis: 0.8, lineStyle: { color: colors.axis, type: "dashed", width: 1, opacity: 0.6 } },
          ],
        },
      }],
    };
  }, [e, colors]);

  if (!e?.available || !j) return null;

  const toneOf = (v: string | null) =>
    v === "高度疑似" ? colors.up
      : v === "疑似入场" ? "#b9770e"
        : v === "有异动" ? "#b9770e"
          : colors.muted;

  const tone = toneOf(j.verdict);
  const sfTone = sf?.tone === "up" ? colors.up : sf?.tone === "down" ? colors.down
    : sf?.tone === "warn" ? "#b9770e" : colors.muted;
  const sfBg = sf?.tone === "warn" ? "#b9770e0c" : rgba(
    sf?.tone === "up" ? colors.up : sf?.tone === "down" ? colors.down : colors.border, 0.05);

  const tier1 = members.filter((m) => m.tier === "一档");
  const tier2n = members.length - tier1.length;

  return (
    <div style={cardStyle(colors)}>
      <CardHead
        title="一档弹性判据 · 成交额能否反推申购"
        tag={`${e.as_of ?? ""} · 一档 ${tier1.length} 只 / 二档 ${tier2n} 只 · 占全池成交额 ${e.tier1_share ?? "—"}% · PIT 口径`}
        colors={colors}
      />

      {av && avState !== "ok" && (
        <div style={{
          display: "flex", alignItems: "baseline", gap: 8,
          background: avDisabled ? rgba(colors.down, 0.07) : "rgba(185,119,14,0.07)",
          borderLeft: `3px solid ${avDisabled ? colors.down : "#b9770e"}`,
          borderRadius: 6, padding: "7px 10px", margin: "2px 0 10px",
          fontSize: 11.5, lineHeight: 1.65, color: colors.text,
        }}>
          <b style={{ color: avDisabled ? colors.down : "#b9770e", flexShrink: 0 }}>
            {avDisabled ? "判据停用" : "判据降级"}
          </b>
          <span style={{ color: colors.muted }}>{av.note}</span>
        </div>
      )}

      <div style={{ display: "grid", gridTemplateColumns: "250px 1fr", gap: 18,
                    alignItems: "center", margin: "2px 0 4px" }}>
        <div>
          <div style={{ fontSize: 11, color: colors.muted, marginBottom: 2 }}>
            一档 {tier1.length} 只合计放量倍数
          </div>
          <div style={{
            fontSize: 32, fontWeight: 700, lineHeight: 1.1, color: tone,
            fontVariantNumeric: "tabular-nums",
          }}>
            {j.mult == null ? "—" : `${j.mult.toFixed(2)}×`}
          </div>
          <div style={{ fontSize: 13, fontWeight: 600, color: tone, marginTop: 4 }}>
            {j.verdict ?? "—"}
          </div>
          <div style={{ fontSize: 11.5, color: colors.muted, marginTop: 6, lineHeight: 1.7 }}>
            成交 <b style={{ color: colors.text }}>{f1(j.amount)}</b> 亿
            ／ 20 日中位 {j.base == null ? "—" : f1(j.base)} 亿
            <br />
            当日净申购 <b style={{ color: (j.net ?? 0) >= 0 ? colors.up : colors.down }}>
              {j.net == null ? "待公布" : `${sgn(j.net)}${f1(j.net)}`}
            </b> 亿
            <br />
            历史同档「国家队级」概率{" "}
            <b style={{ color: colors.text }}>
              {j.prob == null ? "—"
                : curBand?.hits === 0 && curBand.ci_hi != null
                  ? `<${curBand.ci_hi.toFixed(1)}%`
                  : `${j.prob.toFixed(1)}%`}
            </b>
            <span style={{ color: colors.axis }}>（基准 {j.base_rate ?? "—"}%）</span>
            {curBand?.ci_lo != null && curBand.ci_hi != null && (
              <span style={{ color: colors.axis }}>
                <br />95% 区间 {curBand.ci_lo.toFixed(1)}–{curBand.ci_hi.toFixed(1)}%
              </span>
            )}
          </div>
        </div>
        <EChart option={opt} height={186} />
      </div>

      <div style={{ overflowX: "auto", marginTop: 6,
                    opacity: avDisabled ? 0.42 : avDegraded ? 0.85 : 1 }}>
        <table style={{ borderCollapse: "collapse", width: "100%", fontSize: 11.5 }}>
          <thead>
            <tr style={{ color: colors.axis, fontSize: 10.5 }}>
              <th style={{ textAlign: "left", padding: "0 8px 6px 2px", fontWeight: 500 }}>倍数档</th>
              <th style={{ textAlign: "right", padding: "0 8px 6px", fontWeight: 500, width: 56 }}>n</th>
              <th style={{ textAlign: "right", padding: "0 8px 6px", fontWeight: 500, width: 92 }}>净申购中位</th>
              <th style={{ textAlign: "right", padding: "0 8px 6px", fontWeight: 500, width: 72 }}>为正</th>
              <th style={{ textAlign: "right", padding: "0 8px 6px", fontWeight: 500, width: 96 }}>国家队级</th>
              <th style={{ textAlign: "right", padding: "0 8px 6px", fontWeight: 500, width: 104 }}>95% 区间</th>
              <th style={{ textAlign: "right", padding: "0 8px 6px", fontWeight: 500, width: 64 }}>提升</th>
              <th style={{ textAlign: "left", padding: "0 2px 6px 10px", fontWeight: 500 }}>判定</th>
            </tr>
          </thead>
          <tbody>
            {(e.bands ?? []).map((b) => {
              const cur = b.label === j.band;
              return (
                <tr key={b.label} style={{
                  borderTop: `1px solid ${colors.border}`,
                  background: cur ? rgba(colors.accent, 0.06) : undefined,
                }}>
                  <td style={{ padding: "5px 8px 5px 2px", color: colors.text,
                               fontVariantNumeric: "tabular-nums", fontWeight: cur ? 700 : 400 }}>
                    {b.label}
                    {cur && <span style={{ color: colors.accent, marginLeft: 5, fontSize: 10 }}>← 当前</span>}
                  </td>
                  <td style={{ padding: "5px 8px", textAlign: "right", color: colors.muted,
                               fontVariantNumeric: "tabular-nums" }}>{b.n}</td>
                  <td style={{ padding: "5px 8px", textAlign: "right",
                               color: (b.median ?? 0) >= 0 ? colors.up : colors.down,
                               fontVariantNumeric: "tabular-nums" }}>
                    {b.median == null ? "—" : `${sgn(b.median)}${f1(b.median)}`}
                  </td>
                  <td style={{ padding: "5px 8px", textAlign: "right", color: colors.muted,
                               fontVariantNumeric: "tabular-nums" }}>
                    {b.positive == null ? "—" : `${b.positive.toFixed(0)}%`}
                  </td>
                  <td style={{ padding: "5px 8px", textAlign: "right", fontWeight: 600,
                               color: (b.big ?? 0) >= 20 ? colors.up : colors.text,
                               fontVariantNumeric: "tabular-nums" }}>
                    {b.big == null ? "—"
                      : b.hits === 0 && b.ci_hi != null
                        ? `<${b.ci_hi.toFixed(1)}%`
                        : `${b.big.toFixed(1)}%`}
                  </td>
                  <td style={{ padding: "5px 8px", textAlign: "right", color: colors.axis,
                               fontSize: 10.5, fontVariantNumeric: "tabular-nums" }}>
                    {b.ci_lo == null || b.ci_hi == null
                      ? "—" : `${b.ci_lo.toFixed(1)}–${b.ci_hi.toFixed(1)}`}
                  </td>
                  <td style={{ padding: "5px 8px", textAlign: "right", color: colors.muted,
                               fontVariantNumeric: "tabular-nums" }}>
                    {b.lift == null ? "—" : `${b.lift.toFixed(1)}×`}
                  </td>
                  <td style={{ padding: "5px 2px 5px 10px", color: toneOf(b.verdict), fontWeight: cur ? 700 : 400 }}>
                    {b.verdict}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      <div style={{ fontSize: 10.5, color: colors.axis, marginTop: 6, lineHeight: 1.65 }}>
        本表是 <b style={{ color: colors.muted }}>point-in-time 口径</b>：每个历史交易日只用截至当天的数据
        重选一档名单再算倍数，不含未来信息。有效 {pit?.valid_days ?? "—"} / {pit?.total_days ?? "—"} 天
        （覆盖 {pit?.coverage ?? "—"}%，其余日子选不满一档或历史窗口不足，直接跳过）；
        当时一档只数中位 {pit?.tier1_median ?? "—"} 只，与今天名单平均重合 {pit?.overlap_mean ?? "—"} 只。
        <br />
        95% 区间为 Wilson 区间；0 次命中写成「&lt;上界」而不是 0%。
        {sampleTop && pitTop && (
          <span> 对照：拿<b>今天名单回溯全历史</b>的样本内口径会给出 {sampleTop.big?.toFixed(1)}% /
            {sampleTop.lift?.toFixed(1)}×，PIT 口径只有 {pitTop.big?.toFixed(1)}% /
            {pitTop.lift?.toFixed(1)}× —— 选择偏差把 lift 高估了
            {sampleTop.lift && pitTop.lift
              ? ` ${(sampleTop.lift / pitTop.lift).toFixed(1)} 倍` : "不少"}。</span>
        )}
        {avDisabled && <b style={{ color: colors.down }}> 判据已停用，本表不参与今日结论。</b>}
      </div>

      {sf && (
        <div style={{
          background: sfBg, border: `1px solid ${sfTone}33`,
          borderLeft: `3px solid ${sfTone}`,
          borderRadius: 7, padding: "8px 11px", marginTop: 10,
          fontSize: 11.5, lineHeight: 1.7, color: colors.text,
        }}>
          <b style={{ color: sfTone }}>
            {sf.regime === "regime_shift" ? "状态切换"
              : sf.regime === "aligned_sell" ? "判据反向"
                : sf.regime === "aligned_buy" ? "判据有效" : "关系不稳"}
          </b>
          <span style={{ color: colors.muted }}> · {sf.note}</span>
        </div>
      )}

      <div style={{ marginTop: 10 }}>
        <div style={{
          fontSize: 11, color: colors.muted, marginBottom: 2,
          display: "flex", justifyContent: "space-between", gap: 10,
        }}>
          <span>一档合计的滚动 60 日弹性 b（近 250 个交易日）</span>
          <span style={{ color: colors.axis }}>
            虚线 0.8 = 一档门槛 ｜ 实线 0 = 方向分界
          </span>
        </div>
        <EChart option={trendOpt} height={140} />
      </div>

      <div style={{ overflowX: "auto", marginTop: 10 }}>
        <table style={{ borderCollapse: "collapse", width: "100%", fontSize: 11.5 }}>
          <thead>
            <tr style={{ color: colors.axis, fontSize: 10.5 }}>
              <th style={{ textAlign: "left", padding: "0 8px 5px 2px", fontWeight: 500, width: 92 }}>代码</th>
              <th style={{ textAlign: "left", padding: "0 8px 5px", fontWeight: 500 }}>名称</th>
              <th style={{ textAlign: "right", padding: "0 8px 5px", fontWeight: 500, width: 60 }}>b(21)</th>
              <th style={{ textAlign: "right", padding: "0 8px 5px", fontWeight: 500, width: 60 }}>b(60)</th>
              <th style={{ textAlign: "right", padding: "0 8px 5px", fontWeight: 500, width: 64 }}>b(120)</th>
              <th style={{ textAlign: "right", padding: "0 8px 5px", fontWeight: 500, width: 56 }}>R²(60)</th>
              <th style={{ textAlign: "right", padding: "0 2px 5px 8px", fontWeight: 500, width: 78 }}>规模</th>
            </tr>
          </thead>
          <tbody>
            {tier1.map((m) => (
              <tr key={m.symbol} style={{ borderTop: `1px solid ${colors.border}` }}>
                <td style={{ padding: "4px 8px 4px 2px", color: colors.muted,
                             fontVariantNumeric: "tabular-nums" }}>{m.symbol}</td>
                <td style={{ padding: "4px 8px", color: colors.text }}>
                  {m.name}
                  {m.marginal && (
                    <span style={{ fontSize: 10, color: "#b9770e", marginLeft: 6 }}>边缘</span>
                  )}
                </td>
                {[m.b21, m.b60, m.b120].map((v, i) => (
                  <td key={i} style={{
                    padding: "4px 8px", textAlign: "right",
                    color: v == null ? colors.axis : v >= 0.8 ? colors.text : colors.muted,
                    fontVariantNumeric: "tabular-nums",
                  }}>{v == null ? "—" : v.toFixed(2)}</td>
                ))}
                <td style={{ padding: "4px 8px", textAlign: "right", color: colors.muted,
                             fontVariantNumeric: "tabular-nums" }}>
                  {m.r2_60 == null ? "—" : m.r2_60.toFixed(2)}
                </td>
                <td style={{ padding: "4px 2px 4px 8px", textAlign: "right", color: colors.muted,
                             fontVariantNumeric: "tabular-nums" }}>
                  {m.scale ? `${m.scale.toLocaleString()}` : "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {(() => {
        // 二档按 b60 降序展示，否则顺序毫无意义（原顺序是 hits+b21，b60 会忽高忽低）
        const t2 = members.filter((m) => m.tier !== "一档")
          .slice()
          .sort((a, x) => (x.b60 ?? -9) - (a.b60 ?? -9))
          .slice(0, 5);
        if (!t2.length) return null;
        return (
          <div style={{
            marginTop: 6, fontSize: 11, color: colors.axis, lineHeight: 1.7,
          }}>
            二档 {members.length - tier1.length} 只（成交额与申购已脱钩，只能等收盘看份额）：
            {t2.map((m, i) => (
              <span key={m.symbol}>
                {i > 0 ? " · " : " "}
                {m.name.replace(/ETF|成份/g, "")}
                <span style={{ color: colors.muted }}> b60={m.b60 == null ? "—" : m.b60.toFixed(2)}</span>
              </span>
            ))}
            {members.length - tier1.length > 5 ? " …" : ""}
          </div>
        );
      })()}

      <Note colors={colors} label="这张表在讲什么">
        盘中拿不到份额，只能看成交额。但<strong>成交额能不能反推申购，要逐只检验</strong>：
        把「当日成交额 → 当日净申购」做回归，弹性 b 接近 1、R² 接近 1 的，说明成交额基本
        就是申购的镜像，盘中放量可以直接读；b 低、R² 低的，成交额里混了大量二级换手，
        只能等收盘看份额。
        <br />
        一档的门槛是<strong>三个窗口（21/60/120 日）里至少两个</strong>满足 b≥0.80 且 R²≥0.50。
        用多数票而不是单一窗口，是因为 21 个点的回归 R² 容易虚高，而 120 日窗口一旦跨进
        另一种资金状态就会失真 —— 标了「边缘」的那几只，b 就贴着 0.80 上下摆，
        一天数据就能让它翻档，别当信号看。
        <br />
        <span style={{ color: colors.axis }}>
          口径提醒：这里的「国家队级」指一档合计单日净申购 &gt; 100 亿，比第 02 节「20 只托底日」
          更严（池子更小、只含主力通道），两个数不能直接比。
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

      {(() => {
        const w = nsi.window;
        if (!w || !w.active) return null;
        const ratio = w.hold_days ? Math.min(1, w.used / w.hold_days) : 0;
        const tone = w.weakened ? "#b9770e" : colors.accent;
        return (
          <div style={{ margin: "0 0 12px" }}>
            <div style={{
              display: "flex", alignItems: "baseline", gap: 10,
              flexWrap: "wrap", marginBottom: 5,
            }}>
              <span style={{ fontSize: 11.5, fontWeight: 600, color: colors.text }}>加仓窗口</span>
              {/* 已用天数由左侧 KPI「已加仓 15 个交易日」和进度条表达，
                  这里只讲「还剩多少、哪天到期」，避免同一个数字连说两遍 */}
              <span style={{ fontSize: 12.5, fontWeight: 700, color: tone }}>
                剩 {w.remaining} 个交易日
              </span>
              {w.expire && (
                <span style={{ fontSize: 11, color: colors.axis }}>· {w.expire} 到期</span>
              )}
              <span style={{ fontSize: 10.5, color: colors.axis, marginLeft: "auto" }}>
                单向状态机：score 低不减仓，到期自然回落
              </span>
            </div>
            {/* 底色必须带一点 tone 色：用 tableStripe（近白）时，白色的卡片上
                看不出「还剩一截」，进度条会一直像走满了 */}
            <div style={{
              height: 6, background: rgba(tone, 0.2),
              borderRadius: 3, overflow: "hidden",
            }}>
              <div style={{
                width: `${ratio * 100}%`, height: "100%",
                background: tone, opacity: 0.72, borderRadius: 3,
              }} />
            </div>
            <div style={{
              fontSize: 11.5, color: colors.muted, marginTop: 5, lineHeight: 1.6,
            }}>{w.note}</div>
          </div>
        );
      })()}

      {(() => {
        const c = nsi.consistency;
        if (!c) return null;
        const tone = c.status === "aligned"
          ? { fg: colors.accent, bg: rgba(colors.accent, 0.05), bd: rgba(colors.accent, 0.28) }
          : c.status === "divergent"
            ? { fg: "#b9770e", bg: "#b9770e0c", bd: "#b9770e44" }
            : { fg: colors.muted, bg: colors.tableStripe, bd: colors.border };
        const evTxt = c.evidence === "strong" ? "份额口径（强）"
          : c.evidence === "weak" ? "量能口径（弱，分不清买卖）" : "无当日数据";
        return (
          <div style={{
            background: tone.bg, border: `1px solid ${tone.bd}`,
            borderLeft: `3px solid ${tone.fg}`,
            borderRadius: 8, padding: "9px 12px", margin: "0 0 12px",
          }}>
            <div style={{ display: "flex", alignItems: "baseline", gap: 9, flexWrap: "wrap", marginBottom: 4 }}>
              <span style={{
                fontSize: 11, fontWeight: 700, color: tone.fg,
                background: rgba(tone.fg === colors.muted ? colors.border : tone.fg, 0.13),
                borderRadius: 3, padding: "1px 6px",
              }}>{c.label}</span>
              <span style={{ fontSize: 12, color: colors.text }}>
                信号 <b>{c.signal}</b>
              </span>
              <span style={{ fontSize: 12, color: colors.muted }}>
                观察 <b style={{ color: colors.text }}>{c.observation}</b>
              </span>
            </div>
            <div style={{ fontSize: 11.5, color: colors.muted, lineHeight: 1.7 }}>
              {c.note}
              <span style={{ color: colors.axis }}>
                {" "}· 证据强度：{evTxt} · 冲突时以 NSI 为准（它经过回测，观察视图不进任何策略）
              </span>
            </div>
          </div>
        );
      })()}

      {(nsi.history?.length ?? 0) > 0 ? (
        <EChart option={opt} height={272} />
      ) : (
        <Empty colors={colors}>
          NSI 历史序列暂不可用 —— 请检查 data/research/nsi_signal.csv 是否已生成且可读
          （历史文件缺日期列时按交易日历尾部对齐）。
        </Empty>
      )}
      <div style={{ fontSize: 11.5, color: colors.muted, marginTop: 6, lineHeight: 1.7 }}>
        上：score 与触发阈值（{f2(hi)}）；下：建议仓位。NSI 只做加法——score 低不会触发减仓，
        加仓窗口在最后一次触发后自然滚动 20 个交易日。{nsi.note}
      </div>
    </div>
  );
}

/* ── 分钟成交额 × 上证综指 × 累计成交（同屏双轴）─────────────────── */
/**
 * 原来「综指 + 分钟柱」和「累计成交同期对比」是两张卡，看「某一分钟放量 →
 * 指数有没有反应 → 全天量能是不是偏大」要在两屏之间来回对时间轴。
 * 现在合成一张：上格左轴综指、右轴累计成交额（当日 / 上一日 / 20 日均），
 * 下格仍是分钟柱，两格 x 轴联动，一次悬停把同一分钟的六个读数全给出来。
 *
 * 双轴不是为了好看，是量纲不同：指数点位 3600 上下、累计成交额 0→800 亿，
 * 共用一个轴必然有一条被压平。右轴固定从 0 起，保留「累计」的语义。
 */
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
    const amt = minute.today_amount.slice(0, n);
    // 09:31 含集合竞价，单根常是其余分钟的几倍到十几倍。让它去撑 Y 轴，
    // 另外 239 根会被压成贴地的一条线 —— 图上有柱子，但等于看不见。
    // 按「除首分钟外的最大值」定上界，首柱截断显示并在柱顶标真实值，
    // tooltip 始终给原始数值，不伪造数据。
    const rest = amt.slice(1).filter((v): v is number => v != null && v > 0);
    const restMax = rest.length ? Math.max(...rest) : 20;
    const yMax = Math.max(20, Math.ceil(restMax * 1.25 / 5) * 5);
    // 脉冲判定加绝对量门槛：均量接近 0 时，0.1 亿也能算出「10 倍放量」，
    // 那是噪声不是资金进场。
    const hot = amt.map((v, i) =>
      v != null && v >= 5 && minute.avg20_amount[i] > 0.5
      && v / minute.avg20_amount[i] > 2.6);
    // 采样起点：这之前没有分钟数据，累计曲线是「接」上去的，不是从 0 涨上来的。
    const partial = meta.partial_from != null;
    const startLbl = partial ? minute.labels[meta.partial_from as number] : null;
    // 昨收必须显式纳入左轴范围。ECharts 的 scale 只按 series 数据算，低开日
    // 昨收高于当日全部点位（11-06：昨收 3689.91、日内最高 3657）时，
    // 那条虚线会被静默裁掉 —— 而说明里恰恰要求「看蓝线与昨收的相对位置」。
    const pxVals = minute.today_index.filter((v): v is number => v != null);
    const pc = minute.prev_close;
    const pxOk = pxVals.length > 0 && pc != null;
    const lo = pxOk ? Math.min(...pxVals, pc as number) : 0;
    const hi = pxOk ? Math.max(...pxVals, pc as number) : 1;
    const pad = Math.max(2, (hi - lo) * 0.12);
    const idxMin = Math.floor((lo - pad) / 5) * 5;
    const idxMax = Math.ceil((hi + pad) / 5) * 5;
    return {
      legend: {
        top: 0, left: 52, right: 8, itemWidth: 14, itemHeight: 2, itemGap: 10,
        textStyle: { color: colors.muted, fontSize: 10.5 },
        data: ["上证综指", "当日累计", "上一日同期", "20日均累计", "分钟成交额", "20日同期均量"],
      },
      grid: [
        { left: 54, right: 62, top: 42, height: 180 },
        { left: 54, right: 62, top: 266, height: 132 },
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
          const px = minute.today_index[i];
          const pc = minute.prev_close;
          const rel = px != null && pc
            ? `${((px / pc - 1) * 100).toFixed(2)}%` : "—";
          return `${meta.as_of || ""} ${labels[i]}<br/>`
            + `上证 <b>${f2(px)}</b>（较昨收 ${rel}）<br/>`
            + `本分钟成交 <b>${f1(a)}</b> 亿（同期均量 ${mult}×）<br/>`
            + `累计 <b>${f1(minute.today_cum[i])}</b> 亿 ｜ 上一日 ${f1(minute.prev_cum[i])} ｜ 20日均 ${f1(minute.avg20_cum[i])}`;
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
        { // 上格左轴：指数点位（含昨收，低开日也要看得见基准线）
          gridIndex: 0, position: "left",
          ...(pxOk ? { min: idxMin, max: idxMax } : { scale: true }),
          axisLabel: { color: colors.accent, fontSize: 10 },
          splitLine: { lineStyle: { color: colors.border } },
        },
        { // 上格右轴：累计成交额（亿元），从 0 起
          gridIndex: 0, min: 0, position: "right",
          axisLabel: { color: colors.up, fontSize: 10 },
          splitLine: { show: false },
        },
        { // 下格：单分钟成交额（亿元）
          gridIndex: 1, min: 0, max: yMax,
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
            // 标签放左端：默认落在终点（右侧）会压住右轴的累计成交额刻度
            label: {
              formatter: `昨收 ${f2(minute.prev_close)}`, color: colors.muted,
              fontSize: 10, position: "insideStartTop",
            },
            data: [{ yAxis: minute.prev_close ?? 0 }],
          },
        },
        {
          name: "当日累计", type: "line", data: minute.today_cum,
          xAxisIndex: 0, yAxisIndex: 1, showSymbol: false, connectNulls: true,
          smooth: true, smoothMonotone: "x",
          lineStyle: { color: colors.up, width: 2.2 }, itemStyle: { color: colors.up },
          // 采样模式下不铺面积：只有尾部一小段时，面积会变成一根从 0 拉到 700 多的
          // 竖条，看起来像「一分钟内涨了 700 亿」
          markLine: partial ? {
            silent: true, symbol: "none", animation: false,
            lineStyle: { color: colors.axis, type: "dashed", width: 1, opacity: 0.85 },
            label: { show: false },
            data: [{ xAxis: startLbl as string }],
          } : undefined,
          // 不挂 endLabel：末端标签会正好压住右轴「800」刻度。
          // 累计末值已由卡片顶部读数行（761.4 亿）与 tooltip 给出，无需在图里重复。

        },
        {
          name: "上一日同期", type: "line", data: minute.prev_cum,
          xAxisIndex: 0, yAxisIndex: 1, showSymbol: false,
          smooth: true, smoothMonotone: "x",
          lineStyle: { color: colors.muted, width: 1.3, type: [6, 4] },
          itemStyle: { color: colors.muted },
        },
        {
          name: "20日均累计", type: "line", data: minute.avg20_cum,
          xAxisIndex: 0, yAxisIndex: 1, showSymbol: false,
          smooth: true, smoothMonotone: "x",
          lineStyle: { color: colors.axis, width: 1.2, opacity: 0.65 },
          itemStyle: { color: colors.axis },
        },
        {
          name: "分钟成交额", type: "bar", xAxisIndex: 1, yAxisIndex: 2,
          barWidth: "72%",
          data: amt.map((v, i) => ({
            value: v == null ? null : Math.min(v, yMax),
            itemStyle: { color: hot[i] ? colors.up : `${rgba(colors.up,0.30)}`, borderRadius: [1, 1, 0, 0] },
            ...(v != null && v > yMax ? {
              label: { show: true, position: "top", formatter: `${f1(v)}↑`,
                       color: colors.up, fontSize: 10, fontWeight: 600 },
            } : {}),
          })),
        },
        {
          name: "20日同期均量", type: "line", data: minute.avg20_amount,
          xAxisIndex: 1, yAxisIndex: 2, showSymbol: false,
          lineStyle: { color: colors.muted, width: 1 },
        },
      ],
    };
  }, [minute, colors, meta.as_of, meta.partial_from]);

  const n = minute.count || 0;
  const dayTag = meta.as_of ? `${meta.as_of} · ` : "";
  const tag = meta.partial_from != null
    ? `${dayTag}采样自 ${minute.labels[meta.partial_from]}，此前无数据`
    : meta.count ? `${dayTag}${minute.labels[0]}–${minute.labels[meta.count - 1]}`
      : (meta.is_today ? "今日暂无分钟数据" : "等待盘中数据");
  // 柱子序列必须与分钟轴等长。后端曾把日度序列覆盖进这个字段，
  // 结果 ECharts 收不到合法数值，整片柱子静默消失、只剩坐标轴。
  const barsOk = minute.today_amount.length >= (minute.count || 0) && minute.count > 0;
  const lastCum = minute.today_cum[n - 1] ?? null;
  const avgCum = minute.avg20_cum[n - 1] ?? null;
  const ratio = lastCum != null && avgCum ? lastCum / avgCum : null;
  const pxLast = minute.today_index[n - 1] ?? null;
  const pc = minute.prev_close;
  const pxChg = pxLast != null && pc ? (pxLast / pc - 1) * 100 : null;

  return (
    <div style={cardStyle(colors)}>
      <CardHead title="分钟成交额 × 上证综指 × 累计成交" tag={tag} colors={colors} />
      {/* 一行读数把三张图的关键末值钉住，避免「图看着涨、结论卡却说跌」的误读 */}
      {n > 0 && (
        <div style={{
          display: "flex", flexWrap: "wrap", gap: "4px 18px", margin: "2px 0 6px",
          fontSize: 11.5, color: colors.muted, fontVariantNumeric: "tabular-nums",
        }}>
          <span>累计成交 <b style={{ color: colors.up }}>{f1(lastCum)} 亿</b></span>
          <span>20 日同期均值 <b style={{ color: colors.text }}>{f1(avgCum)} 亿</b>
            {ratio != null && <>（<b style={{ color: colors.text }}>{ratio.toFixed(2)}×</b>）</>}
          </span>
          <span>上证 <b style={{ color: colors.accent }}>{f2(pxLast)}</b>
            {pxChg != null && <>（较昨收 <b style={{ color: pxChg >= 0 ? colors.up : colors.down }}>{pxChg >= 0 ? "+" : ""}{pxChg.toFixed(2)}%</b>）</>}
          </span>
          <span>昨收 {f2(pc)}</span>
        </div>
      )}
      {minute.count === 0 ? (
        <Empty colors={colors}>
          暂无分钟数据 —— 交易时段内服务会自动逐分钟采集；
          已收盘的话点状态栏「拉官方分钟线」立即补
        </Empty>
      ) : !barsOk ? (
        <Empty colors={colors}>
          分钟成交额序列长度异常（{minute.today_amount.length} 条 / 应有 {minute.count} 条），
          柱状图已停用以免画出空图。
        </Empty>
      ) : <EChart option={opt} height={440} />}
      <Note colors={colors}>
        <strong>上格双轴</strong>：左轴蓝色是上证综指同步点位，虚线是<strong>昨收</strong>；右轴红色是
        20 只重点 ETF 成交额从开盘逐分钟累加的当日累计值，深灰虚线是上一交易日同期、浅灰是近 20 个交易日同期均值。
        它回答「今天整体算不算放量」——红线骑在均值线上是放量，一直在下方是缩量。
        <strong>判断涨跌只看蓝线与昨收虚线的相对位置</strong>：低开后蓝线一路上行，也可能全天仍是下跌
        （例如低开 −1.79%、收 −1.10%），别把「线在往上走」读成「指数翻红」；悬停时会直接给出较昨收的百分比。
        {meta.partial_from != null && (
          <span style={{ color: colors.axis }}>
            {" "}今天服务从 {minute.labels[meta.partial_from]} 才开始采样，红线只覆盖右侧一小段，
            起点高度是那一刻的<strong>真实累计成交额</strong>，不是从 0 涨上来的。
          </span>
        )}
        {" "}<strong>下格</strong>每根柱子 = 该分钟 20 只 ETF 的真实成交额之和，灰线是过去 20 天同一分钟的均量；
        深红色标记 ≥ 5 亿且超过同期均量 2.6 倍的脉冲（绝对量门槛用来挡住均量趋近 0 时的噪声）。
        柱放量 + 蓝线向昨收靠拢，更像资金进场；柱放量但指数不动或继续走低，更像托底或对倒。
        09:31 含集合竞价，量级常是其余分钟的十几倍，图上按轴上界截断并在柱顶标真实值，悬停可看原始数值。
        上下两格 x 轴联动，一次悬停给全读数。
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

  // 后端给按日汇总；万一命中 60 秒缓存里的旧结构，直接从 matrix 兜底算，
  // 保证「每天总结」不会因为前后端发版顺序不同而整块消失。
  const nDays = category.dates.length;
  const dayTotal = category.day_total?.length === nDays
    ? category.day_total
    : category.dates.map((_, i) => category.matrix.reduce((s, r) => s + (r[i] ?? 0), 0));
  const dayIn = category.day_in?.length === nDays
    ? category.day_in
    : category.dates.map((_, i) => category.matrix.filter((r) => (r[i] ?? 0) > 0.05).length);
  const dayOut = category.day_out?.length === nDays
    ? category.day_out
    : category.dates.map((_, i) => category.matrix.filter((r) => (r[i] ?? 0) < -0.05).length);
  const dayLeader = category.day_leader?.length === nDays
    ? category.day_leader
    : category.dates.map((_, i) => {
      let k = 0;
      category.matrix.forEach((r, j) => {
        if (Math.abs(r[i] ?? 0) > Math.abs(category.matrix[k]?.[i] ?? 0)) k = j;
      });
      return { name: category.labels[k] ?? "—", value: category.matrix[k]?.[i] ?? 0 };
    });
  // 日合计的量级（百亿）远大于单类别（几亿），不能共用 HMAX=26 的色阶，
  // 否则整行都是最深色。按区间最大日合计单独归一。
  const maxDay = Math.max(1, ...dayTotal.map((v) => Math.abs(v)));
  const heatSum = (v: number) => (Math.abs(v) < 0.05
    ? "transparent"
    : (v > 0 ? rgba(colors.up, 0.06 + 0.40 * Math.min(1, Math.abs(v) / maxDay))
      : rgba(colors.down, 0.06 + 0.40 * Math.min(1, Math.abs(v) / maxDay))));
  /** 「每一天的一句话」：表头悬停、总结行悬停、顶部摘要条共用同一套口径。 */
  const brief = (i: number) => {
    const t = dayTotal[i] ?? 0;
    const ld = dayLeader[i];
    return `${category.dates[i]}：全市场 12 类合计 ${sgn(t)}${f1(t)} 亿（${t >= 0 ? "净申购" : "净赎回"}），`
      + `${dayIn[i] ?? 0} 类流入 / ${dayOut[i] ?? 0} 类流出，`
      + `主力 ${ld?.name ?? "—"} ${sgn(ld?.value ?? 0)}${f1(ld?.value ?? 0)} 亿`;
  };
  const last = nDays - 1;
  const lTot = dayTotal[last] ?? 0;
  const lLead = dayLeader[last];
  const rangeTotal = dayTotal.reduce((s, v) => s + (v ?? 0), 0);
  const rangeDays = category.range_days || nDays;
  const lag = category.lag ?? null;
  const nCat = category.labels.length || 1;

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
        tag={`最近 ${nDays} 个交易日 · 单位：亿元`}
        colors={colors}
      />
      {/* 每日总结：最新日置顶一句话，不必先读懂热力表 */}
      <div style={{
        display: "flex", alignItems: "center", gap: "4px 14px", flexWrap: "wrap",
        margin: "6px 0 4px", padding: "8px 10px", borderRadius: 6,
        background: colors.tableStripe, border: `1px solid ${colors.border}`,
        fontSize: 12, fontVariantNumeric: "tabular-nums",
      }}>
        <span style={{ color: colors.muted }}>最新日 <b style={{ color: colors.text }}>{category.dates[last]}</b></span>
        <span>全市场宽基合计&nbsp;
          <b style={{ color: lTot >= 0 ? colors.up : colors.down, fontSize: 14 }}>
            {sgn(lTot)}{f1(lTot)} 亿
          </b>
          <span style={{ color: colors.muted }}>（{lTot >= 0 ? "净申购" : "净赎回"}）</span>
        </span>
        <span style={{ color: colors.muted }}>
          {dayIn[last] ?? 0}/{nCat} 类流入 · 主力 {lLead?.name ?? "—"}&nbsp;
          <b style={{ color: (lLead?.value ?? 0) >= 0 ? colors.up : colors.down }}>
            {sgn(lLead?.value ?? 0)}{f1(lLead?.value ?? 0)} 亿
          </b>
        </span>
        <span style={{ marginLeft: "auto", color: colors.axis, fontSize: 11 }}>
          悬停任意日期可看当日完整摘要
        </span>
      </div>
      {lag && (
        <div style={{ margin: "4px 0 6px", fontSize: 11.5, color: colors.axis, lineHeight: 1.6 }}>
          ⚠️ 份额 T+1：矩阵最新日 {lag.matrix_last}，看板数据日 {lag.as_of}。
          最近一天的申赎要等次日份额公布才可见，这里不是漏数。
        </div>
      )}
      <div style={{ overflowX: "auto", marginTop: 8 }}>
        <table style={{ borderCollapse: "separate", borderSpacing: 0, width: "100%", fontSize: 11.5 }}>
          <thead>
            <tr>
              <th style={{ width: 104 }} />
              {groups.map((g, i) => (
                <th key={i} colSpan={g.n} style={{
                  fontSize: 10, color: colors.axis, fontWeight: 500,
                  borderBottom: `1px solid ${colors.border}`, paddingBottom: 3,
                }}>
                  {Number(g.m.slice(5))}月
                </th>
              ))}
              <th style={{ width: 72 }} />
              <th style={{ width: 62 }} />
            </tr>
            <tr>
              <th style={{ fontSize: 10, color: colors.axis, fontWeight: 500, textAlign: "left" }}>日期 / 分类</th>
              {category.dates.map((d, i) => (
                <th key={d} title={brief(i)} style={{
                  fontSize: 10, color: colors.axis, fontWeight: 500, cursor: "help",
                  paddingBottom: 4, fontVariantNumeric: "tabular-nums",
                }}>{d.slice(8)}</th>
              ))}
              <th title={`最近 ${rangeDays} 个交易日累计，不是自然月`} style={{
                fontSize: 10, color: colors.axis, fontWeight: 500, textAlign: "left",
                paddingLeft: 8, cursor: "help",
              }}>区间合计</th>
              <th style={{ fontSize: 10, color: colors.axis, fontWeight: 500, textAlign: "right", paddingRight: 2 }}>规模</th>
            </tr>
          </thead>
          <tbody>
            {/* 总结行 1：每天全市场 12 类加总 */}
            <tr>
              <th style={{
                textAlign: "left", fontSize: 11.5, fontWeight: 700, color: colors.text,
                paddingRight: 8, borderRight: `1px solid ${colors.border}`, whiteSpace: "nowrap",
              }}>
                全市场日合计
                <div style={{ fontSize: 9.5, fontWeight: 400, color: colors.muted }}>12 类加总</div>
              </th>
              {category.dates.map((d, i) => {
                const v = dayTotal[i] ?? 0;
                return (
                  <td key={d} title={brief(i)} style={{
                    background: heatSum(v), textAlign: "center", height: 28, minWidth: 38,
                    cursor: "help", fontVariantNumeric: "tabular-nums", fontSize: 11, fontWeight: 700,
                    color: Math.abs(v) < 0.05 ? colors.muted : v > 0 ? colors.up : colors.down,
                    borderBottom: `1px solid ${colors.border}`,
                  }}>{sgn(v)}{f1(v)}</td>
                );
              })}
              <td style={{
                textAlign: "left", paddingLeft: 8, fontWeight: 700, fontSize: 11.5,
                fontVariantNumeric: "tabular-nums",
                color: rangeTotal >= 0 ? colors.up : colors.down,
                borderLeft: `1px solid ${colors.border}`,
              }}>{sgn(rangeTotal)}{f1(rangeTotal)}</td>
              <td />
            </tr>
            {/* 总结行 2：当天有几类在净申购（广度），比单看总额更能识别「普涨 vs 一只独大」 */}
            <tr>
              <th style={{
                textAlign: "left", fontSize: 11.5, fontWeight: 500, color: colors.muted,
                paddingRight: 8, borderRight: `1px solid ${colors.border}`, whiteSpace: "nowrap",
              }}>净申购类数</th>
              {category.dates.map((d, i) => {
                const inn = dayIn[i] ?? 0;
                const out = dayOut[i] ?? 0;
                return (
                  <td key={d} title={brief(i)} style={{
                    textAlign: "center", height: 24, cursor: "help", fontSize: 10.5,
                    fontVariantNumeric: "tabular-nums",
                    color: inn > out ? colors.up : inn < out ? colors.down : colors.muted,
                  }}>{inn}/{nCat}</td>
                );
              })}
              <td style={{ textAlign: "left", paddingLeft: 8, color: colors.muted, borderLeft: `1px solid ${colors.border}` }}>—</td>
              <td />
            </tr>
            <tr>
              <td colSpan={nDays + 3} style={{ height: 5, borderBottom: `1px solid ${colors.border}` }} />
            </tr>
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
        把全市场所有宽基 ETF 按跟踪指数分成 {nCat} 类，看每一类每天净申购还是净赎回。
        <strong>表格最前面两行是「每天一句话」</strong>：第一行是当天 {nCat} 类加总（全市场日合计），
        第二行是当天有多少类在净申购（广度）——总额大但只有 1–2 类流入，和 10 类一起流入，含义完全不同；
        鼠标停在任意日期表头或格子，会给出这一天的方向、广度与贡献最大的指数。
        右侧「区间合计」是最近 {rangeDays} 个交易日累计，<strong>不是自然月</strong>（22 个交易日通常跨 2–3 个月）。
        <span style={{ color: colors.axis }}>
          {" "}口径提醒：这里是全市场 {nCat} 类宽基，顶部 KPI 的「当日净流入」是 20 只重点 ETF，
          两者不是同一个池子，日合计天然会多出几十亿，<strong>不能互相验算</strong>；份额 T+1 公布，
          矩阵最新日通常比看板数据日落后一天。
        </span>
        {" "}红色是钱进来、绿色是钱出去，颜色越深代表当天变动越大。单日大额往往是脉冲，连续多日同向才值得跟。
      </Note>
    </div>
  );
}

/* ── 数据勾稽自检 ───────────────────────────────────────────────── */
/**
 * 这一屏十几张图共用同一批底层数据，但各图画的是不同切片。改过口径之后，
 * 最容易出现的不是「报错」而是「两张图各自都对、放一起互相打架」。
 * 这里把能在本页内部验算的关系逐条摆出来，每次刷新自动重算：
 *   pass = 数值对得上 ／ info = 设计上就该有差异 ／ warn = 真的对不上。
 */
type CkState = "pass" | "info" | "warn";
interface Ck { name: string; a: string; b: string; state: CkState; note: string }

function ConsistencyCard({
  data, colors,
}: {
  data: EtfFlowDashboard;
  colors: ReturnType<typeof useTheme>["colors"];
}) {
  const checks = useMemo<Ck[]>(() => {
    const { meta, minute, daily, category, etf_detail, nsi, support, summary } = data;
    const out: Ck[] = [];
    const add = (name: string, a: string, b: string, state: CkState, note: string) =>
      out.push({ name, a, b, state, note });
    const near = (x: number | null | undefined, y: number | null | undefined, eps: number) =>
      x != null && y != null && Math.abs(x - y) <= eps;

    const n = meta.count || 0;
    const cumLast = minute.today_cum[n - 1] ?? null;
    const amtSum = minute.today_amount.slice(0, n)
      .reduce<number>((s, v) => s + (v ?? 0), 0);
    const i = daily.dates.indexOf(meta.as_of);
    const dAmt = i >= 0 ? daily.turnover[i] : null;
    const dIdx = i >= 0 ? daily.index[i] : null;
    const dPrev = i > 0 ? daily.index[i - 1] : null;
    const idxLast = minute.today_index[n - 1] ?? null;
    const avgCum = minute.avg20_cum[n - 1] ?? null;

    add("分钟累计 ↔ 日线成交额", `${f1(cumLast)} 亿`, `${f1(dAmt)} 亿`,
      near(cumLast, dAmt, 1.0) ? "pass" : "warn",
      "同一天：分钟线逐笔累加 vs 日线汇总，日线只保留 1 位小数");

    const partial = meta.partial_from != null;
    add("分钟柱求和 ↔ 累计末值", `${f1(amtSum)} 亿`, `${f1(cumLast)} 亿`,
      partial ? "info" : (near(amtSum, cumLast, 0.5) ? "pass" : "warn"),
      partial ? "采样起点前的成交额不在可见柱里，累计末值本来就更大"
        : "下格每根柱子之和应等于上格红线末值");

    add("综指末值 ↔ 日线收盘", f2(idxLast), f2(dIdx),
      near(idxLast, dIdx, 0.5) ? "pass" : "warn", "上格蓝线末点应等于当日上证收盘");

    add("昨收虚线 ↔ 前一日收盘", f2(minute.prev_close), f2(dPrev),
      near(minute.prev_close, dPrev, 0.5) ? "pass" : "warn",
      "涨跌基准线；低开后蓝线上行也可能全天收跌，判断要看这条线");

    const sm = summary.metrics.find((x) => x.label.includes("成交额"));
    const mm = sm?.sub.match(/([\d.]+)\s*倍/);
    const shown = mm ? parseFloat(mm[1]) : null;
    const calc = cumLast != null && avgCum ? cumLast / avgCum : null;
    add("量能倍数 ↔ 结论卡", calc != null ? `${calc.toFixed(2)}×` : "—",
      shown != null ? `${shown.toFixed(2)}×` : "—",
      calc != null && shown != null && Math.abs(calc - shown) <= 0.02 ? "pass" : "info",
      "当日累计 ÷ 20 日同期均值累计，结论卡按 20 只全池口径显示");

    const detSum = etf_detail.reduce((s, x) => s + (x.amount ?? 0), 0);
    add("单只明细合计 ↔ 全天成交额", `${f1(detSum)} 亿`, `${f1(dAmt)} 亿`,
      near(detSum, dAmt, 1.5) ? "pass" : "warn",
      `20 只各四舍五入到 0.1 亿，允许 ≤1.5 亿累计误差（当前 ${etf_detail.length} 只）`);

    const cDays = category.dates.length;
    const structOk = category.matrix.length === category.labels.length
      && category.matrix.every((r) => r.length === cDays)
      && category.month_total.every((v, k) =>
        Math.abs(v - category.matrix[k].reduce((s, x) => s + x, 0)) < 0.3);
    add("矩阵结构与行合计", `${category.matrix.length} 行 × ${cDays} 列`,
      structOk ? "行合计一致" : "不一致", structOk ? "pass" : "warn",
      "每行长度应等于日期数，右侧区间合计应等于该行逐日加总");

    const catLast = category.dates[cDays - 1] ?? "";
    add("矩阵最新日 ↔ 看板数据日", catLast || "—", meta.as_of || "—",
      catLast === meta.as_of ? "pass" : "info",
      catLast === meta.as_of ? "份额已补齐"
        : "份额 T+1 公布，矩阵落后一个交易日是预期行为，不是缺数");

    const j = daily.dates.indexOf(catLast);
    const flow20 = j >= 0 ? daily.net_inflow[j] : null;
    const catTotal = category.day_total?.[cDays - 1]
      ?? category.matrix.reduce((s, r) => s + (r[cDays - 1] ?? 0), 0);
    add("分类日合计 ↔ 20 只净流入", `${f1(catTotal)} 亿`, `${f1(flow20)} 亿`,
      "info", "不同池：全市场 12 类宽基 vs 20 只重点 ETF，差额是池外产品，不能互相验算");

    add("NSI 数据日 ↔ 看板数据日", nsi.as_of || "—", meta.as_of || "—",
      nsi.as_of === meta.as_of ? "pass" : "warn",
      "顶部信号卡与观察层必须落在同一天，否则「信号 vs 观察」对照无意义");

    add("托底卡成交额 ↔ 日线成交额", `${f1(support.today_turnover)} 亿`, `${f1(dAmt)} 亿`,
      near(support.today_turnover, dAmt, 1.0) ? "pass" : "warn",
      "与顶部 KPI 共用同一个数，避免出现「KPI 761 亿 / 分位图 757 亿」");

    return out;
  }, [data]);

  const nPass = checks.filter((c) => c.state === "pass").length;
  const nInfo = checks.filter((c) => c.state === "info").length;
  const nWarn = checks.filter((c) => c.state === "warn").length;
  const tone = (s: CkState) => s === "pass" ? colors.accent : s === "warn" ? colors.up : colors.axis;
  const mark = (s: CkState) => s === "pass" ? "✓" : s === "warn" ? "⚠" : "·";

  return (
    <div style={cardStyle(colors)}>
      <CardHead
        title="数据勾稽自检"
        tag={`${nPass} 项一致 · ${nInfo} 项预期差异${nWarn ? ` · ${nWarn} 项异常` : ""}`}
        colors={colors}
      />
      <div style={{ overflowX: "auto", marginTop: 6 }}>
        <table style={{ borderCollapse: "collapse", width: "100%", fontSize: 11.5 }}>
          <thead>
            <tr style={{ color: colors.axis, fontSize: 10.5 }}>
              <th style={{ textAlign: "left", width: 34, paddingBottom: 4 }} />
              <th style={{ textAlign: "left", width: 190, paddingBottom: 4, fontWeight: 500 }}>勾稽关系</th>
              <th style={{ textAlign: "right", width: 96, paddingBottom: 4, fontWeight: 500 }}>读数 A</th>
              <th style={{ textAlign: "right", width: 96, paddingBottom: 4, fontWeight: 500 }}>读数 B</th>
              <th style={{ textAlign: "left", paddingLeft: 12, paddingBottom: 4, fontWeight: 500 }}>说明</th>
            </tr>
          </thead>
          <tbody>
            {checks.map((c) => (
              <tr key={c.name} style={{ borderTop: `1px solid ${colors.border}` }}>
                <td style={{ color: tone(c.state), fontWeight: 700, fontSize: 12 }}>{mark(c.state)}</td>
                <td style={{ color: colors.text, padding: "5px 0" }}>{c.name}</td>
                <td style={{ textAlign: "right", color: colors.text, fontVariantNumeric: "tabular-nums" }}>{c.a}</td>
                <td style={{ textAlign: "right", color: colors.text, fontVariantNumeric: "tabular-nums" }}>{c.b}</td>
                <td style={{ paddingLeft: 12, color: colors.muted, lineHeight: 1.5 }}>{c.note}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <Note colors={colors}>
        <strong>「预期差异」不是错误</strong>：份额 T+1、全市场 12 类与 20 只重点 ETF 不同池、
        采样起点前的成交额不可见——这些都会让两个读数天然不相等，强行对齐反而是伪造。
        ✓ 表示同一数据日内部的算术关系成立；⚠ 才需要查数据链路。
        自检只验证<strong>页面内部一致性</strong>，不验证上游行情源本身是否正确，也不构成信号。
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
