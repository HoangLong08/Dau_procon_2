"""Điểm hẹn đi theo điểm dừng khi ALNS phá và sửa; ngân sách theo số vòng cho benchmark tất định."""

import random
import unittest

from hexudon import rules
from hexudon.model import AgentState, DayState
from hexudon.offline.mapgen import generate
from hexudon.offline.report import parse_weights, run_ab, run_benchmark
from hexudon.planner.alns import DESTROY_FUNCS, _remove_stops
from hexudon.planner.construct import InsertionState, repair
from hexudon.planner.decoder import decode
from hexudon.planner.engine import make_context
from hexudon.planner.roles import select_roles
from hexudon.planner.solution import DaySolution, Meet
from hexudon.planner.strategy import MatchStrategist
from hexudon.simulator import run_day


def context_with_cars(seed: int, n_cars: int = 1):
    rng = random.Random(seed)
    config = generate(seed=seed, size=rng.choice([10, 12]), agents=rng.randint(3, 5), days=4)
    n = config.n_agents
    kinds = [rules.PATROL] * n
    for i in range(min(n_cars, n - 2)):
        kinds[n - 1 - i] = rules.REFUEL
    agents = [AgentState(kinds[i], config.agent_starts[i],
                         rng.randint(1, config.fuel_limits) if kinds[i] == rules.PATROL else 0)
              for i in range(n)]
    ctx = make_context(config, 0, agents, {}, [s.stocks for s in config.spots], set())
    return ctx, rng


def solution_with_meets(ctx, rng):
    """Tour không lặp spot trong cùng một xe, để biết chắc điểm hẹn đang gắn vào spot nào."""
    n_spots = len(ctx.config.spots)
    tours = [rng.sample(range(n_spots), rng.randint(0, min(n_spots, 6))) for _ in range(ctx.n_patrols)]
    sol = DaySolution(tours=tours)
    for p, tour in enumerate(tours):
        for idx in range(-1, len(tour)):
            if rng.random() < 0.35:
                sol.meets.append(Meet(p, idx, rng.randrange(ctx.n_cars)))
    return sol


def rng_cars(seed: int) -> int:
    """Xen kẽ 1 và 2 xe tiếp giữa các seed."""
    return 1 + seed % 2


def attached(sol):
    """Tập (xe tuần tra, spot gắn điểm hẹn, xe tiếp); -1 nghĩa là gặp ở ô xuất phát."""
    return {(m.patrol, sol.tours[m.patrol][m.stop_idx] if m.stop_idx >= 0 else -1, m.car)
            for m in sol.meets}


class RemoveStopsTest(unittest.TestCase):
    def test_meets_and_waits_follow_surviving_stops(self):
        sol = DaySolution(tours=[[3, 7, 1], [5, 2]],
                          meets=[Meet(0, 1, 0), Meet(0, 2, 1), Meet(1, 0, 0), Meet(0, -1, 0)],
                          waits={(0, 2): 3, (1, 1): 2})
        _remove_stops(sol, [(0, 0)])
        self.assertEqual(sol.tours, [[7, 1], [5, 2]])
        self.assertEqual(sol.meets, [Meet(0, 0, 0), Meet(0, 1, 1), Meet(1, 0, 0), Meet(0, -1, 0)])
        self.assertEqual(sol.waits, {(0, 1): 3, (1, 1): 2})

    def test_meet_on_removed_stop_goes_with_it(self):
        sol = DaySolution(tours=[[3, 7, 1], [5, 2]],
                          meets=[Meet(0, 1, 0), Meet(0, 2, 1), Meet(1, 0, 0), Meet(0, -1, 0)],
                          waits={(0, 1): 4})
        _remove_stops(sol, [(0, 1)])
        self.assertEqual(sol.tours[0], [3, 1])
        self.assertEqual(sol.meets, [Meet(0, 1, 1), Meet(1, 0, 0), Meet(0, -1, 0)])
        self.assertEqual(sol.waits, {})


