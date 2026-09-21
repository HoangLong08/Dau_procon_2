"""WebTransport: 409 là chưa mở, hạn theo giây còn lại, practice gửi đúng một lần mỗi ngày."""

import time
import unittest

from hexudon.client.net import JsonHttp, Response
from hexudon.client.session import MatchSession
from hexudon.client.transport import MatchEnded, SelectionWindow
from hexudon.client.web_api import WebApi
from hexudon.client.web_games import CONTEST, PRACTICE, GameRef
from hexudon.client.web_transport import WebTransport, score_of
from hexudon.offline.mapgen import generate
from hexudon.offline.web_server import Faults, WebWorld, serve_web

NO_DAY = {"detail": "no day is open yet (the match starts with the agent-kind window)"}


def small_config(days=2, day_seconds=1.5):
    config = generate(seed=21, size=8, agents=3, days=days)
    config.day_seconds = [day_seconds] * config.n_days
    config.__post_init__()
    return config


def status(name, day=-1, **extra):
    payload = {"status": name, "day": day, "steps_today": None, "day_deadline_in": None,
               "selection_deadline_in": None, "selection_opens_in": None,
               "agent_selection_time_limit": 30.0, "road_condition": {}, "teams": {}}
    payload.update(extra)
    return Response(200, payload)


def day_response(config, day, ends_at=0.0):
    return Response(200, {"endsAt": ends_at, "day": day,
                          "agents": [{"kind": 0, "pos": p, "fuel": config.fuel_limits}
                                     for p in config.agent_starts],
                          "others": [], "traffics": []})


def config_response(config):
    return Response(200, dict(config.to_json(), agent_selection_time_limit=30.0))


class ScriptedApi:
    """WebApi giả không qua mạng: mỗi endpoint trả lần lượt các Response đã định.

    Phần tử cuối được lặp lại mãi; endpoint không có kịch bản thì ném AttributeError.
    """

    def __init__(self, **scripts):
        self.scripts = {name: list(responses) for name, responses in scripts.items()}
        self.calls = []

    def __getattr__(self, name):
        scripts = self.__dict__.get("scripts", {})
        if name not in scripts:
            raise AttributeError(name)

        def endpoint(game_id, *args, **kwargs):
            self.calls.append((name, game_id) + args)
            queue = scripts[name]
            return queue.pop(0) if len(queue) > 1 else queue[0]
        return endpoint

    def count(self, name):
        return sum(1 for call in self.calls if call[0] == name)


def scripted_transport(api, mode=CONTEST, **kw):
    kw.setdefault("sleep", lambda seconds: None)
    kw.setdefault("poll_seconds", 0.0)
    return WebTransport(api, GameRef("q1", mode=mode), 78, **kw)


