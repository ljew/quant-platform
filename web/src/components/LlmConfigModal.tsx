import { useCallback, useEffect, useMemo, useState } from "react";

import { api, LlmProvider, LlmProviderList, LlmTestResult, LlmVendorPreset } from "../api/client";
import { ThemeColors } from "../theme";
import { Badge, Btn, inputStyle } from "./ui";

/**
 * 大模型通道配置弹窗。
 *
 * 目标：把「改 key 要动 .env 还得重启后端」这件事从命令行里拿掉 —— 配置落库、
 * 后端每次调用现读，因此这里保存完立即生效；「测试连接」会真实发一条最小请求，
 * 失败时原文回传，避免存进去一个错的配置却毫无察觉。
 *
 * 交互约定：点击遮罩**不**关闭（防止填了一半误触丢失），只能点关闭按钮或按 Esc。
 */

interface Props {
  open: boolean;
  colors: ThemeColors;
  onClose: () => void;
  /** 任一变更后回调，供父组件刷新 AI 卡片状态。 */
  onChanged: () => void;
}

interface Draft {
  id?: number;
  name: string;
  vendor: string;
  base_url: string;
  model: string;
  api_key: string;
  makeDefault: boolean;
}

const emptyDraft = (vendor = "deepseek", label = "", base = "", model = ""): Draft => ({
  name: label || "新通道",
  vendor,
  base_url: base,
  model,
  api_key: "",
  makeDefault: false,
});

const trunc = (s: string, n = 64) => (s.length > n ? s.slice(0, n) + "…" : s);

