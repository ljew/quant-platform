/**
 * 智能体（左栏一级入口）：把 dsh web 嵌进来，右侧补一列「产物」。
 *
 * 2026-11-11 的两轮改动：
 *
 * ① 角色条搬进 dsh 里（`~/.dsh/profiles/node_modules/@quant/quant-ui`）
 *    老刘的原话是「不知道怎么选研究员」。第一版把八个角色放在 iframe 上方 ——
 *    离真正提问的输入框隔着三步远，等于没解决。dsh 的浏览器端是可插拔的
 *    （客户端插件往命名插槽里塞组件），于是改成注册到
 *    `conversation.input.dock`，按钮就长在输入框上方，点一下把
 *    「身份 + 纪律 + 示例问题」拼好的开场白写进输入框，人自己按回车。
 *
 * ② 本页只留外壳：状态条 + iframe + 右栏产物
 *    设计文档 08 节要三栏（会话 / 对话 / 产物）。会话与对话交给 dsh 原生，
 *    产物栏由平台画 —— 它要读平台自己的库，放进 dsh 反而绕远。
 *
 * 产物栏刻意**不**猜「它刚才引用了哪几条」：那要解析消息流，猜错会误导。
 * 这里给的是「最近发生的事实」，按时间排，你自己对号入座；点一下跳回平台核对。
 */
import { useCallback, useEffect, useState } from "react";
import { agentPortalApi, type AgentPortalStatus } from "../api/client";
import { useTheme } from "../theme";

type Deliverable = {
  id: string;
  kind: string;
  title: string;
  sub: string;
  at: string;
  href: string;
};

const KIND_COLOR: Record<string, string> = {
  回测: "#2e86de",
  研究卡: "#6c5ce7",
  报告: "#16a34a",
};

export default function AgentPortalPage() {
  const { colors } = useTheme();
  const [st, setSt] = useState<AgentPortalStatus | null>(null);
  const [probing, setProbing] = useState(true);
  const [nonce, setNonce] = useState(0);
  const [items, setItems] = useState<Deliverable[]>([]);
  const [railOpen, setRailOpen] = useState(true);

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

  const loadArtifacts = useCallback(() => {
    fetch("/api/v1/portal/deliverables")
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((d) => setItems(d.items || []))
      .catch(() => setItems([]));
  }, []);

  useEffect(() => {
    void probe();
    const t = window.setInterval(() => void probe(), 30_000);
    return () => window.clearInterval(t);
  }, [probe]);

  useEffect(() => {
    loadArtifacts();
    // 产物会随 ETL / 编排器 / 手动建盘变化，60s 刷一次够了
    const t = window.setInterval(loadArtifacts, 60_000);
    return () => window.clearInterval(t);
  }, [loadArtifacts]);

  const alive = !!st?.alive;
  const dot = probing ? colors.muted : alive ? "#16a34a" : colors.up;

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      {/* 状态条 */}
      <div
        style={{
          display: "flex", alignItems: "center", gap: 10,
          padding: "0 2px 8px", fontSize: 12.5, color: colors.muted,
        }}
      >
        <span style={{ width: 7, height: 7, borderRadius: 99, background: dot, flexShrink: 0 }} />
        <span style={{ color: colors.text, fontWeight: 600 }}>智能体</span>
        <span>
          {probing
            ? "探测中…"
            : alive
              ? `dsh 已连接 · ${st?.upstream.replace(/^https?:\/\//, "")}`
              : "dsh 未启动"}
        </span>
        <span style={{ flex: 1 }} />
        <button
          onClick={() => {
            void probe();
            loadArtifacts();
            setNonce((n) => n + 1);
          }}
          style={linkBtn(colors)}
        >
          重载
        </button>
        <button onClick={() => setRailOpen((v) => !v)} style={linkBtn(colors)}>
          {railOpen ? "收起产物" : "展开产物"}
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

      {!alive && !probing ? (
        <div
          style={{
            background: colors.card, border: `1px solid ${colors.border}`,
            borderRadius: 12, padding: "48px 32px", textAlign: "center", color: colors.text,
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
              display: "inline-block", textAlign: "left", background: colors.bg,
              border: `1px solid ${colors.border}`, borderRadius: 8, padding: "12px 16px",
              fontSize: 12.5, color: colors.text, margin: 0,
            }}
          >
            ~/quant-platform/scripts/dsh-web.sh start
          </pre>
          <div style={{ marginTop: 18 }}>
            <button
              onClick={() => void probe()}
              style={{ ...linkBtn(colors), border: `1px solid ${colors.border}`, padding: "6px 14px" }}
            >
              重新探测
            </button>
          </div>
          {st?.detail && (
            <div style={{ marginTop: 14, fontSize: 11.5, color: colors.muted }}>探测结果：{st.detail}</div>
          )}
        </div>
      ) : (
        <div style={{ display: "flex", gap: 12, flex: 1, minHeight: 0 }}>
          <iframe
            key={nonce}
            title="智能体"
            src={st?.entry || "/agent/"}
            style={{
              flex: 1, minWidth: 0, height: "calc(100vh - 152px)",
              border: `1px solid ${colors.border}`, borderRadius: 12, background: colors.card,
            }}
          />
          {railOpen && (
            <div
              style={{
                width: 268, flexShrink: 0, height: "calc(100vh - 152px)", overflowY: "auto",
                background: colors.card, border: `1px solid ${colors.border}`,
                borderRadius: 12, padding: "12px 12px 6px",
              }}
            >
              <div style={{ fontSize: 12.5, fontWeight: 700, marginBottom: 2 }}>产物</div>
              <div style={{ fontSize: 11, color: colors.muted, lineHeight: 1.7, marginBottom: 10 }}>
                最近跑出来的东西，点一下跳回平台核对。
                <br />
                不是「它引用过的」—— 那个得解析消息流，猜错更误导。
              </div>
              {items.length === 0 ? (
                <div style={{ fontSize: 11.5, color: colors.muted, padding: "12px 0" }}>
                  暂无产物
                </div>
              ) : (
                items.map((it) => {
                  const c = KIND_COLOR[it.kind] || colors.muted;
                  return (
                    <a
                      key={it.id}
                      href={it.href}
                      style={{
                        display: "block", padding: "8px 9px", marginBottom: 7,
                        borderRadius: 9, border: `1px solid ${colors.border}`,
                        textDecoration: "none", color: colors.text,
                      }}
                    >
                      <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                        <span
                          style={{
                            fontSize: 10, color: c, border: `1px solid ${c}55`,
                            borderRadius: 4, padding: "0 4px", flexShrink: 0,
                          }}
                        >
                          {it.kind}
                        </span>
                        <span style={{ fontSize: 10.5, color: colors.muted, marginLeft: "auto" }}>
                          {it.at.slice(5, 16)}
                        </span>
                      </div>
                      <div style={{ fontSize: 12, marginTop: 4, lineHeight: 1.55 }}>{it.title}</div>
                      <div style={{ fontSize: 10.5, color: colors.muted, marginTop: 2 }}>{it.sub}</div>
                    </a>
                  );
                })
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function linkBtn(colors: ReturnType<typeof useTheme>["colors"]): React.CSSProperties {
  return {
    border: 0, background: "transparent", color: colors.accent,
    fontSize: 12.5, cursor: "pointer", padding: "4px 8px", borderRadius: 6,
  };
}
