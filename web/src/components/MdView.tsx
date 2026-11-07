import { useMemo, type ReactNode } from "react";
import { useTheme, type ThemeColors } from "../theme";

/**
 * 极简 Markdown 渲染器（只覆盖项目文档实际用到的语法）。
 *
 * 为什么自己写而不用 react-markdown：
 * - 只为一个只读文档页引入一个新依赖不划算（本项目前端依赖很克制）；
 * - 文档里大量出现 `<0.8×`、`>2.2×` 这类文本，走 `dangerouslySetInnerHTML`
 *   会被浏览器当成未闭合标签吞掉。这里全程用 React 元素渲染，天然安全。
 *
 * 支持：h1–h4 / 段落 / 无序·有序列表 / 表格 / 代码块 / 引用 / 分隔线，
 * 行内支持 **粗体**、`行内代码`、[链接](url)。
 */

export type MdBlock =
  | { t: "h"; level: 1 | 2 | 3 | 4; text: string; id: string }
  | { t: "p"; text: string }
  | { t: "ul"; items: string[] }
  | { t: "ol"; items: string[] }
  | { t: "table"; head: string[]; rows: string[][] }
  | { t: "code"; lang: string; text: string }
  | { t: "quote"; text: string }
  | { t: "hr" };

const splitRow = (ln: string): string[] =>
  ln.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((s) => s.trim());

const isTableSep = (ln: string) => /^\s*\|[\s:|-]+\|\s*$/.test(ln);

/** 解析成块列表。h 块带稳定 id（h-<序号>），供目录跳转。 */
export function parseMarkdown(md: string): MdBlock[] {
  const lines = md.replace(/\r\n/g, "\n").split("\n");
  const out: MdBlock[] = [];
  let hi = 0;
  let i = 0;

  // 块级起始特征：段落遇到这些就断开
  const startsBlock = (s: string) =>
    /^(#{1,4}\s|\s*\||\s*[-*+]\s|\s*\d+\.\s|\s*>|\s*---+\s*$|```)/.test(s);

  while (i < lines.length) {
    const ln = lines[i];
    if (!ln.trim()) { i++; continue; }

    // ``` 代码块
    if (ln.trimStart().startsWith("```")) {
      const lang = ln.trim().slice(3).trim();
      const buf: string[] = [];
      i++;
      while (i < lines.length && !lines[i].trimStart().startsWith("```")) buf.push(lines[i++]);
      i++; // 跳过结束栅栏
      out.push({ t: "code", lang, text: buf.join("\n") });
      continue;
    }

    // --- 分隔线
    if (/^\s*---+\s*$/.test(ln)) { out.push({ t: "hr" }); i++; continue; }

    // 标题
    const hm = /^(#{1,4})\s+(.*)$/.exec(ln);
    if (hm) {
      const level = hm[1].length as 1 | 2 | 3 | 4;
      out.push({ t: "h", level, text: hm[2].trim(), id: `h-${hi++}` });
      i++;
      continue;
    }

    // 表格：当前行以 | 开头，且下一行是分隔行
    if (/^\s*\|/.test(ln) && i + 1 < lines.length && isTableSep(lines[i + 1])) {
      const head = splitRow(ln);
      i += 2;
      const rows: string[][] = [];
      while (i < lines.length && /^\s*\|/.test(lines[i])) rows.push(splitRow(lines[i++]));
      out.push({ t: "table", head, rows });
      continue;
    }

    // 引用
    if (/^\s*>\s?/.test(ln)) {
      const buf: string[] = [];
      while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
        buf.push(lines[i].replace(/^\s*>\s?/, ""));
        i++;
      }
      out.push({ t: "quote", text: buf.join(" ") });
      continue;
    }

    // 无序列表
    if (/^\s*[-*+]\s+/.test(ln)) {
      const items: string[] = [];
      while (i < lines.length && /^\s*[-*+]\s+/.test(lines[i])) {
        items.push(lines[i++].replace(/^\s*[-*+]\s+/, ""));
      }
      out.push({ t: "ul", items });
      continue;
    }

    // 有序列表
    if (/^\s*\d+\.\s+/.test(ln)) {
      const items: string[] = [];
      while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) {
        items.push(lines[i++].replace(/^\s*\d+\.\s+/, ""));
      }
      out.push({ t: "ol", items });
      continue;
    }

    // 段落（合并连续普通行）
    const buf: string[] = [];
    while (i < lines.length && lines[i].trim() && !startsBlock(lines[i])) buf.push(lines[i++]);
    if (buf.length) {
      out.push({ t: "p", text: buf.join(" ") });
    } else {
      // 兜底：本行无法归类时也必须前进一步，否则死循环
      out.push({ t: "p", text: ln.trim() });
      i++;
    }
  }
  return out;
}

