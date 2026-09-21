"""Server giả phải nói đúng giao thức đã quan sát trên server thật."""

import unittest
import urllib.error
import urllib.request

from hexudon.client.net import JsonHttp
from hexudon.client.web_api import WebApi
from hexudon.model import AgentState, DayState
from hexudon.offline.bots import GreedyBot
from hexudon.offline.mapgen import generate
from hexudon.offline.web_server import Faults, WebWorld, serve_web
from hexudon.simulator import run_day, wait_plan


class Clock:
    """Đồng hồ điều khiển được: test tự đẩy thời gian, không phải ngủ."""

    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


def small_config(days=2, day_seconds=10):
    config = generate(seed=21, size=8, agents=3, days=days)
    config.day_seconds = [day_seconds] * config.n_days
    config.__post_init__()
    return config


class WebServerCase(unittest.TestCase):
    def start(self, faults=None):
        self.clock = Clock()
        self.world = WebWorld([(77, "DAU 1"), (78, "DAU 2")], faults=faults, clock=self.clock)
        self.httpd, _, self.url = serve_web(self.world)
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def api(self, team_id):
        token = self.world.issue_token(team_id)
        return WebApi(JsonHttp(self.url, timeout=5.0), lambda: token)


class TraceTest(unittest.TestCase):
    def test_trace_has_one_entry_per_step_and_does_not_change_the_result(self):
        config = generate(seed=5, size=8, agents=3, days=2)
        bot = GreedyBot(config, seed=1)
        kinds = bot.select_roles()
        agents = [AgentState(k, p, config.fuel_limits) for k, p in zip(kinds, config.agent_starts)]
        plan = bot.plan(DayState(day=0, agents=agents, road_status={}))
        steps = config.steps_of_day(0)
        stocks = [s.stocks for s in config.spots]
        plain = run_day(config, steps, agents, plan, stocks)
        trace = []
        traced = run_day(config, steps, agents, plan, stocks, trace=trace)
        self.assertTrue(plain.ok, plain.error)
        self.assertEqual((traced.end_pos, traced.end_fuel, traced.collections),
                         (plain.end_pos, plain.end_fuel, plain.collections))
        self.assertEqual([t for t, _, _ in trace], list(range(steps + 1)))
        self.assertEqual(trace[0][1], [a.pos for a in agents])
        self.assertEqual((trace[-1][1], trace[-1][2]), (plain.end_pos, plain.end_fuel))


class CloudflareTest(WebServerCase):
    def test_the_default_python_user_agent_is_blocked_like_the_real_server(self):
        self.start()
        req = urllib.request.Request(self.url + "/api/game/state?game_id=x")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(caught.exception.code, 403)
        self.assertEqual(caught.exception.read(), b"error code: 1010")


