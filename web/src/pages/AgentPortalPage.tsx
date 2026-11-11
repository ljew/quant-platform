/**
 * 智能体（左栏一级入口）：把 dsh web 嵌进平台，并给它装上「角色」。
 *
 * 为什么要有角色条（2026-11-11 老刘反馈「和原生的一样，不知道怎么选研究员」）：
 * dsh 自带的 preset（策略研究员 / 风控 / …）配置写在 cordis.patch.yml 里，
 * 但它的 UI 只有「标准 / PTC / 极简 / 创造」四个运行模式，自定义 preset 没有
 * 出现在任何下拉里 —— 进去就是原生编程助手的样子，还挂着别的项目的历史会话。
 *
 * 所以入口这层由平台来给：一排角色按钮，点一个就把「身份 + 纪律 + 问题」
 * 拼好的开场白写进 dsh 的输入框（同源 iframe，execCommand insertText），
 * 你确认内容后自己按回车。不走自动发送 —— 发送要花 token，也该由人按下。
 *
 * 为什么不改用提示词就完事：角色纪律写进开场白只是「软约束」；真正的硬边界
 * 仍然是 dsh 侧 preset 里的 ctx.tools.restrict 白名单。两条一起用才完整。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { agentPortalApi, type AgentPortalStatus } from "../api/client";
import { useTheme } from "../theme";

type Role = {
  key: string;
  name: string;
  hint: string;
  /** 默认问题：点开就填好，可以直接发，也可以改。 */
  q: string;
  /** 这个角色必须守住的那条纪律，会拼进开场白。 */
  rule: string;
};

const ROLES: Role[] = [
  {
    key: "strategy", name: "策略研究员", hint: "这条策略还值不值得跟",
    q: "jk001r 最近两次回测的超额、最大回撤、成交笔数分别是多少？区间是什么？",
    rule: "你负责判断「一条策略值不值得继续跟」。只赚不赚不够，必须比基准；一段区间好不算好，要看不同市场环境。成交笔数少于 5 的回测没有统计意义，|t|<2 只能说「无法判别」。",
  },
  {
    key: "risk", name: "风控", hint: "要不要降级/退役，最坏会怎样",
    q: "现在有哪些策略该降级或退役？给出最坏情况和判断依据。",
    rule: "你负责判断升级/降级/退役，必须同时给出最坏情况。不要只讲优点；给不出最坏情况时明说。",
  },
  {
    key: "data", name: "数据工程师", hint: "数据能不能信",
    q: "今天的数据能不能用来做决策？有哪些表滞后了？",
    rule: "你只判断「数据能不能信」。滞后要按当前时刻判断（交易日 19:00 前允许 1 天滞后），不要一刀切。",
  },
  {
    key: "news", name: "资讯研究员", hint: "今天市场在交易什么",
    q: "今天市场在交易什么？主要主线是什么？",
    rule: "你只回答「今天市场在交易什么」。区分快讯和观点；券商研报不是 alpha，只作参考。",
  },
  {
    key: "ops", name: "运维", hint: "服务还活着吗、任务按时跑了吗",
    q: "服务都正常吗？有哪些任务没按时跑？",
    rule: "你只回答「服务活着吗、任务按时跑了吗」。报状态要带具体指标，不要笼统说「正常」。",
  },
  {
    key: "factor", name: "因子研究员", hint: "这个信号有没有预测力",
    q: "chan_buy 这个信号最近 90 天的预测力怎么样？t 值和样本量是多少？",
    rule: "你只判断「一个信号有没有预测力」。|t|<2 只能写「无法判别」，不许下结论；必须给样本量。",
  },
  {
    key: "portfolio", name: "组合经理", hint: "几个策略怎么配、上多少钱",
    q: "jk001 和 xq_chan_buy 怎么配比？给个建议和理由。",
    rule: "你负责「几个策略怎么配、上多少钱」。只给建议，不执行 —— 建盘这类写操作必须走审批，由人确认。",
  },
  {
    key: "trader", name: "交易员", hint: "模拟盘执行得怎么样",
    q: "模拟盘现在持仓几只？净值跑到哪一天了？",
    rule: "你只管「模拟盘执行情况」，不能下单、不能改参数。净值要说明算到哪一天。",
  },
];

