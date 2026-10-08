/**
 * 自动研究（数据中心 · Tab）：自动挖矿 + 自动复盘的状态总览、开关配置、运行记录与报告查看。
 * 定位：用户「只看结果」——这页回答三个问题：它昨晚干了什么？下次什么时候跑？我要不要关掉它？
 */
import { useCallback, useEffect, useState } from "react";
import { Badge, Btn, Card, PageHeader } from "../components/ui";
import { autoApi, AutoRun, AutoStatus } from "../api/client";
import { useTheme } from "../theme";

const WEEK_CN = ["一", "二", "三", "四", "五", "六", "日"];

function RunRow({ r, onView }: { r: AutoRun; onView: (id: number) => void }) {
  const { colors } = useTheme();
  const s = r.summary || {};
  const brief = r.job === "mining"
    ? (s.evaluated !== undefined
        ? `评估 ${s.evaluated} 候选 → 精英 ${s.elites} · 启用 ${s.enabled?.length ?? 0} · 候选 ${s.candidates?.length ?? 0} · 跳过 ${s.skipped?.length ?? 0}`
        : "—")
    : (s.date
        ? `最强 ${s.best ?? "—"} / 最弱 ${s.worst ?? "—"} · 走弱 ${s.weakening?.length ?? 0} 个 · 建议 ${s.suggestions?.length ?? 0} 条`
        : "—");
  return (
    <tr>
      <td style={{ padding: "8px 10px", borderBottom: `1px solid ${colors.border}`, fontSize: 12.5 }}>
        {r.started_at?.slice(5, 16)}
      </td>
      <td style={{ padding: "8px 10px", borderBottom: `1px solid ${colors.border}` }}>
        <Badge text={r.job === "mining" ? "挖矿" : "复盘"} color={r.job === "mining" ? "#8e44ad" : "#2471a3"} soft />
        <span style={{ color: colors.muted, fontSize: 11, marginLeft: 6 }}>
          {r.trigger === "scheduler" ? "定时" : "手动"}
        </span>
      </td>
      <td style={{ padding: "8px 10px", borderBottom: `1px solid ${colors.border}` }}>
        <Badge text={r.status === "SUCCESS" ? "成功" : r.status === "RUNNING" ? "运行中" : "失败"}
          color={r.status === "SUCCESS" ? "#1e8449" : r.status === "RUNNING" ? "#b9770e" : "#c0392b"} soft />
      </td>
      <td style={{ padding: "8px 10px", borderBottom: `1px solid ${colors.border}`, fontSize: 12.5, color: colors.muted }}>
        {r.error ? <span style={{ color: colors.up }}>{r.error}</span> : brief}
      </td>
      <td style={{ padding: "8px 10px", borderBottom: `1px solid ${colors.border}` }}>
        {r.report_path && <Btn small kind="ghost" onClick={() => onView(r.id)}>看报告</Btn>}
      </td>
    </tr>
  );
}

