"""Kiểm toàn tuyến: chiến lược nhiều ngày phải luôn nộp bài hợp lệ và có điểm."""

import unittest

from hexudon import rules
from hexudon.model import AgentState, DayState
from hexudon.offline.bots import GreedyBot, SampleBot, SearchBot, WaitBot
from hexudon.offline.mapgen import generate
from hexudon.offline.runner import run_match
from hexudon.planner.strategy import MatchStrategist
from hexudon.simulator import run_day


class MatchLevelTest(unittest.TestCase):
    def test_search_bot_beats_wait_and_sample(self):
        config = generate(seed=21, size=12, agents=4, days=4)
        config.players = 3
        bots = [SearchBot(config, search_seconds=0.4, role_seconds=2.0, seed=1),
                SampleBot(config, seed=2),
                WaitBot(config, seed=3)]
        outcome = run_match(config, bots)
        ours, sample, wait = outcome.teams
        self.assertEqual(ours.invalid_days, 0)
        self.assertGreater(ours.score(), sample.score())
        self.assertGreater(ours.score(), wait.score())

    def test_no_invalid_plans_across_many_random_maps(self):
        for seed in range(6):
            config = generate(seed=100 + seed, size=10, agents=3 + seed % 4, days=4)
            config.players = 2
            bots = [SearchBot(config, search_seconds=0.2, role_seconds=1.0, seed=seed),
                    GreedyBot(config, seed=seed + 50)]
            outcome = run_match(config, bots)
            for team in outcome.teams:
                self.assertEqual(team.invalid_days, 0,
                                 f"seed {seed}: đội {team.name} nộp bài không hợp lệ")

    def test_strategist_respects_the_deadline(self):
        import time

        config = generate(seed=7, size=16, agents=5, days=4)
        strategist = MatchStrategist(config, search_seconds=0.5, net_margin=0.0, seed=1)
        strategist.kinds = [rules.PATROL] * config.n_agents
        strategist.kinds[-1] = rules.REFUEL
        agents = [AgentState(strategist.kinds[i], config.agent_starts[i], config.fuel_limits)
                  for i in range(config.n_agents)]
        state = DayState(day=0, agents=agents, road_status={})
        started = time.monotonic()
        decoded = strategist.plan_day(state, deadline=started + 0.5)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 1.5, "planner phải dừng gần đúng hạn chót")
        self.assertTrue(decoded.result.ok)

    def test_safe_plan_is_submitted_before_search_finishes(self):
        import time

        config = generate(seed=9, size=16, agents=5, days=4)
        strategist = MatchStrategist(config, search_seconds=2.0, net_margin=0.0, seed=1)
        strategist.kinds = [rules.PATROL] * config.n_agents
        strategist.kinds[-1] = rules.REFUEL
        agents = [AgentState(strategist.kinds[i], config.agent_starts[i], config.fuel_limits)
                  for i in range(config.n_agents)]
        state = DayState(day=0, agents=agents, road_status={})
        stamps = []
        started = time.monotonic()

        def submit(plan):
            stamps.append(time.monotonic() - started)
            result = run_day(config, config.steps_of_day(0), agents, plan,
                             [s.stocks for s in config.spots], {})
            self.assertTrue(result.ok, f"bài gửi đi không hợp lệ: {result.error}")
            return True

        strategist.plan_day(state, deadline=started + 2.0, submit=submit)
        self.assertGreaterEqual(len(stamps), 1)
        self.assertLess(stamps[0], 0.7, "bài an toàn phải được gửi trong 0.7 giây đầu")

    def test_match_memory_accumulates_chains(self):
        config = generate(seed=31, size=12, agents=4, days=4)
        bot = SearchBot(config, search_seconds=0.2, role_seconds=1.0, seed=2)
        outcome = run_match(config, [bot])
        strategist = bot.strategist
        self.assertEqual(len(strategist.history), config.n_days)
        self.assertEqual(strategist.score_tuple()[0], len(outcome.teams[0].brands))


if __name__ == "__main__":
    unittest.main()