const COMMON = `【硬约束】你看到的每个数字都必须来自工具返回，禁止自己计算或估算。
工具返回空或失败时，必须明确说「没查到 X」并说明原因，不许用「一般来说」补齐。
每个结论后面要能指出出处（接口名 / 回测 id / 区间 / as_of）。
你没有任何写操作、没有 shell、没有文件系统；需要写的东西一律走审批。`;

export default function AgentPortalPage() {
  const { colors } = useTheme();
  const [st, setSt] = useState<AgentPortalStatus | null>(null);
  const [probing, setProbing] = useState(true);
  const [nonce, setNonce] = useState(0);
  const frame = useRef<HTMLIFrameElement>(null);

  const [role, setRole] = useState<Role | null>(null);
  const [draft, setDraft] = useState("");
  const [filled, setFilled] = useState("");

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
    const t = window.setInterval(() => void probe(), 30_000);
    return () => window.clearInterval(t);
  }, [probe]);

  /** 把开场白写进 dsh 的输入框（同源 iframe + contenteditable）。
   *
   * 用 execCommand('insertText') 而不是 innerHTML/textContent：后者只是改了 DOM，
   * React 的受控状态收不到，下一次渲染就被覆盖回去。execCommand 会触发真实的
   * input 事件 —— 实测三种写法里只有它和 paste 事件能留下内容（2026-11-11 验）。
   */
  const inject = (text: string): boolean => {
    const doc = frame.current?.contentDocument;
    const el = doc?.querySelector("[contenteditable]") as HTMLElement | null;
    if (!doc || !el) return false;
    el.focus();
    // 先全选再插入 = 覆盖旧内容，避免上次的问题和新问题拼在一起
    const sel = doc.getSelection();
    const range = doc.createRange();
    range.selectNodeContents(el);
    sel?.removeAllRanges();
    sel?.addRange(range);
    doc.execCommand("insertText", false, text);
    return true;
  };

  const pick = (r: Role) => {
    setRole(r);
    setDraft(r.q);
    setFilled("");
  };

  const doFill = () => {
    if (!role) return;
    const text = `【角色】${role.name} —— ${role.hint}\n${role.rule}\n\n${COMMON}\n\n${draft.trim() || role.q}`;
    const ok = inject(text);
    setFilled(ok ? "已填入下方输入框，按回车发送（发送前可以再改）" : "没找到输入框 —— dsh 可能还在加载，等两秒再试");
  };

  const alive = !!st?.alive;
  const dot = probing ? colors.muted : alive ? "#16a34a" : colors.up;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 0 }}>
      {/* 状态条 */}
      <div style={{
        display: "flex", alignItems: "center", gap: 10,
        padding: "0 2px 8px", fontSize: 12.5, color: colors.muted,
      }}>
        <span style={{ width: 7, height: 7, borderRadius: 99, background: dot, flexShrink: 0 }} />
        <span style={{ color: colors.text, fontWeight: 600 }}>智能体</span>
        <span>
          {probing ? "探测中…" : alive ? `dsh 已连接 · ${st?.upstream.replace(/^https?:\/\//, "")}` : "dsh 未启动"}
        </span>
        <span style={{ flex: 1 }} />
        <button onClick={() => { void probe(); setNonce((n) => n + 1); setFilled(""); }} style={linkBtn(colors)}>
          重载
        </button>
        <a href={st?.entry || "/agent/"} target="_blank" rel="noreferrer"
          style={{ ...linkBtn(colors), textDecoration: "none" }}>
          新窗口 ↗
        </a>
      </div>

      {!alive && !probing ? (
        <div style={{
          background: colors.card, border: `1px solid ${colors.border}`,
          borderRadius: 12, padding: "48px 32px", textAlign: "center", color: colors.text,
        }}>
          <div style={{ fontSize: 15, fontWeight: 600, marginBottom: 8 }}>dsh 没在跑</div>
          <div style={{ color: colors.muted, fontSize: 13, lineHeight: 1.8, marginBottom: 20 }}>
            智能体和平台是两个进程。dsh 起来之后，这一页会自动连上，
            <br />
            登录 cookie 由后端带 token 种下，不用手工复制。
          </div>
          <pre style={{
            display: "inline-block", textAlign: "left", background: colors.bg,
            border: `1px solid ${colors.border}`, borderRadius: 8, padding: "12px 16px",
            fontSize: 12.5, color: colors.text, margin: 0,
          }}>
            ~/quant-platform/scripts/dsh-web.sh start
          </pre>
          <div style={{ marginTop: 18 }}>
            <button onClick={() => void probe()}
              style={{ ...linkBtn(colors), border: `1px solid ${colors.border}`, padding: "6px 14px" }}>
              重新探测
            </button>
          </div>
          {st?.detail && (
            <div style={{ marginTop: 14, fontSize: 11.5, color: colors.muted }}>探测结果：{st.detail}</div>
          )}
        </div>
      ) : (
        <>
          {/* 角色条：平台的入口，不是 dsh 的 */}
          <div style={{
            display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap",
            padding: "0 0 8px",
          }}>
            <span style={{ fontSize: 11.5, color: colors.muted, marginRight: 2 }}>选角色：</span>
            {ROLES.map((r) => {
              const on = role?.key === r.key;
              return (
                <button
                  key={r.key}
                  onClick={() => pick(r)}
                  title={r.hint}
                  style={{
                    border: `1px solid ${on ? colors.accent : colors.border}`,
                    background: on ? `${colors.accent}14` : "transparent",
                    color: on ? colors.accent : colors.text,
                    borderRadius: 16, padding: "3px 11px", fontSize: 12,
                    cursor: "pointer", fontWeight: on ? 600 : 400,
                  }}
                >{r.name}</button>
              );
            })}
            {role ? (
              <span style={{ fontSize: 11.5, color: colors.muted, marginLeft: 4 }}>
                {role.hint}
              </span>
            ) : (
              <span style={{ fontSize: 11, color: colors.muted, marginLeft: 4 }}>
                ← 点一个角色开始。左侧历史会话是 dsh 以前工作区留下的，与量化无关，忽略即可
              </span>
            )}
          </div>

          {/* 选好角色后展开：可编辑的问题 + 填入按钮 */}
          {role && (
            <div style={{
              display: "flex", gap: 8, alignItems: "flex-start",
              padding: "9px 12px", marginBottom: 8,
              background: colors.card, border: `1px solid ${colors.border}`, borderRadius: 10,
            }}>
              <div style={{ flex: 1, minWidth: 0 }}>
                <textarea
                  value={draft}
                  onChange={(e) => setDraft(e.target.value)}
                  rows={2}
                  placeholder="想问它什么？点「填入对话框」后会在下面按回车发送"
                  style={{
                    width: "100%", boxSizing: "border-box", padding: "7px 10px",
                    borderRadius: 8, border: `1px solid ${colors.border}`,
                    background: colors.bg, color: colors.text, fontSize: 12.5,
                    resize: "vertical", lineHeight: 1.6,
                  }}
                />
                <div style={{ fontSize: 11, color: colors.muted, marginTop: 4, lineHeight: 1.6 }}>
                  会一并带上这个角色的纪律（只读、必须引数字、查不到就说没查到）
                </div>
              </div>
              <button
                onClick={doFill}
                style={{
                  padding: "8px 16px", borderRadius: 8, border: 0, fontSize: 12.5,
                  fontWeight: 600, background: colors.accent, color: "#fff",
                  cursor: "pointer", whiteSpace: "nowrap", flexShrink: 0,
                }}
              >填入对话框</button>
            </div>
          )}

          {filled && (
            <div style={{
              marginBottom: 8, padding: "7px 12px", borderRadius: 8, fontSize: 12,
              background: `${colors.down}0f`, border: `1px solid ${colors.down}44`, color: colors.down,
            }}>{filled}</div>
          )}

          <iframe
            key={nonce}
            ref={frame}
            title="智能体"
            src={st?.entry || "/agent/"}
            style={{
              width: "100%",
              height: role ? "calc(100vh - 268px)" : "calc(100vh - 200px)",
              border: `1px solid ${colors.border}`,
              borderRadius: 12,
              background: colors.card,
            }}
          />
        </>
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
