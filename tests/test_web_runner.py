"""Tiến trình cha: --follow bắt từng câu cho từng đội, nhiều đội một câu, web-bench."""

import argparse
import inspect
import json
import os
import tempfile
import time
import unittest
from unittest import mock

from hexudon.cli import build_parser
from hexudon.client import web_runner
from hexudon.client.net import JsonHttp, Response
from hexudon.client.tokens import TeamToken, pick_tokens
from hexudon.client.web_api import WebApi
from hexudon.client.web_games import CONTEST, PRACTICE, GameRef
from hexudon.offline.mapgen import generate
from hexudon.offline.web_server import WebWorld, serve_web
from hexudon.simulator import wait_plan


def small_config(days=2, day_seconds=2.0):
    config = generate(seed=41, size=8, agents=3, days=days)
    config.day_seconds = [day_seconds] * config.n_days
    config.__post_init__()
    return config


class RunnerUnitTest(unittest.TestCase):
    def test_child_command_forwards_token_mode_and_timing(self):
        args = build_parser().parse_args(["web", "--match", "14", "--follow", "--token-env", "TEST_DAU_01",
                                          "--search-seconds", "3", "--env", "x.env"])
        cmd = web_runner.child_command(args, GameRef("qid-1", "Q05", 14, "Mock Contest", CONTEST),
                                       "TEST_DAU_01", "/tmp/a-summary.json", "Mock Contest/Q05")
        self.assertEqual(cmd[1:5], ["-m", "hexudon", "web", "--game"])
        joined = " ".join(cmd)
        self.assertIn("--game qid-1", joined)
        self.assertIn("--token-env TEST_DAU_01", joined)
        self.assertIn("--search-seconds 3.0", joined)
        self.assertIn("--role-seconds auto", joined)
        self.assertIn(os.path.abspath("x.env"), cmd)
        self.assertNotIn("--live", cmd)
        self.assertNotIn("--practice", cmd)
        practice = web_runner.child_command(args, GameRef("qid-2", mode=PRACTICE), "TEST_DAU_02",
                                            "/tmp/b-summary.json", "b")
        self.assertIn("--practice", practice)

    def test_first_ctrl_c_only_stops_new_children_second_stops_all(self):
        args = argparse.Namespace(out_dir=tempfile.gettempdir(), live=False)
        sup = web_runner.Supervisor(args, log=lambda msg: None)
        handler = web_runner._CtrlC(sup, log=lambda msg: None)
        handler(2, None)
        self.assertTrue(sup.stopping)
        with self.assertRaises(KeyboardInterrupt):
            handler(2, None)

    def test_a_failing_state_is_logged_once_and_every_token_is_reread(self):
        class StubToken:
            def __init__(self):
                self.reloads = 0

            def reload(self):
                self.reloads += 1
                return False

        class StubApi:
            def state(self, game_id):
                return Response(401, {"detail": "expired"})

        tokens, logs, reported = [StubToken(), StubToken()], [], set()
        ref = GameRef("qid-1", "Q01", 14, "Mock Contest", CONTEST)
        self.assertIsNone(web_runner.question_status(StubApi(), ref, tokens, logs.append, reported))
        self.assertEqual(len(logs), 1)
        self.assertIn("401", logs[0])
        self.assertIn("expired", logs[0])
        self.assertEqual([tok.reloads for tok in tokens], [1, 1])
        self.assertIsNone(web_runner.question_status(StubApi(), ref, tokens, logs.append, reported))
        self.assertEqual(len(logs), 1, "cùng câu, cùng mã lỗi: không báo lại")

    def test_follow_without_a_match_stops_at_once_instead_of_waiting(self):
        def no_wait(seconds):
            raise AssertionError(f"--follow thiếu --match không được ngồi chờ ({seconds}s)")

        args = build_parser().parse_args(["web", "--follow", "--url", "http://127.0.0.1:1"])
        tok = TeamToken(var="TEST_DAU_02", value="x", env_path=os.path.join(tempfile.gettempdir(), "khong-co.env"))
        logs = []
        self.assertEqual(web_runner.follow(args, [tok], log=logs.append, sleep=no_wait), 2)
        self.assertIn("--match", logs[0])

    def test_follow_refreshes_the_question_list_every_15_s_as_in_the_spec(self):
        params = inspect.signature(web_runner.follow).parameters
        self.assertEqual(params["questions_every"].default, 15.0)
        self.assertEqual(params["status_every"].default, 5.0)


