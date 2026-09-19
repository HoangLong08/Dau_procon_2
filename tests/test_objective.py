"""Hàm mục tiêu: thứ tự từ điển và giá trị cuối ngày."""

import unittest

from hexudon import rules
from hexudon.model import AgentState, MatchConfig, Spot
from hexudon.planner.engine import make_context
from hexudon.planner.objective import end_value, score_day
from hexudon.simulator import run_day


def small_config(days=3):
    # 6x2: hàng trên đồng bằng có spot, hàng dưới có núi và ao
    cells = [rules.PLAIN] * 6 + [rules.PLAIN, rules.MOUNTAIN, rules.ROAD, rules.PLAIN,
                                 rules.POND, rules.PLAIN]
    return MatchConfig(
        width=6, height=2, cells=cells,
        spots=[Spot(0, 1, 2), Spot(1, 3, 1), Spot(2, 5, 1)],
        agent_starts=[0, 6, 9],
        fuel_limits=20,
        day_steps=[20] * days,
        day_seconds=[5] * days,
        players=2, busy_threshold=1, jammed_threshold=3,
    )


def context(config, day=0, visited=frozenset(), kinds=None):
    kinds = kinds or [rules.PATROL, rules.PATROL, rules.REFUEL]
    agents = [AgentState(kinds[i], config.agent_starts[i], config.fuel_limits)
              for i in range(config.n_agents)]
    return make_context(config, day, agents, {}, [s.stocks for s in config.spots],
                        set(visited), forecast_status={})


class ObjectiveTest(unittest.TestCase):
    def test_one_more_chain_beats_many_servings(self):
        """Một loại udon mới trong ngày phải hơn hẳn mọi số phần cộng lại."""
        config = small_config()
        ctx = context(config)
        w = ctx.weights
        max_servings = len(config.spots) * config.n_agents
        self.assertGreater(w.B, w.serving * max_servings,
                           "trọng số loại phải lớn hơn tổng số phần tối đa trong ngày")

    def test_end_value_is_zero_on_the_last_day(self):
        config = small_config(days=3)
        ctx = context(config, day=2)
        result = run_day(config, ctx.steps, ctx.agents,
                         [[-ctx.steps]] * config.n_agents, ctx.stocks, {})
        self.assertEqual(ctx.days_left, 0)
        self.assertEqual(end_value(ctx, result), 0.0)

    def test_unreachable_chain_is_penalised_hard(self):
        """Chuỗi không thể ghé kịp phần trận còn lại phải bị phạt rất nặng."""
        config = small_config(days=3)
        config.day_steps = [20, 1, 1]      # ngày sau gần như không đi được đâu
        config.__post_init__()
        ctx = context(config, day=0)
        result = run_day(config, ctx.steps, ctx.agents,
                         [[-ctx.steps]] * config.n_agents, ctx.stocks, {})
        value = end_value(ctx, result)
        self.assertLess(value, -ctx.weights.cover_penalty * ctx.weights.B / 2)

    def test_score_prefers_more_distinct_chains(self):
        config = small_config()
        ctx = context(config)

        class FakeResult:
            def __init__(self, brands, servings):
                self.brands = set(brands)
                self.servings = servings
                self.end_pos = [a.pos for a in ctx.agents]
                self.end_fuel = [a.fuel for a in ctx.agents]
                self.stay = {}

        two_chains = score_day(ctx, FakeResult({0, 1}, 2))
        one_chain_many = score_day(ctx, FakeResult({0}, 20))
        self.assertGreater(two_chains, one_chain_many)

    def test_new_chain_for_the_match_is_worth_more_than_a_repeat(self):
        config = small_config()
        fresh = context(config, visited=frozenset())
        seen = context(config, visited=frozenset({0, 1, 2}))

        class FakeResult:
            brands = {0}
            servings = 1
            stay = {}
            def __init__(self, ctx):
                self.end_pos = [a.pos for a in ctx.agents]
                self.end_fuel = [a.fuel for a in ctx.agents]

        self.assertGreater(score_day(fresh, FakeResult(fresh)) - end_value(fresh, FakeResult(fresh)),
                           score_day(seen, FakeResult(seen)) - end_value(seen, FakeResult(seen)))


if __name__ == "__main__":
    unittest.main()
