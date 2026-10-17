"""Bộ giải mã phải LUÔN sinh kế hoạch hợp lệ, kể cả với nghiệm vô lý."""

import random
import unittest

from hexudon import rules
from hexudon.model import AgentState, MatchConfig, Spot
from hexudon.offline.mapgen import generate
from hexudon.planner.decoder import _window, decode, walk_patrol
from hexudon.planner.engine import make_context
from hexudon.planner.solution import DaySolution, Meet
from hexudon.simulator import run_day


def random_context(seed: int):
    rng = random.Random(seed)
    config = generate(seed=seed, size=rng.choice([8, 10, 12]), agents=rng.randint(3, 6),
                      days=rng.randint(4, 6))
    n = config.n_agents
    kinds = [rules.PATROL] * n
    for i in range(rng.randint(0, min(2, n - 2))):
        kinds[n - 1 - i] = rules.REFUEL
    agents = [
        AgentState(kinds[i], config.agent_starts[i],
                   rng.randint(0, config.fuel_limits) if kinds[i] == rules.PATROL else 0)
        for i in range(n)
    ]
    day = rng.randrange(config.n_days)
    status = {c: rng.randrange(3) for c in config.road_cells}
    ctx = make_context(config, day, agents, status, [s.stocks for s in config.spots], set())
    return ctx, rng


def random_solution(ctx, rng):
    """Nghiệm ngẫu nhiên, cố tình vô lý: tour dài, điểm hẹn lung tung."""
    n_spots = len(ctx.config.spots)
    tours = []
    for _ in range(ctx.n_patrols):
        k = rng.randint(0, min(n_spots, 8))
        tours.append([rng.randrange(n_spots) for _ in range(k)])
    sol = DaySolution(tours=tours)
    if ctx.n_cars:
        for _ in range(rng.randint(0, 4)):
            p = rng.randrange(ctx.n_patrols)
            idx = rng.randint(-1, max(-1, len(tours[p]) - 1))
            sol.meets.append(Meet(p, idx, rng.randrange(ctx.n_cars)))
    for _ in range(rng.randint(0, 3)):
        p = rng.randrange(ctx.n_patrols)
        if tours[p]:
            sol.waits[(p, rng.randrange(len(tours[p])))] = rng.randint(1, 10)
    return sol


class DecoderInvariantTest(unittest.TestCase):
    CASES = 120

    def test_every_solution_decodes_to_a_valid_plan(self):
        """Hợp lệ theo cả hai cách hiểu: ví dụ chính thức và câu chữ của đề (strict_fuel)."""
        bad = 0
        for seed in range(self.CASES):
            ctx, rng = random_context(seed)
            sol = random_solution(ctx, rng)
            decoded = decode(ctx, sol)
            self.assertEqual(len(decoded.plan), ctx.config.n_agents)
            for strict in (False, True):
                check = run_day(ctx.config, ctx.steps, ctx.agents, decoded.plan,
                                ctx.stocks, ctx.road_status, strict_fuel=strict)
                if not check.ok:
                    bad += 1
                    print(f"seed {seed} strict={strict}: {check.error}")
            self.assertTrue(decoded.result.ok)
        self.assertEqual(bad, 0, "mọi nghiệm phải giải mã ra kế hoạch hợp lệ")

    def test_decoded_plans_have_no_consecutive_waits(self):
        for seed in range(30):
            ctx, rng = random_context(seed + 900)
            decoded = decode(ctx, random_solution(ctx, rng))
            for seq in decoded.plan:
                for a, b in zip(seq, seq[1:]):
                    self.assertFalse(a < 0 and b < 0, f"seed {seed}: lệnh chờ chưa gộp {seq}")

    def test_prediction_matches_simulator_exactly(self):
        for seed in range(40):
            ctx, rng = random_context(seed + 500)
            decoded = decode(ctx, random_solution(ctx, rng))
            check = run_day(ctx.config, ctx.steps, ctx.agents, decoded.plan,
                            ctx.stocks, ctx.road_status)
            self.assertEqual(decoded.result.end_pos, check.end_pos)
            self.assertEqual(decoded.result.end_fuel, check.end_fuel)
            self.assertEqual(decoded.result.collections, check.collections)
            self.assertEqual(decoded.result.stay, check.stay)

    def test_empty_solution_is_valid(self):
        ctx, _ = random_context(7)
        decoded = decode(ctx, ctx.empty_solution())
        self.assertTrue(decoded.result.ok)

    def test_patrol_out_of_fuel_still_produces_a_valid_plan(self):
        ctx, rng = random_context(3)
        for i in ctx.patrols:
            ctx.agents[i] = AgentState(rules.PATROL, ctx.agents[i].pos, 0)
        decoded = decode(ctx, random_solution(ctx, rng))
        self.assertTrue(decoded.result.ok)


def line_context():
    """Bản đồ 8x1 toàn đồng bằng: spot ở ô 2, 4, 6; xe tuần tra ở ô 0, xe tiếp ở ô 7."""
    config = MatchConfig(
        width=8, height=1, cells=[rules.PLAIN] * 8,
        spots=[Spot(0, 2, 1), Spot(1, 4, 1), Spot(2, 6, 1)],
        agent_starts=[0, 7],
        fuel_limits=20, day_steps=[40], day_seconds=[5],
        players=1, busy_threshold=1, jammed_threshold=2,
    )
    agents = [AgentState(rules.PATROL, 0, 20), AgentState(rules.REFUEL, 7, 0)]
    return make_context(config, 0, agents, {}, [1, 1, 1], set())


class MeetWindowTest(unittest.TestCase):
    """Xe tiếp phải tới điểm hẹn trước khi xe tuần tra NHẬN lệnh rời ô (cases 7.7, 11.9)."""

    def test_window_of_a_middle_stop_closes_when_the_patrol_is_ordered_to_leave(self):
        ctx = line_context()
        sol = DaySolution(tours=[[0, 1, 2]], meets=[Meet(0, 1, 0)], waits={(0, 1): 3})
        walk = walk_patrol(ctx, sol, 0, {1: 0}, sol.waits)
        cell, start, end = _window(ctx, walk, sol, 0, 1, sol.waits)
        self.assertEqual(cell, 4)
        self.assertEqual(start, walk.arrivals[1])
        self.assertEqual(end, start + 3, "hết lượt chờ là xe tuần tra nhận lệnh đi")

    def test_window_of_the_last_stop_lasts_until_the_end_of_the_day(self):
        ctx = line_context()
        sol = DaySolution(tours=[[0, 1]], meets=[Meet(0, 1, 0)])
        walk = walk_patrol(ctx, sol, 0, {1: 0}, sol.waits)
        _, _, end = _window(ctx, walk, sol, 0, 1, sol.waits)
        self.assertEqual(end, ctx.steps)

    def test_decoded_meet_is_valid_under_the_strict_reading(self):
        ctx = line_context()
        sol = DaySolution(tours=[[0, 1, 2]], meets=[Meet(0, 1, 0)])
        decoded = decode(ctx, sol)
        strict = run_day(ctx.config, ctx.steps, ctx.agents, decoded.plan, ctx.stocks,
                         ctx.road_status, strict_fuel=True)
        self.assertTrue(strict.ok, strict.error)


if __name__ == "__main__":
    unittest.main()