class InsertionShiftTest(unittest.TestCase):
    def test_insert_before_meet_shifts_it(self):
        ctx, _ = context_with_cars(3)
        sol = DaySolution(tours=[[0, 1]] + [[] for _ in range(ctx.n_patrols - 1)],
                          meets=[Meet(0, 1, 0), Meet(0, -1, 0)], waits={(0, 1): 2})
        state = InsertionState(ctx, sol)
        state.apply(0, 0, 2, 0.0, 0.0)
        self.assertEqual(state.sol.tours[0], [2, 0, 1])
        self.assertEqual(state.sol.meets, [Meet(0, 2, 0), Meet(0, -1, 0)])
        self.assertEqual(state.sol.waits, {(0, 2): 2})
        state.apply(0, 3, 3, 0.0, 0.0)     # chèn sau cùng: không đổi chỉ số nào
        self.assertEqual(state.sol.meets, [Meet(0, 2, 0), Meet(0, -1, 0)])

    def test_meets_never_jump_to_another_spot_during_search(self):
        """Mọi toán tử phá rồi sửa: điểm hẹn còn sống luôn gắn đúng spot cũ, chỉ số luôn hợp lệ."""
        for seed in range(40):
            ctx, rng = context_with_cars(seed, n_cars=rng_cars(seed))
            sol = solution_with_meets(ctx, rng)
            for name, destroy in DESTROY_FUNCS.items():
                if name == "meet_toggle":       # toán tử này cố ý thêm, bớt điểm hẹn
                    continue
                before = attached(sol)
                after = repair(ctx, destroy(ctx, sol.copy(), rng, rng.randint(1, 3)), rng,
                               mode=rng.choice(["greedy", "regret", "coverage"]))
                for m in after.meets:
                    self.assertLess(m.stop_idx, len(after.tours[m.patrol]), (seed, name))
                    self.assertGreaterEqual(m.stop_idx, -1)
                self.assertTrue(attached(after) <= before, (seed, name, before, attached(after)))
                dec = decode(ctx, after)
                res = run_day(ctx.config, ctx.steps, ctx.agents, dec.plan, ctx.stocks,
                              ctx.road_status, strict_fuel=True)
                self.assertTrue(res.ok, (seed, name, res.error))


class IterationBudgetTest(unittest.TestCase):
    def test_strategist_with_iteration_budget_is_deterministic(self):
        config = generate(seed=5, size=10, agents=4, days=3)
        kinds = [rules.PATROL] * 3 + [rules.REFUEL]
        plans = []
        for _ in range(2):
            # 0 giây: nếu planner còn chặn theo giờ thì ALNS không chạy được vòng nào
            strat = MatchStrategist(config, search_seconds=0.0, net_margin=0.0, seed=1,
                                    search_iterations=40)
            strat.kinds = list(kinds)
            agents = [AgentState(k, p, config.fuel_limits) for k, p in zip(kinds, config.agent_starts)]
            plans.append(strat.plan_day(DayState(day=0, agents=agents, road_status={}), record=False).plan)
        self.assertEqual(plans[0], plans[1])

    def test_role_selection_with_iterations_ignores_the_clock(self):
        config = generate(seed=7, size=10, agents=5, days=4)
        first, options = select_roles(config, budget_seconds=0.0, seed=2, iterations_per_day=10)
        again, _ = select_roles(config, budget_seconds=0.0, seed=2, iterations_per_day=10)
        self.assertEqual(first, again)
        self.assertTrue(all(c.evaluated for c in options), "mọi phương án phải được chấm, không bị cắt giờ")


class BenchToolsTest(unittest.TestCase):
    def test_parallel_bench_matches_sequential(self):
        kw = dict(maps=["random10"], seeds=[0, 1], opponents=["greedy"],
                  search_iterations=15, role_iterations=5)
        seq = run_benchmark(jobs=1, **kw)
        par = run_benchmark(jobs=2, **kw)
        key = lambda s: [(r["map"], r["seed"], tuple(r["ours"]), tuple(r["best_opponent"])) for r in s["rows"]]  # noqa: E731
        self.assertEqual(key(seq), key(par))
        self.assertEqual(seq["invalid_plans"], 0)

    def test_ab_is_repeatable(self):
        kw = dict(maps=["random10"], seeds=[3], a={}, b={"fuel_bonus": 2.0}, opponents=["greedy"],
                  search_iterations=15, role_iterations=5)
        one = run_ab(**kw)
        two = run_ab(**kw)
        self.assertEqual([(r["A"], r["B"]) for r in one["rows"]], [(r["A"], r["B"]) for r in two["rows"]])
        self.assertEqual(one["matches"], 1)
        self.assertEqual(one["invalid_A"] + one["invalid_B"], 0)

    def test_parse_weights(self):
        self.assertEqual(parse_weights(" fuel_bonus=2, near_chain=0.4 "), {"fuel_bonus": 2.0, "near_chain": 0.4})
        self.assertEqual(parse_weights(""), {})
        with self.assertRaises(ValueError):
            parse_weights("fuel=2")
        with self.assertRaises(ValueError):
            parse_weights("fuel_bonus")


if __name__ == "__main__":
    unittest.main()
