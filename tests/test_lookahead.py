"""R4 (rollout cuối ngày) và R3 (dừng sớm ngày cuối), 16/11/2026."""

import dataclasses
import unittest
from unittest import mock

from hexudon import rules
from hexudon.model import AgentState, DayState, MatchConfig, Spot
from hexudon.offline.mapgen import load_or_generate
from hexudon.planner import engine, strategy
from hexudon.planner.alns import Elite
from hexudon.planner.engine import (day_upper_bound, make_context, pick_by_lookahead, plan_day,
                                    safe_solution)
from hexudon.planner.lookahead import future_gain, play_days
from hexudon.planner.objective import lexicographic_gain
from hexudon.planner.roles import candidates, rollout
from hexudon.planner.solution import Weights
from hexudon.traffic import TrafficForecast


def sample_ctx(weights=None, day=0):
    cfg = load_or_generate("sample_16")
    kinds = candidates(cfg)[1].kinds
    agents = [AgentState(kinds[i], cfg.agent_starts[i], cfg.fuel_limits) for i in range(cfg.n_agents)]
    return cfg, kinds, make_context(cfg, day, agents, {}, [s.stocks for s in cfg.spots], set(),
                                    weights=weights or Weights())


def one_spot_last_day(ub_stop=1.0):
    """8x8 đồng bằng, một ngày; xe tuần tra đứng cạnh spot duy nhất (kho 1), một xe tiếp."""
    w = 8
    cfg = MatchConfig(width=w, height=w, cells=[rules.PLAIN] * (w * w), spots=[Spot(0, 9, 1)],
                      agent_starts=[10, 40], fuel_limits=20, day_steps=[12], day_seconds=[5],
                      players=1, busy_threshold=1, jammed_threshold=2)
    agents = [AgentState(rules.PATROL, 10, 20), AgentState(rules.REFUEL, 40, 20)]
    return make_context(cfg, 0, agents, {}, [1], set(), weights=Weights(ub_stop=ub_stop))


class TestPlayOut(unittest.TestCase):
    def test_play_days_from_day_zero_equals_roles_rollout(self):
        cfg = load_or_generate("sample_16")
        for cand in candidates(cfg)[:3]:
            expected = rollout(cfg, cand.kinds, TrafficForecast(cfg, n_patrols=2), seed=3)
            agents = [AgentState(cand.kinds[i], cfg.agent_starts[i], cfg.fuel_limits)
                      for i in range(cfg.n_agents)]
            visited, daily, servings, days = play_days(
                cfg, cand.kinds, TrafficForecast(cfg, n_patrols=2), 0, agents, set(),
                {c: rules.SMOOTH for c in cfg.road_cells}, None, None, seed=3)
            self.assertEqual((len(visited), daily, servings), expected)
            self.assertEqual(days, cfg.n_days)

    def test_future_gain_is_zero_on_the_last_day_and_respects_the_horizon(self):
        cfg, kinds, ctx = sample_ctx()
        _, decoded = safe_solution(ctx, 0)
        forecast = TrafficForecast(cfg, n_patrols=3)
        args = (cfg, kinds, forecast, 0, decoded.result, set(), {}, None, None)
        full = future_gain(*args, seed=1)
        one = future_gain(*args, seed=1, max_days=1)
        self.assertEqual(full, future_gain(*args, seed=1), "tất định")
        self.assertLessEqual(one[1], full[1])
        self.assertLessEqual(one[1], len(cfg.brands), "một ngày chơi tiếp thì D ≤ số chuỗi")
        last = future_gain(cfg, kinds, forecast, cfg.n_days - 1, decoded.result, set(), {}, None, None)
        self.assertEqual(last, (0, 0, 0))


class TestElite(unittest.TestCase):
    def test_keeps_two_levels_and_drops_far_candidates(self):
        cfg, kinds, ctx = sample_ctx()
        sol, dec = safe_solution(ctx, 0)
        elite = Elite(ctx, 4)
        elite.offer(sol, dec)
        # chấm xa hơn ELITE_BAND × B dưới nghiệm tốt nhất thì không nhận
        far = dataclasses.replace(dec, score=dec.score - 10 * ctx.weights.B)
        elite.offer(sol, far, best_score=dec.score)
        self.assertEqual(len(elite.entries()), 1)
        # cùng chữ ký, điểm thấp hơn: không thay
        lower = dataclasses.replace(dec, score=dec.score - 1)
        elite.offer(sol, lower, best_score=dec.score)
        self.assertEqual(elite.entries()[0][1].score, dec.score)


