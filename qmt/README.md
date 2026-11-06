# QMT 终端策略包

> 这是 `fresh_ma` 策略在券商终端里跑的完整实现。
> 设计依据：`../docs/GO-LIVE-PLAN.md`（为什么必须搬到终端里）
> 平台侧对应实现：`../backend/app/live/`（决策/风控/对账，已完成）

## 一句话

**策略内核与平台在全市场范围内选股完全一致**（5209 只 × 5 个交易日逐日比对通过），
但它跑在 QMT 沙箱里——不联网、不读文件，状态存在券商端 KV。

## 文件

| 文件 | 是什么 | 能改吗 |
|---|---|---|
| `fresh_ma_qmt.py` | **最终交付物**，单文件 1333 行，直接贴进 QMT | 只改顶部 `ACCOUNT` |
| `chanlun_py36.py` | 缠论引擎（3.6 兼容版，算法零改动） | 不改 |
| `core.py` | 策略内核：选股 / 门控 / 风控 / 订单 | 不改 |
| `strategy.py` | QMT 入口：取数 / 报单 / 状态 | 改配置区 |
| `bootstrap_exc.py` | 门控引导数据（95 个成熟信号日超额） | 平台重导时更新 |
| `build_single.py` | 把上面四个合成单文件 | — |
| `verify_parity.py` | 选股一致性验证 | — |
| `mock_qmt.py` | 本地模拟 QMT（无需账号） | — |
| `watchdog/` | **Windows 看门狗**：进程/心跳/结果三级监控 + 自动重启 + 告警 | 见下 |

## 看门狗（`watchdog/`）

策略在沙箱里卡死时外面看不见，看门狗跑在 QMT 外面替它盯着：
L1 进程 → L2 心跳 → L3 结果，按规则重启或告警。
部署与运维见 **`../docs/WINDOWS-WATCHDOG.md`**。

```bash
cd qmt/watchdog && python3 selftest.py     # 53 项逻辑自检，Mac 上就能跑
python3 scripts/export_calendar.py         # 导出交易日历给它用（跨年前重跑）
```

策略侧为它留的抓手：`heartbeat()` 每 15 分钟打一行 `[hb]`，
放在 handlebar 最前面（当日去重 return 之前），当天跑完也继续打。

## 部署

1. **合成**（改完源码后）
   ```bash
   python qmt/build_single.py      # → qmt/fresh_ma_qmt.py
   ```

2. **贴进 QMT**
   客户端 → 策略编辑器 → 新建 Python 策略 → 全选粘贴 → 改 `ACCOUNT` 为资金账号

3. **先模拟盘**，观察日志出现：
   ```
   [data] 有效标的 5209 只 · 20261028
   [gate] 首次运行，载入引导超额 95 条
   [order] BUY 600123.SH 3100 @ 6.39
   [done] 门控=开 · 候选 20 · 买 19 · 卖 0
   ```

4. **报备通过后**再切实盘。

## 三条必测（mock 测不了）

| 测什么 | 怎么算通过 | 不通过的后果 |
|---|---|---|
| `handlebar` 触发时点 | 日志里出现当天的 `[done]` | 策略静默空跑，一笔不下 |
| `passorder` 参数 | 委托真的出现在 QMT 委托列表 | 日志有 `[order]` 但券商没收到 |
| `save_py_key` 读写 | 重启策略后 `fm_last` 还在 | streak/门控丢失，选股跑偏 |

这三条只能在你自己的券商版本上验证，**别跳过**。

## 本地验证（不需要 QMT）

```bash
# 算法等价性：与平台逐日比对（默认最近 5 个交易日、全市场）
python qmt/verify_parity.py --days 5
# → ✅ chan_buy 一致 5/5 天 · ✅ high_tight_flag 一致 5/5 天

# 入口层：mock 跑通取数→候选→风控→订单
python qmt/mock_qmt.py --limit 300
```

## 部署后必改的三处

| 项 | 现值 | 说明 |
|---|---|---|
| `ACCOUNT` | `YOUR_ACCOUNT_ID` | 资金账号 |
| `RUN_AFTER` | `14:50` | 执行时点。日线周期下无效（收盘才触发），分钟周期下生效 |
| `ENABLED` | `chan_buy, high_tight_flag` | 与研究卡档位对应。卡片降级后要同步改这里 |

## 已知限制

- **门控是冻结的**：引导数据 + 每日追加，但 `daily_exc` 的追加逻辑需要「T+10 窗口走完」才写入，QMT 里没有这个回算能力——所以实盘运行时 `fm_exc` 不会自动增长。**每季度用 `verify_parity` 同款方式在平台重导一次** `bootstrap_exc.py`。
- **没有止损状态回填**：`fm_pos` 里的 `entry_price/entry_date/peak` 由策略自己维护，首次接管已有持仓时是空的（`entry_price=0`），这些票会被跳过止损判定。**第一次部署请空仓开始**，不要接管存量持仓。
- **失败不重试**：`passorder` 报单失败只打日志，不重试。日频策略有次日窗口，可以接受，但要知道。

## 口径备忘（移植时踩过的）

| 坑 | 事实 |
|---|---|
| 代码格式 | QMT 用 `600000.SH`，平台用 `sh600000`。混用会把股票池滤成 0 只 |
| 复权 | 取数用 `dividend_type='back'`；**下单必须用 `'none'`**，用复权价会挂出天价单 |
| 窗口 | 按**交易日**截 260 天，不能按行数——停牌股会错位一天，缠论结果全变 |
| 复权因子缺失 | 平台用 `join`（内连接），缺因子的行整行丢弃。用 `left join` 会多一行历史 |
| 冷启动 | KV 为空 → 门控永远关闭 → 永不建仓，必须有 `BOOTSTRAP_EXC` |
| DataFrame 取值 | `row[1]` 是标签索引不是位置索引；`df.values[-1][-1]` 拿到的是 volume 不是 close |
