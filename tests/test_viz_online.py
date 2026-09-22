"""Bot trên server giả ghi sự kiện: gửi bài, kết quả ngày, điểm server; không lộ token."""

import contextlib
import io
import os
import tempfile
import unittest
from unittest import mock

from hexudon.cli import build_parser, cmd_web
from hexudon.client import web_runner
from hexudon.client.tokens import pick_tokens
from hexudon.offline.mapgen import generate
from hexudon.offline.web_server import WebWorld, serve_web
from hexudon.viz.events import read_events


def small_config(days=2, day_seconds=2.0):
    config = generate(seed=41, size=8, agents=3, days=days)
    config.day_seconds = [day_seconds] * config.n_days
    config.__post_init__()
    return config


class OnlineEventsTest(unittest.TestCase):
    def setUp(self):
        self.world = WebWorld([(77, "DAU 1"), (78, "DAU 2")])
        self.httpd, _, self.url = serve_web(self.world)
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.env = os.path.join(self.dir, ".env")
        self.tokens = {77: self.world.issue_token(77), 78: self.world.issue_token(78)}
        with open(self.env, "w", encoding="utf-8") as fh:
            fh.write(f"TOKEN={self.tokens[77]}\nTEST_DAU_01={self.tokens[77]}\n"
                     f"TEST_DAU_02={self.tokens[78]}\n")
        self.logs = []

    def args(self, *argv):
        return build_parser().parse_args([*argv, "--url", self.url, "--env", self.env,
                                          "--out-dir", os.path.join(self.dir, "out"),
                                          "--role-seconds", "0.3", "--net-margin", "0.3",
                                          "--poll-seconds", "0.05"])

    def test_practice_run_writes_the_whole_story(self):
        self.world.add_question("Q01", small_config(), 2, "Local Practice", practice=True,
                                selection_seconds=1.0)
        path = os.path.join(self.dir, "events.jsonl")
        args = self.args("web", "--match", "2", "--question", "Q01", "--search-seconds", "0.3",
                         "--events", path)
        [tok] = pick_tokens(self.env, [], False)
        self.assertEqual(web_runner.play_one(args, tok, log=self.logs.append), 0)
        events, _ = read_events(path)
        kinds = [e["kind"] for e in events]
        self.assertEqual(kinds[0], "match_start")
        self.assertEqual(events[0]["data"]["mode"], "online")
        self.assertTrue(all(e["bot"] == "TEST_DAU_01" for e in events))
        self.assertIn("roles_chosen", kinds)
        self.assertIn("plan_safe", kinds)
        self.assertEqual([e["data"]["day"] for e in events if e["kind"] == "day_start"], [0, 1])
        submits = [e["data"] for e in events if e["kind"] == "submit"]
        self.assertEqual([s["day"] for s in submits], [0, 1], "practice: đúng một lần gửi mỗi ngày")
        self.assertTrue(all(s["ok"] for s in submits))
        results = [e["data"] for e in events if e["kind"] == "day_result"]
        self.assertEqual([r["day"] for r in results], [0, 1])
        self.assertTrue(all(r["ok"] for r in results), results)
        [end] = [e["data"] for e in events if e["kind"] == "match_end"]
        self.assertIsNotNone(end["score"])
        with open(path, "rb") as fh:
            content = fh.read()
        for token in self.tokens.values():
            self.assertNotIn(token.encode(), content, "token lộ trong file sự kiện")

    def test_events_flag_is_refused_with_follow_or_many_tokens(self):
        for extra in (["--follow"], ["--token-env", "TEST_DAU_01", "--token-env", "TEST_DAU_02"]):
            args = self.args("web", "--match", "1", *extra, "--events", os.path.join(self.dir, "x.jsonl"))
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cmd_web(args), 2)

    def test_events_flag_is_refused_with_live(self):
        # ruling F7: ngày thi thật (--live) không được đi cùng --events, kể cả một câu một token
        args = self.args("web", "--live", "--match", "1", "--events", os.path.join(self.dir, "x.jsonl"))
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cmd_web(args), 2)

    def test_keyboard_interrupt_in_play_one_is_reported_and_returns_130(self):
        # F3: Ctrl-C khi đang chơi không được thoát với traceback hay mã lỗi khác 130
        args = self.args("web", "--match", "2", "--question", "Q01")
        with mock.patch.object(web_runner, "play_one", side_effect=KeyboardInterrupt):
            with contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(cmd_web(args), 130)
        self.assertIn("dừng theo yêu cầu", err.getvalue())
