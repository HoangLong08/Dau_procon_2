"""R5 (17/11/2026): chọn vai trò chấm trên nhiều kịch bản giao thông, chặn 0 xe tiếp, tinh chỉnh song song."""

import dataclasses
import time
import unittest
from unittest import mock

from hexudon import rules
from hexudon.offline.mapgen import generate
from hexudon.planner import roles
from hexudon.planner.roles import role_scenarios, rollout, select_roles, zero_cars_allowed
from hexudon.planner.solution import Weights
from hexudon.traffic import PRIOR, TrafficForecast


def small(fuel=None, days=3):
    cfg = generate(seed=7, size=8, agents=4, days=days)
    cfg.players = 10
    if fuel is not None:
        cfg = dataclasses.replace(cfg, fuel_limits=fuel)
    return cfg


class ScenarioTest(unittest.TestCase):
    def test_knob_selects_the_scenarios(self):
        cfg = small()
        old = role_scenarios(cfg, Weights(role_traffic=0))
        self.assertEqual([n for n, _ in old], ["cũ"])
        self.assertIsNone(old[0][1].model)
        both = role_scenarios(cfg, Weights(role_traffic=1))
        self.assertEqual([n for n, _ in both], ["cũ", "nhiều đội"])
        self.assertEqual(both[1][1].model, PRIOR)
        self.assertIs(both[0][1].pop, both[1][1].pop, "hai kịch bản dùng chung bảng độ phổ biến")
        heavy = role_scenarios(cfg, Weights(role_traffic=2))
        self.assertEqual([n for n, _ in heavy], ["nhiều đội"])

    def test_old_knob_scores_exactly_like_the_old_rollout(self):
        cfg = small()
        _, options = select_roles(cfg, weights=Weights(role_traffic=0), seed=1, iterations_per_day=5)
        legacy = TrafficForecast(cfg, n_patrols=max(1, cfg.n_agents - 1))
        for cand in options:
            if cand.evaluated:
                self.assertEqual(cand.score, rollout(cfg, cand.kinds, legacy, weights=Weights(role_traffic=0),
                                                     seed=1))

    def test_scores_are_summed_over_scenarios(self):
        cfg = small()
        w = Weights()
        _, options = select_roles(cfg, weights=w, seed=1, iterations_per_day=5)
        scen = role_scenarios(cfg, w)
        for cand in options:
            if cand.evaluated:
                parts = [rollout(cfg, cand.kinds, fc, weights=w, seed=1) for _, fc in scen]
                self.assertEqual(cand.score, tuple(sum(p[i] for p in parts) for i in range(3)))

    def test_deterministic_by_iterations(self):
        cfg = small()
        a, _ = select_roles(cfg, seed=2, iterations_per_day=8)
        b, _ = select_roles(cfg, seed=2, iterations_per_day=8)
        self.assertEqual(a, b)


class ZeroCarsTest(unittest.TestCase):
    def test_zero_cars_are_skipped_when_fuel_is_short(self):
        cfg = small(fuel=20)
        self.assertFalse(zero_cars_allowed(cfg))
        rows = []
        kinds, options = select_roles(cfg, seed=0, iterations_per_day=3, on_candidate=rows.append)
        zero = [c for c in options if c.n_cars == 0][0]
        self.assertFalse(zero.evaluated)
        self.assertIn(("skipped", zero.label), [(r["phase"], r["label"]) for r in rows])
        self.assertGreaterEqual(kinds.count(rules.REFUEL), 1)

    def test_zero_cars_stay_a_candidate_with_plenty_of_fuel(self):
        cfg = small(fuel=None)
        cfg = dataclasses.replace(cfg, fuel_limits=3 * cfg.steps_of_day(0))
        self.assertTrue(zero_cars_allowed(cfg))
        _, options = select_roles(cfg, seed=0, iterations_per_day=3)
        self.assertTrue([c for c in options if c.n_cars == 0][0].evaluated)

    def test_old_knob_does_not_skip_zero_cars(self):
        cfg = small(fuel=20)
        _, options = select_roles(cfg, weights=Weights(role_traffic=0), seed=0, iterations_per_day=3)
        self.assertTrue([c for c in options if c.n_cars == 0][0].evaluated)


class ParallelTest(unittest.TestCase):
    def test_parallel_refine_finishes_before_the_deadline(self):
        cfg = small(days=2)
        logs = []
        budget = 4.0
        t0 = time.monotonic()
        kinds, options = select_roles(cfg, budget_seconds=budget, seed=0, log=logs.append, jobs=2,
                                      parallel_min_seconds=0.5)
        elapsed = time.monotonic() - t0
        self.assertEqual(len(kinds), cfg.n_agents)
        self.assertTrue(any("song song" in m for m in logs), logs)
        self.assertLess(elapsed, budget + 1.0, f"vượt hạn {elapsed - budget:.2f}s")
        self.assertTrue(any(c.refined != (0, 0, 0) for c in options), "ít nhất một phương án được tinh chỉnh")

    def test_falls_back_to_sequential_when_the_pool_cannot_start(self):
        cfg = small(days=2)
        logs = []
        with mock.patch.object(roles, "_run_parallel", return_value=None):
            kinds, _ = select_roles(cfg, budget_seconds=2.0, seed=0, log=logs.append, jobs=2,
                                    parallel_min_seconds=0.5)
        self.assertEqual(len(kinds), cfg.n_agents)
        self.assertTrue(any("tuần tự" in m for m in logs), logs)


if __name__ == "__main__":
    unittest.main()
