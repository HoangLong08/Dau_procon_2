"""Golden test: tái hiện ví dụ từng bước trong tài liệu 補足資料 (行動詳細) của BTC.

Bản đồ 4 ô một hàng: ô 0 và 1 là đường (0 thông thoáng, 1 đông đúc),
ô 2 và 3 là đồng bằng. Spot ở ô 2 với kho 4. Nhiên liệu tối đa 3. Ngày 6 bước.
Tám tác nhân: xe tuần tra A..G và xe tiếp nhiên liệu A.
"""

import unittest

from hexudon import rules
from hexudon.model import AgentState, MatchConfig, Spot
from hexudon.simulator import run_day

A, B, C, D, E, F, G, CAR = range(8)


def build_config():
    return MatchConfig(
        width=4,
        height=1,
        cells=[rules.ROAD, rules.ROAD, rules.PLAIN, rules.PLAIN],
        spots=[Spot(brand=0, pos=2, stocks=4)],
        agent_starts=[0, 0, 0, 0, 2, 3, 3, 2],
        fuel_limits=3,
        day_steps=[6],
        day_seconds=[60],
        players=1,
        busy_threshold=1,
        jammed_threshold=3,
    )


def build_agents():
    return [
        AgentState(rules.PATROL, 0, 3),   # A
        AgentState(rules.PATROL, 0, 3),   # B
        AgentState(rules.PATROL, 0, 3),   # C
        AgentState(rules.PATROL, 0, 3),   # D
        AgentState(rules.PATROL, 2, 3),   # E
        AgentState(rules.PATROL, 3, 3),   # F
        AgentState(rules.PATROL, 3, 3),   # G
        AgentState(rules.REFUEL, 2, 0),   # xe tiếp nhiên liệu A
    ]


PLAN = [
    [2, 2, 2, -1],   # A: phải, phải, phải, đợi 1
    [-1, 2, 2, 2],   # B
    [-2, 2, 2, -1],  # C
    [-3, 2, -2],     # D
    [5, 5, -2],      # E: trái, trái, đợi 2
    [5, 5, -2],      # F
    [-2, 5, 5],      # G
    [5, 5, -2],      # xe tiếp
]

ROAD_STATUS = {0: rules.SMOOTH, 1: rules.CONGESTED}


class GoldenExampleTest(unittest.TestCase):
    def setUp(self):
        self.config = build_config()
        self.result = run_day(self.config, 6, build_agents(), PLAN, [4], ROAD_STATUS)

    def test_plan_is_valid(self):
        self.assertIsNone(self.result.error)
        self.assertTrue(self.result.ok)

    def test_final_positions(self):
        # A và B tới ô 3; C dừng ở ô 2; D, F, G ở ô 1; E và xe tiếp ở ô 0
        self.assertEqual(self.result.end_pos, [3, 3, 2, 1, 0, 1, 1, 0])

    def test_final_fuel_of_patrols(self):
        # E luôn đầy bình vì đi cùng ô với xe tiếp; F đi sau một ô nên không được tiếp
        self.assertEqual(self.result.end_fuel[:7], [0, 0, 1, 1, 3, 1, 1])

    def test_collections_and_stock(self):
        # E thu ở bước 1 (đứng sẵn trên spot), F bước 2, A bước 3, B bước 4 -> hết kho
        self.assertEqual(self.result.collections, [(1, E, 0), (2, F, 0), (3, A, 0), (4, B, 0)])
        self.assertEqual(self.result.stocks_left, [0])
        self.assertEqual(self.result.servings, 4)
        self.assertEqual(self.result.brands, {0})

    def test_loser_of_stock_race_is_higher_index(self):
        # B (chỉ số 1) và G (chỉ số 6) cùng tới ô 2 ở bước 4, kho còn 1: B thắng
        winners = {agent for (_, agent, _) in self.result.collections}
        self.assertIn(B, winners)
        self.assertNotIn(G, winners)
        self.assertNotIn(C, winners)   # C tới nơi khi kho đã hết
        self.assertNotIn(D, winners)

    def test_traffic_stay_counts(self):
        # con số trong tài liệu gốc: ô 0 = 12, ô 1 = 17, ô 2 = 12, ô 3 = 7
        self.assertEqual(self.result.stay, {0: 12, 1: 17, 2: 12, 3: 7})
        self.assertEqual(sum(self.result.stay.values()), 8 * 6)


class InvalidPlanTest(unittest.TestCase):
    def setUp(self):
        self.config = build_config()
        self.agents = build_agents()

    def run_plan(self, plan):
        return run_day(self.config, 6, self.agents, plan, [4], ROAD_STATUS)

    def test_move_off_map_is_not_adjacent(self):
        plan = [list(p) for p in PLAN]
        plan[A] = [5, -5]          # A ở ô 0 đi sang trái -> ra ngoài bản đồ
        self.assertEqual(self.run_plan(plan).error.code, rules.E_NOT_ADJACENT)

    def test_step_total_must_match_exactly(self):
        plan = [list(p) for p in PLAN]
        plan[A] = [2, 2, 2]        # thiếu lệnh đợi cuối -> thừa bước trống
        self.assertEqual(self.run_plan(plan).error.code, rules.E_STEP_OVERFLOW)

    def test_step_overflow_when_move_cannot_finish(self):
        plan = [list(p) for p in PLAN]
        plan[C] = [-2, 2, 2, 2]    # C không đủ bước để đi tiếp từ ô 2
        self.assertEqual(self.run_plan(plan).error.code, rules.E_STEP_OVERFLOW)

    def test_out_of_fuel(self):
        agents = build_agents()
        agents[G] = AgentState(rules.PATROL, 3, 0)   # G hết xăng
        plan = [list(p) for p in PLAN]
        result = run_day(self.config, 6, agents, plan, [4], ROAD_STATUS)
        self.assertEqual(result.error.code, rules.E_NO_FUEL)
        self.assertEqual(result.error.agent, G)

    def test_bad_format(self):
        plan = [list(p) for p in PLAN]
        plan[A] = [9, -4]
        self.assertEqual(self.run_plan(plan).error.code, rules.E_BAD_FORMAT)

    def test_wait_plan_is_always_valid(self):
        from hexudon.simulator import wait_plan
        result = run_day(self.config, 6, self.agents, wait_plan(8, 6), [4], ROAD_STATUS)
        self.assertTrue(result.ok)
        # E đứng sẵn trên spot nên vẫn thu được 1 phần ở bước 1
        self.assertEqual(result.collections, [(1, E, 0)])


if __name__ == "__main__":
    unittest.main()
