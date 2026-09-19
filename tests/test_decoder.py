"""Bộ giải mã phải LUÔN sinh kế hoạch hợp lệ, kể cả với nghiệm vô lý."""

import random
import unittest

from hexudon import rules
from hexudon.model import AgentState
from hexudon.offline.mapgen import generate
from hexudon.planner.decoder import decode
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
        bad = 0
        for seed in range(self.CASES):
            ctx, rng = random_context(seed)
            sol = random_solution(ctx, rng)
            decoded = decode(ctx, sol)
            self.assertEqual(len(decoded.plan), ctx.config.n_agents)
            check = run_day(ctx.config, ctx.steps, ctx.agents, decoded.plan,
                            ctx.stocks, ctx.road_status)
            if not check.ok:
                bad += 1
                print(f"seed {seed}: {check.error}")
            self.assertTrue(decoded.result.ok)
        self.assertEqual(bad, 0, "mọi nghiệm phải giải mã ra kế hoạch hợp lệ")

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


if __name__ == "__main__":
    unittest.main()