class ContestTimelineTest(WebServerCase):
    def test_board_is_withheld_until_the_window_opens_then_days_follow_the_clock(self):
        self.start()
        qid = self.world.add_question("Q01", small_config(days=2, day_seconds=10), 14, "Mock Contest",
                                      selection_seconds=30, pre_window=60)
        api = self.api(78)
        st = api.state(qid).payload
        self.assertEqual((st["status"], st["day"]), ("selecting_agents", -1))
        self.assertAlmostEqual(st["selection_opens_in"], 60, delta=0.01)
        self.assertTrue(api.config(qid).payload["board_withheld"])
        self.assertEqual(api.day(qid).status, 409)
        self.assertEqual(api.agent_types(qid, [0, 0, 1]).status, 409, "cửa sổ chưa mở thì chưa được chọn")

        self.clock.now += 61                         # cửa sổ chọn xe mở
        st = api.state(qid).payload
        self.assertAlmostEqual(st["selection_deadline_in"], 29, delta=0.01)
        self.assertIn("cells", api.config(qid).payload["map"])
        self.assertEqual(api.agent_types(qid, [0, 0, 1]).status, 200)

        self.clock.now += 30                         # ngày 0 mở
        st = api.state(qid).payload
        self.assertEqual((st["status"], st["day"]), ("in_progress", 0))
        self.assertAlmostEqual(st["day_deadline_in"], 9, delta=0.01)
        self.assertEqual([a["kind"] for a in api.day(qid).payload["agents"]], [0, 0, 1])
        steps = st["steps_today"]
        self.assertEqual(api.actions(qid, 0, wait_plan(3, steps)).status, 200)
        bad = api.actions(qid, 0, [[-(steps + 5)]] * 3)
        self.assertEqual(bad.status, 400)
        self.assertIn("E_STEP_OVERFLOW", bad.payload["detail"])

        self.clock.now += 20                         # hết cả hai ngày
        st = api.state(qid).payload
        self.assertEqual((st["status"], st["day"]), ("finished", 2))
        detail = api.result(qid).payload["detail"]
        self.assertEqual(detail["78"]["days_submitted"], 1)
        self.assertTrue(detail["77"]["missed_selection"])

    def test_replay_gives_every_step_to_the_caller_and_only_the_last_to_others(self):
        self.start()
        config = small_config(days=1, day_seconds=10)
        qid = self.world.add_question("Q01", config, 14, "Mock Contest", selection_seconds=5)
        api = self.api(78)
        self.clock.now += 16
        teams = {t["team_id"]: t for t in api.replay(qid).payload["days"][0]["teams"]}
        steps = config.steps_of_day(0)
        self.assertEqual([f["step"] for f in teams["78"]["frames"]], list(range(steps + 1)))
        self.assertEqual([f["step"] for f in teams["77"]["frames"]], [steps])


class PracticeTest(WebServerCase):
    def test_practice_is_self_paced_resettable_and_scored_by_composite_id(self):
        self.start()
        config = small_config(days=2)
        qid = self.world.add_question("Q01", config, 11, "Practice Match", practice=True)
        api = self.api(78)
        st = api.state(qid).payload
        self.assertEqual((st["status"], st["day"], st["selection_deadline_in"]),
                         ("selecting_agents", -1, None))
        self.assertEqual(api.day(qid).status, 409)
        self.assertEqual(api.agent_types(f"{qid}:78", [0, 0, 0]).status, 200)
        self.assertEqual(api.state(qid).payload["day"], 0)
        self.assertEqual(api.practice_actions(f"{qid}:78", 0, wait_plan(3, config.steps_of_day(0))).status, 200)
        self.assertEqual(api.state(qid).payload["day"], 1, "practice sang ngày ngay khi nộp")
        self.assertEqual(api.practice_score(qid).status, 404, "ID trần bị 404 như server thật")
        self.assertEqual(api.practice_score(f"{qid}:78").payload["detail"]["78"]["days_submitted"], 1)
        self.assertEqual(api.actions(qid, 1, wait_plan(3, config.steps_of_day(1))).status, 400,
                         "practice không nhận /api/game/actions")
        self.assertEqual(api.practice_reset(f"{qid}:78").status, 200)
        self.assertEqual(api.state(qid).payload["status"], "selecting_agents")


class ManagerAndAuthTest(WebServerCase):
    def test_manager_lists_questions_and_revoked_tokens_get_401(self):
        self.start()
        qid = self.world.add_question("Q01", small_config(), 14, "Mock Contest", selection_seconds=5)
        token = self.world.issue_token(78)
        api = WebApi(JsonHttp(self.url), lambda: token)
        rows = api.questions().payload["data"]
        self.assertEqual([(r["id"], r["match_id"], r["name"]) for r in rows], [(qid, 14, "Q01")])
        self.assertEqual([t["id"] for t in api.matches().payload["data"][0]["teams"]], [77, 78])
        self.world.revoke(token)
        self.assertEqual(api.state(qid).status, 401)
        self.assertEqual(api.questions().status, 401)

    def test_rate_limit_fault_returns_429_with_retry_after(self):
        self.start(faults=Faults(rate_limit_every=2))
        qid = self.world.add_question("Q01", small_config(), 14, "Mock Contest", selection_seconds=5)
        api = self.api(78)
        responses = [api.state(qid) for _ in range(4)]
        self.assertEqual([r.status for r in responses], [200, 429, 200, 429])
        self.assertEqual(responses[1].headers.get("Retry-After"), "0.2")


if __name__ == "__main__":
    unittest.main()