/** 行内语法：**粗体** / `代码` / [文本](链接)。 */
function inline(text: string, colors: ThemeColors): ReactNode[] {
  const parts = text.split(/(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\([^)]+\))/g);
  return parts.filter(Boolean).map((p, idx) => {
    if (p.startsWith("**") && p.endsWith("**") && p.length > 4) {
      return <strong key={idx} style={{ fontWeight: 700, color: colors.text }}>{p.slice(2, -2)}</strong>;
    }
    if (p.startsWith("`") && p.endsWith("`") && p.length > 2) {
      return (
        <code key={idx} style={{
          fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
          fontSize: "0.92em", padding: "1px 5px", borderRadius: 4,
          background: colors.tableStripe, border: `1px solid ${colors.border}`,
          color: colors.text,
        }}>{p.slice(1, -1)}</code>
      );
    }
    const m = /^\[([^\]]+)\]\(([^)]+)\)$/.exec(p);
    if (m) {
      return (
        <a key={idx} href={m[2]} target="_blank" rel="noreferrer"
           style={{ color: colors.accent, textDecoration: "none" }}>{m[1]}</a>
      );
    }
    return <span key={idx}>{p}</span>;
  });
}

const H_SIZE: Record<number, number> = { 1: 19, 2: 15.5, 3: 13.5, 4: 12.5 };

export default function MdView({ blocks }: { blocks: MdBlock[] }) {
  const { colors } = useTheme();

  const styles = useMemo(() => ({
    h: (lvl: number) => ({
      fontSize: H_SIZE[lvl] ?? 13,
      fontWeight: 700,
      color: colors.text,
      margin: lvl <= 2 ? "22px 0 10px" : "16px 0 8px",
      paddingBottom: lvl <= 2 ? 7 : 0,
      borderBottom: lvl <= 2 ? `1px solid ${colors.border}` : "none",
      letterSpacing: -0.2,
      scrollMarginTop: 12,
    }) as React.CSSProperties,
    p: {
      margin: "9px 0",
      fontSize: 12.8,
      lineHeight: 1.75,
      color: colors.text,
    } as React.CSSProperties,
    li: {
      fontSize: 12.8,
      lineHeight: 1.72,
      color: colors.text,
      marginBottom: 4,
    } as React.CSSProperties,
  }), [colors]);

  return (
    <div>
      {blocks.map((b, i) => {
        switch (b.t) {
          case "h":
            return <div key={i} id={b.id} style={styles.h(b.level)}>{inline(b.text, colors)}</div>;

          case "p":
            return <p key={i} style={styles.p}>{inline(b.text, colors)}</p>;

          case "hr":
            return <div key={i} style={{ height: 1, background: colors.border, margin: "18px 0" }} />;

          case "quote":
            return (
              <div key={i} style={{
                borderLeft: `3px solid ${colors.accent}`,
                padding: "2px 0 2px 12px", margin: "10px 0",
                fontSize: 12.6, lineHeight: 1.7, color: colors.muted,
              }}>{inline(b.text, colors)}</div>
            );

          case "code":
            return (
              <pre key={i} style={{
                margin: "10px 0", padding: "11px 13px", borderRadius: 7,
                background: colors.tableStripe, border: `1px solid ${colors.border}`,
                overflowX: "auto", fontSize: 11.8, lineHeight: 1.65,
                color: colors.text,
                fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
              }}>{b.text}</pre>
            );

          case "ul":
            return (
              <ul key={i} style={{ margin: "8px 0", paddingLeft: 20 }}>
                {b.items.map((x, j) => <li key={j} style={styles.li}>{inline(x, colors)}</li>)}
              </ul>
            );

          case "ol":
            return (
              <ol key={i} style={{ margin: "8px 0", paddingLeft: 22 }}>
                {b.items.map((x, j) => <li key={j} style={styles.li}>{inline(x, colors)}</li>)}
              </ol>
            );

          case "table":
            return (
              <div key={i} style={{ overflowX: "auto", margin: "12px 0" }}>
                <table style={{
                  borderCollapse: "collapse", width: "100%",
                  fontSize: 12.2, lineHeight: 1.6,
                }}>
                  <thead>
                    <tr>
                      {b.head.map((h, j) => (
                        <th key={j} style={{
                          textAlign: "left", padding: "7px 10px",
                          background: colors.tableStripe,
                          borderBottom: `1px solid ${colors.border}`,
                          color: colors.muted, fontWeight: 600, whiteSpace: "nowrap",
                        }}>{inline(h, colors)}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {b.rows.map((r, j) => (
                      <tr key={j}>
                        {b.head.map((_, k) => (
                          <td key={k} style={{
                            padding: "7px 10px",
                            borderBottom: `1px solid ${colors.border}`,
                            color: colors.text, verticalAlign: "top",
                          }}>{inline(r[k] ?? "", colors)}</td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            );

          default:
            return null;
        }
      })}
    </div>
  );
}
