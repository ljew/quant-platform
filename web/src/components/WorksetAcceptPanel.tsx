/** W48-2 工作集短名单验收：挂在单次回测页，不依赖主区 result，不写主区净值。 */
import { useCallback, useEffect, useRef, useState } from "react";
import { Badge, Btn, Card } from "./ui";
import EvalBadges from "./EvalBadges";
import { ThemeColors } from "../theme";
import {
  api,
  WorksetAcceptError,
  WorksetAcceptGet,
  WorksetAcceptMember,
  WorksetAcceptRun,
} from "../api/client";

const SLOGAN =
  "工作集短名单验收：按与回测相同的默认成本（万 2.5+万 1）和滚动 OOS 口径，逐个验收工作集成员。通过要看样本外净值和成本翻倍后是否还站得住，不是再看一遍挖掘报告的 IC。";

function goFactorLabWorkset() {
  window.dispatchEvent(new CustomEvent("quant-nav", { detail: { tab: "factorlab" } }));
  setTimeout(() => document.getElementById("factor-workset")
    ?.scrollIntoView({ behavior: "smooth", block: "start" }), 160);
}

function verdictColor(v: string | null | undefined, stale: boolean, colors: ThemeColors): string {
  if (stale) return "#868e96";
  if (v === "通过") return "#2f9e44";
  if (v === "拒绝") return colors.down || "#d92c2c";
  if (v === "未齐" || v === "样本不足") return "#c8860d";
  return "#868e96";
}

function rejectLabel(r: string): string {
  if (r === "wf_overfit") return "过拟合";
  if (r === "wf_fail") return "未通过";
  if (r === "fragile") return "成本翻倍后脆弱";
  return r;
}

function fmtTime(s?: string | null): string {
  if (!s) return "—";
  return s.replace("T", " ").slice(0, 19);
}

