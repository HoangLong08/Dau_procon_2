"""Bước E (19/11/2026): orienteering chính xác cho từng xe tuần tra (planner/orienteering.py)."""

import dataclasses
import unittest

from hexudon import rules
from hexudon.model import AgentState
from hexudon.offline.mapgen import generate
from hexudon.planner import orienteering
from hexudon.planner.decoder import decode
from hexudon.planner.engine import make_context, plan_day, safe_solution
from hexudon.planner.solution import Weights
from hexudon.simulator import run_day


def small_ctx(seed=3, size=9, steps=None, weights=None):
    cfg = generate(seed=seed, size=size, agents=3, days=3)
    kinds = [rules.PATROL, rules.PATROL, rules.REFUEL]
    agents = [AgentState(kinds[i], cfg.agent_starts[i], cfg.fuel_limits if kinds[i] == rules.PATROL else 0)
              for i in range(cfg.n_agents)]
    ctx = make_context(cfg, 0, agents, {}, [s.stocks for s in cfg.spots], set(), weights=weights or Weights())
    if steps is not None:
        ctx = dataclasses.replace(ctx, steps=steps)
    return ctx


def tour_steps(ctx, slot, tour):
    cur = ctx.agents[ctx.patrols[slot]].pos
    total = 0
    for k in tour:
        total += ctx.dist_step.cost(cur, ctx.spot_cells[k])
        cur = ctx.spot_cells[k]
    return total


def brute_force(ctx, slot):
    """Số spot tối đa ghé được (vét cạn có cắt nhánh), cùng mô hình chi phí với MILP."""
    start = ctx.agents[ctx.patrols[slot]].pos
    cand = [k for k, c in enumerate(ctx.spot_cells) if c != start and ctx.stocks[k] > 0]
    best = 0

    def dfs(cur, used, n, cost):
        nonlocal best
        best = max(best, n)
        if n + (len(cand) - len(used)) <= best:
            return
        for k in cand:
            if k in used:
                continue
            c = cost + ctx.dist_step.cost(cur, ctx.spot_cells[k])
            if c <= ctx.steps:
                used.add(k)
                dfs(ctx.spot_cells[k], used, n + 1, c)
                used.discard(k)

    dfs(start, set(), 0, 0.0)
    return best


@unittest.skipUnless(orienteering.available(), "cần scipy (milp)")
class SolvePatrolTest(unittest.TestCase):
    def test_matches_brute_force_on_small_maps(self):
        # ngày ngắn để không ghé được hết spot (ví dụ 6/10, 3/9): MILP phải chọn bỏ spot nào
        for seed, size, steps in ((1, 8, 24), (1, 9, 14), (1, 10, 16), (2, 10, 16), (3, 10, 16)):
            ctx = small_ctx(seed=seed, size=size, steps=steps)
            for slot in range(ctx.n_patrols):
                tour = orienteering.solve_patrol(ctx, slot, 30.0)
                self.assertIsNotNone(tour)
                self.assertEqual(len(tour), len(set(tour)), "không ghé lại spot")
                self.assertLessEqual(tour_steps(ctx, slot, tour), ctx.steps)
                self.assertEqual(len(tour), brute_force(ctx, slot), f"seed {seed}, xe {slot}")

    def test_min_count_and_required_brands(self):
        ctx = small_ctx(seed=2, size=8, steps=24)
        best = len(orienteering.solve_patrol(ctx, 0, 30.0))
        self.assertIsNone(orienteering.solve_patrol(ctx, 0, 30.0, min_count=best + 1))
        self.assertEqual(len(orienteering.solve_patrol(ctx, 0, 30.0, min_count=best)), best)
        brand = ctx.spot_brand[0]
        tour = orienteering.solve_patrol(ctx, 0, 30.0, required_brands=[brand])
        if tour is not None:
            self.assertIn(brand, {ctx.spot_brand[k] for k in tour})

    def test_fuel_cap_is_respected(self):
        ctx = small_ctx(seed=1, size=9)
        cap = 6
        tour = orienteering.solve_patrol(ctx, 0, 30.0, fuel_cap=cap)
        cur = ctx.agents[ctx.patrols[0]].pos
        fuel = 0
        for k in tour:
            fuel += ctx.dist_step.steps_fuel(cur, ctx.spot_cells[k])[1]
            cur = ctx.spot_cells[k]
        self.assertLessEqual(fuel, cap)


