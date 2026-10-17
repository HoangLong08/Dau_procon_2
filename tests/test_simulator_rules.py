"""Luật trong simulator theo cases.md: kiểm xăng chặt, gộp lệnh chờ, bài dự phòng."""

import unittest

from hexudon import rules
from hexudon.model import AgentState, MatchConfig, Spot
from hexudon.simulator import compact_plan, idle_plan, run_day, wait_plan
from tests.test_simulator_golden import A, PLAN, ROAD_STATUS, build_agents, build_config


def corridor(players=2):
    """Bản đồ 5x1: đồng bằng, ba ô đường, đồng bằng. Spot ở hai đầu."""
    return MatchConfig(
        width=5, height=1,
        cells=[rules.PLAIN, rules.ROAD, rules.ROAD, rules.ROAD, rules.PLAIN],
        spots=[Spot(0, 0, 1), Spot(1, 4, 1)],
        agent_starts=[1, 2, 3],
        fuel_limits=10,
        day_steps=[12],
        day_seconds=[5],
        players=players,
        busy_threshold=1,
        jammed_threshold=3,
    )


class StrictFuelTest(unittest.TestCase):
    """Ví dụ chính thức cho nạp giữa lúc đi; câu chữ của đề thì không (cases 7.2, 11.9)."""

    def test_official_example_relies_on_a_mid_move_refuel(self):
        config = build_config()
        official = run_day(config, 6, build_agents(), PLAN, [4], ROAD_STATUS)
        self.assertTrue(official.ok, "mặc định phải khớp ví dụ 補足資料")
        strict = run_day(config, 6, build_agents(), PLAN, [4], ROAD_STATUS, strict_fuel=True)
        self.assertFalse(strict.ok)
        self.assertEqual(strict.error.code, rules.E_NO_FUEL)
        # xe A nhận lệnh rời ô đường 1 ở bước 1 khi chỉ còn 1 xăng (cần 2)
        self.assertEqual((strict.error.agent, strict.error.step), (A, 1))

    def test_strict_mode_accepts_a_car_that_arrives_before_the_order(self):
        config = corridor()
        # xe tuần tra ở ô 1 (đường) hết xăng; xe tiếp đi từ ô 2 sang ô 1, tới ở bước 1;
        # xe tuần tra đợi 1 bước rồi mới nhận lệnh đi, lúc đó đã đầy bình
        agents = [AgentState(rules.PATROL, 1, 0), AgentState(rules.REFUEL, 2, 0),
                  AgentState(rules.PATROL, 3, 10)]
        plan = [[-1, 5, -10], [5, -11], [-12]]
        result = run_day(config, 12, agents, plan, [1, 1], {}, strict_fuel=True)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.end_pos[0], 0)

    def test_strict_mode_rejects_ordering_before_the_car_arrives(self):
        config = corridor()
        agents = [AgentState(rules.PATROL, 1, 0), AgentState(rules.REFUEL, 2, 0),
                  AgentState(rules.PATROL, 3, 10)]
        # đường đông đúc: xe tuần tra nhận lệnh ở bước 0 khi còn 0 xăng, xe tiếp tới ở bước 1
        plan = [[5, -10], [5, -11], [-12]]
        status = {1: rules.CONGESTED}
        self.assertTrue(run_day(config, 12, agents, plan, [1, 1], status).ok,
                        "theo ví dụ chính thức, nạp ở bước 1 vẫn kịp")
        strict = run_day(config, 12, agents, plan, [1, 1], status, strict_fuel=True)
        self.assertEqual(strict.error.code, rules.E_NO_FUEL)


class CompactPlanTest(unittest.TestCase):
    def test_consecutive_waits_merge_and_the_day_plays_the_same(self):
        config = corridor()
        agents = [AgentState(rules.PATROL, 1, 10), AgentState(rules.REFUEL, 2, 0),
                  AgentState(rules.PATROL, 3, 10)]
        plan = [[5, -3, -8], [-2, -4, -6], [2, -1, -1, -9]]
        self.assertEqual(compact_plan(plan), [[5, -11], [-12], [2, -11]])
        a = run_day(config, 12, agents, plan, [1, 1], {})
        b = run_day(config, 12, agents, compact_plan(plan), [1, 1], {})
        self.assertTrue(a.ok and b.ok)
        self.assertEqual((a.end_pos, a.end_fuel, a.collections, a.stay),
                         (b.end_pos, b.end_fuel, b.collections, b.stay))

    def test_moves_are_never_merged(self):
        self.assertEqual(compact_plan([[0, 0, -1], [-1, 0, -1]]), [[0, 0, -1], [-1, 0, -1]])


class IdlePlanTest(unittest.TestCase):
    """Bài dự phòng không được đứng cả ngày trên ô đường (cases 11.4)."""

    def test_agents_on_road_step_onto_a_plain_then_wait(self):
        config = corridor()
        agents = [AgentState(rules.PATROL, 1, 10), AgentState(rules.REFUEL, 3, 0),
                  AgentState(rules.PATROL, 0, 10)]
        plan = idle_plan(config, agents, 12, {})
        result = run_day(config, 12, agents, plan, [1, 1], {}, strict_fuel=True)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.end_pos, [0, 4, 0])
        road_stay = sum(n for cell, n in result.stay.items() if config.cells[cell] == rules.ROAD)
        self.assertEqual(road_stay, 0, "rời ô đường ngay bước đầu thì không để lại vết nào")
        self.assertEqual(plan[2], [-12], "xe không ở ô đường thì đứng yên như cũ")

    def test_a_patrol_without_fuel_to_leave_the_road_just_waits(self):
        config = corridor()
        agents = [AgentState(rules.PATROL, 1, 1), AgentState(rules.REFUEL, 4, 0),
                  AgentState(rules.PATROL, 0, 10)]
        plan = idle_plan(config, agents, 12, {})
        self.assertEqual(plan[0], [-12], "rời ô đường cần 2 xăng; còn 1 thì phải chờ")
        self.assertTrue(run_day(config, 12, agents, plan, [1, 1], {}, strict_fuel=True).ok)

    def test_falls_back_to_waiting_when_the_day_is_too_short(self):
        config = corridor()
        agents = [AgentState(rules.PATROL, 1, 10), AgentState(rules.REFUEL, 2, 0),
                  AgentState(rules.PATROL, 3, 10)]
        jammed = {c: rules.JAMMED for c in config.road_cells}
        self.assertEqual(idle_plan(config, agents, 3, jammed), wait_plan(3, 3),
                         "ô ùn tắc cần 4 bước; ngày 3 bước thì không rời được")


if __name__ == "__main__":
    unittest.main()