class TestPickByLookahead(unittest.TestCase):
    def setUp(self):
        self.cfg, self.kinds, self.ctx = sample_ctx()
        self.sol, self.best = safe_solution(self.ctx, 0)
        self.elite = Elite(self.ctx, 8)
        # một ứng viên khác: bỏ trống tour của xe tuần tra đầu (trạng thái cuối khác hẳn)
        other_sol = self.sol.copy()
        other_sol.tours[0] = []
        other_sol.meets = [m for m in other_sol.meets if m.patrol != 0]
        self.other = engine.decode(self.ctx, other_sol)
        self.other_sol = other_sol

    def test_picks_the_candidate_with_the_best_total(self):
        new_best = lexicographic_gain(self.ctx, self.best.result)[0]
        if lexicographic_gain(self.ctx, self.other.result)[0] < new_best:
            # ứng viên ít chuỗi mới hơn không bao giờ được chọn (L cứng)
            sol, dec, picked, _ = pick_by_lookahead(
                self.ctx, self.sol, self.best, None, lambda d: (9, 99, 99) if d is self.other else (0, 0, 0),
                extra=[(self.other_sol, self.other)])
            self.assertIs(dec, self.best)
            self.assertEqual(picked, "")
            return
        look = lambda d: (0, 50, 0) if d is self.other else (0, 0, 0)
        sol, dec, picked, _ = pick_by_lookahead(self.ctx, self.sol, self.best, None, look,
                                             extra=[(self.other_sol, self.other)])
        self.assertIs(dec, self.other)
        self.assertEqual(picked, "rollout")

    def test_keeps_the_best_when_the_future_is_flat(self):
        sol, dec, picked, _ = pick_by_lookahead(self.ctx, self.sol, self.best, None, lambda d: (0, 0, 0),
                                             extra=[(self.other_sol, self.other)])
        self.assertIs(dec, self.best)
        self.assertEqual(picked, "")

    def test_plan_day_with_lookahead_never_loses_new_chains(self):
        n = 120
        base = plan_day(sample_ctx(Weights())[2], seed=2, max_iterations=n)
        calls = []

        def look(d):
            calls.append(1)
            return (0, len(d.result.end_pos), 0)

        _, _, ctx = sample_ctx(Weights())
        final = plan_day(ctx, seed=2, max_iterations=n, lookahead=look)
        self.assertTrue(calls, "lookahead phải được gọi khi không phải ngày cuối")
        self.assertGreaterEqual(lexicographic_gain(ctx, final.result)[0],
                                lexicographic_gain(ctx, base.result)[0])
        self.assertTrue(final.result.ok)


class TestLookaheadRespectsTheDeadline(unittest.TestCase):
    def test_a_slow_lookahead_does_not_push_the_day_past_its_deadline(self):
        # chạy thật 16/11: chấm lại sau pha Q không xem giờ làm một ngày 4 s kéo dài 7,9 s
        import time as _time
        _, _, ctx = sample_ctx(Weights(rollout_k=8, rollout_share=0.3))

        def slow(d):
            _time.sleep(0.3)
            return (0, len(d.result.end_pos) % 3, 0)

        started = _time.monotonic()
        final = plan_day(ctx, deadline=started + 1.0, seed=1, lookahead=slow)
        elapsed = _time.monotonic() - started
        self.assertTrue(final.result.ok)
        self.assertLess(elapsed, 1.0 + 0.25, f"vượt hạn {elapsed - 1.0:.2f}s")


class TestOfferSendsRolloutPick(unittest.TestCase):
    def _run(self, picked_by: str):
        cfg = load_or_generate("sample_16")
        kinds = candidates(cfg)[1].kinds
        strat = strategy.MatchStrategist(cfg, search_iterations=5, role_iterations=5)
        strat.kinds = list(kinds)
        agents = [AgentState(kinds[i], cfg.agent_starts[i], cfg.fuel_limits) for i in range(cfg.n_agents)]
        state = DayState(day=0, agents=agents, road_status={})
        sent = []

        def fake_plan_day(ctx, deadline=None, seed=0, on_plan=None, on_improve=None,
                          max_iterations=0, lookahead=None, **kw):
            _, first = safe_solution(ctx, 0)
            on_plan(first)
            better = dataclasses.replace(first, score=first.score + ctx.weights.B, plan=[[-1]] + first.plan[1:])
            on_improve(better)
            final = dataclasses.replace(first, score=first.score - 5, plan=[[-2]] + first.plan[1:],
                                        picked_by=picked_by)
            on_plan(final)
            return final

        with mock.patch.object(strategy, "plan_day", fake_plan_day):
            strat.plan_day(state, submit=lambda plan: sent.append(plan) or True, record=False)
        return sent

    def test_rollout_pick_is_sent_on_the_final_slot(self):
        sent = self._run("rollout")
        self.assertEqual(len(sent), 3)
        self.assertEqual(sent[-1][0], [-2])

    def test_a_lower_scoring_plain_final_is_not_sent(self):
        sent = self._run("")
        self.assertEqual(len(sent), 2)


class TestUpperBoundStop(unittest.TestCase):
    def test_upper_bound_counts_reachable_spots(self):
        ctx = one_spot_last_day()
        self.assertEqual(day_upper_bound(ctx), (1, 1, 1))

    def test_last_day_stops_at_once_when_the_safe_plan_reaches_the_bound(self):
        ctx = one_spot_last_day()
        with mock.patch.object(engine, "improve", wraps=engine.improve) as spy:
            decoded = plan_day(ctx, seed=0, max_iterations=200)
        self.assertEqual(lexicographic_gain(ctx, decoded.result), (1, 1, 1))
        self.assertEqual(spy.call_count, 0, "đạt cận trên ngay từ bài an toàn: không cần tìm")

    def test_without_ub_stop_the_search_runs(self):
        ctx = one_spot_last_day(ub_stop=0.0)
        with mock.patch.object(engine, "improve", wraps=engine.improve) as spy:
            plan_day(ctx, seed=0, max_iterations=20)
        self.assertGreater(spy.call_count, 0)

    def test_strategist_has_no_lookahead_on_the_last_day_or_when_disabled(self):
        cfg = load_or_generate("sample_16")
        strat = strategy.MatchStrategist(cfg, search_iterations=5, role_iterations=5)
        strat.kinds = candidates(cfg)[1].kinds
        last = DayState(day=cfg.n_days - 1, agents=[], road_status={})
        self.assertIsNone(strat._lookahead(last, {}, 10))
        self.assertIsNotNone(strat._lookahead(DayState(day=0, agents=[], road_status={}), {}, 10))
        off = strategy.MatchStrategist(cfg, weights=Weights(rollout_k=0), search_iterations=5, role_iterations=5)
        self.assertIsNone(off._lookahead(DayState(day=0, agents=[], road_status={}), {}, 10))


if __name__ == "__main__":
    unittest.main()