class ProtocolTest(unittest.TestCase):
    def setUp(self):
        self.config = small_config()

    def test_409_on_day_means_not_open_yet_not_the_end(self):
        api = ScriptedApi(state=[status("in_progress", 0, day_deadline_in=20.0)],
                          day=[Response(409, NO_DAY), Response(409, NO_DAY), day_response(self.config, 0)])
        t = scripted_transport(api)
        t.config = self.config
        self.assertEqual(t.wait_day_state([0, 0, 0]).day, 0)
        self.assertEqual(api.count("day"), 3)

    def test_the_deadline_comes_from_seconds_left_not_the_server_clock(self):
        api = ScriptedApi(state=[status("in_progress", 0, day_deadline_in=12.5)],
                          day=[day_response(self.config, 0, ends_at=123.0)])
        t = scripted_transport(api, clock=lambda: 5000.0)
        t.config = self.config
        self.assertEqual(t.wait_day_state([0, 0, 0]).ends_at, 5012.5)

    def test_a_withheld_board_is_asked_again_and_the_window_gets_a_deadline(self):
        api = ScriptedApi(state=[status("selecting_agents", selection_deadline_in=20.0)],
                          config=[Response(200, {"board_withheld": True}), config_response(self.config)])
        t = scripted_transport(api, clock=lambda: 100.0)
        self.assertEqual(t.get_setup().n_agents, 3)
        self.assertEqual(api.count("config"), 2)
        self.assertEqual(t.selection_window(), SelectionWindow(open=True, deadline=120.0, limit=30.0))

    def test_waiting_for_the_window_sleeps_until_shortly_before_it_opens(self):
        naps = []
        api = ScriptedApi(state=[status("selecting_agents", selection_opens_in=100.0),
                                 status("selecting_agents", selection_opens_in=10.0),
                                 status("selecting_agents", selection_deadline_in=30.0)],
                          config=[config_response(self.config)])
        scripted_transport(api, sleep=naps.append).get_setup()
        self.assertEqual(naps[:2], [30.0, 1.0])

    def test_joining_a_running_match_closes_the_window(self):
        api = ScriptedApi(state=[status("in_progress", 3, day_deadline_in=9.0)],
                          config=[config_response(self.config)])
        t = scripted_transport(api)
        t.get_setup()
        self.assertFalse(t.selection_window().open)

    def test_a_finished_question_ends_before_setup(self):
        api = ScriptedApi(state=[status("finished", 10)],
                          result=[Response(200, {"ranking": ["78"], "detail": {"78": {"distinct_types": 5}}})])
        with self.assertRaises(MatchEnded):
            scripted_transport(api).get_setup()

    def test_a_rejected_token_waits_for_a_new_env_value_then_continues(self):
        reloads = iter([False, True])
        logs = []
        api = ScriptedApi(state=[Response(401, {"detail": "Could not validate credentials"}),
                                 status("in_progress", 0, day_deadline_in=10.0)],
                          config=[config_response(self.config)])
        t = scripted_transport(api, token_reload=lambda: next(reloads), token_var="TEST_DAU_02",
                               log=logs.append)
        self.assertEqual(t.get_setup().n_agents, 3)
        self.assertTrue(any("TEST_DAU_02" in m for m in logs))

    def test_a_rejected_token_gives_up_after_the_limit(self):
        api = ScriptedApi(state=[Response(401, {"detail": "expired"})])
        with self.assertRaises(MatchEnded):
            scripted_transport(api, give_up_seconds=10.0).get_setup()

    def test_a_cloudflare_block_ends_with_a_clear_message(self):
        api = ScriptedApi(state=[Response(403, {"raw": "error code: 1010"})])
        with self.assertRaises(MatchEnded) as caught:
            scripted_transport(api).get_setup()
        self.assertIn("Cloudflare", str(caught.exception))

    def test_practice_uses_the_composite_id_and_falls_back_once_on_404(self):
        api = ScriptedApi(practice_reset=[Response(200, {"ok": True})],
                          state=[Response(404, {"detail": "game not found"}), status("selecting_agents")],
                          config=[config_response(self.config)])
        t = scripted_transport(api, mode=PRACTICE)
        t.get_setup()
        self.assertEqual(api.calls[0], ("practice_reset", "q1:78"))
        self.assertEqual([c[1] for c in api.calls if c[0] == "state"], ["q1:78", "q1"])
        self.assertEqual([c[1] for c in api.calls if c[0] == "config"], ["q1:78"])
        self.assertEqual(t.selection_window(), SelectionWindow(open=True, deadline=None, limit=30.0))

    def test_live_never_plays_practice(self):
        with self.assertRaises(ValueError):
            scripted_transport(ScriptedApi(), mode=PRACTICE, live=True)

    def test_a_practice_day_with_no_accepted_plan_is_unblocked_with_a_wait_plan(self):
        api = ScriptedApi(state=[status("in_progress", 0), status("in_progress", 1)],
                          practice_actions=[Response(200, {"ok": True})],
                          day=[day_response(self.config, 1)])
        t = scripted_transport(api, mode=PRACTICE)
        t.config = self.config
        t.last_day = 0
        self.assertEqual(t.wait_day_state([0, 0, 0]).day, 1)
        sent = [(c[1], c[2]) for c in api.calls if c[0] == "practice_actions"]
        self.assertEqual(sent, [("q1:78", 0)])

    def test_score_of_reads_rank_and_the_lexicographic_score(self):
        result = {"ranking": ["6", "78"],
                  "detail": {"78": {"distinct_types": 3, "cumulative_daily_types": 9,
                                    "total_servings": 14, "days_submitted": 10}}}
        self.assertEqual(score_of(result, 78),
                         {"L": 3, "D": 9, "Q": 14, "rank": 2, "teams": 2, "days_submitted": 10})
        self.assertIsNone(score_of(result, 99))


