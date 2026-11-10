/**
 * 智能体（左栏一级入口）：把 dsh web 嵌进平台，不再另开 :3080。
 *
 * 这是设计文档「两个平台，一个入口」的落点。三件事在这里收口：
 * 1. 跨端口认证 —— 同域反代（后端 /agent/* → dsh:3080），登录 cookie 同源，
 *    首次进入由 /api/v1/portal/boot 带 token 引导一次，之后不用管。
 * 2. 在线探测 —— dsh 是独立进程，没跑时这里给启动命令，不是甩一个白屏。
 * 3. 跳出去 —— 大屏/窄屏下 iframe 可能憋屈，留一个「新窗口」出口。
 *
 * 为什么 iframe 而不是重定向：跳走就回不来了（dsh 里没有回平台的路），
 * 而智能体的产物常常要「跳回核对」（#cards/384 深链），必须两边都在。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { agentPortalApi, type AgentPortalStatus } from "../api/client";
import { useTheme } from "../theme";

export default function AgentPortalPage() {
  const { colors } = useTheme();
  const [st, setSt] = useState<AgentPortalStatus | null>(null);
  const [probing, setProbing] = useState(true);
  const [nonce, setNonce] = useState(0); // 换 key 强制 iframe 重载
  const frame = useRef<HTMLIFrameElement>(null);

  const probe = useCallback(async () => {
    setProbing(true);
    try {
      setSt(await agentPortalApi.status());
    } catch {
      setSt({ alive: false, detail: "后端无响应", has_token: false, entry: "/agent/", upstream: "", hint: "" });
    } finally {
      setProbing(false);
    }
  }, []);

  useEffect(() => {
    void probe();
    // dsh 可能中途被拉起/关掉，30s 一次足够（不打扰 iframe 里的会话）
    const t = window.setInterval(() => void probe(), 30_000);
    return () => window.clearInterval(t);
  }, [probe]);

  const alive = !!st?.alive;
  const dot = probing ? colors.muted : alive ? "#16a34a" : colors.up;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 0 }}>
      {/* 状态条：一行说清「智能体在哪、通不通」 */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 10,
          padding: "0 2px 10px",
          fontSize: 12.5,
          color: colors.muted,
        }}
      >
        <span style={{ width: 7, height: 7, borderRadius: 99, background: dot, flexShrink: 0 }} />
        <span style={{ color: colors.text, fontWeight: 600 }}>智能体</span>
        <span>
          {probing ? "探测中…" : alive ? `dsh 已连接 · ${st?.upstream.replace(/^https?:\/\//, "")}` : "dsh 未启动"}
        </span>
        <span style={{ flex: 1 }} />
        <button onClick={() => { void probe(); setNonce((n) => n + 1); }} style={linkBtn(colors)}>
          重载
        </button>
        <a
          href={st?.entry || "/agent/"}
          target="_blank"
          rel="noreferrer"
          style={{ ...linkBtn(colors), textDecoration: "none" }}
        >
          新窗口 ↗
        </a>
      </div>

      {probing && !st ? (
        <div style={{ padding: 40, color: colors.muted, fontSize: 13 }}>正在探测 dsh…</div>
      ) : !alive ? (
        <div
          style={{
            background: colors.card,
            border: `1px solid ${colors.border}`,
            borderRadius: 12,
            padding: "48px 32px",
            textAlign: "center",
            color: colors.text,
          }}
        >
          <div style={{ fontSize: 15, fontWeight: 600, marginBottom: 8 }}>dsh 没在跑</div>
          <div style={{ color: colors.muted, fontSize: 13, lineHeight: 1.8, marginBottom: 20 }}>
            智能体和平台是两个进程。dsh 起来之后，这一页会自动连上，
            <br />
            登录 cookie 由后端带 token 种下，不用手工复制。
          </div>
          <pre
            style={{
              display: "inline-block",
              textAlign: "left",
              background: colors.bg,
              border: `1px solid ${colors.border}`,
              borderRadius: 8,
              padding: "12px 16px",
              fontSize: 12.5,
              color: colors.text,
              margin: 0,
            }}
          >
            ~/quant-platform/scripts/dsh-web.sh start
          </pre>
          <div style={{ marginTop: 18 }}>
            <button onClick={() => void probe()} style={{ ...linkBtn(colors), border: `1px solid ${colors.border}`, padding: "6px 14px" }}>
              重新探测
            </button>
          </div>
          {st?.detail && (
            <div style={{ marginTop: 14, fontSize: 11.5, color: colors.muted }}>探测结果：{st.detail}</div>
          )}
        </div>
      ) : (
        <iframe
          key={nonce}
          ref={frame}
          title="智能体"
          src={st?.entry || "/agent/"}
          style={{
            width: "100%",
            height: "calc(100vh - 148px)",
            border: `1px solid ${colors.border}`,
            borderRadius: 12,
            background: colors.card,
          }}
        />
      )}
    </div>
  );
}

function linkBtn(colors: ReturnType<typeof useTheme>["colors"]): React.CSSProperties {
  return {
    border: 0,
    background: "transparent",
    color: colors.accent,
    fontSize: 12.5,
    cursor: "pointer",
    padding: "4px 8px",
    borderRadius: 6,
  };
}
