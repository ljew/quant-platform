"""T8 样本内/样本外切分 —— 防回归：评级不得只靠全样本 IC。

问题背景（B3/M3-1，2026-10-08）：
  过去「选因子」与「评因子」在同一段样本上完成 —— IC 全量算完再评级，
  等于用全样本挑标准再报全样本成绩，属**结构性样本内过拟合**。
  这是 GP 被熔断的三条原因之一。

修复：
  `_is_oos_split` 按区间末30% 切出 OOS，输出 `verdict` 四态：
    通过 / 未通过 / 过拟合 / 样本不足
  并在评级层接入门槛：OOS「过拟合」→ 优秀/可用/反向 一律降为「弱」；
  OOS「未通过」→ 优秀降为可用。

设计要点：**零额外成本**。ic_dates 本就带日期，按切点分流即可，
不需要把数据加载与截面循环跑两遍。

本测试不依赖数据库——`_is_oos_split` / `_oos_metrics` 是纯函数。
"""
from __future__ import annotations

import inspect
import math

from app.services.factor_mining import _is_oos_split, _oos_metrics


def _seq(vals: list[float]) -> list[str]:
    return [f"2024-{i // 28 + 1:02d}-{i % 28 + 1:02d}" for i in range(len(vals))]


def _stable(n: int, mu: float, sd: float = 0.02) -> list[float]:
    """构造 n 个围绕 mu 轻微波动的 IC（模拟真实因子：均值稳定、逐期有噪声）。

    刻意不用常量序列 —— 常量意味着 std≈0，t 检验不可用（会被判「样本不足」），
    而真实 IC 是秩相关的连续量，不会全等。用连续正弦扰动而非三值循环，
    否则 std 偏低会让 t 虚高（实测三值循环时 sd=0.02 却给出 t=2.22）。
    """
    return [mu + sd * math.sin(i * 2.399) for i in range(n)]


class TestOOSMetrics:
    def test_basic_stats(self):
        m = _oos_metrics([0.1, 0.2, 0.3, -0.1, 0.15])
        assert m["n"] == 5
        assert abs(m["ic_mean"] - 0.13) < 1e-6
        assert m["win_rate"] == 0.8, "4 正 1 负"

    def test_insufficient_returns_nulls(self):
        """样本 <3 时不得硬凑统计量（否则会给出虚假的 t 值）。"""
        m = _oos_metrics([0.1, 0.2])
        assert m["ic_mean"] is None and m["t_stat"] is None

    def test_zero_std_gives_null_icir(self):
        """零方差（常量序列）→ icir / t 必须是 None，且不得报天文数字。"""
        m = _oos_metrics([0.05] * 6)
        assert m["ic_std"] == 0.0
        assert m["icir"] is None and m["t_stat"] is None, \
            f"零方差不应产生有限 t/icir，实际 {m['icir']} / {m['t_stat']}"


class TestOOSSplit:
    def test_stable_factor_passes(self):
        """全期同向且稳定的因子 → 通过。"""
        ic = _stable(40, 0.06)
        r = _is_oos_split(_seq(ic), ic)
        assert r["verdict"] == "通过", r["note"]

    def test_sign_flip_is_overfit(self):
        """核心断言：IS 正、OOS 负 → 过拟合（这是最危险的形态）。"""
        ic = _stable(35, 0.08) + _stable(15, -0.09)
        r = _is_oos_split(_seq(ic), ic)
        assert r["verdict"] == "过拟合", r["note"]
        assert r["is"]["ic_mean"] > 0 and r["oos"]["ic_mean"] < 0

    def test_weak_oos_t_fails(self):
        """OOS 同向但区分力弱 → 未通过。"""
        ic = _stable(35, 0.07) + _stable(15, 0.002, sd=0.02)
        r = _is_oos_split(_seq(ic), ic)
        assert r["verdict"] == "未通过", r["note"]

    def test_too_few_snapshots(self):
        r = _is_oos_split(_seq([0.05] * 4), _stable(4, 0.05))
        assert r["verdict"] == "样本不足"

    def test_zero_variance_gives_no_verdict(self):
        """全期常量 → std≈0 → t 不可用 → 判「样本不足」而非误判通过。"""
        ic = [0.05] * 40
        r = _is_oos_split(_seq(ic), ic)
        assert r["verdict"] == "样本不足", r["note"]
        assert "零方差" in r["note"]

    def test_both_sides_have_min_3(self):
        ic = _stable(40, 0.06)
        r = _is_oos_split(_seq(ic), ic)
        assert r["is"]["n"] >= 3, "IS 至少 3 个截面"
        assert r["oos"]["n"] >= 3, "OOS 至少 3 个截面"

    def test_ratio_roughly_respected(self):
        ic = _stable(100, 0.06)
        r = _is_oos_split(_seq(ic), ic, oos_ratio=0.3)
        assert abs(r["oos"]["n"] / 100 - 0.3) < 0.06, \
            f"OOS 占比 {r['oos']['n']}/100 偏离目标 0.3"

    def test_split_date_recorded(self):
        r = _is_oos_split(_seq(_stable(40, 0.06)), _stable(40, 0.06))
        assert r["split_date"] is not None
        assert r["oos_start"] is not None

    def test_no_data_leakage_in_split(self):
        """切分必须按**时间顺序**：IS 在前、OOS 在后，不能随机打乱。"""
        ic = _stable(35, 0.08) + _stable(15, -0.09)
        r = _is_oos_split(_seq(ic), ic)
        assert r["split_date"] < r["oos_start"], "IS 切点应早于 OOS 起点"


class TestRatingGate:
    """OOS 判定必须真正影响评级，而不只是记录在报告里。"""

    def test_mine_factor_accepts_oos_ratio(self):
        from app.services.factor_mining import mine_factor
        sig = inspect.signature(mine_factor)
        assert "oos_ratio" in sig.parameters, "mine_factor 应暴露 oos_ratio 参数"
        assert sig.parameters["oos_ratio"].default == 0.3

    def test_result_contains_oos_block(self):
        from app.services.factor_mining import mine_factor
        src = inspect.getsource(mine_factor)
        assert '"oos": oos_report' in src, "返回体应含oos 字段"

    def test_demotion_logic_present(self):
        """评级层必须接入 OOS 门槛：过拟合 → 降级。"""
        from app.services.factor_mining import mine_factor
        src = inspect.getsource(mine_factor)
        assert 'oos_verdict == "过拟合"' in src, "缺少 OOS 过拟合降级逻辑"
        assert "demoted_from" in src, "降级应记录原评级"

    def test_disabled_when_ratio_zero(self):
        from app.services.factor_mining import mine_factor
        src = inspect.getsource(mine_factor)
        assert "oos_ratio > 0" in src, "oos_ratio=0 应关闭切分"
