/**
 * @quant/quant-ui 浏览器侧：量化平台对 dsh 前端的定制层。
 *
 * 设计文档《智能体架构设计》08 节把组件分三类：
 *   dsh 原生 —— 会话列表、消息流、审批、定时、持久化
 *   需自绘   —— 工具卡、证据卡、缺口标黄、产物栏、四段结构提示
 *   平台提供 —— 数据本身与跳转目标页
 *
 * 这一层只做「需自绘」里能落地的部分，不动对话内核。
 * 插槽契约（实测）：
 *   · single 槽（无 id）—— 已有注册时必须给 priority，值小的渲染
 *   · list  槽（有 id + order）—— 可多方并存，按 order 排
 * 参数写错会在控制台抛 "keyed slot ... requires options.key" 之类，不会静默。
 */
window.__ModuleLoader__.load({
	id: "@quant/quant-ui",
	factory: (require) => {
		var module = { exports: {} };
		var exports = module.exports;
		const react = require("react");
		const h = react.createElement;

		// uiWorkspace 提供 startSession(workspaceId, {agentPreset, prompt}) ——
		// 这是 dsh 里「新建会话并绑定预设」的官方入口（内置的预设切换器就用它）。
		const inject = ["slots", "uiWorkspace"];

		/** 左栏品牌位：把 dsh 的 HARNESS 换成平台标识。
		 *  该槽是 single，官方品牌插件已占 priority 0 —— 必须给更小的值才能顶替。 */
		function BrandName() {
			return h(
				"span",
				{
					style: {
						fontWeight: 700,
						fontSize: 13.5,
						letterSpacing: 1,
						whiteSpace: "nowrap",
					},
					title: "量化投研平台 · 智能体",
				},
				"QUANT·DESK"
			);
		}

		/** 角色定义：身份 + 纪律 + 示例问题。
		 *
		 *  为什么放在 dsh 的输入框上方，而不是平台页面里：问问题的地方就在输入框，
		 *  把「选谁」放在离它三步远的地方（上一版的做法）等于没有。
		 *  纪律是软约束，真正的硬边界在 preset 的 ctx.tools.restrict 白名单里。
		 */
		const ROLES = [
			{
				key: "strategy", preset: "strategy-researcher", name: "策略研究员", hint: "这条策略还值不值得跟",
				q: "jk001r 最近两次回测的超额、最大回撤、成交笔数分别是多少？区间是什么？",
				rule: "你负责判断「一条策略值不值得继续跟」。只看绝对收益不够，必须比基准；一段区间好不算好，要看不同市场环境。成交笔数少于 5 的回测没有统计意义，|t|<2 只能说「无法判别」。",
			},
			{
				key: "risk", preset: "risk", name: "风控", hint: "要不要降级/退役，最坏会怎样",
				q: "现在有哪些策略该降级或退役？给出最坏情况和判断依据。",
				rule: "你负责判断升级/降级/退役，必须同时给出最坏情况。给不出最坏情况时明说，不要只讲优点。",
			},
			{
				key: "data", preset: "data-eng", name: "数据工程师", hint: "数据能不能信",
				q: "今天的数据能不能用来做决策？有哪些表滞后了？",
				rule: "你只判断「数据能不能信」。滞后要按当前时刻判断（交易日 19:00 前允许 1 天滞后），不要一刀切。",
			},
			{
				key: "news", preset: "news", name: "资讯研究员", hint: "今天市场在交易什么",
				q: "今天市场在交易什么？主要主线是什么？",
				rule: "你只回答「今天市场在交易什么」。区分快讯和观点；券商研报不是 alpha，只作参考。",
			},
			{
				key: "ops", preset: "ops", name: "运维", hint: "服务活着吗、任务按时跑了吗",
				q: "服务都正常吗？有哪些任务没按时跑？",
				rule: "你只回答「服务活着吗、任务按时跑了吗」。报状态要带具体指标，不要笼统说「正常」。",
			},
			{
				key: "factor", preset: "factor", name: "因子研究员", hint: "这个信号有没有预测力",
				q: "chan_buy 这个信号最近 90 天的预测力怎么样？t 值和样本量是多少？",
				rule: "你只判断「一个信号有没有预测力」。|t|<2 只能写「无法判别」，不许下结论；必须给样本量。",
			},
			{
				key: "portfolio", preset: "portfolio", name: "组合经理", hint: "几个策略怎么配、上多少钱",
				q: "jk001 和 xq_chan_buy 怎么配比？给个建议和理由。",
				rule: "你负责「几个策略怎么配、上多少钱」。只给建议，不执行 —— 建盘这类写操作必须走审批，由人确认。",
			},
			{
				key: "trader", preset: "trader", name: "交易员", hint: "模拟盘执行得怎么样",
				q: "模拟盘现在持仓几只？净值跑到哪一天了？",
				rule: "你只管「模拟盘执行情况」，不能下单、不能改参数。净值要说明算到哪一天。",
			},
		];

		const COMMON = [
			"【硬约束】你看到的每个数字都必须来自工具返回，禁止自己计算或估算。",
			"工具返回空或失败时，必须明确说「没查到 X」并说明原因，不许用「一般来说」补齐。",
			"每个结论后面要能指出出处（接口名 / 回测 id / 区间 / as_of）。",
			"回答按四段写：结论 / 证据（数值+区间+口径）/ 没查到 / 建议动作。缺哪段就明写「这一段我没有依据」。",
		].join("\n");

		/** 把开场白写进 dsh 的输入框（异步，因为要等 Lexical 同步选区）。
		 *
		 *  dsh 的输入框是 Lexical 富文本，不是 textarea。每个写法的行为都实测过
		 *  （2026-11-11，反复试到能用为止）：
		 *   · innerHTML / textContent —— 只改 DOM，React 受控状态收不到，下次渲染就盖回去
		 *   · execCommand('insertText') —— 认选区（能替换），但**换行全被吃掉**
		 *   · execCommand('insertParagraph' / 'insertHTML') —— Lexical 不响应
		 *   · execCommand('selectAll' / 'delete') —— 同上，清不掉
		 *   · 合成 paste（text/plain）—— 换行能保住，但只往光标处插，不替换选区
		 *
		 *  两个能力各一半，所以组合：先 insertText 顶一个零宽空格把旧内容替换掉，
		 *  再 paste 真文本。零宽字符看不见，不影响阅读。
		 *
		 *  ⚠️ 关键：**改完选区必须等一拍**。直接 range.selectNodeContents() 紧接着
		 *  execCommand，Lexical 内部的 selection 还没同步 —— execCommand 返回 true
		 *  但内容纹丝不动（踩了很久才发现）。等 ~150ms 让 selectionchange 传进去，
		 *  同一句就以「替换整个内容」生效了。
		 *
		 *  另外 lexical 不能 require（loader 的模块表里没有，会报 client-modules
		 *  missed the module table），所以只能走 DOM 事件这条路。
		 */
		const wait = (ms) => new Promise((r) => setTimeout(r, ms));

		async function fillComposer(text) {
			const el = document.querySelector("[contenteditable]");
			if (!el) return false;

			el.focus();
			await wait(120);

			const sel = window.getSelection();
			const range = document.createRange();
			range.selectNodeContents(el);
			sel.removeAllRanges();
			sel.addRange(range);
			await wait(160); // 等 Lexical 收到 selectionchange，否则下面那句是空转

			try {
				document.execCommand("insertText", false, "\u200b");
			} catch (e) {
				/* 没有 execCommand 就跳过，下面的 paste 至少能追加内容 */
			}
			await wait(60);

			try {
				const dt = new DataTransfer();
				dt.setData("text/plain", text);
				el.dispatchEvent(
					new ClipboardEvent("paste", {
						clipboardData: dt,
						bubbles: true,
						cancelable: true,
					})
				);
			} catch (e) {
				/* 合成 paste 不可用时退化成 insertText：内容对，换行会丢 */
				const r2 = document.createRange();
				r2.selectNodeContents(el);
				sel.removeAllRanges();
				sel.addRange(r2);
				await wait(160);
				document.execCommand("insertText", false, text);
			}

			await wait(120);
			return !!(el.innerText || "").trim();
		}

		/** 把「身份 + 纪律 + 硬约束 + 示例问题」拼成开场白。 */
		function rolePrompt(r) {
			return (
				"【角色】" + r.name + " —— " + r.hint + "\n" +
				r.rule + "\n\n" + COMMON + "\n\n" + r.q
			);
		}

		/** 输入框上方的角色条（list 槽，id + order 并存多方）。
		 *
		 *  ⚠️ 2026-11-11 改：点角色不再只是「填一段话」，而是**换掉这个会话的 preset**。
		 *
		 *  背景：我原来以为 dsh 只让选内置的四个运行模式、自定义 persona 进不了界面，
		 *  于是退而求其次用开场白做软约束。查下去发现根本不是 UI 的问题 ——
		 *  是我给 persona 配的 tool-guard 在 apply 阶段就调 ctx.tools.restrict()，
		 *  而 MCP 工具是异步连上的，那一刻名单里的名字全是「未知工具」，
		 *  restrict 直接抛 → preset 加载失败（agent-preset/invalid）。
		 *  也就是说这 8 个角色**从来没生效过**，不只是选不到。
		 *
		 *  修好 tool-guard 之后，preset 能加载了，而且 dsh 暴露了
		 *  uiWorkspace.startSession(workspaceId, {agentPreset, prompt}) ——
		 *  内置的预设切换器用的就是它。所以现在点角色 = 开一个绑定该 persona 的
		 *  新会话，并把开场白预填进去：系统提示词、工具白名单、开场白一起生效。
		 */
		/** 问题关键词 → 该由谁接（零 token 的规则路由）。
		 *
		 *  设计文档 09 节：「不列 8 个数字人让人选，**选人由编排器决定**」。
		 *  完整的 ask 工作流（多 persona 并行取证）还没做，这里先落最轻的一半：
		 *  用户打字时实时判断「这问题该问谁」，给一个一键切换的推荐。
		 *  不拦截发送 —— 用户直接回车仍走通用助手（14 个工具全开，P0 已把
		 *  提示词改成投研助手），推荐只是把更专业的那条路摆在手边。
		 *
		 *  为什么用规则不用模型：这层要可复现、零成本、不因为 API 抖动停摆
		 *  （设计文档 05 节对编排层的三条要求）。关键词表跟着 persona 的 hint 走，
		 *  改 persona 时一起改。
		 */
		const ROUTE_WORDS = {
			strategy: ["策略", "回测", "超额", "回撤", "夏普", "胜率", "值不值得",
				"jk", "chan", "缠论", "海龟", "双参数", "rps", "突破", "定增"],
			risk: ["降级", "退役", "风险", "最坏", "止损", "仓位上限", "该不该停"],
			data: ["数据", "滞后", "哪张表", "表新", "齐不齐", "etl", "采集", "因子截面"],
			news: ["市场在交易", "主线", "资讯", "新闻", "消息面", "热点", "发生了什么"],
			ops: ["服务", "任务", "漏跑", "跑了没", "告警", "调度", "容器", "挂了"],
			factor: ["因子", "ic", "预测力", "t 值", "t值", "有效性", "信号质量", "衰减"],
			portfolio: ["配比", "组合", "怎么配", "上多少", "资金", "权重"],
			trader: ["模拟盘", "持仓", "净值", "成交", "换手", "建盘"],
		};

		/** 一句话 → 最匹配的角色；一个词都不命中就返回 null（走通用助手）。 */
		function routeText(text) {
			const s = String(text || "").toLowerCase();
			if (s.trim().length < 3) return null;
			let best = null;
			let bestN = 0;
			for (const [key, words] of Object.entries(ROUTE_WORDS)) {
				let n = 0;
				for (const w of words) if (s.indexOf(w) >= 0) n += 1;
				if (n > bestN) {
					bestN = n;
					best = key;
				}
			}
			return best ? ROLES.find((r) => r.key === best) || null : null;
		}

		function RoleDock({ uiWorkspace }) {
			const [picked, setPicked] = react.useState(null);
			const [note, setNote] = react.useState("");
			// 8 个角色默认收起：默认形态是「给第一句」（见下面 head 的注释）
			const [open, setOpen] = react.useState(false);
			// 输入框里的草稿（用于实时推荐角色）——dsh 的 composer 是 Lexical，
			// 只能从 DOM 读；input 和 keyup 都挂，Lexical 有时只发其中一个。
			const [draft, setDraft] = react.useState("");

			react.useEffect(() => {
				const read = () => {
					const el = document.querySelector("[data-composer-input]");
					setDraft(el ? el.innerText || "" : "");
				};
				read();
				document.addEventListener("input", read, true);
				document.addEventListener("keyup", read, true);
				return () => {
					document.removeEventListener("input", read, true);
					document.removeEventListener("keyup", read, true);
				};
			}, []);

			const suggest = picked ? null : routeText(draft);

			const base = {
				border: "1px solid var(--dsh-border, rgba(128,128,128,.28))",
				borderRadius: 14,
				padding: "3px 10px",
				fontSize: 11.5,
				cursor: "pointer",
				background: "transparent",
				color: "inherit",
				lineHeight: 1.7,
				fontFamily: "inherit",
			};

			const pick = async (r) => {
				setPicked(r);
				const text = rolePrompt(r);

				// 首选：换 preset（真正的身份 + 工具边界）
				const uw = uiWorkspace;
				if (uw && typeof uw.startSession === "function") {
					try {
						uw.startSession(undefined, {
							agentPreset: r.preset,
							// 只预填「问题」——身份与纪律已经由 preset 的
							// agent-instructions 常驻了，再把那一大段塞进输入框
							// 既重复又难改。输入框留给真正要问的话。
							prompt: r.q,
							clearPreviousDraft: true,
						});
						setNote("已切到「" + r.name + "」，开场白在输入框里，按回车开始");
						return;
					} catch (e) {
						/* 落下去走兜底 */
					}
				}
				// 兜底：至少把开场白塞进输入框
				const ok = await fillComposer(text);
				setNote(
					ok
						? "已填入开场白（预设没切成，角色纪律只在文字里，工具边界仍是默认）"
						: "没找到输入框，等它加载完再点"
				);
			};

			const chips = ROLES.map((r) => {
				const on = picked && picked.key === r.key;
				return h(
					"button",
					{
						key: r.key,
						title: r.hint + "（点一下开新对话并切到该角色）",
						onClick: () => void pick(r),
						style: on
							? Object.assign({}, base, {
									borderColor: "var(--dsh-accent, #4c7dff)",
									background: "rgba(76,125,255,.10)",
									color: "var(--dsh-accent, #4c7dff)",
									fontWeight: 600,
							  })
							: base,
					},
					r.name
				);
			});

			// ── 默认形态：给第一句，不列 8 个人 ──────────────────────────
			// 设计文档 09 节「空会话」写得很硬：「不列 8 个数字人让人选，
			// 选人由编排器决定」。上一版把 8 个 chip 常驻在输入框上方，
			// 等于把调度的活推给人 —— 老刘的原话是「不知道怎么选研究员」。
			// 现在默认只给三个能直接开始的问题（各自绑定对应 persona），
			// 想手动指定再展开。
			const starter = (key, q) => {
				const r = ROLES.find((x) => x.key === key);
				return h(
					"button",
					{
						key: "st-" + key,
						title: "点一下：开新对话（" + r.name + "）并把问题填进输入框，回车发送",
						onClick: () => void pick(Object.assign({}, r, { q: q })),
						style: Object.assign({}, base, {
							borderStyle: "dashed",
							opacity: picked ? 0.45 : 0.95,
						}),
					},
					q.length > 14 ? q.slice(0, 13) + "…" : q
				);
			};

			// 三种形态：已选角色 / 有草稿且能判断该问谁 → 推荐一键切换 / 空态给三个示例
			const body = [];
			if (picked) {
				// 标签已经显示「已选：X」，这里不再放东西
			} else if (suggest && draft.trim().length >= 3) {
				body.push(
					h(
						"button",
						{
							key: "__sug",
							title:
								"按关键词判断，这类问题更适合「" + suggest.name + "」（" +
								suggest.hint + "）。点一下：带着你刚写的问题切过去",
							onClick: () =>
								void pick(Object.assign({}, suggest, { q: draft.trim().slice(0, 800) })),
							style: Object.assign({}, base, {
								borderColor: "var(--dsh-accent, #4c7dff)",
								background: "rgba(76,125,255,.10)",
								color: "var(--dsh-accent, #4c7dff)",
								fontWeight: 600,
							}),
						},
						"→ 推荐：" + suggest.name
					)
				);
			} else {
				body.push(
					starter("strategy", "缠论还值不值得跟"),
					starter("data", "今天数据齐吗"),
					starter("ops", "服务正常吗、有任务漏跑吗")
				);
			}

			const head = h(
				"div",
				{
					key: "__head",
					style: {
						display: "flex", flexWrap: "wrap", alignItems: "center",
						gap: 6, padding: "2px 2px 6px", fontSize: 11.5,
					},
				},
				h("span", { key: "__lb", style: { opacity: 0.55, marginRight: 2 } },
					picked ? "已选：" + picked.name : body.length && suggest ? "判断：" : "试试："),
				body,
				h(
					"button",
					{
						key: "__more",
						onClick: () => setOpen((v) => !v),
						title: "手动指定角色（不指定就走通用助手，14 个工具全开）",
						style: Object.assign({}, base, {
							border: 0, opacity: 0.6, textDecoration: "underline",
							marginLeft: "auto", padding: "3px 6px",
						}),
					},
					open ? "收起 ▴" : "手动指定 ▾"
				)
			);

			const out = [head];
			if (open) {
				out.push(
					h(
						"div",
						{
							key: "__chips",
							style: {
								display: "flex", flexWrap: "wrap", alignItems: "center",
								gap: 6, padding: "0 2px 6px", fontSize: 11.5,
							},
						},
						h("span", { key: "__lb2", style: { opacity: 0.5, marginRight: 2 } },
							"角色："),
						chips
					)
				);
			}
			if (note) {
				out.push(
					h(
						"div",
						{
							key: "__note",
							style: { fontSize: 11, opacity: 0.65, padding: "0 2px 4px" },
						},
						note
					)
				);
			}
			return h("div", { style: { padding: "0 14px" } }, out);
		}

		/* ───────────────── 工具卡（设计文档 08 节「需自绘」最后一块）─────────────────
		 *
		 * dsh 默认把 MCP 工具调用渲染成「一行 + 可展开的原始 JSON」—— 那是给写代码的人看的。
		 * 投研场景要的是：这次查了什么、拿到几个数、**缺了什么**、来源和时点。
		 *
		 * 插槽契约：`tool.call.toolview` 是 keyed 槽，**key 就是工具名**
		 * （ui-tool 里 `entryKey: toolName`），命中就替换掉默认卡片。
		 * 组件拿到的 props（读 ui-tool 源码确认）：
		 *   toolName / block / sessionId / useSessions / inspect / useDisclosure / t
		 * block 的两种形态：
		 *   运行中 { phase: "start"|"preparing"|"running", argsRaw }，没有 kind
		 *   已完成 { kind, call:{argsRaw}, args, content:[{type:'text',text}], error, meta, isError }
		 * 结果文本在 content[0].text —— quant-mcp 返回的是 JSON 字符串。
		 */

		/** 工具名 → 中文标题。只收录平台自己的 11 个，其余走默认卡片。 */
		const TOOL_CN = {
			list_strategies: "策略清单",
			get_backtests: "回测记录",
			get_signal_stats: "信号有效性",
			get_signal_actions: "当日信号动作",
			list_research_cards: "研究卡列表",
			get_research_card: "研究卡详情",
			list_paper_tasks: "模拟盘任务",
			get_framework_health: "框架体检",
			get_latest_digest: "最新日报",
			propose_card_transition: "提议改卡状态",
			create_paper_task: "建模拟盘",
		};

		/** `mcp__quant__get_backtests` → `get_backtests`；不是 quant 工具返回 null。 */
		function quantToolName(name) {
			if (typeof name !== "string") return null;
			const m = /^mcp__quant__(.+)$/.exec(name);
			return m ? m[1] : null;
		}

		function parseJson(text) {
			try {
				return JSON.parse(text);
			} catch (e) {
				return null;
			}
		}

		/** 把任意值压成一行可读文本。 */
		function flat(v) {
			if (v === null || v === undefined) return "—";
			if (typeof v === "number") return Number.isInteger(v) ? String(v) : v.toFixed(2);
			if (typeof v === "boolean") return v ? "是" : "否";
			if (typeof v === "string") return v;
			if (Array.isArray(v)) return `[${v.length}]`;
			return "{…}";
		}

		/** 对象 → 前 n 个标量字段的 "k: v" 串；数组取第一条样例。 */
		function preview(obj, n) {
			if (!obj || typeof obj !== "object") return [];
			const rows = [];
			for (const k of Object.keys(obj)) {
				const v = obj[k];
				// 一层 dict 展开成 `NEW=49 · WATCH=5`：n_by_action / n_by_picker
				// 这类计数最关键，直接跳过等于把答案藏起来。
				if (v && typeof v === "object" && !Array.isArray(v)) {
					const inner = Object.entries(v)
						.filter(([, vv]) => vv === null || typeof vv !== "object")
						.slice(0, 6)
						.map(([kk, vv]) => `${kk}=${flat(vv)}`)
						.join(" · ");
					if (inner) rows.push([k, inner]);
					if (rows.length >= n) break;
					continue;
				}
				if (Array.isArray(v)) {
					rows.push([k, `${v.length} 条`]);
					if (v.length && v[0] && typeof v[0] === "object") {
						const first = Object.entries(v[0])
							.filter(([, vv]) => vv === null || typeof vv !== "object")
							.slice(0, 4)
							.map(([kk, vv]) => `${kk}=${flat(vv)}`)
							.join(" · ");
						if (first) rows.push(["  └ 如", first]);
					}
				} else {
					rows.push([k, flat(v)]);
				}
				if (rows.length >= n) break;
			}
			return rows;
		}

		const C = {
			border: "var(--dsh-border, rgba(128,128,128,.26))",
			ok: "#16a34a",
			bad: "#dc2626",
			warn: "#d97706",
			accent: "#4c7dff",
		};

		/** quant 工具卡：一行摘要 + 展开看数据、缺口、来源。 */
		function QuantToolCard({ toolName, block }) {
			const [open, setOpen] = react.useState(false);
			const short = quantToolName(toolName) || toolName;
			const title = TOOL_CN[short] || short;

			const done = block && "kind" in block;
			const rawArgs = done
				? (block.call && block.call.argsRaw) || ""
				: (block && block.argsRaw) || "";
			// ⚠️ 优先解析 argsRaw，不要用 block.args —— 已完成的 block 里
			// block.args 存的是流式元数据（{chunks:[1], sealed:true}），
			// 真正的调用参数只有 call.argsRaw 那份 JSON 才对。
			const args = parseJson(rawArgs) || (block && block.args) || {};
			const argLine = Object.entries(args)
				.filter(([, v]) => v !== null && v !== undefined && v !== "")
				.map(([k, v]) => `${k}=${flat(v)}`)
				.join(" · ");

			const text = done && Array.isArray(block.content) && block.content[0]
				? block.content[0].text || ""
				: "";
			const payload = done ? parseJson(text) : null;
			const failed = done && (block.isError || (payload && payload.ok === false) || block.error);
			const missing = (payload && payload.missing) || [];
			const tone = failed ? C.bad : missing.length ? C.warn : C.ok;

			const head = react.createElement(
				"div",
				{
					onClick: () => setOpen((v) => !v),
					style: {
						display: "flex", alignItems: "center", gap: 8,
						padding: "5px 10px", cursor: "pointer", fontSize: 12.5,
						borderRadius: 8, border: "1px solid " + C.border,
						// 淡底色：dsh 默认的工具行是纯文本，容易跟正文糊在一起；
						// 给一层极浅的底，扫读时能一眼分出「这是查数、那是结论」。
						background: "rgba(128,128,128,.06)",
					},
				},
				react.createElement("span", {
					style: { width: 6, height: 6, borderRadius: 99, background: tone, flexShrink: 0 },
				}),
				react.createElement("span", { style: { fontWeight: 600 } }, title),
				react.createElement(
					"span",
					{ style: { opacity: 0.5, fontSize: 11 } },
					done ? (failed ? "失败" : missing.length ? "有缺口" : "完成") : "运行中…"
				),
				argLine
					? react.createElement(
							"span",
							{
								style: {
									opacity: 0.55, fontSize: 11, flex: 1, minWidth: 0,
									overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
								},
								title: argLine,
							},
							argLine
					  )
					: react.createElement("span", { style: { flex: 1 } }),
				react.createElement("span", { style: { opacity: 0.4, fontSize: 11 } },
					open ? "收起" : "展开")
			);

			if (!open) return head;

			const body = [];
			if (failed) {
				body.push(react.createElement("div", {
					key: "err",
					style: { color: C.bad, fontSize: 12, marginTop: 6, lineHeight: 1.7 },
				}, "× " + ((payload && payload.error) || (block.error && block.error.message) || "调用失败")));
			}
			// 缺口标黄 —— 设计文档 08 节那条：缺哪段就明写，不许用「一般来说」补
			if (missing.length) {
				body.push(react.createElement("div", {
					key: "miss",
					style: {
						marginTop: 6, padding: "6px 9px", borderRadius: 6, fontSize: 11.5,
						lineHeight: 1.7, color: C.warn,
						border: "1px solid " + C.warn + "55", background: C.warn + "12",
					},
				}, "⚠ 没拿到：" + missing.join("；")));
			}
			if (payload && payload.data !== undefined) {
				const rows = preview(payload.data, 8);
				if (rows.length) {
					body.push(react.createElement("div", {
						key: "data",
						style: { marginTop: 6, fontSize: 11.5, lineHeight: 1.85 },
					}, rows.map(([k, v], i) => react.createElement("div", {
						key: i,
						style: { display: "flex", gap: 8 },
					}, [
						react.createElement("span", {
							key: "k", style: { opacity: 0.55, minWidth: 96, flexShrink: 0 },
						}, k),
						react.createElement("span", {
							key: "v", style: { wordBreak: "break-all" },
						}, v),
					]))));
				}
			} else if (text) {
				body.push(react.createElement("pre", {
					key: "raw",
					style: {
						marginTop: 6, fontSize: 11, maxHeight: 180, overflow: "auto",
						whiteSpace: "pre-wrap", wordBreak: "break-all", opacity: 0.8,
					},
				}, text.slice(0, 2000)));
			}
			if (payload && (payload.source || payload.as_of)) {
				body.push(react.createElement("div", {
					key: "src",
					style: { marginTop: 6, fontSize: 10.5, opacity: 0.45 },
				}, [payload.source, payload.as_of, payload.note]
					.filter(Boolean)
					.join(" · ")
					.slice(0, 220)));
			}

			return react.createElement("div", null, [head, ...body]);
		}

		/* dsh 里模型看到的工具名可能是带前缀的 mcp__quant__x，也可能被剥成 x。
		 * 两种都注册一份，命中哪个都能用上。 */
		function quantToolKeys() {
			const keys = [];
			for (const k of Object.keys(TOOL_CN)) {
				keys.push("mcp__quant__" + k);
				keys.push(k);
			}
			return keys;
		}

		function apply(ctx) {
			ctx.slots.inject("sidebar.brand.name", function* () {
				yield ctx.slots.register(
					{ name: "sidebar.brand.name", priority: -1 },
					BrandName
				);
			});
			ctx.slots.inject("conversation.input.dock", function* () {
				// 把 uiWorkspace 直接塞进去：点角色要调 startSession 换 preset
				const uw = ctx.get("uiWorkspace");
				const Bound = (props) => react.createElement(RoleDock, { ...props, uiWorkspace: uw });
				yield ctx.slots.register(
					{ name: "conversation.input.dock", id: "quant-roles", order: -10 },
					Bound
				);
			});
			// 工具卡：keyed 槽，key = 工具名。11 个 quant 工具各注册一份，
			// 命中就替换掉 dsh 默认的「原始 JSON」卡片。
			ctx.slots.inject("tool.call.toolview", function* () {
				for (const key of quantToolKeys()) {
					yield ctx.slots.register(
						{ name: "tool.call.toolview", key },
						QuantToolCard
					);
				}
			});
		}

		exports.apply = apply;
		exports.inject = inject;
		return module.exports;
	},
});
