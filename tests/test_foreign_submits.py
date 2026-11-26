"""Client khác nộp bài cho cùng đội (26/11/2026).

Trận 1addebd7 ngày 25/11: /state báo submit_count = 7 ở ngày 0 trong khi bot chỉ POST 1 bài. Server
dùng bài hợp lệ cuối cùng nó nhận, nên bài, điểm và thời gian phản hồi (254 s) là của client kia.
WebTransport so submit_count của /state với số lần chính nó POST và cảnh báo."""

import unittest

from hexudon.client.net import Response
from hexudon.client.web_games import PRACTICE
from tests.test_web_transport import ScriptedApi, scripted_transport, small_config, status

OK = Response(200, {"ok": True})


def state(day, count, last=None):
    team = {"submit_count": count}
    if last is not None:
        team["last_submitted_at"] = last
    return status("in_progress", day, day_deadline_in=10.0, steps_today=5, teams={"78": team})


class ForeignSubmitsTest(unittest.TestCase):
    def setUp(self):
        self.config = small_config()
        self.plan = [[-5] for _ in range(self.config.n_agents)]
        self.logs = []

    def transport(self, api, **kw):
        t = scripted_transport(api, clock=lambda: 1000.0, log=self.logs.append, **kw)
        t.config = self.config
        return t

    def warnings(self):
        return [m for m in self.logs if m.startswith("CẢNH BÁO: server ghi")]

    def test_more_submissions_than_the_bot_sent_is_reported_once_per_day(self):
        t = self.transport(ScriptedApi(actions=[OK], state=[state(0, 7, last=1023.0)]))
        self.assertTrue(t.send_actions(0, self.plan).ok)
        t._status()
        t._status()
        self.assertEqual(t.foreign_submits, {0: {"server": 7, "bot": 1, "last_after_bot_s": 23.0}})
        self.assertEqual(len(self.warnings()), 1, "một cảnh báo mỗi ngày")
        self.assertIn("client khác", self.warnings()[0])

    def test_own_submissions_only_raise_nothing(self):
        t = self.transport(ScriptedApi(actions=[OK], state=[state(0, 3)]))
        for _ in range(3):
            t.send_actions(0, self.plan)
        t._status()
        self.assertEqual(t.foreign_submits, {})
        self.assertEqual(self.warnings(), [])

    def test_a_post_whose_answer_was_lost_is_not_another_client(self):
        # lần đầu mất phản hồi (server có thể đã nhận), lần thử lại được nhận: server ghi 2 bài
        t = self.transport(ScriptedApi(actions=[Response(0, None), OK], state=[state(0, 2)]))
        self.assertTrue(t.send_actions(0, self.plan).ok)
        t._status()
        self.assertEqual(t.foreign_submits, {})

    def test_counts_are_per_day(self):
        # sang ngày mới submit_count về 0; bài bot gửi ngày trước không bù cho bài lạ của hôm nay
        t = self.transport(ScriptedApi(actions=[OK], state=[state(1, 1)]))
        t.send_actions(0, self.plan)
        t._status()
        self.assertEqual(t.foreign_submits, {1: {"server": 1, "bot": 0}})

    def test_never_in_practice(self):
        t = self.transport(ScriptedApi(state=[state(0, 5)]), mode=PRACTICE)
        t._status()
        self.assertEqual(t.foreign_submits, {})

    def test_summary_lists_the_days(self):
        api = ScriptedApi(actions=[OK], state=[state(0, 4)], result=[Response(404, {})],
                          replay=[Response(404, {})])
        t = self.transport(api)
        t.send_actions(0, self.plan)
        t._status()
        summary = t.finish()
        self.assertEqual(summary["foreign_submits"], {"0": {"server": 4, "bot": 1}})
        self.assertTrue(any("có client khác nộp bài cho đội ở ngày 1" in m for m in self.logs))


class FakeServerCountsPerDayTest(unittest.TestCase):
    def test_submit_count_is_per_day_like_the_real_server(self):
        from hexudon.offline.web_server import Board
        from hexudon.simulator import wait_plan
        cfg = small_config()
        board = Board(cfg, [(77, "DAU 1")])
        board.open_first_day()
        plan = wait_plan(cfg.n_agents, cfg.steps_of_day(0))
        for _ in range(2):
            self.assertEqual(board.accept_plan(77, 0, plan)[0], 200)
        team = board.state_payload(30.0)["teams"]["77"]
        self.assertEqual(team["submit_count"], 2)
        self.assertIsNotNone(team["last_submitted_at"])
        board.resolve_day()
        team = board.state_payload(30.0)["teams"]["77"]
        self.assertEqual((team["submit_count"], team["last_submitted_at"]), (0, None))


if __name__ == "__main__":
    unittest.main()
