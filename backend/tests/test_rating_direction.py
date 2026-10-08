"""T2 因子评级方向 —— 防回归：负 IC 因子不得被判为「可用/优秀」。

Bug 背景（M1-2，2026-10-08）：factor_mining.py 的评级函数对 t_stat 与 ic_mean
都取了 abs()，抹掉方向。实测 37 个已挖因子中 26 个负 IC，其中 7 个负 IC 被标
「可用/优秀」；factor_registry 的 enabled 记录即由该函数选出，等于把反向信号
当正向建仓。修复后负 IC 显著者单列「反向」评级。
"""
from __future__ import annotations

import math


def _rating(ic_mean: float, t_stat: float, mono: float = 0.7,
            max_corr: float = 0.3, n_tested: int = 2) -> str:
    """复刻修复后的评级逻辑（与 factor_mining.py:554-575 保持一致）。

    独立实现而非直接调函数：mine_factor 需要 DB 上下文，单元测试直接跑不起。
    若修改主实现，此处需同步——T2.5 会做一致性校验。
    """
    luck_bar = math.sqrt(2 * math.log(max(n_tested, 2)))
    t_excellent = max(2.0, luck_bar + 0.3)
    t_usable = max(1.5, luck_bar - 0.3)
    has_pos = ic_mean > 0
    if (has_pos and t_stat >= t_excellent and ic_mean > 0.02
            and mono >= 0.6 and abs(max_corr) < 0.8):
        return "优秀"
    elif has_pos and t_stat >= t_usable and ic_mean > 0.01:
        return "可用"
    elif (not has_pos) and abs(t_stat) >= t_usable and abs(ic_mean) > 0.01:
        return "反向"
    elif abs(ic_mean) > 0.005:
        return "弱"
    else:
        return "无效"


class TestRatingDirection:
    def test_negative_ic_not_usable(self):
        """实测案例：GP-1·可用 roc(c_r, 40) ic=-0.0599 t=-1.629 不得再被判可用。"""
        r = _rating(ic_mean=-0.0599, t_stat=-1.629)
        assert r not in ("可用", "优秀"), f"负 IC 被判为 {r}，方向 bug 复现"
        assert r == "反向"

    def test_strong_negative_ic_not_excellent(self):
        """即使 |t| 很高、|ic| 很大，负 IC 也绝不能是「优秀」。"""
        r = _rating(ic_mean=-0.15, t_stat=5.0, mono=0.9)
        assert r != "优秀"
        assert r == "反向"

    def test_positive_ic_still_usable(self):
        """正 IC 显著时应正常评为「可用」——修复不能误伤正向因子。"""
        r = _rating(ic_mean=0.056, t_stat=1.8)
        assert r == "可用"

    def test_positive_ic_excellent(self):
        r = _rating(ic_mean=0.083, t_stat=2.94)   # 红利低波实测值
        assert r == "优秀"

    def test_weak_and_invalid_unchanged(self):
        assert _rating(ic_mean=0.006, t_stat=0.8) == "弱"
        assert _rating(ic_mean=0.002, t_stat=0.3) == "无效"

    def test_no_abs_on_direction(self):
        """静态检查：主实现里评级分支不得对 t_stat/ic_mean 取 abs 来判正向。"""
        import inspect
        from app.services import factor_mining
        src = inspect.getsource(factor_mining)
        seg = src[src.find("luck_bar"):src.find('rating = "优秀"') + 200]
        assert "has_pos" in seg, "主实现应使用 has_pos 判断方向"
        # 正向分支必须用 t_stat >= ... 而非 abs(t_stat) >= ...
        assert "has_pos and t_stat >=" in seg, \
            "正向分支应直接比较 t_stat，不应先取 abs"