@unittest.skipUnless(orienteering.available(), "cần scipy (milp)")
class SeedTest(unittest.TestCase):
    def test_exact_tours_never_shorter_than_base(self):
        ctx = small_ctx(seed=4, size=10)
        base, _ = safe_solution(ctx, 0)
        sol = orienteering.exact_tours(ctx, 20.0, base=base)
        if sol is None:  # mọi xe đã tối ưu sẵn
            return
        for slot in range(ctx.n_patrols):
            self.assertGreaterEqual(len(sol.tours[slot]), len(base.tours[slot]))

    def test_add_meets_never_lowers_the_score(self):
        ctx = small_ctx(seed=5, size=10)
        sol = orienteering.exact_tours(ctx, 20.0, cap_fuel=False)
        before = decode(ctx, sol)
        sol2, after = orienteering.add_meets(ctx, sol)
        self.assertTrue(after.result.ok)
        self.assertGreaterEqual(after.score, before.score)

    def test_plan_day_with_orienteering_is_valid(self):
        w = Weights(or_seconds=5.0, or_k_near=10.0, or_fuel=0.0)
        ctx = small_ctx(seed=6, size=10, weights=w)
        dec = plan_day(ctx, seed=0, max_iterations=50)
        self.assertTrue(dec.result.ok)
        res = run_day(ctx.config, ctx.steps, ctx.agents, dec.plan, ctx.stocks, ctx.road_status)
        self.assertTrue(res.ok, res.error)
        self.assertEqual(res.servings, dec.result.servings)

    def test_plan_day_with_orienteering_keeps_its_deadline_on_a_vn_sized_map(self):
        # theo giây: MILP lấy tối đa or_share của phần còn lại, rồi ALNS, pha Q và rollout vẫn phải
        # kịp hạn (bản đồ 30–32 ô như server VN, ngày dài nhất của trận)
        import time

        from hexudon.offline.mapgen import generate_vn
        cfg = generate_vn(5)
        kinds = [rules.PATROL] * (cfg.n_agents - 1) + [rules.REFUEL]
        agents = [AgentState(kinds[i], cfg.agent_starts[i], cfg.fuel_limits) for i in range(cfg.n_agents)]
        day = max(range(cfg.n_days), key=cfg.steps_of_day)
        w = Weights(or_seconds=20.0, or_share=0.75, or_k_near=10.0, or_fuel=0.0, or_min_seconds=0.0)
        ctx = make_context(cfg, day, agents, {}, [s.stocks for s in cfg.spots], set(), weights=w)
        budget = 4.0
        t0 = time.monotonic()
        dec = plan_day(ctx, seed=0, deadline=t0 + budget)
        self.assertLess(time.monotonic() - t0, budget + 0.6)
        self.assertTrue(dec.result.ok)

    def test_skipped_when_the_day_budget_is_short(self):
        # cùng điểm xuất phát với DAU 2: MILP thiệt ở 3–5 s/ngày, nên dưới or_min_seconds thì bỏ
        import time
        from unittest import mock

        from hexudon.planner import engine
        ctx = small_ctx(seed=6, size=10, weights=Weights(or_min_seconds=10.0))
        with mock.patch.object(orienteering, "exact_tours", side_effect=AssertionError("không được gọi")):
            dec = plan_day(ctx, seed=0, deadline=time.monotonic() + 2.0)
        self.assertTrue(dec.result.ok)
        called = []
        real = orienteering.exact_tours
        with mock.patch.object(orienteering, "exact_tours", side_effect=lambda *a, **k: called.append(1) or real(*a, **k)):
            engine.plan_day(small_ctx(seed=6, size=10, weights=Weights(or_min_seconds=1.0)), seed=0,
                            deadline=time.monotonic() + 2.0)
        self.assertEqual(called, [1])

    def test_on_by_default_and_off_in_role_selection_and_extra_seeds(self):
        w = Weights()
        self.assertEqual((w.or_seconds, w.or_share, w.or_k_near, w.or_fuel, w.or_min_seconds),
                         (20.0, 0.75, 10.0, 0.0, 10.0))
        import inspect

        from hexudon.planner import strategy
        src = inspect.getsource(strategy)
        self.assertIn("or_seconds=0.0", src)


if __name__ == "__main__":
    unittest.main()
