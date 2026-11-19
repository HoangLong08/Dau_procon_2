"""Ngày D an toàn: rollout (R4) và chọn seed (R7) không đổi D hôm nay lấy D dự báo (19/11/2026).

Chẩn đoán cả trận: 1addebd7 (ngày 2) và gen:1011 (ngày 7) đều hụt 1 D vì rollout chọn bài
D 19 thay cho D 20; giải lại đúng ngữ cảnh đó không có rollout thì cả 3 seed đều đạt D 20.
"""

import dataclasses
import unittest
from unittest import mock

from hexudon.model import AgentState
from hexudon.offline.mapgen import load_or_generate
from hexudon.planner import engine, strategy
from hexudon.planner.engine import make_context, pick_by_lookahead, safe_solution, today_hard_len
from hexudon.planner.objective import lexicographic_gain
from hexudon.planner.parallel import DayJob
from hexudon.planner.roles import candidates
from hexudon.planner.solution import Weights
from hexudon.planner.strategy import MatchStrategist


def safe_day(weights):
    """sample_16, ngày 2, mọi chuỗi đã thu (chuỗi mới luôn 0): chỉ còn D và Q để so."""
    cfg = load_or_generate("sample_16")
    kinds = candidates(cfg)[1].kinds
    agents = [AgentState(kinds[i], cfg.agent_starts[i], cfg.fuel_limits) for i in range(cfg.n_agents)]
    ctx = make_context(cfg, 1, agents, {}, [s.stocks for s in cfg.spots], set(cfg.brands), weights=weights)
    sol, best = safe_solution(ctx, 0)
    worse_sol = sol.copy()
    worse_sol.tours = [[] for _ in worse_sol.tours]
    worse_sol.meets = []
    worse = engine.decode(ctx, worse_sol)
    return cfg, kinds, agents, ctx, (sol, best), (worse_sol, worse)


class RolloutDHardTest(unittest.TestCase):
    """Bản rộng (rollout_d_streak 0): mọi ngày D an toàn."""

    def test_hard_length(self):
        self.assertEqual(today_hard_len(safe_day(Weights(rollout_d_hard=0.0))[3]), 1)
        self.assertEqual(today_hard_len(safe_day(Weights(rollout_d_hard=1.0, soft_unit=20.0, rollout_d_streak=0.0))[3]), 2)
        # ngày chưa an toàn (soft_auto đặt soft_unit = 0): chỉ L cứng như trước
        self.assertEqual(today_hard_len(safe_day(Weights(rollout_d_hard=1.0, soft_unit=0.0))[3]), 1)

    def test_rollout_never_trades_todays_d_on_a_safe_day(self):
        for hard, rollout_wins in ((0.0, True), (1.0, False)):
            _, _, _, ctx, (sol, best), (worse_sol, worse) = safe_day(Weights(rollout_d_hard=hard, soft_unit=20.0, rollout_d_streak=0.0))
            self.assertTrue(worse.result.ok)
            self.assertLess(lexicographic_gain(ctx, worse.result)[1], lexicographic_gain(ctx, best.result)[1])
            look = lambda d: (0, 50, 0) if d is worse else (0, 0, 0)  # dự báo rất lạc quan
            _, dec, picked, _ = pick_by_lookahead(ctx, sol, best, None, look, extra=[(worse_sol, worse)])
            self.assertIs(dec, worse if rollout_wins else best, f"rollout_d_hard={hard}")
            self.assertEqual(picked, "rollout" if rollout_wins else "")

    def test_seed_pick_floors_d_at_the_best_seed(self):
        for hard, side_wins in ((0.0, False), (1.0, True)):
            w = Weights(rollout_d_hard=hard, soft_unit=20.0, rollout_d_streak=0.0)
            cfg, kinds, agents, ctx, (_, best), (_, worse) = safe_day(w)
            s = MatchStrategist(cfg, net_margin=0.0, seed=1, weights=w, search_iterations=10, search_jobs=2)
            s.kinds = list(kinds)
            job = DayJob(day=1, kinds=kinds, agents=agents, status={}, visited=set(cfg.brands), weights=w,
                         forecast_status=None, steps_override=None, seed=1)
            # seed chính ra bài ít D hơn, nhưng chơi tiếp dự báo tổng của nó hơn hẳn seed phụ
            main = dataclasses.replace(worse, total=(0, 30, 0))
            with mock.patch.object(strategy, "run_job", return_value=(best, (0, 20, 0))):
                pick = s._pick_seed(ctx, main, None, job, [7], None, None, 10)
            self.assertIs(pick, best if side_wins else main, f"rollout_d_hard={hard}")


class StreakTest(unittest.TestCase):
    """rollout_d_streak: chỉ giữ D cứng khi D "dễ" (bản đồ kiểu VN). Trên bản đồ mẫu thiếu D,
    rollout đổi 1 D hôm nay lấy D ngày sau thường là đúng (bản rộng ΔD −0,50 trên 48 cặp)."""

    def test_needs_a_plan_at_the_d_cap_today(self):
        w = Weights(rollout_d_hard=1.0, soft_unit=20.0, rollout_d_streak=2.0)
        cfg, _, _, ctx, (_, best), _ = safe_day(w)
        n = len(cfg.brands)
        for d, want in ((n, 2), (n - 1, 1)):
            with mock.patch.object(engine, "lexicographic_gain", return_value=(0, d, 5)):
                self.assertEqual(today_hard_len(ctx, [best.result]), want, f"D hôm nay {d}/{n}")
        self.assertEqual(today_hard_len(ctx, []), 1)

    def test_strategist_keeps_the_rule_only_after_a_streak_of_capped_days(self):
        cfg = load_or_generate("sample_16")
        n = len(cfg.brands)
        w = Weights(rollout_d_hard=1.0, rollout_d_streak=2.0, soft_unit=20.0, soft_auto=1.0)
        s = MatchStrategist(cfg, net_margin=0.0, seed=1, weights=w, search_iterations=10)
        s.visited_brands = set(cfg.brands)
        for types, on in (([], False), ([n], False), ([n, n], True), ([n, n - 1, n], False), ([n, n, n], True)):
            s.history = [{"types": t} for t in types]
            self.assertEqual(s.day_weights().rollout_d_hard > 0, on, f"lịch sử D {types}")
        # bản rộng (streak 0) không đổi gì
        s.weights = Weights(rollout_d_hard=1.0, soft_unit=20.0, soft_auto=1.0, rollout_d_streak=0.0)
        s.history = [{"types": n - 1}]
        self.assertEqual(s.day_weights().rollout_d_hard, 1.0)


if __name__ == "__main__":
    unittest.main()
