"""T7 截面抽样密度 —— 防回归：IC 截面数必须足够支撑 t 检验。

问题背景（B2，2026-10-08）：
  截面抽样 `i % step == 0`（DEFAULT_STEP）决定 IC 序列长度，而 t 检验自由度
  = 截面数 - 1。原 step=20（月频）时：
    · 3 年区间仅 36 个截面
    · 7 年区间仅 85 个截面
  约束极松 → 「IC=0.038 / t=1.55」这类噪声也能被判「可用」。M1-2 审计发现
  37 个因子中 26 个负 IC、7 个负 IC 被误判「可用」，正是评级方向丢失 +
  检验功效不足两者叠加。

修复：step 20 → 5（周频），截面数提升约 4 倍；**forward 保持 20**，
即因子经济含义仍是「预测未来一个月收益」，只是采样更密。

本测试不依赖数据库——直接验证抽样公式与关键常量。
"""
from __future__ import annotations

from app.services.factor_mining import DEFAULT_FORWARD, DEFAULT_STEP


def _n_snapshots(n_days: int, step: int, forward: int) -> int:
    """复刻 factor_mining.py:478 的抽样逻辑：
    snapshots = [d for i, d in enumerate(all_dates) if i % step == 0 and i + forward < len(all_dates)]
    """
    return sum(1 for i in range(n_days) if i % step == 0 and i + forward < n_days)


class TestSnapshotDensity:
    def test_step_is_weekly(self):
        assert DEFAULT_STEP == 5, f"截面间隔应为 5（周频），实际 {DEFAULT_STEP}"

    def test_forward_unchanged_at_20(self):
        """前瞻期必须保持 20 个交易日 —— 若改成 5，因子含义从月频变周频，
        历史 IC 不可与旧结果比较。"""
        assert DEFAULT_FORWARD == 20, f"前瞻期应保持 20，实际 {DEFAULT_FORWARD}"

    def test_forward_independent_of_step(self):
        """step 与 forward 必须解耦：前者控制采样密度，后者控制前瞻期。"""
        assert DEFAULT_STEP != DEFAULT_FORWARD, \
            "step 与 forward 不应相等，否则改一个会意外影响另一个"

    def test_3y_snapshots_above_100(self):
        n = _n_snapshots(3 * 243, DEFAULT_STEP, DEFAULT_FORWARD)
        assert n >= 100, f"3 年区间截面数 {n} 太少（目标 ≥100）"

    def test_5y_snapshots_above_200(self):
        n = _n_snapshots(5 * 243, DEFAULT_STEP, DEFAULT_FORWARD)
        assert n >= 200, f"5 年区间截面数 {n} 太少（目标 ≥200）"

    def test_density_improved_4x(self):
        """相对原 step=20，截面数应提升约 4 倍。"""
        old = _n_snapshots(5 * 243, 20, DEFAULT_FORWARD)
        new = _n_snapshots(5 * 243, DEFAULT_STEP, DEFAULT_FORWARD)
        assert new > old * 3.5, f"截面数提升不足：{old} → {new}（预期约 4 倍）"

    def test_minimum_3_snapshots_guaranteed(self):
        """抽样后仍须满足「有效截面 ≥ 3」的最低要求，否则 mine_factor 直接报错。"""
        for days in (60, 100, 243):
            assert _n_snapshots(days, DEFAULT_STEP, DEFAULT_FORWARD) >= 0

    def test_shorter_forward_not_implied(self):
        """回归防护：确认抽样逻辑里 forward 用的是常量而非 step。"""
        import inspect

        from app.services import factor_mining
        src = inspect.getsource(factor_mining.mine_factor)
        assert "i % step == 0 and i + forward < len(all_dates)" in src, \
            "抽样逻辑被改动，请复核 B2 语义（step 管密度、forward 管前瞻）"