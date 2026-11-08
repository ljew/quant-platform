/**
 * 综述类文本的关键词高亮（简报的每轮综述 + 每日全景综述共用）。
 *
 * 只做视觉锚点：政策/产业/市场词用主色，数字用浅底。
 * **故意不用红绿** —— 同一个数字在不同语境下多空相反，上色会误导。
 */
import React from "react";
import { ThemeColors } from "../theme";

/** 高亮词表：政策与监管 / 产业 / 市场口径。 */
export const HL_KEYWORDS = [
  "国常会", "政治局", "证监会", "央行", "发改委", "工信部", "商务部", "财政部",
  "降准", "降息", "再贷款", "逆回购", "MLF", "LPR", "专项债", "财政", "监管",
  "立案", "反倾销", "关税", "制裁", "回购", "增持", "减持", "中标",
  "半导体", "算力", "人工智能", "新能源", "光伏", "储能", "军工",
  "黄金", "原油", "煤炭", "化工", "医药", "地产", "汽车", "机器人", "信创",
  "成交额", "两融", "北向", "估值", "股息", "ETF", "涨停", "跌停", "换手",
];

const KW_RE = new RegExp(`(${HL_KEYWORDS.join("|")})`, "g");
const NUM_RE =
  /(\d[\d,]*(?:\.\d+)?(?:万亿|亿|万|个百分点|个基点|bp|%|美元|盎司|吨|家|项|倍|点)?)/g;

export function highlight(text: string, colors: ThemeColors): React.ReactNode[] {
  const out: React.ReactNode[] = [];
  text.split(KW_RE).forEach((chunk, i) => {
    if (!chunk) return;
    if (HL_KEYWORDS.includes(chunk)) {
      out.push(
        <span key={`k${i}`} style={{ color: colors.accent, fontWeight: 600 }}>
          {chunk}
        </span>,
      );
      return;
    }
    chunk.split(NUM_RE).forEach((p, j) => {
      if (!p) return;
      if (/^\d/.test(p)) {
        out.push(
          <span
            key={`n${i}-${j}`}
            style={{
              fontWeight: 600,
              background: `${colors.accent}16`,
              padding: "0 1px",
              borderRadius: 2,
            }}
          >
            {p}
          </span>,
        );
      } else {
        out.push(<span key={`t${i}-${j}`}>{p}</span>);
      }
    });
  });
  return out;
}
