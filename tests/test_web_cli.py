"""Lệnh web: chặn token sai trước khi gọi mạng, chơi trọn một câu, không lộ token ra file."""

import contextlib
import glob
import io
import json
import os
import tempfile
import unittest

from hexudon.cli import main
from hexudon.offline.mapgen import generate
from hexudon.offline.web_server import WebWorld, serve_web


def small_config(days=2):
    config = generate(seed=31, size=8, agents=3, days=days)
    config.day_seconds = [2] * config.n_days
    config.__post_init__()
    return config


class WebCliTest(unittest.TestCase):
    def setUp(self):
        self.world = WebWorld([(77, "DAU 1"), (78, "DAU 2")])
        self.httpd, _, self.url = serve_web(self.world)
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.out = os.path.join(tmp.name, "out")
        os.makedirs(os.path.join(tmp.name, "secrets"))
        self.env = os.path.join(tmp.name, "secrets", ".env")
        self.tokens = [self.world.issue_token(77), self.world.issue_token(78)]
        with open(self.env, "w", encoding="utf-8") as fh:
            fh.write(f"TOKEN={self.tokens[0]}\nTEST_DAU_01={self.tokens[0]}\nTEST_DAU_02={self.tokens[1]}\n")

    def web(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["web", "--url", self.url, "--env", self.env, "--out-dir", self.out,
                         "--poll-seconds", "0.05", "--net-margin", "0.1", *argv])
        return code, out.getvalue() + err.getvalue()

    def test_the_real_token_is_refused_without_live_before_any_request(self):
        code, text = self.web("--token-env", "TOKEN", "--game", "x")
        self.assertEqual(code, 2)
        self.assertIn("--live", text)
        self.assertEqual(self.world.requests, 0)

    def test_live_and_token_env_together_are_refused(self):
        code, _ = self.web("--live", "--token-env", "TEST_DAU_02", "--game", "x")
        self.assertEqual(code, 2)
        self.assertEqual(self.world.requests, 0)

    def test_live_refuses_a_practice_question(self):
        self.world.add_question("Q01", small_config(), 11, "Practice Match", practice=True)
        code, text = self.web("--live", "--match", "11", "--question", "Q01")
        self.assertEqual(code, 2)
        self.assertIn("contest", text)
        self.assertEqual([k for k in self.world.hits if k.startswith("POST")], [])

    def test_plays_a_practice_question_end_to_end_without_leaking_the_token(self):
        self.world.add_question("Q01", small_config(), 11, "Practice Match", practice=True,
                                selection_seconds=1.0)
        code, text = self.web("--match", "Practice Match", "--question", "Q01",
                              "--search-seconds", "0.3", "--role-seconds", "0.3")
        self.assertEqual(code, 0, text)
        self.assertIn("KẾT QUẢ", text)
        [summary_path] = glob.glob(os.path.join(self.out, "*-summary.json"))
        with open(summary_path, encoding="utf-8") as fh:
            summary = json.load(fh)
        self.assertEqual(summary["score"]["days_submitted"], 2)
        self.assertEqual(summary["token_var"], "TEST_DAU_02")
        for path in glob.glob(os.path.join(self.out, "*")):
            with open(path, encoding="utf-8") as fh:
                content = fh.read()
            for token in self.tokens:
                self.assertNotIn(token, content, f"token lọt vào {path}")
        for token in self.tokens:
            self.assertNotIn(token, text)

    def test_a_practice_match_without_question_lists_the_questions(self):
        config = small_config()
        self.world.add_question("Q01", config, 11, "Practice Match", practice=True)
        self.world.add_question("Q02", config, 11, "Practice Match", practice=True)
        code, text = self.web("--match", "11")
        self.assertEqual(code, 1)
        self.assertIn("Q01", text)
        self.assertIn("Q02", text)


if __name__ == "__main__":
    unittest.main()
