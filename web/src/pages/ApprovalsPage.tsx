/**
 * 审批中心（智能体 · 第二个页签）：左列表、右详情、三个按钮。
 *
 * 为什么这页必须存在：状态机、令牌、两段式写工具早就跑通了，但批准只能
 * `python3 agent/actions_cli.py approve 3` —— 人不在终端就没法批，
 * 「提议 → 人批 → 带令牌执行」这条链就断在中间。
 *
 * 三条硬规矩（来自《智能体架构设计》第 07 节）：
 * · 令牌不出现在界面上。approvals_api 返回前就把 token 剥掉了，这里连字段都没有。
 * · 否决必须填理由（≥10 字），理由会进记忆 —— 不是走过场。
 * · 不做「一键全批」。批的是动作，不是列表。
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { approvalsApi, ApprovalRow } from "../api/client";
import { useTheme } from "../theme";

const STATUS_META: Record<string, { cn: string; color: string }> = {
  pending: { cn: "待人批", color: "#b9770e" },
  approved: { cn: "已通过", color: "#2e86de" },
  done: { cn: "已执行", color: "#1e8449" },
  rejected: { cn: "已否决", color: "#c0392b" },
  expired: { cn: "已过期", color: "#8b93a7" },
  failed: { cn: "执行失败", color: "#c0392b" },
};

const ACTION_CN: Record<string, string> = {
  create_paper_task: "建模拟盘",
  card_transition: "改研究卡状态",
};

const FILTERS = [
  { key: "", label: "全部" },
  { key: "pending", label: "待人批" },
  { key: "approved", label: "已通过" },
  { key: "done", label: "已执行" },
  { key: "rejected", label: "已否决" },
  { key: "expired", label: "已过期" },
];

/** 后端时间是 UTC（状态机用 utcnow），界面上必须转本地 —— 直接 slice 会差 8 小时。 */
function localTime(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso.slice(0, 16);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

export default function ApprovalsPage() {
  const { colors } = useTheme();
  const [rows, setRows] = useState<ApprovalRow[] | null>(null);
  const [filter, setFilter] = useState("");
  const [sel, setSel] = useState<number | null>(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [rejectFor, setRejectFor] = useState<number | null>(null);
  const [reason, setReason] = useState("");

  const load = useCallback(() => {
    setErr("");
    approvalsApi.list(filter, 100)
      .then((r) => {
        setRows(r.items);
        setSel((cur) => cur ?? (r.items[0]?.id ?? null));
      })
      .catch((e) => { setErr((e as Error).message); setRows([]); });
  }, [filter]);

  useEffect(() => { load(); }, [load]);

  const cur = useMemo(() => rows?.find((r) => r.id === sel) ?? null, [rows, sel]);
  const pending = (rows ?? []).filter((r) => r.status === "pending").length;

  const doApprove = async (id: number) => {
    setBusy(true); setErr("");
    try { await approvalsApi.approve(id); load(); }
    catch (e) { setErr((e as Error).message); }
    finally { setBusy(false); }
  };

  const doReject = async (id: number) => {
    if (reason.trim().length < 10) { setErr("否决要写清理由（至少 10 字），理由会进记忆"); return; }
    setBusy(true); setErr("");
    try {
      await approvalsApi.reject(id, reason.trim());
      setRejectFor(null); setReason(""); load();
    } catch (e) { setErr((e as Error).message); }
    finally { setBusy(false); }
  };

  const card: React.CSSProperties = {
    background: colors.card, border: `1px solid ${colors.border}`, borderRadius: 12,
  };

  return (
    <div>
      <div style={{ display: "flex", alignItems: "baseline", gap: 12, marginBottom: 4 }}>
        <span style={{ fontSize: 17, fontWeight: 700 }}>审批中心</span>
        <span style={{ fontSize: 12, color: colors.muted }}>
          智能体只能提议，写操作要人点过才签发一次性令牌
        </span>
        <span style={{ flex: 1 }} />
        {pending > 0 && (
          <span style={{
            fontSize: 12, color: "#b9770e", border: "1px solid #b9770e55",
            background: "#b9770e12", borderRadius: 6, padding: "2px 9px",
          }}>{pending} 条待人批</span>
        )}
        <button onClick={load} style={{
          fontSize: 12, color: colors.accent, background: "none",
          border: `1px solid ${colors.border}`, borderRadius: 6, padding: "3px 10px", cursor: "pointer",
        }}>刷新</button>
      </div>

      <div style={{ display: "flex", gap: 2, borderBottom: `1px solid ${colors.border}`, margin: "12px 0 14px" }}>
        {FILTERS.map((f) => (
          <button key={f.key} onClick={() => { setFilter(f.key); setSel(null); }} style={{
            border: 0, background: "transparent", padding: "7px 13px", fontSize: 13, cursor: "pointer",
            color: filter === f.key ? colors.accent : colors.muted,
            borderBottom: `2px solid ${filter === f.key ? colors.accent : "transparent"}`,
            marginBottom: -1, fontWeight: filter === f.key ? 700 : 400,
          }}>{f.label}</button>
        ))}
      </div>

      {err && (
        <div style={{
          marginBottom: 12, padding: "10px 12px", borderRadius: 8, fontSize: 12.5,
          background: `${colors.up}12`, border: `1px solid ${colors.up}44`, color: colors.up,
        }}>{err}</div>
      )}

      <div style={{ display: "grid", gridTemplateColumns: "minmax(240px, 320px) minmax(0, 1fr)", gap: 14 }}>
        {/* 左：列表 */}
        <div style={{ ...card, overflow: "hidden" }}>
          {rows == null && (
            <div style={{ padding: 24, color: colors.muted, fontSize: 13 }}>加载中…</div>
          )}
          {rows?.length === 0 && (
            <div style={{ padding: 26, color: colors.muted, fontSize: 12.5, lineHeight: 1.9 }}>
              还没有任何审批记录。
              <div style={{ marginTop: 6, fontSize: 11.5 }}>
                智能体发起写操作（建模拟盘 / 改卡状态）时，会先在这里留一条待人批。
              </div>
            </div>
          )}
          {rows?.map((r) => {
            const m = STATUS_META[r.status] ?? { cn: r.status, color: colors.muted };
            const on = r.id === sel;
            return (
              <div
                key={r.id}
                onClick={() => { setSel(r.id); setRejectFor(null); setReason(""); }}
                style={{
                  padding: "10px 12px", cursor: "pointer",
                  borderBottom: `1px solid ${colors.border}`,
                  borderLeft: `3px solid ${on ? colors.accent : "transparent"}`,
                  background: on ? `${colors.accent}0d` : "transparent",
                }}
              >
                <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <span style={{ color: colors.muted, fontSize: 11.5 }}>#{r.id}</span>
                  <span style={{ fontSize: 12.5, fontWeight: 600 }}>
                    {ACTION_CN[r.action] ?? r.action}
                  </span>
                  <span style={{ flex: 1 }} />
                  <span style={{
                    fontSize: 10.5, color: m.color, border: `1px solid ${m.color}55`,
                    background: `${m.color}10`, borderRadius: 4, padding: "1px 5px",
                  }}>{m.cn}</span>
                </div>
                <div style={{ fontSize: 11.5, color: colors.muted, marginTop: 3 }}>
                  {localTime(r.created_at)} · {r.proposed_by || "agent"}
                </div>
              </div>
            );
          })}
        </div>

        {/* 右：详情 */}
        <div style={card}>
          {!cur ? (
            <div style={{ padding: 30, color: colors.muted, fontSize: 13 }}>选一条看详情</div>
          ) : (
            <div style={{ padding: 16 }}>
              <div style={{ display: "flex", alignItems: "baseline", gap: 10 }}>
                <span style={{ fontSize: 15, fontWeight: 700 }}>
                  {ACTION_CN[cur.action] ?? cur.action}
                </span>
                <span style={{ fontSize: 12, color: colors.muted }}>#{cur.id}</span>
                <span style={{ flex: 1 }} />
                <span style={{
                  fontSize: 11.5,
                  color: (STATUS_META[cur.status] ?? { color: colors.muted }).color,
                  fontWeight: 600,
                }}>{(STATUS_META[cur.status] ?? { cn: cur.status }).cn}</span>
              </div>

              {/* 提议理由 */}
              <div style={{ marginTop: 12 }}>
                <div style={{ fontSize: 11.5, color: colors.muted, marginBottom: 4 }}>为什么提议</div>
                <div style={{ fontSize: 12.5, lineHeight: 1.85 }}>{cur.reason || "（未填）"}</div>
              </div>

              {/* 要执行什么 */}
              <div style={{ marginTop: 12 }}>
                <div style={{ fontSize: 11.5, color: colors.muted, marginBottom: 4 }}>要执行的动作</div>
                <pre style={{
                  margin: 0, padding: "10px 12px", borderRadius: 8, fontSize: 12,
                  background: colors.bg, border: `1px solid ${colors.border}`,
                  overflow: "auto", maxHeight: 220, lineHeight: 1.7,
                }}>{JSON.stringify(cur.payload, null, 2)}</pre>
              </div>

              {/* 时间线 */}
              <div style={{
                marginTop: 12, display: "grid", gridTemplateColumns: "repeat(3,1fr)", gap: 10,
                fontSize: 11.5, color: colors.muted,
              }}>
                <div>提议 · {localTime(cur.created_at)}</div>
                <div>决策 · {localTime(cur.decided_at)}{cur.decided_by ? `（${cur.decided_by}）` : ""}</div>
                <div>执行 · {localTime(cur.executed_at)}</div>
              </div>

              {cur.status === "pending" && (
                <div style={{ fontSize: 11.5, color: colors.muted, marginTop: 6 }}>
                  超时未批会在 {localTime(cur.expires_at)} 自动过期
                </div>
              )}

              {cur.note && (
                <div style={{ marginTop: 10, fontSize: 12, color: colors.muted }}>备注：{cur.note}</div>
              )}

              {/* 三个按钮 */}
              {cur.status === "pending" && rejectFor !== cur.id && (
                <div style={{ display: "flex", gap: 8, marginTop: 16 }}>
                  <button
                    onClick={() => doApprove(cur.id)}
                    disabled={busy}
                    style={{
                      padding: "8px 20px", borderRadius: 8, border: 0, fontSize: 12.5, fontWeight: 600,
                      background: busy ? colors.tableStripe : "#1e8449", color: "#fff",
                      cursor: busy ? "wait" : "pointer",
                    }}
                  >通过并签发令牌</button>
                  <button
                    onClick={() => { setRejectFor(cur.id); setReason(""); }}
                    disabled={busy}
                    style={{
                      padding: "8px 20px", borderRadius: 8, fontSize: 12.5,
                      border: `1px solid ${colors.up}66`, background: "transparent", color: colors.up,
                      cursor: "pointer",
                    }}
                  >否决</button>
                  <span style={{ flex: 1 }} />
                  <span style={{ fontSize: 11.5, color: colors.muted, alignSelf: "center" }}>
                    通过只发令牌，动作由智能体自己取用执行
                  </span>
                </div>
              )}

              {rejectFor === cur.id && (
                <div style={{ marginTop: 16 }}>
                  <div style={{ fontSize: 11.5, color: colors.muted, marginBottom: 5 }}>
                    否决理由（≥10 字，会进记忆，下次它会避开同类提议）
                  </div>
                  <textarea
                    value={reason}
                    onChange={(e) => setReason(e.target.value)}
                    rows={3}
                    style={{
                      width: "100%", boxSizing: "border-box", padding: "8px 10px", fontSize: 12.5,
                      borderRadius: 8, border: `1px solid ${colors.border}`,
                      background: colors.bg, color: colors.text, resize: "vertical",
                    }}
                  />
                  <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
                    <button
                      onClick={() => doReject(cur.id)}
                      disabled={busy || reason.trim().length < 10}
                      style={{
                        padding: "7px 16px", borderRadius: 8, border: 0, fontSize: 12.5,
                        background: busy || reason.trim().length < 10 ? colors.tableStripe : colors.up,
                        color: busy || reason.trim().length < 10 ? colors.muted : "#fff",
                        cursor: busy || reason.trim().length < 10 ? "not-allowed" : "pointer",
                      }}
                    >确认否决</button>
                    <button
                      onClick={() => { setRejectFor(null); setReason(""); }}
                      style={{
                        padding: "7px 16px", borderRadius: 8, fontSize: 12.5,
                        border: `1px solid ${colors.border}`, background: "transparent",
                        color: colors.text, cursor: "pointer",
                      }}
                    >取消</button>
                  </div>
                </div>
              )}

              {cur.status !== "pending" && (
                <div style={{
                  marginTop: 16, padding: "10px 12px", borderRadius: 8, fontSize: 12,
                  background: colors.bg, border: `1px solid ${colors.border}`, color: colors.muted,
                }}>
                  {cur.status === "approved" && "已通过，等智能体取令牌执行（一次性，取走即标记 done）。"}
                  {cur.status === "done" && "已执行完毕。令牌已作废，不可重放。"}
                  {cur.status === "rejected" && "已否决。这条动作不会再执行。"}
                  {cur.status === "expired" && "超过有效期自动过期，未执行。"}
                  {cur.status === "failed" && "执行时报错。可以重放，但要重新走一次审批。"}
                </div>
              )}
            </div>
          )}
        </div>
      </div>

      <div style={{ marginTop: 12, fontSize: 11.5, color: colors.muted, lineHeight: 1.9 }}>
        <b>怎么读这一页：</b>智能体只能「提议」，写操作在这里等一个人点。
        通过后签发的是<b>一次性、绑动作、30 分钟过期</b>的令牌，而且
        <b>令牌不进工具入参、不出现在这个界面上</b> —— 否则被注入的文本就能把它骗走。
        否决必须写理由，理由会进记忆，它下次会避开同类提议。
      </div>
    </div>
  );
}
