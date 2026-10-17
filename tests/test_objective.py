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


class FakeDay:
    """Kết quả ngày giả: chỉ những trường hàm mục tiêu đọc."""

    def __init__(self, ctx, brands=(), servings=0, end_pos=None, end_fuel=None):
        self.brands = set(brands)
        self.servings = servings
        self.end_pos = list(end_pos) if end_pos is not None else [a.pos for a in ctx.agents]
        self.end_fuel = list(end_fuel) if end_fuel is not None else [a.fuel for a in ctx.agents]
        self.stay = {}


class LexicographicOrderTest(unittest.TestCase):
    """Tiêu chí 1 (L) phải thắng mọi phần của tiêu chí 2 (D) trong ngày (cases 9.7, 10.8)."""

    def test_one_new_type_beats_every_type_already_seen(self):
        config = small_config(days=3)
        for day in (0, 2):                     # cả ngày đầu lẫn ngày cuối
            ctx = context(config, day=day, visited=frozenset({0, 1}))
            new_only = FakeDay(ctx, brands={2}, servings=1)
            old_ones = FakeDay(ctx, brands={0, 1}, servings=config.n_agents * len(config.spots))
            self.assertGreater(score_day(ctx, new_only) - end_value(ctx, new_only),
                               score_day(ctx, old_ones) - end_value(ctx, old_ones),
                               f"ngày {day}: 1 loại mới phải hơn {len(old_ones.brands)} loại đã có")

    def test_new_type_bonus_exceeds_a_full_day_of_daily_types(self):
        ctx = context(small_config())
        self.assertGreater(ctx.new_brand_value, len(ctx.config.brands) * ctx.weights.B)


class StrandedFuelTest(unittest.TestCase):
    """Xăng tính theo ô ĐANG ĐỨNG: còn 1 xăng trên đường là kẹt, trên đồng bằng thì không (cases 7.11)."""

    def setUp(self):
        self.config = small_config(days=3)
        # không có xe tiếp, để phạt kẹt xăng không bị xe tiếp ở gần xoá đi
        self.ctx = context(self.config, kinds=[rules.PATROL] * 3)
        self.penalty = self.ctx.weights.strand * self.ctx.weights.B * self.ctx.days_left

    def value_at(self, cell, fuel):
        ends = [cell, 6, 9]
        fuels = [fuel, self.config.fuel_limits, self.config.fuel_limits]
        return end_value(self.ctx, FakeDay(self.ctx, end_pos=ends, end_fuel=fuels))

    def test_one_fuel_on_a_road_cell_is_stranded_even_next_to_a_plain(self):
        road = 8                               # ô đường, kề ô đồng bằng 1, 2, 9
        self.assertEqual(self.config.cells[road], rules.ROAD)
        self.assertGreater(self.value_at(road, 2) - self.value_at(road, 1), 0.99 * self.penalty)

    def test_one_fuel_on_a_plain_cell_is_not_stranded(self):
        plain = 3
        self.assertEqual(self.config.cells[plain], rules.PLAIN)
        self.assertLess(self.value_at(plain, 2) - self.value_at(plain, 1), 0.5 * self.penalty)


if __name__ == "__main__":
    unittest.main()
