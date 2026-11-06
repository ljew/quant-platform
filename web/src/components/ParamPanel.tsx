import { useEffect, useMemo, useState } from "react";
import { ParamField, StrategyInfo } from "../api/client";
import { ThemeColors } from "../theme";
import { inputStyle } from "./ui";

/**
 * 策略参数面板 —— 按语义分组折叠。
 *
 * 背景：jk001/jk002 各有 30 个参数、enhanced_factor 有 23 个，之前是把所有参数
 * 直接 flex-wrap 平铺在页面上，导致：① 找不到目标参数；② 跑完回来忘了改过哪几个；
 * ③ 从未启用的组（如波动率目标）一直占版面。
 *
 * 本组件只做展示层：分组、标签、默认值全部来自后端 registry，这里不含任何策略知识。
 *
 * 三个约定：
 * - **比例型参数自动按百分比显示**（max ≤ 1 的 float，如 0.15 显示成 15%），
 *   输入时再除以 100 回传，后端语义不变。
 * - **改动可见**：与默认值不同的参数标蓝点，组头显示「N 项已改」。
 * - **组数 ≤ 1 时退化为平铺**（单标的策略只有 2~3 个参数，折叠反而碍事）。
 */

export type ParamValue = number | string;
export type ParamMap = Record<string, ParamValue>;

export interface ParamPreset {
  name: string;
  params: ParamMap;
  ts: number;
}

interface Props {
  strategy: StrategyInfo;
  params: ParamMap;
  onChange: (key: string, value: ParamValue) => void;
  /** 批量替换（恢复默认 / 套用预设）。 */
  onPatch: (next: ParamMap) => void;
  colors: ThemeColors;
  /** 面板宽度，默认 224。 */
  width?: number;
  /**
   * 取值模式：
   * - `value`（默认）每个参数一个值 —— 回测用。
   * - `grid` 每个参数填逗号分隔的候选值 —— 参数寻优的网格搜索用。
   *   此时「已改」的语义变为「已填取值」（空 = 不参与扫描，用默认单点）。
   */
  mode?: "value" | "grid";
}

// —— 参数类型判定 ——
const isEnum = (f: ParamField) => f.type === "str" && Array.isArray(f.options) && f.options!.length > 0;
/** label 里写明「1开/0关」的才算布尔开关；industry_level 这类 0/1 口径选择仍用数字框。 */
const isBool = (f: ParamField) => f.type === "int" && f.min === 0 && f.max === 1 && /1\s*开\s*\/\s*0\s*关/.test(f.label);
/** 比例型：0~1 之间的 float（权重、仓位、阈值），按百分比展示更直观。 */
const isPct = (f: ParamField) =>
  f.type === "float" && typeof f.max === "number" && f.max <= 1 && (f.min ?? 0) >= 0;

const toDisplay = (f: ParamField, v: unknown) =>
  isPct(f) ? +(Number(v) * 100).toFixed(4) : Number(v);
const fromDisplay = (f: ParamField, n: number) => (isPct(f) ? n / 100 : n);

const fmt = (v: unknown): string => {
  const n = Number(v);
  if (!isFinite(n)) return String(v ?? "");
  return String(+n.toFixed(6));
};

/** 与默认值不同的参数 key 集合（供父组件显示「已改 N 项」）。 */
export function changedKeys(strategy: StrategyInfo, params: ParamMap): Set<string> {
  const out = new Set<string>();
  (strategy.param_schema || []).forEach((f) => {
    const d = strategy.default_params?.[f.key];
    if (d === undefined || d === null) return;
    const cur = params[f.key];
    if (cur === undefined) return;
    if (isEnum(f)) {
      if (String(cur) !== String(d)) out.add(f.key);
    } else if (Math.abs(Number(cur) - Number(d)) > 1e-9) {
      out.add(f.key);
    }
  });
  return out;
}

/**
 * 网格模式的「已填取值」集合 —— 语义与 changedKeys 不同：
 * 空串代表「不参与扫描」（用默认单点），只要填了就视为参与了扫描。
 */
