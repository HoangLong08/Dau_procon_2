"""Tra trận/câu theo ID hoặc tên; sai hay trùng thì dừng, không đoán. Lệnh games."""

import contextlib
import io
import os
import tempfile
import unittest

from hexudon.cli import main
from hexudon.client.net import Response
from hexudon.client.web_games import (COMPETITIVE_PRACTICE, CONTEST, PRACTICE, ResolveError,
                                      fetch_questions, resolve)
from hexudon.offline.mapgen import generate
from hexudon.offline.web_server import WebWorld, serve_web

QID_MOCK_1 = "f06e9b26-9e6e-4401-b058-7bceaddf10cb"
QID_MOCK_5 = "2dd1ab59-8954-4a8c-8400-2529e47cafa9"
QID_PRAC_1 = "c57bfe4b-55ef-416f-893f-1ccaf86a8b87"


def row(qid, match_id, name, match_name, practice=False, no_reset=False):
    return {"id": qid, "match_id": match_id, "name": name,
            "match": {"name": match_name, "is_active": True,
                      "is_practice": practice, "no_reset": no_reset}}


class StubApi:
    def __init__(self, rows=None, status=200, count=None):
        self.rows = rows if rows is not None else [
            row(QID_MOCK_1, 14, "Q01", "Mock Contest"),
            row(QID_MOCK_5, 14, "Q05", "Mock Contest"),
            row(QID_PRAC_1, 11, "Q01", "Practice Match", practice=True),
        ]
        self.status = status
        self.count = count
        self.calls = 0

    def questions(self):
        self.calls += 1
        if self.status != 200:
            return Response(self.status, {"message": "Unauthorized"})
        count = self.count if self.count is not None else len(self.rows)
        return Response(200, {"count": count, "data": self.rows})


class ResolveTest(unittest.TestCase):
    def test_match_by_id_and_question_by_name(self):
        [ref] = resolve(StubApi(), match="14", question="q05")
        self.assertEqual((ref.game_id, ref.match_name, ref.question, ref.mode),
                         (QID_MOCK_5, "Mock Contest", "Q05", CONTEST))
        self.assertEqual(ref.label(), "trận 14 «Mock Contest» câu Q05")

    def test_match_by_name_ignores_case_and_question_by_uuid(self):
        [ref] = resolve(StubApi(), match="practice match", question=QID_PRAC_1)
        self.assertEqual((ref.match_id, ref.mode), (11, PRACTICE))

    def test_whole_match_when_no_question_is_given(self):
        self.assertEqual([r.question for r in resolve(StubApi(), match="Mock Contest")], ["Q01", "Q05"])

    def test_unknown_match_lists_what_exists_instead_of_guessing(self):
        with self.assertRaises(ResolveError) as caught:
            resolve(StubApi(), match="Mock")
        self.assertIn("Mock Contest", str(caught.exception))
        self.assertIn(QID_MOCK_5, str(caught.exception))

    def test_unknown_question_lists_the_match(self):
        with self.assertRaises(ResolveError) as caught:
            resolve(StubApi(), match="14", question="Q09")
        self.assertIn("Q05", str(caught.exception))

    def test_duplicate_match_names_require_the_id(self):
        api = StubApi(rows=[row(QID_MOCK_1, 14, "Q01", "Vòng 1"), row(QID_MOCK_5, 15, "Q01", "Vòng 1")])
        with self.assertRaises(ResolveError) as caught:
            resolve(api, match="Vòng 1")
        self.assertIn("ID trận", str(caught.exception))

    def test_game_id_goes_straight_without_the_manager_api(self):
        api = StubApi(status=401)
        [ref] = resolve(api, game=QID_MOCK_5, label="Mock Contest/Q05")
        self.assertEqual((ref.game_id, ref.mode, ref.question), (QID_MOCK_5, CONTEST, "Mock Contest/Q05"))
        self.assertEqual(ref.label(), "câu Mock Contest/Q05")
        [ref] = resolve(api, game=QID_PRAC_1, practice=True)
        self.assertEqual(ref.mode, PRACTICE)
        self.assertEqual(api.calls, 0)

    def test_manager_failure_points_to_the_game_flag(self):
        with self.assertRaises(ResolveError) as caught:
            resolve(StubApi(status=401), match="14")
        self.assertIn("--game", str(caught.exception))

    def test_competitive_practice_is_refused(self):
        api = StubApi(rows=[row(QID_MOCK_1, 20, "Q01", "Shared", practice=True, no_reset=True)])
        self.assertEqual(fetch_questions(api)[0].mode, COMPETITIVE_PRACTICE)
        with self.assertRaises(ResolveError) as caught:
            resolve(api, match="20")
        self.assertIn("competitive_practice", str(caught.exception))

    def test_a_truncated_list_is_reported(self):
        messages = []
        fetch_questions(StubApi(count=40), log=messages.append)
        self.assertTrue(any("40" in m for m in messages))


class GamesCommandTest(unittest.TestCase):
    def test_games_lists_matches_questions_and_live_status(self):
        world = WebWorld([(77, "DAU 1"), (78, "DAU 2")])
        httpd, _, url = serve_web(world)
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        config = generate(seed=3, size=8, agents=3, days=2)
        contest = world.add_question("Q01", config, 14, "Mock Contest", selection_seconds=30, pre_window=120)
        practice = world.add_question("Q01", config, 11, "Practice Match", practice=True)
        with tempfile.TemporaryDirectory() as tmp:
            env = os.path.join(tmp, ".env")
            with open(env, "w", encoding="utf-8") as fh:
                fh.write(f"TEST_DAU_01={world.issue_token(77)}\n")
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = main(["games", "--url", url, "--env", env])
        self.assertEqual(code, 0, err.getvalue())
        text = out.getvalue()
        self.assertIn("DAU 1 (id 77)", text)
        self.assertIn(contest, text)
        self.assertIn("chờ mở cửa sổ chọn xe", text)
        self.assertIn(practice, text)
        self.assertIn("chờ chọn xe (tự nhịp)", text)


if __name__ == "__main__":
    unittest.main()