export default function AutoResearchPage() {
  const { colors } = useTheme();
  const [st, setSt] = useState<AutoStatus | null>(null);
  const [runs, setRuns] = useState<AutoRun[]>([]);
  const [report, setReport] = useState<{ id: number; html: string } | null>(null);
  const [busy, setBusy] = useState("");
  const [toast, setToast] = useState("");

  const load = useCallback(() => {
    autoApi.status().then(setSt).catch(() => {});
    autoApi.runs(20).then(setRuns).catch(() => {});
  }, []);
  useEffect(load, [load]);

  // 有 RUNNING 任务时轮询
  useEffect(() => {
    if (!runs.some((r) => r.status === "RUNNING")) return;
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, [runs, load]);

  const showToast = (m: string) => { setToast(m); setTimeout(() => setToast(""), 5000); };
  const trigger = (job: "mining" | "review") => {
    setBusy(job);
    autoApi.runJob(job)
      .then(() => { showToast(job === "mining" ? "挖矿已启动，约 1~3 分钟出结果" : "复盘已启动，约 10 秒出报告"); setTimeout(load, 2000); })
      .catch((e) => showToast(`触发失败：${(e as Error).message}`))
      .finally(() => setBusy(""));
  };
  const toggle = (key: keyof AutoStatus["config"], v: boolean) => {
    autoApi.saveConfig({ [key]: v } as Partial<AutoStatus["config"]>)
      .then((r) => { setSt((p) => (p ? { ...p, config: r.config } : p)); })
      .catch((e) => showToast(`保存失败：${(e as Error).message}`));
  };
  const viewReport = (id: number) => {
    autoApi.runDetail(id).then((d) => {
      if (d.report_html) setReport({ id, html: d.report_html });
      else showToast("报告文件不存在");
    }).catch((e) => showToast((e as Error).message));
  };

  const cfg = st?.config;
  const lm = st?.last_mining;
  const lr = st?.last_review;

  const Toggle = ({ label, k, hint }: { label: string; k: keyof AutoStatus["config"]; hint: string }) => (
    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", padding: "8px 0", borderBottom: `1px solid ${colors.border}` }}>
      <div>
        <div style={{ fontSize: 13 }}>{label}</div>
        <div style={{ fontSize: 11.5, color: colors.muted }}>{hint}</div>
      </div>
      <button onClick={() => cfg && toggle(k, !cfg[k])} style={{
        width: 44, height: 24, borderRadius: 12, border: 0, cursor: "pointer",
        background: cfg?.[k] ? "#1e8449" : "#3a4258",
        position: "relative", transition: "background .2s",
      }}>
        <span style={{
          position: "absolute", top: 3, left: cfg?.[k] ? 23 : 3, width: 18, height: 18,
          borderRadius: 9, background: "#fff", transition: "left .2s",
        }} />
      </button>
    </div>
  );

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      <PageHeader title="自动研究" desc="平台自主完成「挖因子 → 入库生产 → 复盘迭代」闭环，你只看结果"
        actions={
          <div style={{ display: "flex", gap: 8 }}>
            <Btn small kind="ghost" disabled={busy === "mining"} onClick={() => trigger("mining")}>立即挖矿</Btn>
            <Btn small kind="ghost" disabled={busy === "review"} onClick={() => trigger("review")}>立即复盘</Btn>
          </div>
        } />
      {toast && <div style={{ fontSize: 12.5, color: colors.accent }}>{toast}</div>}

      {/* —— 状态卡 —— */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(4,1fr)", gap: 12 }}>
        <Card colors={colors} pad={14}>
          <div style={{ fontSize: 11.5, color: colors.muted }}>每日挖矿</div>
          <div style={{ fontSize: 15, fontWeight: 700, marginTop: 4 }}>
            {cfg?.mining_enabled ? <span style={{ color: colors.down }}>● 开启</span> : <span style={{ color: colors.muted }}>○ 关闭</span>}
          </div>
          <div style={{ fontSize: 11.5, color: colors.muted, marginTop: 4 }}>{st?.mining_next}</div>
        </Card>
        <Card colors={colors} pad={14}>
          <div style={{ fontSize: 11.5, color: colors.muted }}>每周复盘</div>
          <div style={{ fontSize: 15, fontWeight: 700, marginTop: 4 }}>
            {cfg?.review_enabled ? <span style={{ color: colors.down }}>● 开启</span> : <span style={{ color: colors.muted }}>○ 关闭</span>}
          </div>
          <div style={{ fontSize: 11.5, color: colors.muted, marginTop: 4 }}>{st?.review_next}</div>
        </Card>
        <Card colors={colors} pad={14}>
          <div style={{ fontSize: 11.5, color: colors.muted }}>最近一次挖矿</div>
          <div style={{ fontSize: 13, fontWeight: 600, marginTop: 4 }}>
            {lm ? (lm.status === "SUCCESS"
              ? `${lm.summary.enabled?.length ?? 0} 启用 / ${lm.summary.candidates?.length ?? 0} 候选`
              : lm.status === "RUNNING" ? "运行中…" : "失败") : "未运行"}
          </div>
          <div style={{ fontSize: 11.5, color: colors.muted, marginTop: 4 }}>{lm?.started_at?.slice(0, 16) ?? ""}</div>
        </Card>
        <Card colors={colors} pad={14}>
          <div style={{ fontSize: 11.5, color: colors.muted }}>最近一次复盘</div>
          <div style={{ fontSize: 13, fontWeight: 600, marginTop: 4 }}>
            {lr ? (lr.status === "SUCCESS" ? `${lr.summary.date}` : lr.status) : "未运行"}
          </div>
          <div style={{ fontSize: 11.5, color: colors.muted, marginTop: 4 }}>
            {lr?.status === "SUCCESS" ? `走弱信号 ${lr.summary.weakening?.length ?? 0} 个` : ""}
          </div>
        </Card>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 340px", gap: 14, alignItems: "start" }}>
        {/* —— 运行记录 —— */}
        <Card colors={colors} pad={8} title="运行记录" extra={
          <span style={{ fontSize: 11.5, color: colors.muted }}>共 {st?.total_runs ?? 0} 次</span>}>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
            <thead><tr>
              {["时间", "任务", "状态", "摘要", ""].map((h) => (
                <th key={h} style={{ color: colors.muted, fontWeight: 500, textAlign: "left", padding: "7px 10px", borderBottom: `1px solid ${colors.border}`, fontSize: 12 }}>{h}</th>
              ))}
            </tr></thead>
            <tbody>
              {runs.map((r) => <RunRow key={r.id} r={r} onView={viewReport} />)}
              {!runs.length && <tr><td colSpan={5} style={{ padding: 18, textAlign: "center", color: colors.muted }}>暂无记录</td></tr>}
            </tbody>
          </table>
        </Card>

        {/* —— 配置 —— */}
        <Card colors={colors} title="自动化开关" pad={14}>
          <Toggle label="每日自动挖矿" k="mining_enabled" hint={`${cfg?.mining_time ?? ""} · GP ${cfg?.mining_pop}×${cfg?.mining_gens}`} />
          <Toggle label="优秀因子自动启用" k="auto_enable_excellent" hint="启用即进入每日生产（可逆）" />
          <Toggle label="每周自动复盘" k="review_enabled" hint={`周${WEEK_CN[cfg?.review_weekday ?? 5]} ${cfg?.review_time ?? ""}`} />
          <Toggle label="复盘邮件推送" k="email_review" hint="复用信号报告的邮件通道" />
          <div style={{ fontSize: 11.5, color: colors.muted, marginTop: 10, lineHeight: 1.6 }}>
            安全边界：自动动作仅限因子登记/启用；策略降级与参数调整只写进复盘建议，不自动执行。
          </div>
        </Card>

        {/* —— 研究纪律（源自《量化交易：算法、分析、数据、模型和优化》）—— */}
        <Card colors={colors} title="研究纪律" pad={14}>
          {[
            "数据先行：结论前先问数据是否干净、新鲜、无未来函数",
            "样本外才算数：单窗口亮眼结果只算候选，滚动窗口复核",
            "挖得多门槛就高：批量挖掘的好结果默认含运气成分",
            "扣完成本还活着才是 α：换手、延迟、冲击成本之后的超额",
            "错了要留痕：升降档与修复都记入决策文档，可回溯",
          ].map((t, i) => (
            <div key={i} style={{ display: "flex", gap: 8, fontSize: 12.3, lineHeight: 1.55, marginBottom: 7 }}>
              <span style={{ color: colors.down, flexShrink: 0 }}>✓</span>
              <span>{t}</span>
            </div>
          ))}
          <a href="/docs/methodology.html" target="_blank" rel="noreferrer"
            style={{ fontSize: 11.8, color: colors.accent, textDecoration: "none" }}>
            📚 方法论出处与落地对照 →
          </a>
        </Card>
      </div>

      {/* —— 报告查看 —— */}
      {report && (
        <Card colors={colors} pad={0} title={`复盘报告 #${report.id}`} extra={
          <Btn small kind="ghost" onClick={() => setReport(null)}>关闭 ×</Btn>}>
          <iframe title="report" srcDoc={report.html}
            style={{ width: "100%", height: 560, border: 0, borderRadius: "0 0 12px 12px", background: "#fff" }} />
        </Card>
      )}
    </div>
  );
}