class RunnerEndToEndTest(unittest.TestCase):
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

    def test_follow_plays_overlapping_questions_for_two_teams(self):
        # cửa sổ rộng hơn thời gian khởi động tiến trình con, kể cả khi bốn con khởi động cùng lúc
        q1 = self.world.add_question("Q01", small_config(), 1, "Local Contest",
                                     selection_seconds=2.0, pre_window=2.0)
        q2 = self.world.add_question("Q02", small_config(), 1, "Local Contest",
                                     selection_seconds=2.0, pre_window=3.5)
        args = self.args("web", "--match", "1", "--follow",
                         "--token-env", "TEST_DAU_01", "--token-env", "TEST_DAU_02")
        tokens = pick_tokens(self.env, args.token_env, False)
        started = time.monotonic()
        code = web_runner.follow(args, tokens, log=self.logs.append, status_every=0.2, questions_every=1.0,
                                 stop_when=lambda sup: len(sup.done) >= 4 or time.monotonic() - started > 90)
        self.assertEqual(code, 0)
        for qid in (q1, q2):
            for team_id in (77, 78):
                team = self.world.contests[qid].board.teams[team_id]
                self.assertTrue(team.types_selected, f"câu {qid} đội {team_id} không kịp chọn xe")
                self.assertEqual(team.days_submitted, 2, f"câu {qid} đội {team_id}")

    def test_follow_survives_a_malformed_question_list(self):
        qid = self.world.add_question("Q01", small_config(), 1, "Local Contest",
                                      selection_seconds=2.0, pre_window=2.0)
        args = self.args("web", "--match", "1", "--follow")
        tokens = pick_tokens(self.env, args.token_env, False)
        seen = []
        # cha mà chết thì con thành mồ côi: dọn con để test hỏng không để lại tiến trình
        self.addCleanup(lambda: seen and seen[-1].terminate_all(grace=1.0))
        started = time.monotonic()
        with mock.patch("hexudon.client.web_runner.fetch_questions", side_effect=KeyError("id")):
            code = web_runner.follow(
                args, tokens, log=self.logs.append, status_every=0.2, questions_every=0.5,
                stop_when=lambda sup: seen.append(sup) or len(sup.done) >= 1 or time.monotonic() - started > 60)
        self.assertEqual(code, 0)
        self.assertEqual(self.world.contests[qid].board.teams[77].days_submitted, 2)
        self.assertTrue(any("KeyError" in m for m in self.logs), "lỗi phải hiện trong log của cha")

    def test_follow_waits_for_a_match_that_is_not_published_yet(self):
        published = []

        def sleep(seconds):
            if not published:        # BTC công bố câu trong lúc bot đang chờ
                published.append(self.world.add_question("Q01", small_config(), 5, "Late Contest",
                                                         selection_seconds=2.0, pre_window=2.0))
            time.sleep(seconds)

        args = self.args("web", "--match", "5", "--follow")
        tokens = pick_tokens(self.env, args.token_env, False)
        started = time.monotonic()
        code = web_runner.follow(args, tokens, log=self.logs.append, sleep=sleep, status_every=0.2,
                                 questions_every=0.5,
                                 stop_when=lambda sup: len(sup.done) >= 1 or time.monotonic() - started > 60)
        self.assertEqual(code, 0)
        team = self.world.contests[published[0]].board.teams[77]
        self.assertTrue(team.types_selected)
        self.assertEqual(team.days_submitted, 2)

    def test_follow_stops_at_once_when_the_match_name_is_ambiguous(self):
        self.world.add_question("Q01", small_config(), 1, "Trùng tên")
        self.world.add_question("Q01", small_config(), 2, "Trùng tên")

        def no_wait(seconds):
            raise AssertionError(f"tên trận trùng không tự hết: không được ngồi chờ ({seconds}s)")

        args = self.args("web", "--match", "Trùng tên", "--follow")
        tokens = pick_tokens(self.env, args.token_env, False)
        self.assertEqual(web_runner.follow(args, tokens, log=self.logs.append, sleep=no_wait), 1)
        self.assertTrue(any("trùng nhiều trận" in m for m in self.logs))

    def test_follow_waiting_for_the_match_rereads_a_replaced_token(self):
        self.world.add_question("Q01", small_config(), 1, "Local Contest")
        args = self.args("web", "--match", "1", "--follow")
        tokens = pick_tokens(self.env, args.token_env, False)          # TEST_DAU_01, đội 77
        self.world.revoke(self.tokens[77])                              # manager API trả 401
        naps = []

        def sleep(seconds):          # người dùng dán token mới vào .env trong lúc bot chờ
            naps.append(seconds)
            if len(naps) > 3:
                raise AssertionError("token mới trong .env không được đọc lại")
            with open(self.env, "w", encoding="utf-8") as fh:
                fresh = self.world.issue_token(77)
                fh.write(f"TOKEN={fresh}\nTEST_DAU_01={fresh}\n")

        code = web_runner.follow(args, tokens, log=self.logs.append, sleep=sleep,
                                 stop_when=lambda sup: any("theo dõi trận" in m for m in self.logs))
        self.assertEqual(code, 0)
        self.assertTrue(any("HTTP 401" in m for m in self.logs))
        self.assertEqual(len(naps), 1)

    def test_follow_live_plays_the_contest_with_the_real_token_and_leaks_nothing(self):
        qid = self.world.add_question("Q01", small_config(), 1, "Local Contest",
                                      selection_seconds=2.0, pre_window=2.0)
        args = self.args("web", "--live", "--match", "1", "--follow")
        tokens = pick_tokens(self.env, [], True)
        started = time.monotonic()
        code = web_runner.follow(args, tokens, log=self.logs.append, status_every=0.2, questions_every=1.0,
                                 stop_when=lambda sup: len(sup.done) >= 1 or time.monotonic() - started > 60)
        self.assertEqual(code, 0)
        team = self.world.contests[qid].board.teams[77]
        self.assertTrue(team.types_selected, "đội 77 không kịp chọn xe")
        self.assertFalse(team.missed_selection)
        self.assertEqual(team.days_submitted, 2)
        self.assertEqual([key for key in self.world.hits if "/practice/" in key], [])
        out = os.path.join(self.dir, "out")
        files = [os.path.join(root, name) for root, _, names in os.walk(out) for name in names]
        [summary_path] = [p for p in files if p.endswith("-summary.json")]
        with open(summary_path, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["token_var"], "TOKEN")
        [log_path] = [p for p in files if p.endswith(".log")]
        with open(log_path, encoding="utf-8") as fh:
            self.assertIn("THI THẬT", fh.read())
        for path in files:
            with open(path, "rb") as fh:
                content = fh.read()
            for token in self.tokens.values():
                self.assertNotIn(token.encode(), content, f"token lộ trong {path}")

    def test_play_many_runs_one_child_per_team_on_one_question(self):
        self.world.add_question("Q01", small_config(), 2, "Local Practice", practice=True,
                                selection_seconds=1.0)
        args = self.args("web", "--match", "2", "--question", "Q01", "--search-seconds", "0.3",
                         "--token-env", "TEST_DAU_01", "--token-env", "TEST_DAU_02")
        tokens = pick_tokens(self.env, args.token_env, False)
        self.assertEqual(web_runner.play_many(args, tokens, log=self.logs.append), 0)
        self.assertEqual(self.world.hits["POST /api/game/practice/actions"], 4)
        self.assertEqual(self.world.hits["POST /api/game/practice/reset"], 4)

    def test_bench_runs_practice_questions_and_compares_with_peers(self):
        config = small_config()
        q1 = self.world.add_question("Q01", config, 2, "Local Practice", practice=True, selection_seconds=1.0)
        self.world.add_question("Q02", config, 2, "Local Practice", practice=True, selection_seconds=1.0)
        peer = WebApi(JsonHttp(self.url), lambda: self.tokens[78])
        peer.agent_types(f"{q1}:78", [0, 0, 0])
        peer.practice_actions(f"{q1}:78", 0, wait_plan(3, config.steps_of_day(0)))
        report_path = os.path.join(self.dir, "bench.json")
        args = self.args("web-bench", "--match", "Local Practice", "--jobs", "2",
                         "--search-seconds", "0.3", "--out", report_path)
        [tok] = pick_tokens(self.env, [], False)
        self.assertEqual(web_runner.bench(args, tok, log=self.logs.append), 0)
        with open(report_path, encoding="utf-8") as fh:
            rows = {row["question"]: row for row in json.load(fh)["rows"]}
        self.assertEqual(sorted(rows), ["Q01", "Q02"])
        for row in rows.values():
            self.assertEqual(row["ours"]["days_submitted"], 2)
            self.assertEqual(row["mismatch_days"], [])
        self.assertEqual(rows["Q01"]["peers_with_data"], 1)
        self.assertEqual(rows["Q02"]["peers_with_data"], 0)
        self.assertEqual(self.world.practice_game(q1, 77).board.status(), "selecting_agents")


if __name__ == "__main__":
    unittest.main()
