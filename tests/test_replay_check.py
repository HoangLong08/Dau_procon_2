"""Đối chiếu replay: khớp khi server chạy đúng luật của ta, báo đúng chỗ khi lệch."""

import copy
import json
import os
import unittest

from hexudon.client.net import JsonHttp
from hexudon.client.replay_check import check
from hexudon.client.session import MatchSession
from hexudon.client.web_api import WebApi
from hexudon.client.web_games import PRACTICE, GameRef
from hexudon.client.web_transport import WebTransport
from hexudon.model import AgentState, DayState, MatchConfig
from hexudon.offline.bots import GreedyBot
from hexudon.offline.mapgen import generate
from hexudon.offline.web_server import WebWorld, frames_from_trace, serve_web
from hexudon.simulator import run_day

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "web")


def fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        return json.load(fh)


def play_and_record(config, team_id=78, skip_days=()):
    """Chơi GreedyBot vài ngày và dựng replay đúng dạng server thật gửi cho đội mình."""
    bot = GreedyBot(config, seed=2)
    kinds = bot.select_roles()
    agents = [AgentState(k, p, config.fuel_limits) for k, p in zip(kinds, config.agent_starts)]
    plans, days = {}, []
    for d in range(config.n_days):
        steps = config.steps_of_day(d)
        plan = bot.plan(DayState(day=d, agents=agents, road_status={}))
        if d in skip_days:
            plan = [[-steps] for _ in agents]
        else:
            plans[d] = plan
        trace = []
        result = run_day(config, steps, agents, plan, [s.stocks for s in config.spots], {}, trace=trace)
        assert result.ok, result.error
        days.append({"day": d, "steps": steps, "road_condition": {},
                     "teams": [{"team_id": str(team_id), "kinds": kinds, "servings": result.servings,
                                "types": len(result.brands), "submitted": d not in skip_days,
                                "frames": frames_from_trace(config, kinds, trace, result.collections)}]})
        agents = [AgentState(kinds[i], result.end_pos[i], result.end_fuel[i]) for i in range(config.n_agents)]
    return {"days": days}, plans


class ReplayCheckTest(unittest.TestCase):
    def setUp(self):
        self.config = generate(seed=8, size=10, agents=4, days=3)

    def test_a_replay_from_our_own_rules_matches_everywhere(self):
        replay, plans = play_and_record(self.config)
        report = check(self.config, 78, replay, plans)
        self.assertEqual(report.days_checked, 3)
        self.assertTrue(report.ok, report.summary())

    def test_a_changed_fuel_value_is_reported_at_the_right_day_step_and_agent(self):
        replay, plans = play_and_record(self.config)
        broken = copy.deepcopy(replay)
        broken["days"][1]["teams"][0]["frames"][5]["agents"][0]["fuel"] += 7
        report = check(self.config, 78, broken, plans)
        self.assertFalse(report.ok)
        first = report.mismatches[0]
        self.assertEqual((first.day, first.step, first.agent, first.what), (1, 5, 0, "fuel"))
        self.assertEqual(report.days_with_mismatch(), [1])
        self.assertIn("Ngày 2 bước 5 xe 0", report.summary())

    def test_a_day_without_submission_is_checked_as_a_wait_plan(self):
        replay, plans = play_and_record(self.config, skip_days={1})
        self.assertNotIn(1, plans)
        self.assertTrue(check(self.config, 78, replay, plans).ok)

    def test_the_real_replay_fixture_is_understood(self):
        config = MatchConfig.from_json(fixture("config.json"))
        report = check(config, 78, fixture("replay.json"), {})
        self.assertEqual(report.days_checked, 1)
        self.assertTrue(report.ok, report.summary())
        self.assertTrue(any("4 frame" in note for note in report.notes))


class FinishIntegrationTest(unittest.TestCase):
    def test_a_practice_run_is_checked_against_the_server_replay(self):
        world = WebWorld([(77, "DAU 1"), (78, "DAU 2")])
        httpd, _, url = serve_web(world)
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        config = generate(seed=8, size=10, agents=4, days=3)
        config.day_seconds = [2] * config.n_days
        config.__post_init__()
        qid = world.add_question("Q01", config, 11, "Practice Match", practice=True, selection_seconds=1.0)
        token = world.issue_token(78)
        transport = WebTransport(WebApi(JsonHttp(url), lambda: token), GameRef(qid, mode=PRACTICE), 78,
                                 poll_seconds=0.05)
        MatchSession(transport, search_seconds=0.3, role_seconds=0.3, net_margin=0.1, seed=4).run()
        result = transport.finish()["replay_check"]
        self.assertEqual(result["days_checked"], 3)
        self.assertTrue(result["ok"], result)


if __name__ == "__main__":
    unittest.main()
