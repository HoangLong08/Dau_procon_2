"""Strategist và SearchBot phát sự kiện suy luận khi có emit; không có emit thì như cũ."""

import json
import time
import unittest

from hexudon import rules
from hexudon.model import AgentState, DayState
from hexudon.offline.bots import SearchBot
from hexudon.offline.mapgen import generate
from hexudon.planner.strategy import MatchStrategist
from hexudon.simulator import wait_plan
from hexudon.viz.shapes import agents_payload, config_payload, simulate, status_payload


class Recorder:
    def __init__(self):
        self.events = []

    def __call__(self, kind, data):
        json.dumps(data)          # mọi sự kiện phải ghi được ra JSON
        self.events.append((kind, data))

    def kinds(self):
        return [k for k, _ in self.events]

    def of(self, kind):
        return [d for k, d in self.events if k == kind]


def first_day_state(config, kinds):
    agents = [AgentState(kinds[i], config.agent_starts[i], config.fuel_limits)
              for i in range(config.n_agents)]
    return DayState(day=0, agents=agents, road_status={}, others=[])


class ShapesTest(unittest.TestCase):
    def test_config_payload_keeps_what_the_page_draws(self):
        config = generate(seed=5, size=8, agents=3, days=2)
        p = config_payload(config)
        self.assertEqual((p["width"], p["height"], p["n_days"]), (8, 8, 2))
        self.assertEqual(len(p["cells"]), 64)
        self.assertEqual(len(p["spots"]), len(config.spots))
        self.assertEqual(p["starts"], config.agent_starts)
        json.dumps(p)

    def test_status_payload_drops_smooth_cells(self):
        self.assertEqual(status_payload({3: 0, 5: 2, 1: 1}), {"1": 1, "5": 2})

    def test_simulate_traces_every_step_and_reports_errors(self):
        config = generate(seed=5, size=8, agents=3, days=2)
        agents = [AgentState(rules.PATROL, p, config.fuel_limits) for p in config.agent_starts]
        steps = config.steps_of_day(0)
        stocks = [s.stocks for s in config.spots]
        result, trace = simulate(config, agents, wait_plan(3, steps), steps, stocks, {})
        self.assertTrue(result.ok)
        self.assertEqual(len(trace["pos"]), steps + 1)
        self.assertEqual(trace["pos"][-1], config.agent_starts)
        self.assertIsNone(trace["error"])
        bad, bad_trace = simulate(config, agents, [[-steps]], steps, stocks, {})
        self.assertFalse(bad.ok)
        self.assertEqual(bad_trace["error"]["code"], rules.E_BAD_FORMAT)
        self.assertEqual(agents_payload(agents)[0],
                         {"kind": rules.PATROL, "pos": config.agent_starts[0], "fuel": config.fuel_limits})


class StrategistEventsTest(unittest.TestCase):
    def setUp(self):
        self.config = generate(seed=11, size=8, agents=3, days=2)
        self.rec = Recorder()
        self.strat = MatchStrategist(self.config, search_seconds=0.4, net_margin=0.0,
                                     role_seconds=1.0, emit=self.rec)

    def test_role_selection_reports_candidates_then_the_choice(self):
        kinds = self.strat.select_roles(cap_by_start=False)
        self.assertTrue(self.rec.of("roles_candidate"))
        self.assertEqual(self.rec.kinds()[-1], "roles_chosen")
        self.assertEqual(self.rec.of("roles_chosen")[-1]["kinds"], kinds)

    def test_a_day_reports_safe_plan_progress_improvements_and_forecast(self):
        kinds = self.strat.select_roles(cap_by_start=False)
        best = self.strat.plan_day(first_day_state(self.config, kinds), deadline=time.monotonic() + 0.4)
        seen = self.rec.kinds()
        [safe] = self.rec.of("plan_safe")
        self.assertLess(seen.index("plan_safe"), seen.index("alns_progress"))
        self.assertEqual(len(safe["trace"]["pos"]), self.config.steps_of_day(0) + 1)
        self.assertIn("ms", safe)
        self.assertTrue(all(p["day"] == 0 for p in self.rec.of("alns_progress")))
        scores = [safe["score"]] + [b["score"] for b in self.rec.of("alns_better")]
        self.assertEqual(scores, sorted(scores))
        self.assertAlmostEqual(max(scores), round(best.score, 3), places=3)
        [forecast] = self.rec.of("forecast")
        self.assertEqual(forecast["day"], 1)

    def test_without_emit_plans_still_come_back(self):
        strat = MatchStrategist(self.config, search_seconds=0.2, net_margin=0.0, role_seconds=0.5)
        kinds = strat.select_roles(cap_by_start=False)
        best = strat.plan_day(first_day_state(self.config, kinds), deadline=time.monotonic() + 0.2)
        self.assertEqual(len(best.plan), self.config.n_agents)


class SearchBotSubmitTest(unittest.TestCase):
    def test_search_bot_reports_each_submission(self):
        config = generate(seed=13, size=8, agents=3, days=2)
        rec = Recorder()
        bot = SearchBot(config, search_seconds=0.3, role_seconds=0.5, seed=1, emit=rec)
        kinds = bot.select_roles()
        plan = bot.plan(first_day_state(config, kinds))
        self.assertEqual(len(plan), config.n_agents)
        submits = rec.of("submit")
        self.assertGreaterEqual(len(submits), 1)
        self.assertEqual([s["n"] for s in submits], list(range(1, len(submits) + 1)))
        self.assertTrue(all(s["ok"] and s["day"] == 0 for s in submits))

    def test_search_bot_without_emit_keeps_emit_off(self):
        config = generate(seed=13, size=8, agents=3, days=2)
        bot = SearchBot(config, search_seconds=0.2, role_seconds=0.5, seed=1)
        self.assertIsNone(bot.emit)
        self.assertIsNone(bot.strategist.emit)