export default function WorksetAcceptPanel({
  start, end, colors,
}: {
  start: string;
  end: string;
  colors: ThemeColors;
}) {
  const [payload, setPayload] = useState<WorksetAcceptGet | null>(null);
  const [err, setErr] = useState("");
  const [running, setRunning] = useState(false);
  const pollRef = useRef<number | null>(null);

  const stopPoll = () => {
    if (pollRef.current != null) {
      window.clearInterval(pollRef.current);
      pollRef.current = null;
    }
  };

  const load = useCallback(async (jobId?: string) => {
    const q = new URLSearchParams();
    if (start) q.set("start", start);
    if (end) q.set("end", end);
    if (jobId) q.set("job_id", jobId);
    const qs = q.toString();
    const got = await api.worksetAcceptGet(qs);
    setPayload(got);
    const st = got.run?.status;
    if (st === "running" || st === "pending") {
      setRunning(true);
      return "running";
    }
    setRunning(false);
    return st || "idle";
  }, [start, end]);

  useEffect(() => {
    load().catch((e) => setErr(`读取验收状态失败: ${(e as Error).message}`));
    return () => stopPoll();
  }, [load]);

  const beginPoll = (jobId?: string) => {
    stopPoll();
    pollRef.current = window.setInterval(() => {
      load(jobId).then((st) => {
        if (st !== "running" && st !== "pending") stopPoll();
      }).catch(() => { /* 下轮再试 */ });
    }, 2000);
  };

  const onStart = async () => {
    setErr("");
    setRunning(true);
    try {
      const r = await api.worksetAcceptStart({ start, end, adj: "qfq", initial_cash: 1_000_000 });
      setPayload((prev) => ({
        ok: true,
        workset_available: prev?.workset_available ?? true,
        workset: prev?.workset ?? null,
        run: {
          ...(prev?.run || {}),
          run_id: r.run_id,
          job_id: r.job_id,
          status: r.status,
          snapshot: r.snapshot,
          start: r.start,
          end: r.end,
          rules_version: r.rules_version,
          stale: false,
          members: [],
          message: "验收中（0/n）… 中证800 组合回测可能需要数分钟",
        } as WorksetAcceptRun,
      }));
      if (r.status === "running" || r.status === "pending") {
        beginPoll(r.job_id);
      } else {
        await load(r.job_id);
        setRunning(false);
      }
    } catch (e) {
      const we = e as WorksetAcceptError;
      if (we.error_code === "empty_workset") {
        setErr("工作集为空，请先到因子实验室申请入集。");
      } else if (we.error_code === "workset_unavailable") {
        setErr("工作集服务不可用（需要 W48-1）。");
      } else if (we.error_code === "job_running" || we.status === 409) {
        setErr("已有进行中的验收任务，请等待当前任务结束。本页不提供取消。");
        beginPoll(we.job_id);
      } else if (we.error_code === "rules_param_forbidden") {
        setErr(we.message || "不能改写死的验收规则。");
        setRunning(false);
      } else if (we.error_code === "invalid_range") {
        setErr(we.message || "区间无效。");
        setRunning(false);
      } else {
        setErr(we.message || (e as Error).message);
        setRunning(false);
      }
    }
  };

  const available = payload?.workset_available !== false;
  const workset = payload?.workset;
  const n = workset?.n ?? 0;
  const cap = workset?.cap ?? 5;
  const members = workset?.members ?? [];
  const run = payload?.run ?? null;
  const stale = Boolean(run?.stale);
  const collection = run?.collection;
  const runMembers: WorksetAcceptMember[] = run?.members ?? [];
  const empty = n === 0;
  const busy = running || run?.status === "running" || run?.status === "pending";
  const progressMsg = run?.message || (busy
    ? `验收中（${run?.members_done ?? 0}/${run?.members_total ?? n}）… 中证800 组合回测可能需要数分钟`
    : "");

  return (
    <Card
      id="workset-accept"
      title="工作集短名单验收"
      colors={colors}
      extra={<span style={{ fontSize: 11.5, color: colors.muted }}>与上方策略回测不是同一对象</span>}
    >
      <div style={{ fontSize: 12.5, color: colors.text, lineHeight: 1.85, marginBottom: 10 }}>
        {SLOGAN}
      </div>
      <div style={{ fontSize: 12, color: colors.muted, lineHeight: 1.8, marginBottom: 12 }}>
        口径（本切片写死，不可调）：股票池 <b style={{ color: colors.text }}>中证800</b>（sh000906）；
        规则 <b style={{ color: colors.text }}>多头等权 Top 50 / 21 日调仓 / 单只≤5%</b>，无空头、无行业/市值中性；
        成本 <b style={{ color: colors.text }}>万 2.5+万 1</b>；
        WF <b style={{ color: colors.text }}>3 窗 expanding / sharpe</b>。
        区间与上方控件相同（{start}～{end}）；不够滚动窗下限则该成员为样本不足。
        验收不改变多重检验计数，不自动启用或移出因子。
      </div>

      {!available && (
        <div style={{ fontSize: 12.5, color: colors.up, marginBottom: 10 }}>
          工作集服务不可用（需要 W48-1）。
        </div>
      )}

      {available && empty && (
        <div style={{ fontSize: 12.5, color: colors.muted, lineHeight: 1.8, marginBottom: 10 }}>
          工作集为空。请先到{" "}
          <a
            href="#factorlab"
            onClick={(e) => { e.preventDefault(); goFactorLabWorkset(); }}
            style={{ color: colors.accent }}
          >
            因子实验室
          </a>
          {" "}申请入集（将滚到工作集卡片）。
        </div>
      )}

      {available && n > 0 && (
        <div style={{ marginBottom: 12 }}>
          <div style={{ fontSize: 12.5, marginBottom: 6 }}>
            当前工作集 <b>{n}/{cap}</b>
            {n < 3 && <span style={{ color: colors.muted, marginLeft: 8 }}>尚未达到目标下限 3</span>}
          </div>
          <div style={{ overflowX: "auto" }}>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12.5 }}>
              <thead>
                <tr style={{ color: colors.muted, textAlign: "left" }}>
                  <th style={{ padding: "6px 8px", fontWeight: 500 }}>名称</th>
                  <th style={{ padding: "6px 8px", fontWeight: 500 }}>表达式</th>
                  <th style={{ padding: "6px 8px", fontWeight: 500 }}>主路评级</th>
                  <th style={{ padding: "6px 8px", fontWeight: 500 }}>主路 t</th>
                  <th style={{ padding: "6px 8px", fontWeight: 500 }}>入集时间</th>
                </tr>
              </thead>
              <tbody>
                {members.map((m) => (
                  <tr key={m.registry_id} style={{ borderTop: `1px solid ${colors.border}` }}>
                    <td style={{ padding: "7px 8px" }}>{m.name || `#${m.registry_id}`}</td>
                    <td style={{ padding: "7px 8px" }}>
                      <code style={{ fontSize: 11 }}>{m.expr_excerpt || m.expr || "—"}</code>
                    </td>
                    <td style={{ padding: "7px 8px" }}>{m.rating ?? "—"}</td>
                    <td style={{ padding: "7px 8px", fontVariantNumeric: "tabular-nums" }}>
                      {m.t_stat == null ? "—" : Number(m.t_stat).toFixed(2)}
                    </td>
                    <td style={{ padding: "7px 8px", color: colors.muted, fontSize: 12 }}>
                      {fmtTime(m.added_at)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap", marginBottom: 8 }}>
        <Btn onClick={onStart} disabled={busy || empty || !available}>
          {busy ? "验收中…" : "按默认成本验收"}
        </Btn>
        {busy && (
          <span style={{ fontSize: 12, color: colors.muted }}>{progressMsg}</span>
        )}
      </div>
      {err && (
        <div style={{ fontSize: 12.5, color: colors.down || "#d92c2c", marginBottom: 8, lineHeight: 1.7 }}>
          {err}
        </div>
      )}

      {run && run.status === "error" && (
        <div style={{ fontSize: 12.5, color: colors.down, marginBottom: 8 }}>
          验收任务失败：{run.error_message || "未知错误"}
        </div>
      )}

      {run && (run.status === "done" || (runMembers.length > 0 && run.status === "running")) && (
        <div style={{ marginTop: 10, opacity: stale ? 0.6 : 1 }}>
          {stale && (
            <div style={{ fontSize: 12.5, color: "#c8860d", marginBottom: 8, fontWeight: 600 }}>
              {run.stale_reason || "结论已过期，请重跑"}
            </div>
          )}
          {collection && (
            <div style={{ display: "flex", gap: 10, alignItems: "center", marginBottom: 10, flexWrap: "wrap" }}>
              <span style={{ fontSize: 12.5, color: colors.muted }}>集合</span>
              <Badge
                text={stale ? `已过期·${collection.verdict}` : collection.verdict}
                color={verdictColor(collection.verdict, stale, colors)}
                soft={stale || collection.verdict === "未齐" || collection.verdict === "无法判定"}
              />
              <span style={{ fontSize: 12, color: colors.muted }}>
                n={collection.n} · 通过 {collection.n_pass} · 拒绝 {collection.n_reject}
                {" "}· 样本不足 {collection.n_insufficient} · 无法验收 {collection.n_unevaluable}
              </span>
              {collection.verdict === "未齐" && (
                <span style={{ fontSize: 12, color: "#c8860d" }}>
                  目标下限为 3，当前 n={collection.n}。成员结论仍可见。
                </span>
              )}
            </div>
          )}

          {runMembers.map((m) => (
            <div
              key={m.registry_id}
              style={{
                borderTop: `1px solid ${colors.border}`,
                padding: "10px 0",
              }}
            >
              <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", marginBottom: 4 }}>
                <b style={{ fontSize: 13 }}>{m.name || `#${m.registry_id}`}</b>
                <Badge
                  text={m.verdict}
                  color={verdictColor(m.verdict, stale, colors)}
                  soft={stale || m.verdict !== "通过"}
                />
                {m.n_held != null && (
                  <span style={{ fontSize: 11.5, color: colors.muted }}>持仓 {m.n_held} 只</span>
                )}
                {m.n_valid != null && (
                  <span style={{ fontSize: 11.5, color: colors.muted }}>有效值 {m.n_valid}</span>
                )}
              </div>
              <div style={{ fontSize: 12, color: colors.muted, lineHeight: 1.7 }}>
                {m.verdict === "拒绝" && (m.reject_reasons || []).length > 0 && (
                  <div>拒绝原因：{(m.reject_reasons || []).map(rejectLabel).join("、")}</div>
                )}
                {m.reason && m.verdict !== "通过" && <div>{m.reason}</div>}
                {m.wf?.final && (
                  <div>
                    WF final={m.wf.final}
                    {m.wf.agg ? ` · agg=${m.wf.agg}` : ""}
                    {m.wf.legacy ? ` · legacy=${m.wf.legacy}` : ""}
                    {m.wf.windows && m.wf.windows.length > 0 && (
                      <span>
                        {" "}· 窗级：{m.wf.windows.map((w) => `#${w.index} ${w.verdict}`).join("，")}
                      </span>
                    )}
                  </div>
                )}
                {m.cost && (
                  <div>
                    成本 fragile={m.cost.fragile ? "是" : "否"}
                    {m.cost.default_excess != null ? ` · ×1 超额=${Number(m.cost.default_excess).toFixed(4)}` : ""}
                    {m.cost.double_excess != null ? ` · ×2 超额=${Number(m.cost.double_excess).toFixed(4)}` : ""}
                    {m.cost.note ? ` · ${m.cost.note}` : ""}
                  </div>
                )}
              </div>
              {m.cost && (
                <div style={{ marginTop: 6 }}>
                  <EvalBadges
                    cost_profile={{
                      commission: 0.00025,
                      slippage: 0.0001,
                      source: "default",
                      label: m.cost.fragile ? "仅低摩擦成立" : undefined,
                    }}
                    oos={m.wf?.final ? { enabled: true, verdict: m.wf.final, oos_ratio: 0.3 } : null}
                    colors={colors}
                  />
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </Card>
  );
}