class EndToEndTest(unittest.TestCase):
    def start(self, faults=None, clock=time.time):
        self.world = WebWorld([(77, "DAU 1"), (78, "DAU 2")], faults=faults, clock=clock)
        self.httpd, _, self.url = serve_web(self.world)
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def transport(self, qid, mode, **kw):
        token = self.world.issue_token(78)
        api = WebApi(JsonHttp(self.url, timeout=5.0), lambda: token)
        return WebTransport(api, GameRef(qid, mode=mode), 78, poll_seconds=0.05, **kw)

    def play(self, transport, **kw):
        kw.setdefault("search_seconds", 0.3)
        kw.setdefault("role_seconds", 0.3)
        kw.setdefault("net_margin", 0.1)
        MatchSession(transport, seed=3, **kw).run()
        return transport.finish()

    def test_a_practice_match_submits_once_per_day_and_resets_before_and_after(self):
        self.start()
        qid = self.world.add_question("Q01", small_config(days=3), 11, "Practice Match",
                                      practice=True, selection_seconds=1.0)
        summary = self.play(self.transport(qid, PRACTICE))
        hits = self.world.hits
        self.assertEqual(hits["POST /api/game/practice/actions"], 3)
        self.assertEqual(hits.get("POST /api/game/actions", 0), 0)
        self.assertEqual(hits["POST /api/game/practice/reset"], 2)
        self.assertEqual(hits["POST /api/game/agent-types"], 1)
        self.assertEqual(summary["accepted_days"], [0, 1, 2])
        self.assertEqual(summary["score"]["days_submitted"], 3)
        self.assertEqual(self.world.practice_game(qid, 78).board.status(), "selecting_agents")

    def test_keep_leaves_the_finished_practice_on_the_server(self):
        self.start()
        qid = self.world.add_question("Q01", small_config(days=2), 11, "Practice Match", practice=True)
        self.play(self.transport(qid, PRACTICE, keep=True))
        self.assertEqual(self.world.hits["POST /api/game/practice/reset"], 1)
        self.assertEqual(self.world.practice_game(qid, 78).board.status(), "finished")

    def test_a_live_contest_plays_every_day_and_never_touches_practice_endpoints(self):
        self.start()
        qid = self.world.add_question("Q01", small_config(days=2, day_seconds=1.5), 14, "Mock Contest",
                                      selection_seconds=1.0, pre_window=0.5)
        self.play(self.transport(qid, CONTEST, live=True), search_seconds=None, net_margin=0.3)
        team = self.world.contests[qid].board.teams[78]
        self.assertEqual(team.days_submitted, 2)
        self.assertTrue(team.types_selected)
        self.assertFalse(team.missed_selection)
        self.assertEqual([key for key in self.world.hits if "/practice/" in key], [])

    def test_joining_mid_match_plays_the_remaining_days_without_choosing_roles(self):
        self.start()
        qid = self.world.add_question("Q01", small_config(days=3, day_seconds=1.5), 14, "Mock Contest",
                                      selection_seconds=0.2)
        time.sleep(0.6)
        self.play(self.transport(qid, CONTEST), search_seconds=None, net_margin=0.3)
        team = self.world.contests[qid].board.teams[78]
        self.assertEqual(self.world.hits.get("POST /api/game/agent-types", 0), 0)
        self.assertTrue(team.missed_selection)
        self.assertGreaterEqual(team.days_submitted, 2)

    def test_a_skewed_server_clock_does_not_change_our_deadline(self):
        self.start(clock=lambda: time.time() + 600)
        qid = self.world.add_question("Q01", small_config(days=2, day_seconds=3.0), 14, "Mock Contest",
                                      selection_seconds=0.3)
        t = self.transport(qid, CONTEST)
        t.get_setup()
        state = t.wait_day_state([0, 0, 0])
        left = state.ends_at - time.time()
        self.assertGreater(left, 0.0)
        self.assertLessEqual(left, 3.1)
        self.assertGreater(t.api.day(qid).payload["endsAt"] - time.time(), 500,
                           "server lệch 600 s nhưng hạn của ta không được lệch theo")

    def test_rate_limits_are_absorbed(self):
        self.start(faults=Faults(rate_limit_every=6))
        qid = self.world.add_question("Q01", small_config(days=2), 11, "Practice Match", practice=True)
        summary = self.play(self.transport(qid, PRACTICE))
        self.assertEqual(summary["accepted_days"], [0, 1])
        self.assertGreater(self.world.requests, 6)


if __name__ == "__main__":
    unittest.main()
