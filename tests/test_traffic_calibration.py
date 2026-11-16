"""R6 (17/11/2026): mô hình lưu lượng các đội khác, xe đỗ, và tự hiệu chỉnh mỗi sáng."""

import random
import unittest

from hexudon import rules
from hexudon.offline.bots import SearchBot
from hexudon.offline.mapgen import generate
from hexudon.offline.runner import run_match
from hexudon.planner.solution import Weights
from hexudon.traffic import (LEGACY, PERSIST, PRIOR, ForecastCalibrator, ForecastModel, TrafficForecast,
                             default_models, parked_stay)

from tests.test_traffic import corridor_config


def random_inputs(config, rng):
    roads = config.road_cells
    status = {c: rng.choice([0, 0, 1, 2]) for c in roads}
    yesterday = {c: rng.choice([0, 1]) for c in roads}
    own_today = {c: rng.randint(0, 3) for c in roads}
    own_yesterday = {c: rng.randint(0, 3) for c in roads}
    parked = {c: rng.randint(0, 1) for c in roads}
    return status, yesterday, own_today, own_yesterday, parked


class ModelTest(unittest.TestCase):
    def test_without_a_model_predict_is_the_old_formula(self):
        config = corridor_config(players=4, busy=1, jam=3)
        old = TrafficForecast(config, n_patrols=3)
        self.assertIsNone(old.model)
        legacy = old.with_model(LEGACY)
        rng = random.Random(5)
        for _ in range(50):
            status, yesterday, own_today, own_yesterday, parked = random_inputs(config, rng)
            kw = dict(own_stay_today=own_today, own_stay_yesterday=own_yesterday,
                      opponents_parked=parked, steps_today=20, floor_today=rng.random() < 0.3)
            self.assertEqual(old.predict(status, yesterday, **kw),
                             legacy.predict(status, yesterday, parked_stay={2: 99}, **kw),
                             "LEGACY phải bỏ qua parked_stay và ra đúng công thức cũ")

    def test_persist_returns_today(self):
        config = corridor_config(players=10, busy=1, jam=3)
        fc = TrafficForecast(config, model=PERSIST)
        today = {1: rules.SMOOTH, 2: rules.JAMMED, 3: rules.CONGESTED}
        self.assertEqual(fc.predict(today, own_stay_today={1: 50}), today)

    def test_teams_like_us_jam_the_cell_we_use(self):
        config = corridor_config(players=10, busy=1, jam=3)
        zero_pop = {c: 0.0 for c in config.road_cells}
        alone = TrafficForecast(config, pop=zero_pop, model=ForecastModel(like_us=0.0, pop_scale=0.0))
        like_us = TrafficForecast(config, pop=zero_pop, model=ForecastModel(like_us=1.0, pop_scale=0.0))
        kw = dict(own_stay_today={2: 1}, own_stay_yesterday={2: 2})
        # một mình: (1 + 2) / 10 = 0,3 -> thông; mọi đội như ta: (3 + 9 × 3) / 10 = 3 -> tắc
        self.assertEqual(alone.predict({}, {}, **kw)[2], rules.SMOOTH)
        self.assertEqual(like_us.predict({}, {}, **kw)[2], rules.JAMMED)
        # không có hôm qua (ngày 0): chỉ hôm nay, (1 + 9) / 10 = 1 -> đông
        self.assertEqual(like_us.predict({}, None, own_stay_today={2: 1})[2], rules.CONGESTED)

    def test_parked_stay_is_added_as_stay_steps(self):
        config = corridor_config(players=2, busy=1, jam=3)
        fc = TrafficForecast(config, pop={c: 0.0 for c in config.road_cells}, model=ForecastModel())
        self.assertEqual(fc.predict({}, {}, parked_stay={2: 6})[2], rules.JAMMED, "6 / 2 = 3")
        self.assertEqual(fc.predict({}, {}, parked_stay={2: 2})[2], rules.CONGESTED, "2 / 2 = 1")

    def test_floor_today_still_holds(self):
        config = corridor_config(players=4, busy=1, jam=3)
        fc = TrafficForecast(config, pop={c: 0.0 for c in config.road_cells}, model=ForecastModel())
        today = {1: rules.SMOOTH, 2: rules.JAMMED, 3: rules.CONGESTED}
        floored = fc.predict(today, None, floor_today=True)
        self.assertEqual([floored[c] for c in (1, 2, 3)], [rules.SMOOTH, rules.JAMMED, rules.CONGESTED])


class ParkedTest(unittest.TestCase):
    def test_two_mornings_on_the_same_road_cell_is_a_whole_day(self):
        roads = [1, 2, 3]
        status = {2: rules.CONGESTED}
        prev = [[2, 0], [4]]
        now = [[2, 3], [1]]
        out = parked_stay(prev, now, roads, status, steps=30)
        # đội 0 xe 0 đứng ô 2 hai sáng: 30; đội 0 xe 1 mới tới ô 3 (thông): 1; đội 1 ở ô 1: 1
        self.assertEqual(out, {2: 30, 3: 1, 1: 1})

    def test_first_morning_and_plain_cells(self):
        out = parked_stay(None, [[2, 0, 4]], [1, 2, 3], {2: rules.JAMMED}, steps=30)
        self.assertEqual(out, {2: 4}, "đi qua ô tắc để lại 4 lượt; ô đồng bằng không tính")


