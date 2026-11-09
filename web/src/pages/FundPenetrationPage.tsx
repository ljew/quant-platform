/**
 * 基金持仓补齐 · 行业穿透
 *
 * 核心事实（页面第一屏就要讲清楚）：
 * 旧 fund_portfolio_year.csv 在 2020-2025 每年恰好 416000 行，是 Tushare
 * offset>400000 的硬截断，不是「季报只披露前十大」。按 ts_code 分片重拉后，
 * 年报/半年报权重和回到 100%，Q1/Q3 季报才是真实的前十大（约 53%）。
 *
 * 双轨纪律：disclosed（真实披露）与 carried（锚点继承的影子持仓）分开存、
 * 分开展示；carried 只用于行业结构的分母校正，绝不进收益/归因结论。
 */
import { useEffect, useMemo, useState } from "react";
import { Btn, Card, KpiCard, PageHeader, inputStyle } from "../components/ui";
import EChart from "../components/EChart";
import FundStylePanel from "../components/FundStylePanel";
import { get } from "../api/client";
import { useTheme } from "../theme";

interface CoverageType {
  n_sections: number; cover_median: number; n_holdings_median: number; pct_complete: number;
}
interface YearRow { year: number; n_sections: number; q13_cover: number | null; annual_cover: number | null }
interface IndRow { industry: string; total_w: number; active_w: number; carried_w: number; n: number }
/** 行业反查：可选行业（含该期全池统计） */
interface IndOption {
  industry: string; n_funds: number; avg_w: number; max_w: number; n_ge5: number;
}
/** 行业反查：命中的基金。exposure_basis 标明该期权重口径 */
interface IndPick {
  base_code: string; ts_code: string; name: string;
  disclosed_w: number; carried_w: number; total_w: number;
  benchmark_w: number; active_w: number;
  disclosed_cover: number; exposure_basis: string | null;
  n_disclosed: number; is_complete: boolean;
}
interface Overview {
  ok: boolean; error?: string; cached_at?: string;
  scope: { fund_pool: string; n_funds: number; n_sections: number; date_range: string[]; is_sample: boolean };
  coverage_by_type: Record<string, CoverageType>;
  yearly: YearRow[];
  old_cover_median_by_year: Record<string, number>;
  industry_overview: IndRow[];
  non_sw_share?: Record<string, number>;
  concentration: { hhi_median: number; top3_median: number; carried_sections: number;
                   lookahead_sections: number; mean_confidence: number };
  industry_source: Record<string, number>;
  detail_rows: number;
  discipline: string;
  /** total/completed 为 null = 基金池规模未知（主库读不到），不能当作「未完成」 */
  fetch_progress?: {
    done: number; total: number | null;
    completed: boolean | null; pct?: number | null;
  };
  proxy?: { rows: number; weight: number; share_pct: number };
  regulatory?: {
    source: string; n_sections: number; share_pct: number;
    a_share_pct_median: number | null; anchor_sections: number; note: string;
  };
  regression?: {
    version: string; n_sections: number; n_funds: number; skipped_low_info: number;
    r2_median: number; r2_mean: number; l1_median: number; spearman_median: number;
    n_obs_median: number; agree_share: number; high_conflict_share: number;
    metric_note: string;
  };
  method?: {
    version: string;
    steps: { n: number; title: string; desc: string }[];
    confidence: string; benchmark: string; discipline: string; not_done: string;
  };
}
interface FundItem {
  base_code: string; ts_code: string; name: string; n_sections: number;
  last_end_date: string; avg_cover: number; n_carried: number;
}
interface CovPoint {
  end_date: string; ann_date: string; report_type: string;
  n_disclosed: number; disclosed_cover: number; is_complete: boolean;
  anchor_end_date: string; anchor_gap_days: number; lookahead: boolean;
  n_carried: number; carried_cover: number; confidence: number;
  hhi: number; top3_share: number;
}
interface IndExp {
  industry: string; disclosed_w: number; carried_w: number;
  total_w: number; benchmark_w: number; active_w: number;
}
interface HoldRow {
  symbol: string; industry: string; weight: number; source: string;
  anchor_end_date: string; anchor_gap_days: number; confidence: number;
}
interface FundDetail {
  ok: boolean; error?: string; code: string; ts_code: string; name: string;
  end_date: string; dates: string[];
  coverage_series: CovPoint[];
  current: { report_type: string; disclosed_cover: number; carried_cover: number;
             confidence: number; hhi: number; top3_share: number;
             anchor_end_date: string; anchor_gap_days: number; lookahead: boolean;
             warning?: string | null;
             /** net_value=占净值比 / regulatory=监管仓位锚定 / structure_only=仅结构归一 */
             exposure_basis?: string | null;
             equity_source: string; reg_equity: number | null };
  industries: IndExp[];
  turnover: { end_date: string; turnover: number }[];
  detail?: HoldRow[];
  regression?: {
    available: boolean;
    meta: { r2: number; n_obs: number; mse: number; lambda: number;
            bayes_l1: number; spearman: number } | null;
    industries: { industry: string; beta: number; bayes_w: number; diff: number }[];
  };
  regulatory?: {
    available: boolean; a_share_pct: number | null; our_a_share_pct: number | null;
    industries: { code: string; name: string; weight: number }[];
  };
  fof?: { is_fof: boolean; name?: string; n_sections?: number;
          reg_equity_med?: number; unpenetrated_upper?: number; verdict?: string };
}
interface RegHoldRow {
  symbol: string; industry: string; beta_w: number; bayes_w: number;
  diff: number; source: string; confidence: number; quality: string;
}
interface RegHoldResp {
  ok: boolean; available: boolean; reason?: string;
  ts_code: string; end_date: string; dates: string[];
  meta: { r2: number; spearman: number; n_obs: number; beta_total: number;
          disclosed_total: number; implied_total: number; bayes_total: number;
          l1_vs_bayes: number; n_stocks: number; n_proxy: number;
          quality: string } | null;
  by_source: { source: string; n: number; weight: number }[];
  items: RegHoldRow[];
  summary: { version: string; n_sections: number; n_rows: number; n_funds: number;
             quality: Record<string, number>; l1_vs_bayes_median: number;
             conserve_gap_median: number; conserve_within_1pp: number;
             discipline: string; not_done: string };
}
interface FofLayerFund {
  base_code: string; name: string; end_date: string; report_type: string;
  n_funds: number; w_total: number; w_pool: number; w_equity: number;
  a_share_decl: number | null; eq_total: number | null; n_missing_eq: number;
}
interface FofLayer {
  ok: boolean; error?: string;
  summary: { n_reports: number; n_fof: number; n_holdings: number;
             success_rate: number | null; as_of: string | null; n_funds_latest: number;
             w_total_med: number | null; w_pool_med: number | null };
  by_type: { report_type: string; status: string; n: number }[];
  funds: FofLayerFund[];
}
interface FofDiag {
  ok: boolean; n_fof: number; n_fof_sections: number; last_end_date: string;
  fof: { reg_equity_median: number; disclosed_cover_median: number; n_sections: number };
  non_fof: { reg_equity_median: number; disclosed_cover_median: number; n_sections: number };
  unpenetrated_upper_median: number;
  unpenetrated_note?: string; industry_mean_note?: string;
  industry_impact_excluding_fof: { industry: string; with_fof: number;
                                   without_fof: number; delta: number }[];
  verdict: string; recommendation: string; updated_at: string;
}
interface YearDiff { year: number; n_top: number; top: number; rest: number; diff: number }
interface Recheck {
  ok: boolean; error?: string; recheck_at: string; data_source: string;
  n_annual_rows: number; n_funds: number; n_sections: number; cover_median: number;
  excluded: string;
  style: {
    n_obs: number; years: string; hold_diff: number; hold_t: number; hold_p: number;
    hold_pos: string; fund_diff: number; fund_t: number; fund_p: number;
    fund_pos: string; gap: number; yearly: YearDiff[];
    old: { hold_diff: number; hold_t: number; fund_diff: number; fund_t: number;
           gap: number; note: string };
  };
  copy: {
    n_obs: number; years: string; known_pct: number;
    lag_diff: number | null; lag_t: number | null; lag_p: number | null;
    lag_pos: string | null; yearly: YearDiff[];
    old: { lag_diff: number; lag_t: number; lag_pos: string; note: string };
  };
}

const C_DISC = "#2456c8";   // 披露
const C_CARR = "#b7c4de";   // 继承
const C_OLD = "#d97706";    // 旧截断数据
const TYPE_LABEL: Record<string, string> = { Q1: "Q1 季报", H1: "半年报", Q3: "Q3 季报", FY: "年报" };

/** 业界对照：来自公开研报 / 监管通报，非一手来源，需核实原文。 */
const PEERS: { who: string; what: string; model: string; acc: string }[] = [
  { who: "代销平台（天天基金 / 蚂蚁 / 同花顺）", what: "盘中估值：前十大加权 × 实时行情",
    model: "不补全、不做行业穿透",
    acc: "主动基金平均偏离约 0.8%，极端 >3%；2026-01 监管通报要求下架实时估值" },
  { who: "券商金工（天风 / 东吴 / 国金）", what: "重仓股补全 + 季报证监会行业配置约束",
    model: "上期非重仓股按比例缩放，行业约束优先于个股猜测",
    acc: "东吴 2026-05 实证全行业 MAE ≈ 0.6%；把完整持仓观测从半年频提到季频" },
  { who: "高频测算（华泰 / 信达 / 兴证 / 摩根士丹利）", what: "净值回归 WLS、二次规划、卡尔曼滤波",
    model: "基金日收益 ~ 申万一级行业指数，约束非负、Σβ≈0.95，窗口约 60 交易日",
    acc: "WLS 最稳健；885001 复制年化跟踪误差 3.16%、年报静态行业偏离 0.31%" },
  { who: "专业终端（Wind AMS / iFinD）", what: "季报资产配置 + 行业配置 + 证监会分类深度补全",
    model: "付费机构数据源，服务 FOF 穿透、归因与合规风控", acc: "需机构数据权限，非公开口径" },
  { who: "本平台 v5", what: "双轨 + 贝叶斯行业约束 + 监管仓位约束 + 净值回归校验 + 回归隐含个股",
    model: "锚点真实持仓作先验、当期前十大作观测、Dirichlet 后验分配；用季报法定披露的证监会行业配置合计（真实 A 股仓位）作总量硬约束；再以披露权重作下界做约束 WLS，并把行业 β 反推到个股",
    acc: "五条轨道分开存分展示，监管值只约束 A 股总量、不强行映射到申万；FOF 多层嵌套穿透因数据源不返回被投基金而明令不做，逐只打标" },
];

