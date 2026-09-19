"""Dự báo giao thông và quy tắc tính lưu lượng."""

import unittest

from hexudon import rules
from hexudon.model import MatchConfig, Spot
from hexudon.simulator import road_status_from_history
from hexudon.traffic import TrafficForecast, accuracy, popularity


def corridor_config(players=4, busy=1, jam=3):
    """Bản đồ 5x1: hai đầu là đồng bằng, giữa là hành lang đường một ô."""
    return MatchConfig(
        width=5, height=1,
        cells=[rules.PLAIN, rules.ROAD, rules.ROAD, rules.ROAD, rules.PLAIN],
        spots=[Spot(0, 0, 1), Spot(1, 4, 1)],
        agent_starts=[0, 4, 0],
        fuel_limits=20,
        day_steps=[20, 20, 20, 20],
        day_seconds=[5, 5, 5, 5],
        players=players,
        busy_threshold=busy,
        jammed_threshold=jam,
    )


class TrafficRuleTest(unittest.TestCase):
    def test_volume_is_divided_by_team_count(self):
        config = corridor_config(players=4, busy=1, jam=3)
        # 12 stay-step của mọi đội trên ô 2 trong một ngày -> lưu lượng 3 -> ùn tắc
        status = road_status_from_history(config, [{2: 12}])
        self.assertEqual(status[2], rules.JAMMED)
        # 4 stay-step -> lưu lượng 1 -> đông đúc
        status = road_status_from_history(config, [{2: 4}])
        self.assertEqual(status[2], rules.CONGESTED)
        # 3 stay-step -> lưu lượng 0.75 -> vẫn thông thoáng
        status = road_status_from_history(config, [{2: 3}])
        self.assertEqual(status[2], rules.SMOOTH)

    def test_only_two_previous_days_count(self):
        config = corridor_config(players=1, busy=1, jam=3)
        status = road_status_from_history(config, [{2: 10}, {2: 0}, {2: 0}])
        self.assertEqual(status[2], rules.SMOOTH, "ngày thứ ba trước đó không được tính")

    def test_day_one_has_no_history(self):
        config = corridor_config()
        status = road_status_from_history(config, [])
        self.assertTrue(all(v == rules.SMOOTH for v in status.values()))


class ForecastTest(unittest.TestCase):
    def test_corridor_is_the_most_popular_cell(self):
        config = corridor_config()
        pop = popularity(config)
        self.assertEqual(set(pop), {1, 2, 3})
        self.assertEqual(max(pop.values()), 1.0)
        self.assertGreater(pop[2], 0.0)

    def test_forecast_degrades_over_days(self):
        config = corridor_config(players=2, busy=1, jam=3)
        forecast = TrafficForecast(config, n_patrols=2)
        day1 = forecast.static_guess(1)
        day3 = forecast.static_guess(3)
        self.assertLessEqual(day1[2], day3[2], "đường chỉ xấu đi hoặc giữ nguyên theo ngày")

    def test_accuracy_counts_optimistic_errors(self):
        predicted = {1: rules.SMOOTH, 2: rules.JAMMED, 3: rules.CONGESTED}
        actual = {1: rules.JAMMED, 2: rules.JAMMED, 3: rules.CONGESTED}
        stats = accuracy(predicted, actual)
        self.assertAlmostEqual(stats["correct"], 2 / 3)
        self.assertAlmostEqual(stats["optimistic"], 1 / 3)


if __name__ == "__main__":
    unittest.main()