def corridor_days(truth: ForecastModel, n_days=5, seed=0):
    """Sinh trạng thái đường 'thật' theo một mô hình biết trước, với vết của ta ngẫu nhiên."""
    config = corridor_config(players=10, busy=1, jam=3)
    base = TrafficForecast(config)
    fc = base.with_model(truth)
    rng = random.Random(seed)
    statuses = [{c: rules.SMOOTH for c in config.road_cells}]
    owns = []
    for day in range(n_days):
        owns.append({c: rng.randint(0, 2) for c in config.road_cells})
        if day + 1 < n_days:
            statuses.append(fc.predict(statuses[day], statuses[day - 1] if day >= 1 else None,
                                       own_stay_today=owns[day],
                                       own_stay_yesterday=owns[day - 1] if day >= 1 else None,
                                       floor_today=day == 1))
    return config, base, statuses, owns


class CalibratorTest(unittest.TestCase):
    def test_without_observations_the_prior_is_used(self):
        cal = ForecastCalibrator(TrafficForecast(corridor_config()))
        self.assertEqual(cal.best(), PRIOR)
        self.assertEqual(cal.morning(0, {}), PRIOR, "sáng ngày 0 chưa có gì để chấm")
        self.assertEqual(len(default_models()), 18)

    def test_finds_the_model_that_generated_the_roads(self):
        truth = ForecastModel(like_us=1.0, pop_scale=0.0)
        config, base, statuses, owns = corridor_days(truth, n_days=6, seed=3)
        cal = ForecastCalibrator(base)
        for day in range(len(statuses)):
            cal.morning(day, statuses[day])
            cal.evening(day, owns[day])
        i = cal.models.index(truth)
        self.assertEqual(cal.scores[i], 0, "mô hình sinh dữ liệu dự báo đúng mọi ô")
        self.assertEqual(max(cal.scores), 0)
        best = cal.best()
        self.assertEqual(cal.scores[cal.models.index(best)], 0)
        self.assertLess(min(cal.scores), 0, "có mô hình sai, nên việc chấm có ý nghĩa")
        self.assertEqual(cal.observed, len(statuses) - 1)

    def test_empty_roads_pick_a_light_model(self):
        config = corridor_config(players=10, busy=1, jam=3)
        base = TrafficForecast(config)
        cal = ForecastCalibrator(base)
        smooth = {c: rules.SMOOTH for c in config.road_cells}
        for day in range(4):
            cal.morning(day, smooth)
            cal.evening(day, {2: 3, 1: 1})
        best = cal.best()
        self.assertNotEqual(best, PRIOR, "PRIOR đoán đông ở ô ta đi nhiều; đường thật luôn thông")
        pred = base.with_model(best).predict(smooth, smooth, own_stay_today={2: 3, 1: 1},
                                             own_stay_yesterday={2: 3, 1: 1})
        self.assertTrue(all(v == rules.SMOOTH for v in pred.values()))

    def test_a_second_morning_call_does_not_count_twice(self):
        truth = ForecastModel(like_us=0.5, pop_scale=1.0)
        config, base, statuses, owns = corridor_days(truth, n_days=3)
        cal = ForecastCalibrator(base)
        cal.morning(0, statuses[0])
        cal.evening(0, owns[0])
        cal.morning(1, statuses[1])
        once = list(cal.scores)
        cal.morning(1, statuses[1])
        self.assertEqual(cal.scores, once)
        self.assertEqual(cal.observed, 1)

    def test_report_has_out_of_sample_counts(self):
        truth = ForecastModel(like_us=0.25, pop_scale=2.0)
        config, base, statuses, owns = corridor_days(truth, n_days=4)
        cal = ForecastCalibrator(base)
        for day in range(len(statuses)):
            cal.morning(day, statuses[day])
            cal.evening(day, owns[day])
        rep = cal.report()
        self.assertEqual(rep["so_sang"], 3)
        n_roads = len(config.road_cells)
        for key in ("chon", "cu", "nhu_hom_nay"):
            self.assertEqual(rep["ngoai_mau"][key][1], 3 * n_roads)


class StrategistTest(unittest.TestCase):
    def _bots(self, config, weights):
        return [SearchBot(config, seed=i, weights=weights, search_iterations=5, role_iterations=5)
                for i in range(2)]

    def test_calibrator_is_fed_every_morning(self):
        config = generate(seed=7, size=8, agents=4, days=3)
        config.players = 2
        bots = self._bots(config, Weights(traffic_calib=1.0))
        run_match(config, bots)
        for bot in bots:
            cal = bot.strategist.calibrator
            self.assertIsNotNone(cal)
            self.assertEqual(cal.observed, config.n_days - 1)
            self.assertIs(bot.strategist.forecast.model, cal.best())

    def test_default_keeps_the_old_forecast(self):
        # R6 tắt mặc định (17/11/2026): đo được dự báo đúng hơn nhưng D −0,33 mỗi trận
        self.assertEqual(Weights().traffic_calib, 0.0)
        config = generate(seed=7, size=8, agents=4, days=2)
        config.players = 2
        bots = self._bots(config, Weights())
        run_match(config, bots)
        for bot in bots:
            self.assertIsNone(bot.strategist.calibrator)
            self.assertIsNone(bot.strategist.forecast.model)


if __name__ == "__main__":
    unittest.main()