export default function LlmConfigModal({ open, colors, onClose, onChanged }: Props) {
  const [data, setData] = useState<LlmProviderList | null>(null);
  const [presets, setPresets] = useState<LlmVendorPreset[]>([]);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [test, setTest] = useState<LlmTestResult | null>(null);
  const [busy, setBusy] = useState("");
  const [err, setErr] = useState("");
  const [note, setNote] = useState("");

  const load = useCallback(async () => {
    try {
      const d = await api.llmProviders();
      setData(d);
    } catch (e) {
      setErr(String(e instanceof Error ? e.message : e));
    }
  }, []);

  useEffect(() => {
    if (!open) return;
    load();
    api.llmPresets().then((r) => setPresets(r.presets)).catch(() => {});
  }, [open, load]);

  // Esc 关闭（遮罩点击不关闭：避免填了一半被误触清掉）
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  const activePreset = useMemo(
    () => presets.find((p) => p.vendor === draft?.vendor),
    [presets, draft?.vendor]
  );

  if (!open) return null;

  const reset = () => {
    setDraft(null);
    setTest(null);
    setErr("");
    setNote("");
  };

  const startCreate = () => {
    const p = presets.find((x) => x.vendor === "deepseek") || presets[0];
    setDraft({
      ...emptyDraft(p?.vendor, p?.label, p?.base_url, p?.models?.[0] || ""),
      makeDefault: (data?.items.length || 0) === 0,
    });
    setTest(null);
    setErr("");
    setNote("");
  };

  const startEdit = (p: LlmProvider) => {
    setDraft({
      id: p.id,
      name: p.name,
      vendor: p.vendor,
      base_url: p.base_url,
      model: p.model,
      api_key: "",
      makeDefault: p.is_default,
    });
    setTest(null);
    setErr("");
    setNote("");
  };

  const pickVendor = (vendor: string) => {
    const p = presets.find((x) => x.vendor === vendor);
    setDraft((d) =>
      d
        ? {
            ...d,
            vendor,
            name: d.id ? d.name : p?.label || d.name,
            base_url: p?.base_url || d.base_url,
            model: p?.models?.[0] || d.model,
          }
        : d
    );
    setTest(null);
  };

  const doTest = async () => {
    if (!draft) return;
    setBusy("test");
    setErr("");
    setNote("");
    try {
      const r = await api.llmTestDraft({
        id: draft.id,
        base_url: draft.base_url,
        model: draft.model,
        api_key: draft.api_key,
      });
      setTest(r);
    } catch (e) {
      setErr(String(e instanceof Error ? e.message : e));
    } finally {
      setBusy("");
    }
  };

  const doSave = async () => {
    if (!draft) return;
    if (!draft.base_url.trim() || !draft.model.trim()) {
      setErr("接口地址与模型名不能为空");
      return;
    }
    setBusy("save");
    setErr("");
    try {
      const base = {
        name: draft.name,
        vendor: draft.vendor,
        base_url: draft.base_url,
        model: draft.model,
        make_default: draft.makeDefault,
      };
      let msg: string;
      if (draft.id) {
        // api_key 留空 ⇒ 字段被省略 ⇒ 后端保持原值（不回显明文，故不能要求原样提交）
        await api.llmUpdate(draft.id, draft.api_key ? { ...base, api_key: draft.api_key } : base);
        msg = "已保存";
      } else {
        await api.llmCreate({ ...base, api_key: draft.api_key });
        msg = "已新增通道";
      }
      // 注意顺序：reset() 内部会清空提示，故提示必须在 reset 之后再设
      reset();
      setNote(msg);
      await load();
      onChanged();
    } catch (e) {
      setErr(String(e instanceof Error ? e.message : e));
    } finally {
      setBusy("");
    }
  };

  const doSetDefault = async (id: number) => {
    setBusy(`def${id}`);
    try {
      await api.llmSetDefault(id);
      await load();
      onChanged();
      setNote("默认通道已切换");
    } catch (e) {
      setErr(String(e instanceof Error ? e.message : e));
    } finally {
      setBusy("");
    }
  };

  const doTestSaved = async (id: number) => {
    setBusy(`t${id}`);
    setErr("");
    try {
      const r = await api.llmTestSaved(id);
      setTest(r);
      if (!r.ok) setNote("");
      await load();
    } catch (e) {
      setErr(String(e instanceof Error ? e.message : e));
    } finally {
      setBusy("");
    }
  };

  const doDelete = async (p: LlmProvider) => {
    if (!window.confirm(`确定删除通道「${p.name}」？该操作不可撤销。`)) return;
    setBusy(`d${p.id}`);
    try {
      await api.llmDelete(p.id);
      await load();
      onChanged();
      setNote("已删除");
    } catch (e) {
      setErr(String(e instanceof Error ? e.message : e));
    } finally {
      setBusy("");
    }
  };

  const act = data?.active;
  const label = (t: string) => (
    <div style={{ fontSize: 12, color: colors.muted, marginBottom: 4 }}>{t}</div>
  );

  return (
    <div
      style={{
        position: "fixed", inset: 0, zIndex: 1000,
        background: "rgba(15,23,42,.42)",
        display: "flex", alignItems: "center", justifyContent: "center",
        padding: 20,
      }}
    >
      <div
        style={{
          width: 780, maxWidth: "94vw", maxHeight: "88vh", overflow: "auto",
          background: colors.card, borderRadius: 14,
          border: `1px solid ${colors.border}`,
          boxShadow: "0 24px 60px rgba(15,23,42,.28)",
        }}
      >
        {/* 头部 */}
        <div style={{
          display: "flex", alignItems: "center", justifyContent: "space-between",
          padding: "14px 18px", borderBottom: `1px solid ${colors.border}`,
          position: "sticky", top: 0, background: colors.card, zIndex: 2,
        }}>
          <div>
            <div style={{ fontSize: 15, fontWeight: 700 }}>大模型配置</div>
            <div style={{ fontSize: 12, color: colors.muted, marginTop: 3 }}>
              配置保存在平台内，保存后<b>立即生效</b>，无需重启后端
            </div>
          </div>
          <Btn kind="ghost" small onClick={onClose} title="关闭（Esc）">关闭</Btn>
        </div>

        <div style={{ padding: 18 }}>
          {/* 当前生效摘要 */}
          {act && (
            <div style={{
              padding: "10px 12px", borderRadius: 9, marginBottom: 14,
              border: `1px solid ${act.configured ? colors.accent + "55" : colors.border}`,
              background: act.configured ? colors.accent + "0f" : colors.tableStripe,
              fontSize: 12.5, lineHeight: 1.8,
            }}>
              {act.configured ? (
                <>
                  <span style={{ fontWeight: 600 }}>当前生效：{act.name}</span>
                  <span style={{ color: colors.muted }}>
                    {" "}· {act.model} · {act.key_masked}
                    {act.source === "env" ? "（来自环境变量 .env）" : ""}
                  </span>
                </>
              ) : (
                <span style={{ color: colors.muted }}>
                  尚未配置大模型 —— 因子挖掘页当前使用本地关键词「试用模式」。
                </span>
              )}
              {act.env_shadowed && (
                <div style={{ color: "#c8860d", marginTop: 2 }}>
                  提示：环境变量里也配置了 key，但已被页面配置覆盖（页面优先）。
                </div>
              )}
            </div>
          )}

          {note && (
            <div style={{ marginBottom: 12 }}>
              <Badge text={note} color={colors.down} soft />
            </div>
          )}
          {err && (
            <div style={{
              marginBottom: 12, padding: "8px 10px", borderRadius: 7,
              background: colors.up + "12", border: `1px solid ${colors.up}44`,
              color: colors.up, fontSize: 12.5, wordBreak: "break-all",
            }}>{err}</div>
          )}

          {/* 通道列表 */}
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 8 }}>
            <span style={{ fontSize: 13, fontWeight: 600 }}>
              已配置通道{data ? `（${data.items.length}）` : ""}
            </span>
            {!draft && <Btn small onClick={startCreate}>+ 新增通道</Btn>}
          </div>

          {data && data.items.length === 0 && !draft && (
            <div style={{
              padding: "18px 14px", borderRadius: 9, textAlign: "center",
              border: `1px dashed ${colors.border}`, color: colors.muted, fontSize: 12.5,
            }}>
              还没有配置任何通道。点「+ 新增通道」，选一家服务商、粘贴 API Key 即可。
            </div>
          )}

          {data?.items.map((p) => (
            <div key={p.id} style={{
              display: "flex", gap: 12, alignItems: "flex-start",
              padding: "10px 12px", borderRadius: 9, marginBottom: 8,
              border: `1px solid ${p.is_default ? colors.accent + "66" : colors.border}`,
              background: p.is_default ? colors.accent + "0a" : "transparent",
            }}>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                  <span style={{ fontWeight: 600, fontSize: 13 }}>{p.name}</span>
                  {p.is_default && <Badge text="默认" color={colors.accent} soft />}
                  {!p.has_key && <Badge text="缺 Key" color={colors.up} soft />}
                  {p.last_test_ok === true && (
                    <Badge text={`连接正常 ${p.last_test_ms ?? ""}ms`} color={colors.down} soft />
                  )}
                  {p.last_test_ok === false && (
                    <Badge text="测试未通过" color={colors.up} soft />
                  )}
                </div>
                <div style={{ fontSize: 12, color: colors.muted, marginTop: 4, lineHeight: 1.7 }}>
                  <div>模型 {p.model} · {p.base_url}</div>
                  <div>Key {p.api_key_masked || "(未填)"}</div>
                  {p.last_test_msg && (
                    <div title={p.last_test_msg} style={{ wordBreak: "break-all" }}>
                      上次测试：{trunc(p.last_test_msg, 90)}
                    </div>
                  )}
                </div>
              </div>
              <div style={{ display: "flex", gap: 6, flexWrap: "wrap", justifyContent: "flex-end" }}>
                {!p.is_default && (
                  <Btn kind="ghost" small disabled={busy === `def${p.id}`}
                       onClick={() => doSetDefault(p.id)}>设为默认</Btn>
                )}
                <Btn kind="ghost" small disabled={busy === `t${p.id}`}
                     onClick={() => doTestSaved(p.id)}>
                  {busy === `t${p.id}` ? "测试中…" : "测试"}
                </Btn>
                <Btn kind="ghost" small onClick={() => startEdit(p)}>编辑</Btn>
                <button onClick={() => doDelete(p)} disabled={busy === `d${p.id}`} style={{
                  border: 0, background: "transparent", color: colors.up,
                  fontSize: 12, cursor: "pointer", padding: "4px 6px",
                }}>删除</button>
              </div>
            </div>
          ))}

          {/* 新增 / 编辑表单 */}
          {draft && (
            <div style={{
              marginTop: 14, padding: 14, borderRadius: 10,
              border: `1px solid ${colors.accent}55`, background: colors.accent + "08",
            }}>
              <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 10 }}>
                {draft.id ? `编辑通道 #${draft.id}` : "新增通道"}
              </div>

              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
                <div>
                  {label("服务商（选中自动填充地址与候选模型）")}
                  <select
                    value={draft.vendor}
                    onChange={(e) => pickVendor(e.target.value)}
                    style={inputStyle(colors)}
                  >
                    {presets.map((p) => (
                      <option key={p.vendor} value={p.vendor}>{p.label}</option>
                    ))}
                  </select>
                </div>
                <div>
                  {label("通道名称")}
                  <input value={draft.name}
                         onChange={(e) => setDraft({ ...draft, name: e.target.value })}
                         placeholder="如：DeepSeek 主号" style={inputStyle(colors)} />
                </div>
                <div style={{ gridColumn: "1 / -1" }}>
                  {label("接口地址（OpenAI 兼容端点）")}
                  <input value={draft.base_url}
                         onChange={(e) => setDraft({ ...draft, base_url: e.target.value })}
                         placeholder="https://api.deepseek.com/v1"
                         style={{ ...inputStyle(colors), fontFamily: "'SF Mono', Menlo, monospace", fontSize: 12.5 }} />
                </div>
                <div>
                  {label("模型名")}
                  <input value={draft.model} list="llm-model-options"
                         onChange={(e) => setDraft({ ...draft, model: e.target.value })}
                         placeholder="deepseek-chat"
                         style={{ ...inputStyle(colors), fontFamily: "'SF Mono', Menlo, monospace", fontSize: 12.5 }} />
                  <datalist id="llm-model-options">
                    {(activePreset?.models || []).map((m) => <option key={m} value={m} />)}
                  </datalist>
                </div>
                <div>
                  {label(draft.id ? "API Key（留空 = 不修改）" : "API Key")}
                  <input type="password" value={draft.api_key}
                         onChange={(e) => setDraft({ ...draft, api_key: e.target.value })}
                         placeholder={draft.id ? "留空则保持原 Key 不变" : "sk-..."}
                         autoComplete="new-password"
                         style={{ ...inputStyle(colors), fontFamily: "'SF Mono', Menlo, monospace", fontSize: 12.5 }} />
                </div>
              </div>

              {activePreset?.note && (
                <div style={{ fontSize: 11.5, color: colors.muted, marginTop: 8 }}>
                  说明：{activePreset.note}
                </div>
              )}

              {/* 测试结果：成功给延迟，失败给原始错误原文 */}
              {test && (
                <div style={{
                  marginTop: 10, padding: "8px 10px", borderRadius: 7, fontSize: 12.5,
                  wordBreak: "break-all",
                  background: test.ok ? colors.down + "12" : colors.up + "12",
                  border: `1px solid ${test.ok ? colors.down : colors.up}44`,
                  color: test.ok ? colors.down : colors.up,
                }}>
                  {test.ok
                    ? `连接正常 · ${test.ms}ms · 模型返回「${test.reply || "(空)"}」`
                    : `连接失败 · ${test.ms}ms · ${test.error || "未知错误"}`}
                </div>
              )}

              <div style={{ display: "flex", alignItems: "center", gap: 10, marginTop: 14, flexWrap: "wrap" }}>
                <Btn onClick={doSave} disabled={busy === "save"}>
                  {busy === "save" ? "保存中…" : "保存"}
                </Btn>
                <Btn kind="ghost" onClick={doTest} disabled={busy === "test" || !draft.base_url.trim()}>
                  {busy === "test" ? "测试中…" : "测试连接"}
                </Btn>
                <label style={{ fontSize: 12.5, color: colors.muted, display: "flex", alignItems: "center", gap: 5 }}>
                  <input type="checkbox" checked={draft.makeDefault}
                         onChange={(e) => setDraft({ ...draft, makeDefault: e.target.checked })} />
                  保存后设为默认通道
                </label>
                <button onClick={reset} style={{
                  marginLeft: "auto", border: 0, background: "transparent",
                  color: colors.muted, fontSize: 12.5, cursor: "pointer",
                }}>取消</button>
              </div>
            </div>
          )}

          <div style={{ marginTop: 16, fontSize: 11.5, color: colors.muted, lineHeight: 1.8 }}>
            安全说明：Key 仅保存在本机数据库（与 .env 同权限层级），接口返回一律脱敏
            （如 {act?.key_masked || "sk-abc******wxyz"}），前端不回显明文。
          </div>
        </div>
      </div>
    </div>
  );
}
