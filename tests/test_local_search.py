"""Bước 1–2 (18/11/2026): đơn vị hạng mục mềm, vá D hôm nay, tìm kiếm cục bộ, chia vai trò xe."""

import dataclasses
import random
import unittest

from hexudon import rules
from hexudon.model import AgentState
from hexudon.offline.mapgen import generate
from hexudon.planner import local
from hexudon.planner.decoder import decode
from hexudon.planner.engine import ensure_today_chains, make_context, plan_day, safe_solution
from hexudon.planner.objective import end_value, lexicographic_gain
from hexudon.planner.roles import candidates, rank_candidates, RoleCandidate
from hexudon.planner.solution import DaySolution, Meet, Weights
from hexudon.planner.strategy import MatchStrategist


def make_ctx(seed: int, weights=None, cars: int = 1, day: int = 1, visited_all: bool = True):
    rng = random.Random(seed)
    config = generate(seed=seed, size=rng.choice([10, 12, 14]), agents=rng.randint(3, 5), days=4)
    n = config.n_agents
    kinds = [rules.PATROL] * n
    for i in range(min(cars, n - 2)):
        kinds[n - 1 - i] = rules.REFUEL
    agents = [AgentState(kinds[i], config.agent_starts[i],
                         rng.randint(config.fuel_limits // 3, config.fuel_limits) if kinds[i] == rules.PATROL else 0)
              for i in range(n)]
    visited = set(config.brands) if visited_all else set()
    return make_context(config, day, agents, {}, [s.stocks for s in config.spots], visited,
                        weights=weights or Weights())


def attached(sol):
    return {(m.patrol, sol.tours[m.patrol][m.stop_idx] if m.stop_idx >= 0 else -1, m.car) for m in sol.meets}


class SoftUnitTest(unittest.TestCase):
    def test_defaults_from_18_11(self):
        w = Weights()
        self.assertEqual((w.soft_unit, w.soft_auto, w.today_chain_first), (20.0, 1.0, 1.0))
        self.assertEqual((w.q_B, w.q_share), (0.05, 0.5))
        self.assertEqual((w.role_all_positions, w.role_d_tolerance, w.role_finalists, w.role_seeds),
                         (1.0, 0.02, 4.0, 2.0))
        self.assertEqual((w.soft_cap, w.ls_share), (0.0, 0.0), "chưa đo thấy lợi: để tắt")
        self.assertEqual(w.soft, 20.0)

    def test_zero_knobs_mean_the_old_behaviour(self):
        w = Weights(soft_unit=0.0)
        self.assertEqual(w.soft, w.B, "soft_unit = 0 nghĩa là hạng mục mềm theo B như trước")

    def test_soft_unit_equal_to_b_is_identical(self):
        for seed in range(4):
            ctx = make_ctx(seed, Weights(soft_unit=0.0))
            sol, dec = safe_solution(ctx, seed)
            # V-VN chỉ chạy khi soft_unit > 0: tắt để so đúng hạng mục tự viết
            c = dataclasses.replace(ctx, weights=Weights(soft_unit=ctx.weights.B, value_q_weight=0.0))
            self.assertAlmostEqual(end_value(ctx, dec.result), end_value(c, dec.result), places=6)

    def test_small_soft_unit_never_trades_a_chain_today(self):
        # với soft_unit nhỏ, hai nghiệm khác nhau một chuỗi hôm nay thì nghiệm nhiều chuỗi hơn luôn hơn
        for seed in range(6):
            ctx = make_ctx(seed, Weights(soft_unit=20.0))
            spread = []
            for s in range(3):
                _, dec = safe_solution(ctx, s)
                spread.append(end_value(ctx, dec.result))
            self.assertLess(max(spread) - min(spread), ctx.weights.B)

    def test_soft_cap_bounds_the_soft_part(self):
        for seed in range(4):
            ctx = make_ctx(seed)
            _, dec = safe_solution(ctx, seed)
            capped = dataclasses.replace(ctx, weights=Weights(soft_cap=0.01, value_weight=0.0, deadline=0.0,
                                                              urgency=0.0, cover_penalty=0.0, strand=0.0))
            self.assertLessEqual(abs(end_value(capped, dec.result)), 0.01 * capped.weights.B + 1e-9)


class SoftAutoTest(unittest.TestCase):
    def test_soft_unit_only_once_d_is_safe(self):
        config = generate(seed=3, size=12, agents=4, days=4)
        strat = MatchStrategist(config, weights=Weights(soft_unit=20.0, soft_auto=1.0), search_iterations=5,
                                role_iterations=1)
        self.assertEqual(strat.day_weights().soft_unit, 0.0, "ngày 0: chưa biết D có an toàn không")
        strat.visited_brands = set(config.brands)
        strat.history = [{"day": 0, "types": len(config.brands)}]
        self.assertEqual(strat.day_weights().soft_unit, 20.0)
        strat.history.append({"day": 1, "types": len(config.brands) - 1})
        self.assertEqual(strat.day_weights().soft_unit, 0.0, "hôm qua hụt chuỗi: về B")
        strat.close()

    def test_role_selection_plays_in_b_units(self):
        # chơi thử cả trận từ ngày 0: chưa biết D an toàn, nên chọn vai trò không được dùng soft_unit
        from unittest import mock
        from hexudon.planner import strategy

        config = generate(seed=4, size=10, agents=4, days=3)
        strat = MatchStrategist(config, weights=Weights(soft_unit=20.0, soft_auto=1.0), search_iterations=5,
                                role_iterations=1)
        seen = {}

        def fake(cfg, **kw):
            seen["w"] = kw["weights"]
            return [rules.PATROL] * (cfg.n_agents - 1) + [rules.REFUEL], []

        with mock.patch.object(strategy, "select_roles", side_effect=fake):
            strat.select_roles()
        self.assertEqual(seen["w"].soft_unit, 0.0)
        self.assertEqual(strat.weights.soft_unit, 20.0, "trọng số của trận không bị đổi")
        strat.close()


class RemapTest(unittest.TestCase):
    def test_reorder_keeps_meets_on_their_stops(self):
        sol = DaySolution(tours=[[3, 7, 1, 9], [5, 2]], meets=[Meet(0, 1, 0), Meet(0, 3, 0), Meet(0, -1, 0)],
                          waits={(0, 1): 4, (0, 3): 2})
        before = attached(sol)
        local._reorder(sol, 0, [3, 2, 1, 0])
        self.assertEqual(sol.tours[0], [9, 1, 7, 3])
        self.assertEqual(attached(sol), before)
        self.assertEqual(sol.waits, {(0, 2): 4, (0, 0): 2})

    def test_reorder_dropping_a_stop_drops_its_meet(self):
        sol = DaySolution(tours=[[3, 7, 1]], meets=[Meet(0, 1, 0), Meet(0, 2, 0)])
        local._reorder(sol, 0, [0, 2])
        self.assertEqual(sol.tours[0], [3, 1])
        self.assertEqual(attached(sol), {(0, 1, 0)})

    def test_insert_shifts_later_meets(self):
        sol = DaySolution(tours=[[3, 7, 1]], meets=[Meet(0, 0, 0), Meet(0, 2, 0)])
        local._insert_at(sol, 0, 1, 8)
        self.assertEqual(sol.tours[0], [3, 8, 7, 1])
        self.assertEqual(attached(sol), {(0, 3, 0), (0, 1, 0)})


class LocalSearchTest(unittest.TestCase):
    def test_every_move_decodes_to_a_valid_plan(self):
        rng = random.Random(5)
        for seed in range(6):
            ctx = make_ctx(seed, cars=1 + seed % 2)
            sol, _ = safe_solution(ctx, seed)
            est = local._Estimator(ctx)
            for move in local.MOVES:
                for _ in range(6):
                    prop = local._propose(ctx, sol, move, rng, est)
                    if prop is None:
                        continue
                    dec = decode(ctx, prop[0])
                    self.assertTrue(dec.result.ok, f"{move} seed {seed}")

    def test_never_worse_and_keeps_the_lock(self):
        for seed in range(5):
            ctx = make_ctx(seed, Weights(soft_unit=5.0))
            sol, dec = safe_solution(ctx, seed)
            res = local.local_search(ctx, sol, dec, max_moves=150, seed=seed)
            self.assertTrue(res.decoded.result.ok)
            self.assertGreaterEqual(res.decoded.score, dec.score)
            self.assertGreaterEqual(lexicographic_gain(ctx, res.decoded.result)[:2],
                                    lexicographic_gain(ctx, dec.result)[:2])

    def test_deterministic_for_a_seed(self):
        ctx = make_ctx(2, Weights(soft_unit=5.0))
        sol, dec = safe_solution(ctx, 0)
        a = local.local_search(ctx, sol, dec, max_moves=80, seed=3)
        b = local.local_search(ctx, sol, dec, max_moves=80, seed=3)
        self.assertEqual(a.decoded.plan, b.decoded.plan)

    def test_plan_day_with_local_search_is_valid_and_not_worse_than_safe(self):
        for seed in range(3):
            base = make_ctx(seed)
            ctx = dataclasses.replace(base, weights=Weights(q_share=0.6, ls_share=0.4, ls_moves_per_iter=3.0))
            dec = plan_day(ctx, seed=seed, max_iterations=120)
            _, safe = safe_solution(ctx, seed)
            self.assertTrue(dec.result.ok)
            self.assertGreaterEqual(lexicographic_gain(ctx, dec.result)[:2],
                                    lexicographic_gain(ctx, safe.result)[:2])
            self.assertGreaterEqual(dec.score, safe.score)


class TodayChainsTest(unittest.TestCase):
    def test_off_when_knob_is_zero(self):
        ctx = make_ctx(1, Weights(today_chain_first=0.0))
        sol, dec = safe_solution(ctx, 0)
        got_sol, got_dec = ensure_today_chains(ctx, sol, dec)
        self.assertIs(got_dec, dec)

    def test_never_loses_chains_and_stays_valid(self):
        for seed in range(8):
            ctx = make_ctx(seed, Weights(today_chain_first=1.0))
            # nghiệm cố ý thiếu: mỗi xe chỉ giữ tối đa 1 điểm dừng
            sol, _ = safe_solution(ctx, seed)
            sol = DaySolution(tours=[t[:1] for t in sol.tours])
            dec = decode(ctx, sol)
            got_sol, got_dec = ensure_today_chains(ctx, sol, dec)
            self.assertTrue(got_dec.result.ok)
            self.assertGreaterEqual(lexicographic_gain(ctx, got_dec.result)[:2],
                                    lexicographic_gain(ctx, dec.result)[:2])
            if got_dec is not dec:
                self.assertGreater(lexicographic_gain(ctx, got_dec.result)[:2],
                                   lexicographic_gain(ctx, dec.result)[:2])


class RoleCandidatesTest(unittest.TestCase):
    def test_all_positions_for_one_car(self):
        config = generate(seed=11, size=12, agents=5, days=4)
        old = candidates(config, Weights(role_all_positions=0.0))
        wide = candidates(config, Weights(role_all_positions=1.0))
        ones = [c.kinds for c in wide if c.n_cars == 1]
        self.assertEqual(len(ones), config.n_agents)
        self.assertEqual(sorted(tuple(k).index(rules.REFUEL) for k in ones), list(range(config.n_agents)))
        twos = [c for c in wide if c.n_cars == 2]
        self.assertEqual(len(twos), 10, "5 xe: mọi cặp")
        for c in old:
            self.assertIn(c.kinds, [x.kinds for x in wide], "danh sách mới chứa danh sách cũ")

    def test_rank_without_tolerance_is_lexicographic(self):
        cs = [RoleCandidate([0], "a", score=(5, 40, 100)), RoleCandidate([0], "b", score=(5, 41, 80)),
              RoleCandidate([0], "c", score=(4, 50, 200))]
        self.assertEqual([c.label for c in rank_candidates(cs, lambda c: c.score)], ["b", "a", "c"])

    def test_rank_with_tolerance_prefers_q_inside_the_band(self):
        cs = [RoleCandidate([0], "a", score=(5, 198, 600)), RoleCandidate([0], "b", score=(5, 200, 560)),
              RoleCandidate([0], "c", score=(5, 180, 900)), RoleCandidate([0], "d", score=(4, 200, 999))]
        got = [c.label for c in rank_candidates(cs, lambda c: c.score, tol=0.02)]
        self.assertEqual(got, ["a", "b", "c", "d"])


if __name__ == "__main__":
    unittest.main()