export function gridKeys(schema: ParamField[], params: ParamMap): Set<string> {
  const out = new Set<string>();
  schema.forEach((f) => {
    const v = params[f.key];
    if (v !== undefined && v !== null && String(v).trim() !== "") out.add(f.key);
  });
  return out;
}

/** 把参数还原成该策略的默认值（只覆盖 schema 里可见的参数）。 */
export function defaultParams(strategy: StrategyInfo): ParamMap {
  const p: ParamMap = {};
  (strategy.param_schema || []).forEach((f) => {
    p[f.key] = isEnum(f) ? String(f.default ?? f.options![0]) : Number(f.default ?? 0);
  });
  return p;
}

function Tri({ open, color }: { open: boolean; color: string }) {
  return (
    <span
      style={{
        width: 0, height: 0, flex: "0 0 auto",
        borderLeft: "4px solid transparent", borderRight: "4px solid transparent",
        borderTop: `5px solid ${color}`,
        transform: open ? "none" : "rotate(-90deg)",
        transition: "transform .15s",
      }}
    />
  );
}

export default function ParamPanel({
  strategy, params, onChange, onPatch, colors, width = 224, mode = "value",
}: Props) {
  const grid = mode === "grid";
  const schema = useMemo(() => strategy.param_schema || [], [strategy]);
  const defaults = strategy.default_params || {};

  const [query, setQuery] = useState("");
  const [closed, setClosed] = useState<Record<string, boolean>>({});
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [presets, setPresets] = useState<ParamPreset[]>([]);
  const [saving, setSaving] = useState(false);
  const [presetName, setPresetName] = useState("");
  const [pickedPreset, setPickedPreset] = useState("");

  const storeKey = `quant-param-presets:${strategy.key}`;

  // 切换策略时重置交互状态，并载入该策略自己的预设
  useEffect(() => {
    setQuery("");
    setClosed({});
    setDraft({});
    setSaving(false);
    setPresetName("");
    setPickedPreset("");
    try {
      const raw = localStorage.getItem(storeKey);
      setPresets(raw ? (JSON.parse(raw) as ParamPreset[]) : []);
    } catch {
      setPresets([]);
    }
  }, [storeKey]);

  const groups = useMemo(() => {
    const order: string[] = [];
    const map: Record<string, ParamField[]> = {};
    schema.forEach((f) => {
      const g = f.group || "其他";
      if (!map[g]) {
        map[g] = [];
        order.push(g);
      }
      map[g].push(f);
    });
    return order.map((name) => ({ name, fields: map[name] }));
  }, [schema]);

  const changed = useMemo(
    () => (grid ? gridKeys(schema, params) : changedKeys(strategy, params)),
    [grid, schema, strategy, params],
  );

  const q = query.trim().toLowerCase();
  const match = (f: ParamField) =>
    !q ||
    f.key.toLowerCase().includes(q) ||
    f.label.toLowerCase().includes(q) ||
    (f.group || "").toLowerCase().includes(q);

  const flat = groups.length <= 1;
  const openOf = (name: string, idx: number) => {
    if (q) return true; // 搜索时全部展开，只显示命中项
    if (closed[name] !== undefined) return !closed[name];
    return idx < 2 || groups[idx].fields.some((f) => changed.has(f.key));
  };
  const toggle = (name: string, idx: number) =>
    setClosed((c) => ({ ...c, [name]: openOf(name, idx) }));

  // —— 预设 ——
  const persist = (list: ParamPreset[]) => {
    setPresets(list);
    try {
      localStorage.setItem(storeKey, JSON.stringify(list));
    } catch {
      /* 隐私模式等场景忽略 */
    }
  };
  const savePreset = () => {
    const name = presetName.trim();
    if (!name) return;
    persist([...presets.filter((p) => p.name !== name), { name, params: { ...params }, ts: Date.now() }]);
    setSaving(false);
    setPresetName("");
    setPickedPreset(name);
  };
  const applyPreset = (name: string) => {
    setPickedPreset(name);
    const p = presets.find((x) => x.name === name);
    if (!p) return;
    // 只套用当前 schema 里仍存在的 key（策略参数可能已增删）
    const next: ParamMap = { ...params };
    schema.forEach((f) => {
      if (p.params[f.key] !== undefined) next[f.key] = p.params[f.key];
    });
    onPatch(next);
    setDraft({});
  };
  const delPreset = () => {
    if (!pickedPreset) return;
    persist(presets.filter((p) => p.name !== pickedPreset));
    setPickedPreset("");
  };

  const resetAll = () => {
    if (grid) {
      // 网格模式没有「默认值」概念，清空即回到「全部用默认单点扫描」
      const next: ParamMap = { ...params };
      schema.forEach((f) => { next[f.key] = ""; });
      onPatch(next);
    } else {
      onPatch(defaultParams(strategy));
    }
    setDraft({});
  };
  const resetGroup = (name: string) => {
    const next: ParamMap = { ...params };
    const byKey = new Map(schema.map((f) => [f.key, f]));
    groups.find((g) => g.name === name)?.fields.forEach((f) => {
      if (grid) {
        next[f.key] = "";
        return;
      }
      const meta = byKey.get(f.key)!;
      next[f.key] = isEnum(meta) ? String(meta.default ?? meta.options![0]) : Number(meta.default ?? 0);
    });
    onPatch(next);
    setDraft({});
  };

  if (!schema.length) return null;

  const linkBtn: React.CSSProperties = {
    border: 0, background: "transparent", color: colors.accent,
    fontSize: 11.5, cursor: "pointer", padding: "2px 0",
  };

  const renderRow = (f: ParamField) => {
    const ch = changed.has(f.key);
    const cur = params[f.key];
    const labelColor = ch ? colors.accent : colors.muted;
    const borderColor = ch ? colors.accent : colors.border;

    // —— 网格模式：每个参数填一组候选值（逗号分隔），用上下布局给输入框留足宽度 ——
    if (grid) {
      const pct = isPct(f);
      const dft = f.default;
      const hint = isEnum(f)
        ? f.options!.join(" / ")
        : dft === undefined || dft === null
          ? "默认值"
          : pct
            ? `默认 ${fmt(dft)}（${fmt(Number(dft) * 100)}%）`
            : `默认 ${fmt(dft)}`;
      return (
        <div key={f.key} style={{ padding: "4px 0", opacity: q && !match(f) ? 0.25 : 1 }}>
          <div
            title={f.desc || f.label}
            style={{
              fontSize: 11.5, color: labelColor, fontWeight: ch ? 600 : 400, marginBottom: 2,
              whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis",
            }}
          >
            {ch && (
              <span
                style={{
                  display: "inline-block", width: 5, height: 5, borderRadius: "50%",
                  background: colors.accent, marginRight: 5, verticalAlign: 1,
                }}
              />
            )}
            {f.label}
          </div>
          <input
            value={cur === undefined || cur === null ? "" : String(cur)}
            onChange={(e) => onChange(f.key, e.target.value)}
            placeholder={hint}
            spellCheck={false}
            style={{ ...inputStyle(colors), width: "100%", fontSize: 11.5, padding: "3px 7px", borderColor }}
          />
        </div>
      );
    }

    let ctrl: React.ReactNode;
    if (isEnum(f)) {
      ctrl = (
        <select
          value={String(cur ?? f.default ?? f.options![0])}
          onChange={(e) => onChange(f.key, e.target.value)}
          style={{ ...inputStyle(colors), width: 82, padding: "3px 4px", fontSize: 12, borderColor }}
        >
          {f.options!.map((o) => (
            <option key={o} value={o}>{o}</option>
          ))}
        </select>
      );
    } else if (isBool(f)) {
      const on = Number(cur) === 1;
      ctrl = (
        <button
          onClick={() => onChange(f.key, on ? 0 : 1)}
          title={on ? "点击关闭" : "点击开启"}
          style={{
            width: 34, height: 18, borderRadius: 99, border: 0, cursor: "pointer",
            position: "relative", background: on ? colors.accent : colors.border,
            transition: "background .15s", flex: "0 0 auto",
          }}
        >
          <span
            style={{
              position: "absolute", top: 2, left: on ? 18 : 2, width: 14, height: 14,
              borderRadius: "50%", background: "#fff", transition: "left .15s",
            }}
          />
        </button>
      );
    } else {
      const pct = isPct(f);
      ctrl = (
        <span style={{ position: "relative", display: "inline-flex", alignItems: "center", flex: "0 0 auto" }}>
          <input
            type="number"
            step={f.step ?? (f.type === "int" ? 1 : 0.01)}
            value={draft[f.key] ?? fmt(toDisplay(f, cur ?? f.default ?? 0))}
            onFocus={() => setDraft((d) => ({ ...d, [f.key]: fmt(toDisplay(f, params[f.key] ?? f.default ?? 0)) }))}
            onChange={(e) => {
              const raw = e.target.value;
              setDraft((d) => ({ ...d, [f.key]: raw }));
              if (raw === "" || raw === "-" || raw === ".") return; // 输入中间态不回传
              const n = Number(raw);
              if (isFinite(n)) onChange(f.key, fromDisplay(f, n));
            }}
            onBlur={() => setDraft((d) => { const n = { ...d }; delete n[f.key]; return n; })}
            style={{
              ...inputStyle(colors), width: 82, fontSize: 12, textAlign: "right",
              padding: pct ? "3px 15px 3px 6px" : "3px 6px", borderColor,
            }}
          />
          {pct && (
            <span style={{ position: "absolute", right: 6, fontSize: 11, color: colors.muted, pointerEvents: "none" }}>
              %
            </span>
          )}
        </span>
      );
    }

    return (
      <div
        key={f.key}
        style={{
          display: "flex", alignItems: "center", gap: 8, padding: "3px 0",
          opacity: q && !match(f) ? 0.25 : 1,
        }}
      >
        <span
          title={f.desc || f.label}
          style={{
            flex: 1, minWidth: 0, fontSize: 12, color: labelColor,
            whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis",
            fontWeight: ch ? 600 : 400,
          }}
        >
          {ch && (
            <span
              style={{
                display: "inline-block", width: 5, height: 5, borderRadius: "50%",
                background: colors.accent, marginRight: 5, verticalAlign: 1,
              }}
            />
          )}
          {f.label}
        </span>
        {ctrl}
      </div>
    );
  };

  const body = flat
    ? schema.filter(match).map(renderRow)
    : groups.map((g, idx) => {
        const open = openOf(g.name, idx);
        const rows = g.fields.filter(match);
        if (q && !rows.length) return null;
        const nChanged = g.fields.filter((f) => changed.has(f.key)).length;
        return (
          <div
            key={g.name}
            style={{
              border: `1px solid ${colors.border}`, borderRadius: 9, marginBottom: 6,
              overflow: "hidden", background: colors.card,
            }}
          >
            <div
              onClick={() => toggle(g.name, idx)}
              style={{
                display: "flex", alignItems: "center", gap: 7, padding: "7px 9px",
                cursor: "pointer", userSelect: "none",
              }}
            >
              <Tri open={open} color={colors.muted} />
              <span style={{ fontSize: 12.5, fontWeight: 600, color: colors.text }}>{g.name}</span>
              <span style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 5 }}>
                {nChanged > 0 && (
                  <span
                    style={{
                      fontSize: 11, padding: "1px 6px", borderRadius: 99,
                      background: `${colors.accent}1a`, color: colors.accent, fontWeight: 600,
                    }}
                  >
                    {nChanged} 项{grid ? "参与扫描" : "已改"}
                  </span>
                )}
                <span style={{ fontSize: 11, color: colors.muted }}>{g.fields.length}</span>
              </span>
            </div>
            {open && (
              <div style={{ padding: "2px 9px 8px", borderTop: `1px solid ${colors.border}` }}>
                {rows.map(renderRow)}
                {nChanged > 0 && !q && (
                  <div style={{ textAlign: "right", paddingTop: 3 }}>
                    <button onClick={() => resetGroup(g.name)} style={linkBtn}>
                      {grid ? "清空本组取值" : "恢复本组默认"}
                    </button>
                  </div>
                )}
              </div>
            )}
          </div>
        );
      });

  const total = schema.length;
  const nAll = changed.size;

  return (
    <aside
      className="param-panel"
      style={{
        width, flexShrink: 0, position: "sticky", top: 16, alignSelf: "flex-start",
        maxHeight: "calc(100vh - 32px)", display: "flex", flexDirection: "column",
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 8 }}>
        <span style={{ fontSize: 13, fontWeight: 600 }}>{grid ? "参数取值" : "参数配置"}</span>
        <span style={{ fontSize: 11.5, color: colors.muted }}>
          {total} 项{nAll > 0 ? (grid ? ` · 已填 ${nAll}` : ` · 已改 ${nAll}`) : ""}
        </span>
        <button
          onClick={resetAll}
          disabled={nAll === 0}
          title={grid ? "清空全部取值（回到用默认单点扫描）" : "全部恢复为该策略的默认参数"}
          style={{
            marginLeft: "auto", border: 0, background: "transparent",
            color: nAll > 0 ? colors.accent : colors.muted,
            fontSize: 11.5, cursor: nAll > 0 ? "pointer" : "default", padding: "2px 0",
          }}
        >
          {grid ? "清空全部" : "恢复默认"}
        </button>
      </div>

      <input
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        placeholder="搜索参数名 / 口径…"
        style={{ ...inputStyle(colors), fontSize: 12, padding: "5px 9px", marginBottom: 7 }}
      />

      {!flat && (
        <div style={{ display: "flex", gap: 6, marginBottom: 8 }}>
          <select
            value={pickedPreset}
            onChange={(e) => applyPreset(e.target.value)}
            style={{ ...inputStyle(colors), flex: 1, fontSize: 11.5, padding: "4px 6px" }}
          >
            <option value="">预设…</option>
            {presets.map((p) => (
              <option key={p.name} value={p.name}>{p.name}</option>
            ))}
          </select>
          <button
            onClick={() => setSaving((s) => !s)}
            title="把当前整套参数存为预设"
            style={{
              ...inputStyle(colors), width: 30, padding: 0, cursor: "pointer",
              fontSize: 14, lineHeight: 1, color: colors.accent,
            }}
          >
            {saving ? "×" : "+"}
          </button>
          <button
            onClick={delPreset}
            disabled={!pickedPreset}
            title="删除选中的预设"
            style={{
              ...inputStyle(colors), width: 30, padding: 0,
              cursor: pickedPreset ? "pointer" : "default",
              fontSize: 12, color: pickedPreset ? colors.down : colors.muted,
            }}
          >
            −
          </button>
        </div>
      )}

      {saving && (
        <div style={{ display: "flex", gap: 6, marginBottom: 8 }}>
          <input
            autoFocus
            value={presetName}
            onChange={(e) => setPresetName(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") savePreset(); }}
            placeholder="预设名称，如 低波0.2实验组"
            style={{ ...inputStyle(colors), flex: 1, fontSize: 11.5, padding: "4px 7px" }}
          />
          <button
            onClick={savePreset}
            style={{
              ...inputStyle(colors), width: 42, padding: 0, cursor: "pointer",
              fontSize: 11.5, color: colors.accent,
            }}
          >
            保存
          </button>
        </div>
      )}

      <div style={{ overflowY: "auto", flex: 1, paddingRight: 2 }}>{body}</div>
    </aside>
  );
}
