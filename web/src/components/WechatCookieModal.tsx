/**
 * 微信读书凭证更新弹窗。
 *
 * 为什么要在界面里做：Cookie 是浏览器登录态，约 1-2 周过期。过期后
 * **列表接口还能用、正文接口先挂**（-2012 登录超时）—— 采集日志会显示
 * 「已最新 7 / 失败 0」，看起来正常，实际一篇没采到。2026-11-10 早上
 * 就是这样，查了半天才发现。
 *
 * 以前续期要开终端跑 Playwright 脚本，还得记得放哪（/tmp 会被清）。
 * 现在：页面上粘贴 → 自动验证 → 有效才算成功。
 */
import { useState } from "react";
import { Btn } from "./ui";
import { useTheme, ThemeColors } from "../theme";

const API = "/api/v1/wechat/cookie";

interface SaveResult {
  ok: boolean;
  saved: number;
  valid: boolean;
  reason: string;
  code: number | null;
}

export default function WechatCookieModal({
  onClose, onSaved,
}: { onClose: () => void; onSaved: () => void }) {
  const { colors } = useTheme();
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<SaveResult | null>(null);
  const [err, setErr] = useState("");

  const save = async () => {
    if (!text.trim()) { setErr("请先粘贴 Cookie"); return; }
    setBusy(true); setErr(""); setResult(null);
    try {
      const r = await fetch(API, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
      });
      const d = await r.json();
      if (!r.ok) { setErr(String(d.detail || r.status)); return; }
      setResult(d);
      if (d.valid) onSaved();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div
      style={{
        position: "fixed", inset: 0, background: "#0007", zIndex: 1000,
        display: "flex", alignItems: "center", justifyContent: "center", padding: 20,
      }}
      onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}
    >
      <div style={{
        background: colors.card, borderRadius: 12, padding: 22, width: 640,
        maxWidth: "100%", maxHeight: "90vh", overflowY: "auto",
        border: `1px solid ${colors.border}`,
      }}>
        <div style={{ fontSize: 15, fontWeight: 700, marginBottom: 4 }}>
          更新微信读书凭证
        </div>
        <div style={{ fontSize: 12, color: colors.muted, lineHeight: 1.8, marginBottom: 14 }}>
          Cookie 是浏览器登录态，约 1~2 周会过期。过期后<b>列表接口还能用、正文接口先挂</b>
          （-2012 登录超时），采集会显示「成功」但实际一篇没采到。
        </div>

        <div style={{
          fontSize: 12.5, lineHeight: 2, background: colors.bg,
          borderRadius: 8, padding: "10px 14px", marginBottom: 14,
        }}>
          <div><b>①</b> 浏览器打开 <code>weread.qq.com</code>，用微信扫码登录</div>
          <div style={{ marginTop: 6 }}>
            <b>②</b> 按 <kbd>F12</kbd> → 切到 <b>Network / 网络</b> 标签
          </div>
          <div><b>③</b> 刷新页面（<kbd>Cmd+R</kbd> / <kbd>F5</kbd>）</div>
          <div><b>④</b> 点列表里任意一条 <code>weread.qq.com</code> 的请求</div>
          <div>
            <b>⑤</b> 右侧找 <b>Request Headers</b> 里的 <code>Cookie:</code>，
            把冒号后面<b>整串</b>复制下来
          </div>
          <div style={{ marginTop: 8 }}>
            <b>⑥</b> 回到这里，在下面按 <kbd>Cmd/Ctrl+V</kbd> 粘贴，点保存
          </div>

          <div style={{
            marginTop: 10, padding: "8px 12px", borderRadius: 6,
            background: "#b9770e14", border: "1px solid #b9770e44",
            color: "#b9770e", fontSize: 11.5, lineHeight: 1.75,
          }}>
            ⚠️ <b>不要</b>用 Console 里的 <code>document.cookie</code> ——
            关键字段 <code>wr_vid</code> / <code>wr_skey</code> 是 <b>HttpOnly</b>，
            那一行读不到，复制出来会缺两条，服务端直接返回 401。
          </div>
        </div>

        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder="粘贴 Cookie（形如 wr_fp=...; wr_vid=...; ...）也可以直接粘 JSON 数组"
          rows={5}
          style={{
            width: "100%", boxSizing: "border-box", fontSize: 12,
            fontFamily: "ui-monospace, monospace", lineHeight: 1.6,
            padding: "8px 10px", borderRadius: 8,
            border: `1px solid ${colors.border}`, background: colors.bg,
            color: colors.text, resize: "vertical",
          }}
        />

        {err && (
          <div style={{ marginTop: 10, fontSize: 12.5, color: colors.up }}>{err}</div>
        )}
        {result && (
          <div style={{
            marginTop: 10, padding: "10px 12px", borderRadius: 8, fontSize: 12.5,
            lineHeight: 1.7,
            background: result.valid ? "#1e844912" : "#b9770e14",
            border: `1px solid ${result.valid ? "#1e844944" : "#b9770e44"}`,
            color: result.valid ? "#1e8449" : "#b9770e",
          }}>
            {result.valid
              ? `✅ 已保存 ${result.saved} 条 Cookie，验证通过：${result.reason}`
              : `⚠️ 已保存 ${result.saved} 条，但验证没过：${result.reason}`}
          </div>
        )}

        <div style={{ marginTop: 16, display: "flex", gap: 10, justifyContent: "flex-end" }}>
          <Btn kind="ghost" onClick={onClose}>关闭</Btn>
          <Btn onClick={save} disabled={busy}>
            {busy ? "验证中…" : "保存并验证"}
          </Btn>
        </div>
      </div>
    </div>
  );
}
