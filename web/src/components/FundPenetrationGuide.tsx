/**
 * 「这一页怎么用」提示条
 *
 * 背景：基金穿透页有 14 个板块平铺在一起，从「当日估算」到「第五轨回归」全在一屏，
 * 第一次打开的人不知道先看哪块、也不知道哪块能当结论用。这条提示条的作用只有一个：
 * 把「你心里那个问题」直接映射到「该看哪一块」，点一下就滚过去。
 *
 * 折叠态只留一句主线 + 三个最常见的入口，展开态给完整的四类问题。
 * 展开状态存 localStorage，老刘看过一次就可以长期收起，不占屏幕。
 */
import { useState } from "react";
import { useTheme } from "../theme";

interface Ask {
  /** 用户嘴里的问题 */
  q: string;
  /** 对应板块（label 要和页面上的卡片标题对得上，别自造名词） */
  blocks: { label: string; id: string }[];
  /** 这块最容易被读错的地方 */
  trap: string;
}

const ASKS: Ask[] = [
  {
    q: "这只基金现在到底押的是什么？",
    blocks: [
      { label: "单基金钻取", id: "sec-drill" },
      { label: "风格穿透", id: "sec-style" },
    ],
    trap: "先看钻取卡片顶部的「权重口径」标签：net_value 才是真占净值比，structure_only 只代表结构，不能乘净值当收益来源。",
  },
  {
    q: "这个行业，谁在重仓、谁在偷偷减？",
    blocks: [{ label: "行业反查", id: "sec-reverse" }],
    trap: "按「超配（active_w）」排序，别按绝对权重排 —— 银行天然权重高，不代表有人主动押注。",
  },
  {
    q: "全市场这一阵在加什么、减什么？",
    blocks: [
      { label: "当日估算截面", id: "sec-nowcast" },
      { label: "全市场行业配置", id: "sec-industry" },
    ],
    trap: "「日变化」是真漂移还是基准位移，取决于风格 z 的基准有没有跟着滚动 —— 基准固定在前一标准报告期，所以这里是真漂移。",
  },
  {
    q: "这些数字，我能信几分？",
    blocks: [
      { label: "披露完整度", id: "sec-coverage" },
      { label: "净值回归校验", id: "sec-nav" },
      { label: "监管披露校验", id: "sec-reg" },
      { label: "方法与口径", id: "sec-method" },
    ],
    trap: "置信度低于 0.6 时只能写「可能」；Q1/Q3 只有前十大（约 53%）是制度限制，不是数据没抓好。",
  },
];

const OPEN_KEY = "fp-guide-open";

function readOpen(): boolean {
  try {
    return localStorage.getItem(OPEN_KEY) !== "0";
  } catch {
    return true;
  }
}

function jump(id: string) {
  const el = document.getElementById(id);
  if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
}

export default function FundPenetrationGuide() {
  const { colors, mode } = useTheme();
  const [open, setOpen] = useState(readOpen);

  function toggle() {
    const next = !open;
    setOpen(next);
    try {
      localStorage.setItem(OPEN_KEY, next ? "1" : "0");
    } catch {
      /* 隐私模式下写不进去也无所谓，刷新后回到展开态 */
    }
  }

  const bg = mode === "dark" ? "#141b2b" : "#f7f9fd";

  return (
    <div
      style={{
        background: bg,
        border: `1px solid ${colors.border}`,
        borderRadius: 12,
        marginBottom: 16,
        overflow: "hidden",
      }}
    >
      {/* 折叠态也保留的一条主线：四步动线，点任一步直接跳 */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 10,
          padding: "10px 14px",
          fontSize: 12.5,
          flexWrap: "wrap",
        }}
      >
        <span style={{ fontWeight: 600, color: colors.text }}>这一页怎么用</span>
        <span style={{ color: colors.muted }}>先看「现在押什么」，再看「谁在动」，最后看「能信几分」</span>
        <span style={{ flex: 1 }} />
        <button
          onClick={toggle}
          style={{
            border: `1px solid ${colors.border}`,
            background: colors.card,
            color: colors.text,
            borderRadius: 6,
            padding: "3px 10px",
            fontSize: 12,
            cursor: "pointer",
          }}
        >
          {open ? "收起" : "四类问题 →"}
        </button>
      </div>

      {open && (
        <div style={{ padding: "0 14px 14px" }}>
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fit,minmax(280px,1fr))",
              gap: 10,
            }}
          >
            {ASKS.map((a, i) => (
              <div
                key={a.q}
                style={{
                  background: colors.card,
                  border: `1px solid ${colors.border}`,
                  borderRadius: 9,
                  padding: "10px 12px",
                }}
              >
                <div style={{ fontSize: 12.5, fontWeight: 600, marginBottom: 7, lineHeight: 1.5 }}>
                  <span style={{ color: colors.accent, marginRight: 6 }}>{i + 1}</span>
                  {a.q}
                </div>
                <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginBottom: 7 }}>
                  {a.blocks.map((b) => (
                    <button
                      key={b.id}
                      onClick={() => jump(b.id)}
                      style={{
                        border: `1px solid ${colors.border}`,
                        background: mode === "dark" ? "#1b2333" : "#eef3fc",
                        color: colors.accent,
                        borderRadius: 999,
                        padding: "2px 9px",
                        fontSize: 11.5,
                        cursor: "pointer",
                        whiteSpace: "nowrap",
                      }}
                    >
                      {b.label} ↓
                    </button>
                  ))}
                </div>
                <div style={{ fontSize: 11.5, color: colors.muted, lineHeight: 1.6 }}>
                  坑：{a.trap}
                </div>
              </div>
            ))}
          </div>

          {/* 一条铁律，比任何板块都重要，所以单独拎出来 */}
          <div
            style={{
              marginTop: 10,
              padding: "8px 12px",
              borderRadius: 8,
              fontSize: 11.5,
              lineHeight: 1.7,
              background: mode === "dark" ? "#2a2118" : "#fff7e9",
              border: `1px solid ${mode === "dark" ? "#4a3a22" : "#f0d9ad"}`,
              color: colors.text,
            }}
          >
            <b>一条铁律</b>：标了 <code>carried</code>（锚点继承）的权重，
            只能进「行业结构的分母校正」，<b>永远不进收益归因和持仓结论</b>。
            看到钻取里的持仓明细带灰底/继承标记，就是不能直接当"它现在持有"用的那批。
          </div>
        </div>
      )}
    </div>
  );
}