function fmtDate(d?: string) {
  return d && d.length === 8 ? `${d.slice(0, 4)}-${d.slice(4, 6)}-${d.slice(6)}` : (d || "—");
}

/** 最新期持仓缺口：源头未披露的那批（非抓取遗漏） */
interface GapInfo {
  ok: boolean; end_date: string; active_funds_45d: number;
  with_holdings: number; missing: number; missing_pct: number;
  items: { ts_code: string; name: string }[]; note?: string;
}

export default function FundPenetrationPage() {
  const { colors, mode } = useTheme();
  const [ov, setOv] = useState<Overview | null>(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [query, setQuery] = useState("");
  const [funds, setFunds] = useState<FundItem[]>([]);
  const [code, setCode] = useState("");
  const [endDate, setEndDate] = useState("");
  const [fd, setFd] = useState<FundDetail | null>(null);
  const [fdBusy, setFdBusy] = useState(false);
  const [fdErr, setFdErr] = useState("");
  const [showAll, setShowAll] = useState(false);
  const [recheck, setRecheck] = useState<Recheck | null>(null);
  const [regHold, setRegHold] = useState<RegHoldResp | null>(null);
  const [fof, setFof] = useState<FofDiag | null>(null);
  const [fofLayer, setFofLayer] = useState<FofLayer | null>(null);
  // 行业反查（与单基金钻取互为反向）
  const [indList, setIndList] = useState<IndOption[]>([]);
  const [indSel, setIndSel] = useState("");
  const [indMin, setIndMin] = useState(0);
  const [indRows, setIndRows] = useState<IndPick[]>([]);
  const [indDate, setIndDate] = useState("");
  const [indBusy, setIndBusy] = useState(false);
  const [indErr, setIndErr] = useState("");
  // 最新期持仓缺口（源头未披露的那批，需扫全量净值表，异步加载不阻塞首屏）
  const [gaps, setGaps] = useState<GapInfo | null>(null);

  useEffect(() => {
    setBusy(true);
    get<Overview>("/fund/penetration/overview")
      .then((d) => { if (!d.ok) setErr(d.error || "接口返回异常"); else setOv(d); })
      .catch((e) => setErr((e as Error).message))
      .finally(() => setBusy(false));
    // 缺口要扫 1,160 万行净值表（约 5 秒），单独异步拉，不拖慢首屏
    get<GapInfo>("/fund/penetration/gaps")
      .then((d) => { if (d.ok) setGaps(d); })
      .catch(() => { /* 缺口统计缺失不阻塞主页面 */ });
  }, []);

  useEffect(() => {
    get<Recheck>("/fund/penetration/recheck")
      .then((d) => { if (d.ok) setRecheck(d); })
      .catch(() => { /* 复核结果缺失不阻塞主页面 */ });
    get<FofDiag>("/fund/penetration/fof")
      .then((d) => { if (d.ok) setFof(d); })
      .catch(() => { /* FOF 诊断缺失不阻塞主页面 */ });
    get<FofLayer>("/fund/penetration/fof/layer")
      .then((d) => { if (d.ok) setFofLayer(d); })
      .catch(() => { /* 基金层穿透缺失不阻塞主页面 */ });
  }, []);

  // 第五轨跟随单基金钻取的报告期联动
  useEffect(() => {
    if (!fd?.ts_code || !fd?.end_date) { setRegHold(null); return; }
    get<RegHoldResp>(
      `/fund/penetration/regression-holdings?code=${encodeURIComponent(fd.ts_code)}` +
      `&end_date=${fd.end_date}&limit=30`)
      .then((d) => { if (d.ok) setRegHold(d); else setRegHold(null); })
      .catch(() => setRegHold(null));
  }, [fd?.ts_code, fd?.end_date]);

  const search = (q: string) => {
    get<{ ok: boolean; items: FundItem[] }>(`/fund/penetration/funds?q=${encodeURIComponent(q)}&limit=40`)
      .then((d) => { if (d.ok) setFunds(d.items || []); })
      .catch(() => setFunds([]));
  };
  useEffect(() => { search(""); }, []);

  const loadIndustry = (ind: string, minW: number) => {
    if (!ind) return;
    setIndBusy(true); setIndErr("");
    get<{ ok: boolean; end_date: string; items: IndPick[]; error?: string }>(
      `/fund/penetration/industry/${encodeURIComponent(ind)}?min_w=${minW}&limit=40`)
      .then((d) => {
        if (!d.ok) { setIndErr(d.error || "查询失败"); setIndRows([]); return; }
        setIndRows(d.items || []);
        if (d.end_date) setIndDate(d.end_date);
      })
      .catch((e) => { setIndErr((e as Error).message); setIndRows([]); })
      .finally(() => setIndBusy(false));
  };

  // 行业反查：首次进入拉行业清单，默认选「5% 以上仓位的基金数」最多的行业。
  // 不用平均权重排序 —— 均值和基金数量无关，容易选中一个只有少数基金配置的冷门行业。
  useEffect(() => {
    get<{ ok: boolean; end_date: string; industries: IndOption[] }>(
      "/fund/penetration/industries")
      .then((d) => {
        if (!d.ok) return;
        const list = d.industries || [];
        setIndList(list);
        if (d.end_date) setIndDate(d.end_date);
        const top = [...list].sort((a, b) => b.n_ge5 - a.n_ge5)[0];
        if (top) { setIndSel(top.industry); loadIndustry(top.industry, 0); }
      })
      .catch(() => { /* 行业清单缺失不阻塞主页面 */ });
  }, []);

  const loadFund = (c: string, ed: string) => {
    if (!c) return;
    setFdBusy(true); setFdErr("");
    const qs = `?detail=1${ed ? `&end_date=${ed}` : ""}`;
    get<FundDetail>(`/fund/penetration/fund/${encodeURIComponent(c)}${qs}`)
      .then((d) => {
        if (!d.ok) { setFdErr(d.error || "接口返回异常"); setFd(null); }
        else { setFd(d); setEndDate(d.end_date); }
      })
      .catch((e) => { setFdErr((e as Error).message); setFd(null); })
      .finally(() => setFdBusy(false));
  };
  const pick = (f: FundItem) => { setCode(f.base_code); setEndDate(""); loadFund(f.base_code, ""); };
  useEffect(() => { if (funds.length && !code) pick(funds[0]); /* eslint-disable-line */ }, [funds]);

  // ---- 图 1：修复前后对比 ----
  const repairOption = useMemo(() => {
    if (!ov) return null;
    const years = ov.yearly.map((y) => String(y.year));
    const oldV = years.map((y) => ov.old_cover_median_by_year[y] ?? null);
    const newV = ov.yearly.map((y) => y.annual_cover);
    return {
      grid: { left: 52, right: 20, top: 44, bottom: 42 },
      legend: { top: 6, textStyle: { color: colors.muted, fontSize: 11 },
                data: ["旧数据（offset 截断）", "重拉后年报"] },
      tooltip: { trigger: "axis", valueFormatter: (v: number | null) => v == null ? "无" : `${v}%` },
      xAxis: { type: "category", data: years, axisLabel: { color: colors.muted, fontSize: 11 } },
      yAxis: { type: "value", max: 110, name: "权重和 %", nameTextStyle: { color: colors.muted, fontSize: 11 },
               axisLabel: { color: colors.muted, fontSize: 11 }, splitLine: { lineStyle: { color: colors.border } } },
      series: [
        { name: "旧数据（offset 截断）", type: "bar", data: oldV, itemStyle: { color: C_OLD, opacity: 0.75 },
          barMaxWidth: 26 },
        { name: "重拉后年报", type: "bar", data: newV, itemStyle: { color: C_DISC }, barMaxWidth: 26 },
      ],
    } as never;
  }, [ov, colors]);

  // ---- 图 2：全市场行业配置 + 平均超配 ----
  const marketOption = useMemo(() => {
    if (!ov || !ov.industry_overview.length) return null;
    const rows = [...ov.industry_overview]
      .filter((r) => r.industry !== "港股" && r.industry !== "其他A股")
      .slice(0, 18).reverse();
    return {
      grid: { left: 84, right: 52, top: 24, bottom: 34 },
      legend: { top: 6, textStyle: { color: colors.muted, fontSize: 11 },
                data: ["真实披露", "锚点继承"] },
      tooltip: { trigger: "axis", axisPointer: { type: "shadow" } },
      xAxis: { type: "value", name: "%", nameTextStyle: { color: colors.muted },
               axisLabel: { color: colors.muted, fontSize: 11 }, splitLine: { lineStyle: { color: colors.border } } },
      yAxis: { type: "category", data: rows.map((r) => r.industry),
               axisLabel: { color: colors.text, fontSize: 11 } },
      series: [
        { name: "真实披露", type: "bar", stack: "m",
          data: rows.map((r) => Math.max(0, r.total_w - r.carried_w)),
          itemStyle: { color: C_DISC }, barMaxWidth: 14 },
        { name: "锚点继承", type: "bar", stack: "m", data: rows.map((r) => r.carried_w),
          itemStyle: { color: C_CARR }, barMaxWidth: 14 },
      ],
    } as never;
  }, [ov, colors]);

  // ---- 图 3：单基金覆盖度时间序列 ----
  const covOption = useMemo(() => {
    if (!fd) return null;
    const cs = fd.coverage_series;
    return {
      grid: { left: 50, right: 20, top: 42, bottom: 56 },
      legend: { top: 6, textStyle: { color: colors.muted, fontSize: 11 }, data: ["真实披露", "锚点继承"] },
      tooltip: {
        trigger: "axis",
        formatter: (ps: { dataIndex: number }[]) => {
          const c = cs[ps[0].dataIndex];
          return `${fmtDate(c.end_date)} · ${TYPE_LABEL[c.report_type] || c.report_type}<br/>` +
            `真实披露 <b>${c.disclosed_cover.toFixed(1)}%</b>（${c.n_disclosed} 只）<br/>` +
            `锚点继承 <b>${c.carried_cover.toFixed(1)}%</b>（${c.n_carried} 只）<br/>` +
            (c.anchor_end_date ? `锚点 ${fmtDate(c.anchor_end_date)} · 间隔 ${c.anchor_gap_days} 天 · 置信度 ${c.confidence.toFixed(2)}` : "本期为完整披露，无需补齐");
        },
      },
      xAxis: { type: "category", data: cs.map((c) => c.end_date.slice(2, 6)),
               axisLabel: { color: colors.muted, fontSize: 10, interval: 3, rotate: 45 } },
      yAxis: { type: "value", max: 110, name: "占净值 %", nameTextStyle: { color: colors.muted, fontSize: 11 },
               axisLabel: { color: colors.muted, fontSize: 11 }, splitLine: { lineStyle: { color: colors.border } } },
      series: [
        { name: "真实披露", type: "bar", stack: "w", data: cs.map((c) => c.disclosed_cover),
          itemStyle: { color: C_DISC }, barMaxWidth: 16 },
        { name: "锚点继承", type: "bar", stack: "w", data: cs.map((c) => c.carried_cover),
          itemStyle: { color: C_CARR }, barMaxWidth: 16 },
      ],
    } as never;
  }, [fd, colors]);

  // ---- 图 4：当期行业双轨配置 ----
  const indOption = useMemo(() => {
    if (!fd || !fd.industries.length) return null;
    const rows = [...fd.industries]
      .filter((r) => r.industry !== "港股" && r.industry !== "其他A股")
      .slice(0, 16).reverse();
    return {
      grid: { left: 84, right: 58, top: 40, bottom: 34 },
      legend: { top: 6, textStyle: { color: colors.muted, fontSize: 11 }, data: ["披露", "继承", "相对基准"] },
      tooltip: { trigger: "axis", axisPointer: { type: "shadow" } },
      xAxis: { type: "value", name: "权重 %", nameTextStyle: { color: colors.muted, fontSize: 11 },
               axisLabel: { color: colors.muted, fontSize: 11 }, splitLine: { lineStyle: { color: colors.border } } },
      yAxis: { type: "category", data: rows.map((r) => r.industry),
               axisLabel: { color: colors.text, fontSize: 11 } },
      series: [
        { name: "披露", type: "bar", stack: "w", data: rows.map((r) => r.disclosed_w),
          itemStyle: { color: C_DISC }, barMaxWidth: 15 },
        { name: "继承", type: "bar", stack: "w", data: rows.map((r) => r.carried_w),
          itemStyle: { color: C_CARR }, barMaxWidth: 15 },
        { name: "相对基准", type: "bar",
          data: rows.map((r) => ({ value: r.active_w,
            itemStyle: { color: r.active_w >= 0 ? colors.up : colors.down, opacity: 0.9 } })),
          barMaxWidth: 7 },
      ],
    } as never;
  }, [fd, colors]);

  const regOption = useMemo(() => {
    if (!fd?.regression?.available) return null;
    const rows = [...fd.regression.industries]
      .filter((r) => r.beta + r.bayes_w > 0.5).slice(0, 14).reverse();
    return {
      grid: { left: 84, right: 46, top: 38, bottom: 34 },
      legend: { top: 6, textStyle: { color: colors.muted, fontSize: 11 },
                data: ["贝叶斯补齐", "净值回归 β"] },
      tooltip: { trigger: "axis", axisPointer: { type: "shadow" } },
      xAxis: { type: "value", name: "权重 %", nameTextStyle: { color: colors.muted, fontSize: 11 },
               axisLabel: { color: colors.muted, fontSize: 11 }, splitLine: { lineStyle: { color: colors.border } } },
      yAxis: { type: "category", data: rows.map((r) => r.industry),
               axisLabel: { color: colors.text, fontSize: 11 } },
      series: [
        { name: "贝叶斯补齐", type: "bar", data: rows.map((r) => r.bayes_w),
          itemStyle: { color: C_DISC }, barMaxWidth: 10 },
        { name: "净值回归 β", type: "bar", data: rows.map((r) => r.beta),
          itemStyle: { color: "#8b5cf6" }, barMaxWidth: 10 },
      ],
    } as never;
  }, [fd, colors]);

  const recheckOption = useMemo(() => {
    if (!recheck) return null;
    const s = recheck.style.yearly, c = recheck.copy.yearly;
    const years = Array.from(new Set([...s.map((r) => r.year), ...c.map((r) => r.year)])).sort();
    const sm = new Map(s.map((r) => [r.year, r.diff]));
    const cm = new Map(c.map((r) => [r.year, r.diff]));
    return {
      grid: { left: 52, right: 20, top: 40, bottom: 40 },
      legend: { top: 4, textStyle: { color: colors.muted, fontSize: 11 },
                data: ["卡20 持仓组合收益差", "卡21 抄作业超额"] },
      tooltip: { trigger: "axis",
                 valueFormatter: (v: number | null) => v == null ? "无" : `${v.toFixed(1)}%` },
      xAxis: { type: "category", data: years.map(String),
               axisLabel: { color: colors.muted, fontSize: 11 } },
      yAxis: { type: "value", name: "收益差 %", nameTextStyle: { color: colors.muted, fontSize: 11 },
               axisLabel: { color: colors.muted, fontSize: 11 },
               splitLine: { lineStyle: { color: colors.border } } },
      series: [
        { name: "卡20 持仓组合收益差", type: "bar", data: years.map((y) => sm.get(y) ?? null),
          itemStyle: { color: C_DISC }, barMaxWidth: 22 },
        { name: "卡21 抄作业超额", type: "bar", data: years.map((y) => cm.get(y) ?? null),
          itemStyle: { color: "#8b5cf6" }, barMaxWidth: 22 },
      ],
    } as never;
  }, [recheck, colors]);

  const conflicts = useMemo(
    () => [...(fd?.regression?.industries || [])]
      .sort((a, b) => Math.abs(b.diff) - Math.abs(a.diff)).slice(0, 5), [fd]);
  const rmeta = fd?.regression?.meta;

  const th: React.CSSProperties = { textAlign: "left", padding: "7px 10px", fontSize: 11.5,
    color: colors.muted, borderBottom: `1px solid ${colors.border}`, fontWeight: 500, whiteSpace: "nowrap" };
  const td: React.CSSProperties = { padding: "7px 10px", fontSize: 12,
    borderBottom: `1px solid ${colors.border}`, whiteSpace: "nowrap" };
  const note = (bg: string, border: string, title: string, body: React.ReactNode) => (
    <div style={{ background: bg, border: `1px solid ${border}`, borderRadius: 10,
      padding: "12px 16px", marginBottom: 16 }}>
      <div style={{ fontWeight: 700, fontSize: 13.5, marginBottom: 5 }}>{title}</div>
      <div style={{ fontSize: 12.5, lineHeight: 1.75 }}>{body}</div>
    </div>
  );

  if (busy && !ov) return <div style={{ color: colors.muted, padding: 24 }}>加载穿透数据中…</div>;
  if (err) return <div style={{ color: colors.up, padding: 24 }}>加载失败：{err}</div>;
  if (!ov) return null;

  const src = ov.industry_source;
  const mapped = (src.sw_point || 0) + (src.sw_current || 0) + (src.tushare_mapped || 0);
  const totalMap = mapped + (src.unmapped_a ?? src.unmapped ?? 0);
  const hitRate = totalMap ? (mapped / totalMap * 100).toFixed(1) : "—";
  const hkShare = ov.non_sw_share?.["港股"] ?? 0;
  const brokenYears = Object.entries(ov.old_cover_median_by_year).filter(([, v]) => v < 90).map(([y]) => y);
  const detail = fd?.detail || [];
  const shown = showAll ? detail : detail.slice(0, 12);

  return (
    <div>
      <PageHeader
        title="基金持仓补齐 · 行业穿透"
        desc="双轨隔离口径：披露值与继承值分开存、分开展示，继承值只用于行业结构分母校正"
      />

      {/* completed 为 null 表示「池规模未知」（主库被容器占用时读不到），
          此时不能当成「未完成」渲染 —— total 为 null 会直接把整页打崩 */}
      {ov.fetch_progress && ov.fetch_progress.completed === false
        && ov.fetch_progress.total != null && (
        <div style={{ background: mode === "dark" ? "#1a2740" : "#eef4ff",
          border: `1px solid ${colors.border}`, borderRadius: 10,
          padding: "10px 16px", marginBottom: 14, fontSize: 12.5, lineHeight: 1.7 }}>
          全量持仓补齐进行中：<b>{ov.fetch_progress.done.toLocaleString()} / {ov.fetch_progress.total.toLocaleString()}</b>
          （{ov.fetch_progress.pct ?? 0}%）—— 当前页面展示的是已完成部分，
          拉数结束后会自动重算并刷新全量口径。
        </div>
      )}

      {note(
        mode === "dark" ? "#3a2318" : "#fff6e8", "#e8b96a",
        "先说结论：旧的「47% 未知」不是披露限制，是数据被截断了",
        <>
          旧 <code>fund_portfolio_year.csv</code> 在 <b>{brokenYears.join("、")}</b> 年每年恰好
          416000 行（52 页 × 8000，末页仍是满页），是 Tushare <code>offset&gt;400000</code> 硬截断，
          权重和只有 46–49%；2015–2019 未截断年份是 100%。按基金代码分片重拉后，
          <b>年报/半年报回到 100%，Q1/Q3 季报才是真实的前十大（约 53%）</b>。
          因此 11-07 记录的「只复制披露的 53% 持仓」以及基于它的持仓归因结论，需要按新数据重算。
        </>
      )}

      <div style={{ display: "grid", gridTemplateColumns: "repeat(4,1fr)", gap: 12, marginBottom: 16 }}>
        <KpiCard label="覆盖基金" value={ov.scope.n_funds.toLocaleString()}
          sub={gaps
            ? `最新期 ${gaps.with_holdings} 只有披露 / ${gaps.active_funds_45d} 只活跃，缺 ${gaps.missing} 只`
            : ov.scope.fund_pool} colors={colors} />
        <KpiCard label="持仓截面" value={ov.scope.n_sections.toLocaleString()}
          sub={`${fmtDate(ov.scope.date_range[0])} ~ ${fmtDate(ov.scope.date_range[1])}`} colors={colors} />
        <KpiCard label="A 股行业映射命中" value={`${hitRate}%`} sub={`${mapped.toLocaleString()} / ${totalMap.toLocaleString()} 股票-期`} colors={colors} tone="accent" />
        <KpiCard label="补齐明细行" value={ov.detail_rows.toLocaleString()}
          sub={ov.scope.is_sample ? "⚠ 当前为小样本，全量补齐进行中" : "全量口径"} colors={colors} />
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 14, marginBottom: 14 }}>
        <Card title="修复前后：年报持仓权重和（按年中位）" colors={colors}
          extra={<span style={{ fontSize: 11, color: colors.muted }}>橙=旧截断 / 蓝=重拉后</span>}>
          {repairOption && <EChart option={repairOption} height={260} />}
        </Card>
        <Card title="按报告期类型：覆盖度诊断" colors={colors}>
          <table style={{ width: "100%", borderCollapse: "collapse", marginTop: 4 }}>
            <thead><tr>
              <th style={th}>报告期</th><th style={th}>截面数</th><th style={th}>持仓数中位</th>
              <th style={th}>权重和中位</th><th style={th}>完整率</th>
            </tr></thead>
            <tbody>{["FY", "H1", "Q1", "Q3"].map((k) => {
              const c = ov.coverage_by_type[k];
              if (!c) return null;
              return <tr key={k}>
                <td style={{ ...td, fontWeight: 600 }}>{TYPE_LABEL[k]}</td>
                <td style={td}>{c.n_sections.toLocaleString()}</td>
                <td style={td}>{c.n_holdings_median}</td>
                <td style={{ ...td, color: c.cover_median >= 90 ? colors.down : colors.up, fontWeight: 700 }}>
                  {c.cover_median}%</td>
                <td style={td}>{(c.pct_complete * 100).toFixed(1)}%</td>
              </tr>;
            })}</tbody>
          </table>
          <div style={{ fontSize: 12, color: colors.muted, lineHeight: 1.7, marginTop: 12 }}>
            半年报/年报依法披露全部持仓，权重和约 100%，是补齐的<b>锚点</b>；
            Q1/Q3 季报只披露前十大（约 53%），差额部分才是需要「补齐」的对象。
            这两类缺口性质完全不同，不能混为一谈。
          </div>
        </Card>
      </div>

      <Card title="全市场行业配置：平均权重与补齐贡献" colors={colors}
        extra={<span style={{ fontSize: 11, color: colors.muted }}>蓝=真实披露 / 浅=锚点继承</span>} >
        {marketOption && <EChart option={marketOption} height={430} />}
        <div style={{ fontSize: 11.5, color: colors.muted, marginTop: 6, lineHeight: 1.8 }}>
          平均权重 = 全池所有持仓截面的均值（未配置该行业的截面按 0 计入）；浅色部分来自
          Q1/Q3 季报的锚点继承影子持仓，只用于行业结构分母校正。
          <b>相对基准的超配/低配请看下方单基金钻取</b> —— 单基金权重和为 100%，
          必然有正有负；而全市场层面对「持有该行业」的子集求超配恒为正，没有信息量，故不展示。<br />
          港股通标的（腾讯、美团、中芯等）不属于申万 A 股行业分类，
          <b>单列「港股」并从上方申万行业图中剔除</b>，全池港股平均仓位 {hkShare.toFixed(2)}%。
        </div>
      </Card>

      <div style={{ height: 14 }} />

      <Card title="行业反查：谁在重仓这个行业" colors={colors}
        extra={<span style={{ fontSize: 11, color: colors.muted }}>与下方单基金钻取互为反向</span>}>
        <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap", marginBottom: 12 }}>
          <select value={indSel}
            onChange={(e) => { setIndSel(e.target.value); loadIndustry(e.target.value, indMin); }}
            style={{ ...inputStyle(colors), width: 240 }}>
            {indList.map((x) => (
              <option key={x.industry} value={x.industry}>
                {x.industry} · 均 {x.avg_w.toFixed(1)}% · {x.n_ge5} 只≥5%
              </option>
            ))}
          </select>
          <span style={{ fontSize: 12, color: colors.muted }}>最低权重</span>
          <input type="number" value={indMin} min={0} max={100} step={1}
            onChange={(e) => setIndMin(Math.max(0, Math.min(100, Number(e.target.value) || 0)))}
            onKeyDown={(e) => { if (e.key === "Enter") loadIndustry(indSel, indMin); }}
            style={{ ...inputStyle(colors), width: 72 }} />
          <span style={{ fontSize: 12, color: colors.muted }}>%</span>
          <Btn small onClick={() => loadIndustry(indSel, indMin)}>{indBusy ? "…" : "查询"}</Btn>
          <span style={{ marginLeft: "auto", fontSize: 11.5, color: indErr ? colors.up : colors.muted }}>
            {indErr || (indRows.length ? `${indRows.length} 只 · 截至 ${fmtDate(indDate)}` : "")}
          </span>
        </div>
        {indRows.length > 0 && (
          <div style={{ maxHeight: 380, overflow: "auto" }}>
            <table style={{ width: "100%", borderCollapse: "collapse" }}>
              <thead><tr>
                <th style={th}>基金</th>
                <th style={th}>该行业权重</th>
                <th style={th}>真实披露</th>
                <th style={th}>锚点继承</th>
                <th style={th}>相对基准</th>
                <th style={th}>披露覆盖</th>
                <th style={th}>持仓</th>
                <th style={th}>口径</th>
              </tr></thead>
              <tbody>{indRows.map((r) => <tr key={r.ts_code}>
                <td style={td}>
                  <a onClick={() => { setCode(r.base_code); setQuery(r.base_code); loadFund(r.base_code, ""); }}
                    style={{ color: colors.accent, cursor: "pointer" }}>
                    {r.base_code} {r.name}
                  </a>
                </td>
                <td style={{ ...td, fontVariantNumeric: "tabular-nums", fontWeight: 600 }}>
                  {r.total_w.toFixed(2)}%
                </td>
                <td style={{ ...td, color: C_DISC }}>{r.disclosed_w.toFixed(2)}</td>
                <td style={{ ...td, color: colors.muted }}>{r.carried_w.toFixed(2)}</td>
                <td style={{ ...td, color: r.active_w >= 0 ? colors.up : colors.down }}>
                  {r.active_w >= 0 ? "+" : ""}{r.active_w.toFixed(2)}pp
                </td>
                <td style={{ ...td, color: colors.muted }}>{r.disclosed_cover.toFixed(0)}%</td>
                <td style={{ ...td, color: colors.muted }}>{r.n_disclosed}</td>
                <td style={td}>
                  <span style={{ padding: "2px 7px", borderRadius: 5, fontSize: 11,
                    background: r.exposure_basis === "structure_only" ? "rgba(217,119,6,.14)"
                      : "rgba(36,86,200,.10)",
                    color: r.exposure_basis === "structure_only" ? "#b45309" : colors.accent }}>
                    {r.exposure_basis === "structure_only" ? "仅结构"
                      : r.exposure_basis === "regulatory" ? "监管锚定" : "占净值"}
                  </span>
                </td>
              </tr>)}</tbody>
            </table>
          </div>
        )}
        <div style={{ fontSize: 11.5, color: colors.muted, marginTop: 8, lineHeight: 1.8 }}>
          「相对基准」= 该基金该行业权重 − 当期全池平均，<b>正值即超配</b>，按全池均值口径计算。
          口径标「仅结构」的基金，其原始权重不是占净值比（如 ETF 联接的 stk_mkv_ratio
          退化为占股票市值比），只做了结构归一 ——
          <b>结构可横向比，绝对仓位不可比</b>。已排除持仓少于 5 只的迷你基金
          （它们会以「100% 单一行业」挤满榜首）。点基金名跳到下方单基金钻取。
        </div>
      </Card>

      <div style={{ height: 14 }} />

      <FundStylePanel />

      <div style={{ height: 14 }} />

      <Card title="单基金钻取：覆盖度轨迹与行业穿透" colors={colors}
        extra={<span style={{ fontSize: 11, color: colors.muted }}>{fd ? `${fd.name} · ${fd.ts_code}` : ""}</span>}>
        <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap", marginBottom: 14 }}>
          <input value={query} onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") search(query); }}
            placeholder="基金代码或名称，如 000001 / 华夏" style={{ ...inputStyle(colors), width: 250 }} />
          <Btn small onClick={() => search(query)}>搜索</Btn>
          <select value={code} onChange={(e) => { const f = funds.find((x) => x.base_code === e.target.value); if (f) pick(f); }}
            style={{ ...inputStyle(colors), width: 230 }}>
            {funds.map((f) => <option key={f.base_code} value={f.base_code}>{f.base_code} {f.name}</option>)}
          </select>
          {fd && <select value={endDate} onChange={(e) => { setEndDate(e.target.value); loadFund(code, e.target.value); }}
            style={{ ...inputStyle(colors), width: 150 }}>
            {[...fd.dates].reverse().map((d) => <option key={d} value={d}>{fmtDate(d)}</option>)}
          </select>}
        </div>

        {fdErr && <div style={{ color: colors.up, fontSize: 12.5, marginBottom: 10 }}>{fdErr}</div>}
        {fdBusy && <div style={{ color: colors.muted, fontSize: 12.5 }}>加载中…</div>}
        {fd?.fof?.is_fof && (
          <div style={{ background: mode === "dark" ? "#3a2318" : "#fff6e8",
            border: `1px solid ${colors.up}`, borderRadius: 8, padding: "10px 14px",
            marginBottom: 12, fontSize: 12.5, lineHeight: 1.8, color: colors.text }}>
            <b>⚠ 这是一只 FOF，行业穿透不适用。</b>现有数据源（Tushare / 天天基金 F10）对 FOF
            只返回<b>直接持有的股票</b>，不返回被投基金；该基金直接 A 股仓位中位约
            {fd.fof.reg_equity_med ?? "—"}%，净值中约 <b>{fd.fof.unpenetrated_upper ?? "—"}%</b>
            属于「被投基金 + 债券 + 现金」的不可观测上界。下方行业配置只代表直接持股，
            <b>不能当作该 FOF 的完整行业敞口</b>。
          </div>
        )}
        {fd && <>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(4,1fr)", gap: 10, marginBottom: 14 }}>
            <KpiCard label="当期披露覆盖" value={`${fd.current.disclosed_cover.toFixed(1)}%`}
              sub={`${TYPE_LABEL[fd.current.report_type] || fd.current.report_type} · ${fmtDate(fd.end_date)}${
                fd.current.exposure_basis === "structure_only" ? " · 仅结构" : ""}`} colors={colors} />
            <KpiCard label="锚点继承" value={`${fd.current.carried_cover.toFixed(1)}%`}
              sub={fd.current.anchor_end_date ? `锚点 ${fmtDate(fd.current.anchor_end_date)} · ${fd.current.anchor_gap_days} 天` : "完整披露，无需补齐"}
              colors={colors} tone="accent" />
            <KpiCard label="补齐置信度" value={fd.current.confidence.toFixed(2)}
              sub={fd.current.lookahead ? "⚠ 锚点公告日晚于当期" : "前十大命中率 × 时间衰减"} colors={colors} />
            <KpiCard label="行业集中度" value={fd.current.hhi.toFixed(3)}
              sub={`HHI · 前三大行业 ${fd.current.top3_share.toFixed(1)}%`} colors={colors} />
          </div>
          {fd.current.warning && (
            <div style={{ background: mode === "dark" ? "#3a2318" : "#fff6e8",
              border: `1px solid ${colors.up}`, borderRadius: 8, padding: "9px 13px",
              marginBottom: 12, fontSize: 12.5, lineHeight: 1.7 }}>
              {fd.current.warning}
            </div>
          )}
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 14 }}>
            <div>
              <div style={{ fontSize: 12.5, fontWeight: 600, marginBottom: 6 }}>覆盖度轨迹（披露 + 继承）</div>
              {covOption && <EChart option={covOption} height={300} />}
            </div>
            <div>
              <div style={{ fontSize: 12.5, fontWeight: 600, marginBottom: 6 }}>当期行业配置（双轨）</div>
              {indOption && <EChart option={indOption} height={300} />}
            </div>
          </div>

          <div style={{ marginTop: 16, fontSize: 12.5, fontWeight: 600, marginBottom: 6 }}>
            当期持仓明细（{detail.length} 条，逐条标注来源）
          </div>
          <div style={{ overflowX: "auto", border: `1px solid ${colors.border}`, borderRadius: 8 }}>
            <table style={{ width: "100%", borderCollapse: "collapse" }}>
              <thead><tr>
                <th style={th}>股票</th><th style={th}>申万一级</th><th style={th}>权重</th>
                <th style={th}>来源</th><th style={th}>锚点</th><th style={th}>置信度</th>
              </tr></thead>
              <tbody>{shown.map((r, i) => <tr key={`${r.symbol}-${r.source}-${i}`}>
                <td style={{ ...td, fontFamily: "monospace" }}>
                  {r.symbol.startsWith("PROXY:") ? `行业代理 · ${r.industry}` : r.symbol}
                </td>
                <td style={td}>{r.industry}</td>
                <td style={{ ...td, fontVariantNumeric: "tabular-nums" }}>{r.weight.toFixed(2)}%</td>
                <td style={td}>
                  <span style={{ padding: "2px 7px", borderRadius: 5, fontSize: 11,
                    background: r.source === "disclosed" ? "rgba(36,86,200,.12)"
                      : r.source === "carried_proxy" ? "rgba(217,119,6,.14)" : "rgba(183,196,222,.28)",
                    color: r.source === "disclosed" ? colors.accent
                      : r.source === "carried_proxy" ? "#b45309" : colors.muted }}>
                    {r.source === "disclosed" ? "真实披露"
                      : r.source === "carried_proxy" ? "行业代理" : "锚点继承"}
                  </span>
                </td>
                <td style={{ ...td, color: colors.muted }}>{r.anchor_end_date ? fmtDate(r.anchor_end_date) : "—"}</td>
                <td style={{ ...td, color: colors.muted }}>{r.source === "carried" ? r.confidence.toFixed(2) : "1.00"}</td>
              </tr>)}</tbody>
            </table>
          </div>
          {detail.length > 12 && <div style={{ marginTop: 8 }}>
            <Btn small kind="ghost" onClick={() => setShowAll(!showAll)}>
              {showAll ? "收起" : `展开全部 ${detail.length} 条`}
            </Btn>
          </div>}
        </>}
      </Card>

      {fd?.regression?.available && (
        <>
          <div style={{ height: 14 }} />
          <Card title="净值回归校验 · 第三轨交叉验证" colors={colors}
            extra={<span style={{ fontSize: 11, color: colors.muted }}>回归结果不覆盖主口径，只作验证</span>}>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(145px,1fr))",
              gap: 10, marginBottom: 14 }}>
              <KpiCard label="回归 R²" value={(rmeta?.r2 ?? 0).toFixed(2)}
                sub={`${rmeta?.n_obs ?? 0} 个交易日 · 时间衰减 WLS`} colors={colors} tone="accent" />
              <KpiCard label="排序一致性" value={(rmeta?.spearman ?? 0).toFixed(2)}
                sub="Spearman · ≥0.6 视为一致" colors={colors} />
              <KpiCard label="行业偏离 L1" value={`${(rmeta?.bayes_l1 ?? 0).toFixed(1)}pp`}
                sub="31 行业合计绝对偏离" colors={colors} />
              <KpiCard label="建议融合权重 λ" value={(rmeta?.lambda ?? 0).toFixed(2)}
                sub="仅供参考，当前不融合" colors={colors} />
              <KpiCard label="加权 MSE" value={(rmeta?.mse ?? 0).toFixed(2)}
                sub="越低拟合越好" colors={colors} />
            </div>
            {regOption && <EChart option={regOption} height={380} />}
            <div style={{ fontSize: 12.5, fontWeight: 600, margin: "14px 0 6px" }}>分歧最大的 5 个行业</div>
            <div style={{ overflowX: "auto", border: `1px solid ${colors.border}`, borderRadius: 8 }}>
              <table style={{ width: "100%", borderCollapse: "collapse" }}>
                <thead><tr>
                  <th style={th}>行业</th><th style={th}>贝叶斯补齐</th>
                  <th style={th}>净值回归 β</th><th style={th}>差值</th>
                </tr></thead>
                <tbody>{conflicts.map((r) => (
                  <tr key={r.industry}>
                    <td style={{ ...td, fontWeight: 600 }}>{r.industry}</td>
                    <td style={td}>{r.bayes_w.toFixed(2)}%</td>
                    <td style={td}>{r.beta.toFixed(2)}%</td>
                    <td style={{ ...td, color: r.diff >= 0 ? colors.up : colors.down, fontWeight: 700 }}>
                      {r.diff >= 0 ? "+" : ""}{r.diff.toFixed(2)}pp
                    </td>
                  </tr>
                ))}</tbody>
              </table>
            </div>
            <div style={{ marginTop: 12, fontSize: 12, color: colors.muted, lineHeight: 1.85 }}>
              第三轨是独立验证：以「已披露行业权重」为下界硬约束，用报告期前 60 个交易日的申万一级行业日收益
              对基金日收益做时间衰减 WLS（单纯形投影梯度求解，无 scipy 依赖），
              <b>不覆盖贝叶斯双轨主结果</b>。两者一致 → 补齐可信度提高；分歧大 → 该期补齐假设存疑。
              已知分歧来源：股票总仓位按锚点继承、港股 / 债券 / 现金未建模、窗口内调仓、
              行业指数与个股组合的权重差异。单基金回归误差大，横截面聚合才相对稳定。
            </div>
          </Card>
        </>
      )}

      {fd?.regulatory?.available && (
        <>
          <div style={{ height: 14 }} />
          <Card title="监管披露校验 · 第四轨（证监会行业配置）" colors={colors}
            extra={<span style={{ fontSize: 11, color: colors.muted }}>
              总仓位来源：{fd.current.equity_source === "regulatory" ? "监管披露值" : "锚点回退"}
            </span>}>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(160px,1fr))",
              gap: 10, marginBottom: 14 }}>
              <KpiCard label="监管 A 股仓位" value={`${(fd.regulatory.a_share_pct ?? 0).toFixed(1)}%`}
                sub="Σ 门类占基金资产净值比" colors={colors} tone="accent" />
              <KpiCard label="补齐后 A 股申万合计" value={`${(fd.regulatory.our_a_share_pct ?? 0).toFixed(1)}%`}
                sub="本平台主口径" colors={colors} />
              <KpiCard label="偏差" value={`${((fd.regulatory.our_a_share_pct ?? 0) - (fd.regulatory.a_share_pct ?? 0) >= 0 ? "+" : "")}${((fd.regulatory.our_a_share_pct ?? 0) - (fd.regulatory.a_share_pct ?? 0)).toFixed(1)}pp`}
                sub="接近 0 说明总量约束生效" colors={colors} />
              <KpiCard label="监管门类数" value={String(fd.regulatory.industries.length)}
                sub="证监会行业分类（约 19 类）" colors={colors} />
            </div>
            <div style={{ overflowX: "auto", border: `1px solid ${colors.border}`, borderRadius: 8 }}>
              <table style={{ width: "100%", borderCollapse: "collapse" }}>
                <thead><tr><th style={th}>证监会门类</th><th style={th}>代码</th>
                  <th style={th}>占净值比例（披露值）</th></tr></thead>
                <tbody>{fd.regulatory.industries.map((r) => (
                  <tr key={r.code}>
                    <td style={{ ...td, fontWeight: 600 }}>{r.name}</td>
                    <td style={{ ...td, fontFamily: "monospace", color: colors.muted }}>{r.code}</td>
                    <td style={{ ...td, fontVariantNumeric: "tabular-nums" }}>{r.weight.toFixed(2)}%</td>
                  </tr>
                ))}</tbody>
              </table>
            </div>
            <div style={{ marginTop: 12, fontSize: 12, color: colors.muted, lineHeight: 1.85 }}>
              <b>法规依据</b>：证监会《证券投资基金信息披露编报规则第4号》第五条 / 《内容与格式准则第2号》第三十条
              —— 定期报告必须披露「报告期末按行业分类的股票投资组合」，列示各行业市值及
              <b>占基金资产净值比例</b>。季报虽只披露前十大个股，但行业配置是<b>按全部持仓计算的真实分布</b>，
              不是重仓股推算，因此是补齐缺失仓位最硬的约束。<br />
              <b>主口径改动</b>：用 Σ 门类占比（真实 A 股仓位）替换「按锚点继承 100% 股票仓位」假设，
              作为 residual 与贝叶斯后验目标的总量约束；监管数据缺失才回退锚点，逐截面标
              <code>equity_source</code>。抽样诊断显示，补齐后 A 股合计相对监管值的
              高估 &gt;10pp 占比从 <b>63.1% 降到 9.4%</b>，中位偏差归零。<br />
              <b>口径边界</b>：分母是基金资产净值（不是总资产）；港股通不在证监会 A 股分类内，
              故监管值只约束 A 股、港股仍单列；证监会门类与申万一级 31 类<b>没有官方一一映射</b>
              （制造业常占 60%+），因此门类分布只作独立展示与总量约束，不强行映射到申万。<br />
              全局：当前 <b>{ov.regulatory?.n_sections.toLocaleString() ?? "—"}</b> 个截面用上监管值
              （占 {ov.regulatory?.share_pct ?? "—"}%），中位 A 股仓位
              {ov.regulatory?.a_share_pct_median ?? "—"}%，
              回退锚点 {ov.regulatory?.anchor_sections.toLocaleString() ?? "—"} 个截面。
              数据源：{ov.regulatory?.source ?? "—"}。
            </div>
          </Card>
        </>
      )}

      {regHold?.available && (
        <>
          <div style={{ height: 14 }} />
          <Card title="回归隐含持仓 · 第五轨（行业 β → 个股）" colors={colors}
            extra={<span style={{ fontSize: 11, color: colors.muted }}>
              {fmtDate(regHold.end_date)} · 质量 {regHold.meta?.quality ?? "—"}
            </span>}>
            <div style={{ background: mode === "dark" ? "#1a2740" : "#eef4ff",
              border: `1px solid ${colors.border}`, borderRadius: 8,
              padding: "10px 14px", marginBottom: 14, fontSize: 12.5, lineHeight: 1.8 }}>
              约束 WLS 只给到<b>申万行业一级</b>的 β。这一轨把 β 当作每个行业的目标权重，
              减去真实披露的前十大（<b>硬下界，回归不能推翻</b>），再把行业缺口按锚点完整持仓的
              真实候选股分配，得到<b>逐股</b>的「回归隐含权重」。它比主口径多一层推断，
              <b>独立存储、绝不覆盖 disclosed / carried</b>；quality=low 的截面只作参考。
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(150px,1fr))",
              gap: 10, marginBottom: 14 }}>
              <KpiCard label="β 目标总仓位" value={`${(regHold.meta?.beta_total ?? 0).toFixed(1)}%`}
                sub="行业回归 + 非申万保留" colors={colors} tone="accent" />
              <KpiCard label="真实披露下界" value={`${(regHold.meta?.disclosed_total ?? 0).toFixed(1)}%`}
                sub="不可被回归推翻" colors={colors} />
              <KpiCard label="回归隐含未披露" value={`${(regHold.meta?.implied_total ?? 0).toFixed(1)}%`}
                sub={`${regHold.meta?.n_stocks ?? 0} 只候选股 / ${regHold.meta?.n_proxy ?? 0} 个行业代理`} colors={colors} />
              <KpiCard label="与贝叶斯 L1 差异" value={`${(regHold.meta?.l1_vs_bayes ?? 0).toFixed(1)}pp`}
                sub="逐股双向绝对偏离合计" colors={colors} />
              <KpiCard label="回归 R² / 排序一致" value={`${(regHold.meta?.r2 ?? 0).toFixed(2)} / ${(regHold.meta?.spearman ?? 0).toFixed(2)}`}
                sub={`${regHold.meta?.n_obs ?? 0} 交易日窗口`} colors={colors} />
            </div>
            <div style={{ overflowX: "auto", border: `1px solid ${colors.border}`, borderRadius: 8 }}>
              <table style={{ width: "100%", borderCollapse: "collapse" }}>
                <thead><tr>
                  <th style={th}>股票 / 占位</th><th style={th}>申万行业</th>
                  <th style={th}>β 隐含权重</th><th style={th}>贝叶斯权重</th>
                  <th style={th}>差异</th><th style={th}>来源</th>
                </tr></thead>
                <tbody>{regHold.items.slice(0, 18).map((r) => (
                  <tr key={`${r.symbol}-${r.source}`}>
                    <td style={{ ...td, fontFamily: "monospace" }}>{r.symbol}</td>
                    <td style={td}>{r.industry}</td>
                    <td style={{ ...td, fontVariantNumeric: "tabular-nums", color: colors.accent }}>
                      {r.beta_w.toFixed(2)}%</td>
                    <td style={{ ...td, fontVariantNumeric: "tabular-nums", color: colors.muted }}>
                      {r.bayes_w.toFixed(2)}%</td>
                    <td style={{ ...td, fontVariantNumeric: "tabular-nums",
                      color: r.diff >= 0 ? colors.up : colors.down }}>
                      {r.diff >= 0 ? "+" : ""}{r.diff.toFixed(2)}pp</td>
                    <td style={td}>
                      <span style={{ padding: "2px 7px", borderRadius: 5, fontSize: 10.5,
                        background: r.source === "regression_implied"
                          ? "rgba(36,86,200,.12)" : "rgba(183,196,222,.28)",
                        color: r.source === "regression_implied" ? colors.accent : colors.muted }}>
                        {{ regression_implied: "回归隐含", regression_proxy: "行业代理",
                           unchanged_non_sw: "非申万保留", dropped_by_beta: "β 归零" }[r.source] || r.source}
                      </span>
                    </td>
                  </tr>
                ))}</tbody>
              </table>
            </div>
            <div style={{ marginTop: 12, fontSize: 12, color: colors.muted, lineHeight: 1.85 }}>
              <b>关键局限</b>：β 只到行业一级，行业<b>内部</b>的个股分布仍来自锚点历史持仓，
              因此这是二级推断，置信度低于行业 β；单股上限仍取本期第 10 大重仓，
              撞上限的余额用 <code>PROXY:行业</code> 占位、不伪造个股。港股 / 其他 A 股不在 β 覆盖内，
              原样保留贝叶斯值。真实披露值一律不参与反推。<br />
              <b>全局</b>：{regHold.summary?.n_sections?.toLocaleString() ?? "—"} 个截面 /
              {regHold.summary?.n_rows?.toLocaleString() ?? "—"} 行 /
              {regHold.summary?.n_funds?.toLocaleString() ?? "—"} 只基金；
              质量分布 high {regHold.summary?.quality?.high ?? 0} / medium {regHold.summary?.quality?.medium ?? 0} /
              low {regHold.summary?.quality?.low ?? 0}；
              权重守恒 |偏差|&lt;1pp 占比
              {(((regHold.summary?.conserve_within_1pp ?? 0) * 100) || 0).toFixed(1)}%。
              {regHold.summary?.discipline}
            </div>
          </Card>
        </>
      )}

      <div style={{ height: 14 }} />
      {note(
        mode === "dark" ? "#1a2740" : "#eef4ff", colors.border,
        "口径纪律（不可放宽）",
        <>
          <b>disclosed</b> = 报告期真实披露持仓；<b>carried</b> = 用「过去最近一个完整截面」的真实持仓
          生成的影子持仓，按剩余仓位缩放，仅用于行业结构的分母校正。
          两条轨道分开存、分开展示，<b>收益/归因类结论一律只用 disclosed</b>。
          每条继承记录都带锚点日期、间隔天数与置信度；若锚点公告日晚于当期报告期，
          会打 lookahead 标记并把置信度减半（当前 {ov.concentration.lookahead_sections.toLocaleString()} 个截面命中）。
          {ov.discipline}
        </>
      )}
      {ov.method && (
        <>
          <div style={{ height: 14 }} />
          <Card title="方法与口径 · 数据怎么来 / 怎么补 / 怎么穿透" colors={colors}
            extra={<span style={{ fontSize: 11, color: colors.muted }}>{ov.method.version}</span>}>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(215px,1fr))", gap: 10 }}>
              {ov.method.steps.map((s) => (
                <div key={s.n} style={{ border: `1px solid ${colors.border}`, borderRadius: 9, padding: "10px 12px" }}>
                  <div style={{ fontSize: 10.5, color: colors.accent, fontWeight: 700, letterSpacing: 1 }}>STEP {s.n}</div>
                  <div style={{ fontSize: 13, fontWeight: 600, margin: "3px 0 6px" }}>{s.title}</div>
                  <div style={{ fontSize: 11.5, color: colors.muted, lineHeight: 1.7 }}>{s.desc}</div>
                </div>
              ))}
            </div>

            <div style={{ marginTop: 14, background: mode === "dark" ? "#111826" : "#f7f9fd",
              border: `1px solid ${colors.border}`, borderRadius: 8, padding: "11px 14px",
              fontFamily: "'SF Mono', Menlo, Consolas, monospace", fontSize: 11.8, lineHeight: 1.85 }}>
              <div style={{ fontWeight: 700, marginBottom: 4 }}>关键公式（当前生效口径）</div>
              <div>α_i = 1 + κ·a_i + n·d_i　　　# Dirichlet 共轭后验；a = 锚点行业分布，d = 当期前十大行业分布</div>
              <div>κ = max(2, 60·overlap·exp(−gap/365))　# 间隔越久 / 换手越大越不信锚点；n = 0.6 × disclosed_cover</div>
              <div>g_i = max(0, posterior_i × anchor_cover − disclosed_i)　# 只补正的行业缺口</div>
              <div>置信度 = {ov.method.confidence}</div>
              <div>基准 = {ov.method.benchmark}</div>
            </div>

            <div style={{ fontSize: 13, fontWeight: 600, margin: "16px 0 8px" }}>业界怎么做 · 我们处在哪一层</div>
            <div style={{ overflowX: "auto", border: `1px solid ${colors.border}`, borderRadius: 8 }}>
              <table style={{ width: "100%", borderCollapse: "collapse" }}>
                <thead><tr>
                  <th style={th}>玩家</th><th style={th}>做法</th><th style={th}>是否建模</th><th style={th}>精度 / 限制</th>
                </tr></thead>
                <tbody>{PEERS.map((p) => (
                  <tr key={p.who} style={{ background: p.who.startsWith("本平台")
                    ? (mode === "dark" ? "#1a2740" : "#eef4ff") : undefined }}>
                    <td style={{ ...td, fontWeight: 600, whiteSpace: "normal", minWidth: 150 }}>{p.who}</td>
                    <td style={{ ...td, whiteSpace: "normal", minWidth: 180 }}>{p.what}</td>
                    <td style={{ ...td, whiteSpace: "normal", minWidth: 190 }}>{p.model}</td>
                    <td style={{ ...td, whiteSpace: "normal", minWidth: 220, color: colors.muted }}>{p.acc}</td>
                  </tr>
                ))}</tbody>
              </table>
            </div>
            <div style={{ fontSize: 10.5, color: colors.muted, marginTop: 6, lineHeight: 1.7 }}>
              对照来源：东吴证券 2026-05-19《利用高频数据优化公募基金持仓分析》、天风 / 国金 / 华泰 / 信达 / 兴证金工公开研报、
              证监会 2026 年第一期《机构监管情况通报》、Wind AMS 产品说明。均为非一手来源，需核实原文。
            </div>

            <div style={{ marginTop: 14, background: mode === "dark" ? "#3a2318" : "#fff6e8",
              border: "1px solid #e8b96a", borderRadius: 8, padding: "11px 14px" }}>
              <div style={{ fontWeight: 700, fontSize: 13, marginBottom: 5 }}>
                净值回归：数据补齐后已经做上了（v3 第三轨）
              </div>
              <div style={{ fontSize: 12, lineHeight: 1.8 }}>
                <b>数据源已找到并落地。</b>Tushare <code>sw_daily</code> 提供申万 2021 一级 31 个行业指数日线
                （89,187 行，2015-01-05 ~ 2026-11-06，直接带日收益率）；<code>fund_nav</code> 按基金代码
                提供历史日频复权净值（7,489 只 × 约 2,700 交易日）。两者全部进独立研究库，
                不再依赖平台原先只有 25 天的净值。<br />
                回归把「已披露行业权重」作为下界硬约束，用报告期前 60 个交易日做时间衰减 WLS，
                单纯形投影梯度求解（环境无 scipy，用 numpy 自实现）。
                <b>结果作为独立第三轨，只做交叉验证、不覆盖贝叶斯主口径。</b>
                {ov.regression && <>
                  <br />当前覆盖 <b>{ov.regression.n_sections.toLocaleString()}</b> 个截面 /
                  {ov.regression.n_funds.toLocaleString()} 只基金，R² 中位 <b>{ov.regression.r2_median}</b>，
                  排序相关中位 <b>{ov.regression.spearman_median}</b>，
                  一致 {(ov.regression.agree_share * 100).toFixed(1)}% /
                  高分歧 {(ov.regression.high_conflict_share * 100).toFixed(1)}%，
                  低信息截面剔除 {ov.regression.skipped_low_info.toLocaleString()} 个。
                </>}
                <br />仍未做：{ov.method.not_done}。调研里两条实证依然成立：
                <b>单只基金回归不可信、横截面聚合才可信</b>；
                <b>重仓行业补全法更准、轻仓行业回归法更准</b>（补全法前三大行业平均偏高 +0.47 / +0.34 / +0.59pp，
                回归法第一大行业平均偏低 −1.11pp），所以两者并列而非择一。
              </div>
            </div>

            <div style={{ fontSize: 13, fontWeight: 600, margin: "16px 0 6px" }}>已知局限（读结论前先过一遍）</div>
            <ul style={{ margin: 0, paddingLeft: 20, fontSize: 12, color: colors.muted, lineHeight: 1.9 }}>
              <li>季报前十大只占净值约 55%，未披露部分是<b>估计</b>不是事实；披露仓位越低，补齐假设越不可靠（页面会自动警示）。</li>
              <li>贝叶斯后验只用「上期行业结构 + 当期前十大」，没有净值信息，捕捉不到报告期内的快速调仓。</li>
              <li>股票总仓位按锚点继承；基金若主动减仓 / 加仓，继承权重会系统性偏高或偏低。</li>
              <li>报告期是时点快照，披露滞后 15 个工作日～3 个月，不能当作实时持仓。</li>
              <li>港股通标的单列「港股」不做申万穿透；北交所 / 退市股用 Tushare 细分行业兜底。</li>
              <li>单只基金的估计误差大，横截面聚合才相对稳定；季报结论应视为区间而非点估计。</li>
            </ul>

            <div style={{ marginTop: 12, fontSize: 11.5, color: colors.muted, lineHeight: 1.8 }}>
              当前运行：行业代理 {ov.proxy?.rows.toLocaleString() ?? "—"} 条，
              占继承权重 {ov.proxy?.share_pct ?? "—"}%（其余全部落到真实历史个股）；
              A 股行业映射命中 {hitRate}%；补齐截面 {ov.concentration.carried_sections.toLocaleString()} 个，
              平均置信度 {ov.concentration.mean_confidence}。{ov.method.discipline}
            </div>
          </Card>
        </>
      )}
      {recheck && (
        <>
          <div style={{ height: 14 }} />
          <Card title="结论复核 · 修好持仓数据后，两个旧结论变显著了" colors={colors}
            extra={<span style={{ fontSize: 11, color: colors.muted }}>
              研究卡 20 / 21 · {recheck.recheck_at.slice(0, 10)}
            </span>}>
            <div style={{ background: mode === "dark" ? "#1a2740" : "#eef4ff",
              border: `1px solid ${colors.border}`, borderRadius: 8,
              padding: "10px 14px", marginBottom: 14, fontSize: 12.5, lineHeight: 1.8 }}>
              卡 20 / 21 原本建立在被 offset 截断的持仓上（2020–2025 权重和只有 46–49%）。
              改用完整年报持仓（权重和中位 <b>{recheck.cover_median}%</b>，
              {recheck.n_annual_rows.toLocaleString()} 行 / {recheck.n_funds.toLocaleString()} 只基金）重算后，
              <b>两个「方向成立但证据不足」的结论都跨过了 t=2</b>——修数据不只是改数字，是改结论强度。
            </div>
            <div style={{ overflowX: "auto", border: `1px solid ${colors.border}`, borderRadius: 8 }}>
              <table style={{ width: "100%", borderCollapse: "collapse" }}>
                <thead><tr>
                  <th style={th}>指标</th><th style={th}>旧（截断数据）</th>
                  <th style={th}>新（完整持仓）</th><th style={th}>变化</th>
                </tr></thead>
                <tbody>
                  {[
                    ["卡20 持仓组合收益差（Top5% − 其余）",
                     `+${recheck.style.old.hold_diff}% (t=${recheck.style.old.hold_t})`,
                     `+${recheck.style.hold_diff}% (t=${recheck.style.hold_t})`,
                     "接近翻倍，t 过 2"],
                    ["卡20 基金实际收益差",
                     `+${recheck.style.old.fund_diff}% (t=${recheck.style.old.fund_t})`,
                     `+${recheck.style.fund_diff}% (t=${recheck.style.fund_t})`,
                     "从不显著到显著"],
                    ["卡20 交易 / 择时损耗 gap",
                     `${recheck.style.old.gap}pp`, `${recheck.style.gap}pp`,
                     "几乎不变，机制结论稳健"],
                    ["卡21 无前视抄作业超额",
                     `+${recheck.copy.old.lag_diff}% (t=${recheck.copy.old.lag_t}, ${recheck.copy.old.lag_pos})`,
                     `+${recheck.copy.lag_diff}% (t=${recheck.copy.lag_t}, ${recheck.copy.lag_pos})`,
                     "数值稳定，显著性与年份数提升"],
                  ].map((row) => (
                    <tr key={row[0]}>
                      <td style={{ ...td, fontWeight: 600, whiteSpace: "normal" }}>{row[0]}</td>
                      <td style={{ ...td, color: colors.muted }}>{row[1]}</td>
                      <td style={{ ...td, color: colors.accent, fontWeight: 700 }}>{row[2]}</td>
                      <td style={{ ...td, whiteSpace: "normal", color: colors.muted }}>{row[3]}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {recheckOption && <div style={{ marginTop: 14 }}><EChart option={recheckOption} height={260} /></div>}

            <div style={{ marginTop: 12, fontSize: 12, color: colors.muted, lineHeight: 1.85 }}>
              <b>稳健性</b>：卡20 {recheck.style.years}（{recheck.style.yearly.length} 年，中位
              +9.25%）、卡21 {recheck.copy.years}（{recheck.copy.yearly.length} 年，中位 +7.09%）；
              剔除贡献最大的 2024 年后 t 仍为 2.07 / 2.13，不是单一年份主导。
              已披露日早于 4/15 建仓日的持仓占 {recheck.copy.known_pct}%。<br />
              <b>但别过度解读</b>：卡20 的 style 口径是「年末持仓 → 次年整年」，年报 3 月底才披露，
              <b>含前视，只作机制分解不可实盘</b>；卡21 才是 4/15 建仓的无前视可交易口径。
              2024 年贡献偏大（+28.1% / +17.3%），剔掉后属边际显著，不能当成稳定 alpha。<br />
              <b>口径</b>：只用年报真实披露持仓（不用补齐/回归值）；{recheck.excluded}；
              显著性用年度等权序列 t（n≤10），不用 pooled 观测。数据源：{recheck.data_source}。
            </div>
          </Card>
        </>
      )}
      {fof && (
        <>
          <div style={{ height: 14 }} />
          <Card title="FOF 直接持仓诊断 · 不穿透口径" colors={colors}
            extra={<span style={{ fontSize: 11, color: colors.muted }}>
              样本内 {fof.n_fof} 只 FOF · {fof.n_fof_sections.toLocaleString()} 个截面
            </span>}>
            <div style={{ background: mode === "dark" ? "#3a2318" : "#fff6e8",
              border: `1px solid ${colors.up}`, borderRadius: 8,
              padding: "10px 14px", marginBottom: 14, fontSize: 12.5, lineHeight: 1.8 }}>
              <b>先给结论：不是不想做，是数据源不返回被投基金。</b>{fof.verdict}
              我们实测了 Tushare <code>fund_portfolio</code>（17 只 FOF 抽样，4 只有数据、返回的全是股票代码）
              和天天基金 F10 <code>jjcc</code>（页面明确写「报告期末占基金资产净值比例排序的<b>股票</b>一览」），
              两条路都拿不到「基金投资明细」。样本里这 {fof.n_fof} 只 FOF 的持仓明细
              <b>0 行被投基金代码</b>，所以不做任何伪造的嵌套展开。
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(165px,1fr))",
              gap: 10, marginBottom: 14 }}>
              <KpiCard label="FOF 直接 A 股仓位" value={`${fof.fof.reg_equity_median.toFixed(1)}%`}
                sub="监管披露值 · 中位" colors={colors} tone="accent" />
              <KpiCard label="非 FOF 直接 A 股仓位" value={`${fof.non_fof.reg_equity_median.toFixed(1)}%`}
                sub="相差约 74pp" colors={colors} />
              <KpiCard label="不可观测资产上界" value={`${fof.unpenetrated_upper_median.toFixed(1)}%`}
                sub="含被投基金 + 债券 + 现金" colors={colors} />
              <KpiCard label="FOF 披露覆盖中位" value={`${fof.fof.disclosed_cover_median.toFixed(1)}%`}
                sub={`非 FOF ${fof.non_fof.disclosed_cover_median.toFixed(1)}%`} colors={colors} />
            </div>
            <div style={{ fontSize: 12.5, fontWeight: 600, marginBottom: 8 }}>
              排除 FOF 前后：全池平均行业权重（{fmtDate(fof.last_end_date)}）
            </div>
            <div style={{ overflowX: "auto", border: `1px solid ${colors.border}`, borderRadius: 8 }}>
              <table style={{ width: "100%", borderCollapse: "collapse" }}>
                <thead><tr><th style={th}>申万行业 / 港股</th><th style={th}>含 FOF</th>
                  <th style={th}>排除 FOF</th><th style={th}>差异</th></tr></thead>
                <tbody>{fof.industry_impact_excluding_fof.map((r) => (
                  <tr key={r.industry}>
                    <td style={{ ...td, fontWeight: 600 }}>{r.industry}</td>
                    <td style={{ ...td, fontVariantNumeric: "tabular-nums" }}>{r.with_fof.toFixed(2)}%</td>
                    <td style={{ ...td, fontVariantNumeric: "tabular-nums" }}>{r.without_fof.toFixed(2)}%</td>
                    <td style={{ ...td, fontVariantNumeric: "tabular-nums", color: colors.accent }}>
                      {r.delta >= 0 ? "+" : ""}{r.delta.toFixed(2)}pp</td>
                  </tr>
                ))}</tbody>
              </table>
            </div>
            <div style={{ marginTop: 12, fontSize: 12, color: colors.muted, lineHeight: 1.85 }}>
              <b>「不可观测资产上界」怎么读</b>：= 100% − 直接 A 股仓位 − 港股仓位，
              里面混着被投基金、债券、现金和其他资产；债券与现金<b>本来就没有行业属性</b>，
              所以这是「无法用现有数据源穿透的比例上界」，<b>不等于被投基金占比</b>。<br />
              <b>行业均值口径</b>：{fof.industry_mean_note ?? "分母为该报告期全部基金，未配置按 0 计入。"}
              实测排除 FOF 后各行业均值变化都 &lt;0.1pp —— 因为 FOF 只占 196/7489 且直接股票仓位极低，
              <b>对全市场结论几乎无影响</b>，但<b>单只 FOF 的行业穿透必须打标不适用</b>
              （页面钻取已自动提示）。<br />
              <b>要做真多层穿透缺什么</b>：Wind AMS / 聚源等机构库的「基金投资明细」，
              或基金定期报告 PDF 中「报告期末基金投资明细」章节。
              <b>后者已经落地</b>：见下一张卡（基金层穿透），解析的是原始披露、无任何估算。<br />
              <b>处置建议</b>：{fof.recommendation}
            </div>
          </Card>
          <div style={{ height: 14 }} />
          <Card title="FOF 基金层穿透 · 直接解析定期报告" colors={colors}
            extra={<span style={{ fontSize: 11, color: colors.muted }}>
              {fofLayer ? `${fofLayer.summary.n_reports.toLocaleString()} 份报告 · `
                : ""}定期报告「基金投资明细」
            </span>}>
            {!fofLayer || fofLayer.summary.n_reports === 0 ? (
              <div style={{ fontSize: 12.5, color: colors.muted, lineHeight: 1.9 }}>
                基金层数据尚未生成。运行
                <code style={{ margin: "0 4px" }}>fetch_fof_reports.py</code>
                抓取并解析定期报告，再用
                <code style={{ margin: "0 4px" }}>audit_fof_holdings.py --write</code>
                生成两层穿透结果。
              </div>
            ) : (
              <>
                <div style={{ background: mode === "dark" ? "#1b2b22" : "#f0faf4",
                  border: `1px solid ${colors.down}`, borderRadius: 8,
                  padding: "10px 14px", marginBottom: 14, fontSize: 12.5, lineHeight: 1.8 }}>
                  <b>这张表是免费的，而且比买数据更可靠。</b>
                  Tushare <code>fund_portfolio</code> 与天天基金 F10 <code>jjcc</code> 对 FOF 都只返回股票，
                  但定期报告里的「基金投资明细」是法定披露：中报/年报 §7.12.2 给<b>全部</b>被投基金，
                  季报 §6.1 给前十大。管道为「公告清单 → PDF 直链 → 章节定位 → 表格+文本双通道解析 → 两层穿透」，
                  已覆盖 {fofLayer.summary.n_fof} 只 FOF、
                  {fofLayer.summary.n_holdings.toLocaleString()} 行明细，
                  解析成功率 {((fofLayer.summary.success_rate ?? 0) * 100).toFixed(1)}%，
                  全部字段来自原始披露，<b>没有任何估算</b>。
                </div>
                <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(165px,1fr))",
                  gap: 10, marginBottom: 14 }}>
                  <KpiCard label="基金层覆盖（中位）" value={`${fofLayer.summary.w_total_med ?? "—"}%`}
                    sub="被投基金占净值比" colors={colors} tone="accent" />
                  <KpiCard label="可穿透部分（中位）" value={`${fofLayer.summary.w_pool_med ?? "—"}%`}
                    sub="底层有持仓数据的部分" colors={colors} />
                  <KpiCard label="覆盖 FOF 数" value={`${fofLayer.summary.n_fof}`}
                    sub={`最新期 ${fofLayer.summary.n_funds_latest} 只`} colors={colors} />
                  <KpiCard label="明细行数" value={fofLayer.summary.n_holdings.toLocaleString()}
                    sub={`最新期 ${fofLayer.summary.as_of ? fmtDate(fofLayer.summary.as_of) : "—"}`}
                    colors={colors} />
                </div>
                <div style={{ fontSize: 12.5, fontWeight: 600, marginBottom: 8 }}>
                  基金层覆盖最高的 12 只（各自最新报告期）
                </div>
                <div style={{ overflowX: "auto", border: `1px solid ${colors.border}`, borderRadius: 8 }}>
                  <table style={{ width: "100%", borderCollapse: "collapse" }}>
                    <thead><tr>
                      <th style={th}>FOF</th><th style={th}>报告期</th><th style={th}>被投基金数</th>
                      <th style={th}>基金层</th><th style={th}>可穿透</th>
                      <th style={th}>穿透权益</th><th style={th}>直接权益</th><th style={th}>合计</th>
                    </tr></thead>
                    <tbody>{fofLayer.funds.slice(0, 12).map((r) => (
                      <tr key={r.base_code + r.end_date}>
                        <td style={td}>
                          <span style={{ fontWeight: 600 }}>{r.name || r.base_code}</span>
                          <span style={{ color: colors.muted, marginLeft: 6, fontSize: 11 }}>{r.base_code}</span>
                        </td>
                        <td style={{ ...td, fontVariantNumeric: "tabular-nums" }}>
                          {fmtDate(r.end_date)}
                          <span style={{ color: colors.muted, fontSize: 11, marginLeft: 4 }}>{r.report_type}</span></td>
                        <td style={{ ...td, fontVariantNumeric: "tabular-nums" }}>{r.n_funds}</td>
                        <td style={{ ...td, fontVariantNumeric: "tabular-nums", fontWeight: 600 }}>{r.w_total.toFixed(2)}%</td>
                        <td style={{ ...td, fontVariantNumeric: "tabular-nums" }}>{r.w_pool.toFixed(2)}%</td>
                        <td style={{ ...td, fontVariantNumeric: "tabular-nums", color: colors.accent }}>
                          {r.w_equity.toFixed(2)}%</td>
                        <td style={{ ...td, fontVariantNumeric: "tabular-nums" }}>
                          {r.a_share_decl == null ? "—" : `${r.a_share_decl.toFixed(2)}%`}</td>
                        <td style={{ ...td, fontVariantNumeric: "tabular-nums" }}>
                          {r.eq_total == null ? "—" : `${r.eq_total.toFixed(2)}%`}</td>
                      </tr>
                    ))}</tbody>
                  </table>
                </div>
                <div style={{ marginTop: 12, fontSize: 12, color: colors.muted, lineHeight: 1.85 }}>
                  <b>「可穿透」远小于「基金层」是设计选择，不是缺陷</b>：FOF 大部分仓位在债基与货基，
                  这两类已明确不覆盖（债基只披露前五大债券、货基不披露券种，买数据也买不全），
                  所以能一路算到股票的只有权益类被投基金。<br />
                  <b>「穿透权益」</b>= Σ(被投基金占净值比 × 该基金同报告期监管 A 股仓位)；
                  <b>「直接权益」</b>= FOF 自己披露的行业配置合计。两者相加是硬约束，
                  超过 100% 说明解析或取值有误。<br />
                  <b>解析状态</b>：<code>ok</code> 抽到明细；<code>no_code</code> 报告里有明细表但底层是
                  境外基金、没有 6 位代码（QDII-FOF，不在覆盖范围）；<code>empty</code> 才是真失败，会进重试队列。
                </div>
              </>
            )}
          </Card>
        </>
      )}
      <div style={{ height: 10 }} />
      <div style={{ fontSize: 11, color: colors.muted, textAlign: "right" }}>
        缓存时间 {ov.cached_at || "—"} · 行业映射：申万时点 {src.sw_point || 0} / 当前 {src.sw_current || 0} /
        细分兜底 {src.tushare_mapped || 0} / 港股单列 {src.hk || 0} / A股未映射 {src.unmapped_a ?? src.unmapped ?? 0}
      </div>
    </div>
  );
}
