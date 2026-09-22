"""Web trên server thật: chỉ Practice + Mock, chỉ token TEST_*, không chạy trùng."""

import unittest

from hexudon.client.web_games import CONTEST, PRACTICE, GameRef
from hexudon.viz.guard import GuardError, RunRegistry, allowed_on_real, check_real_game, check_token_var


class GuardTest(unittest.TestCase):
    def test_only_practice_and_mock_matches_are_allowed_on_the_real_server(self):
        self.assertTrue(allowed_on_real(GameRef("a", "Q01", 11, "Practice Match", PRACTICE)))
        self.assertTrue(allowed_on_real(GameRef("b", "Q01", 14, "Mock Contest", CONTEST)))
        self.assertTrue(allowed_on_real(GameRef("c", "Q01", 15, "mock contest 2", CONTEST)))
        self.assertFalse(allowed_on_real(GameRef("d", "Q01", 20, "Vòng chung kết", CONTEST)))
        self.assertFalse(allowed_on_real(GameRef("e", "Q01", None, None, CONTEST)))
        with self.assertRaises(GuardError) as ctx:
            check_real_game(GameRef("d", "Q01", 20, "Vòng chung kết", CONTEST))
        self.assertEqual(ctx.exception.status, 403)

    def test_only_test_token_variables_are_accepted(self):
        check_token_var("TEST_DAU_01")
        check_token_var("TEST_DAU_02")
        for bad in ("TOKEN", "test_dau_01", "TEST_", "", "TEST_DAU_01;rm"):
            with self.assertRaises(GuardError, msg=bad):
                check_token_var(bad)

    def test_token_var_regex_is_anchored_with_fullmatch(self):
        # .match() thì "$" vẫn khớp trước "\n" cuối chuỗi; fullmatch mới chặn được.
        with self.assertRaises(GuardError):
            check_token_var("TEST_DAU_01\n")

    def test_the_same_token_cannot_play_the_same_question_twice(self):
        reg = RunRegistry()
        reg.claim("real", "q1", ["TEST_DAU_01"], "run-a")
        with self.assertRaises(GuardError) as ctx:
            reg.claim("real", "q1", ["TEST_DAU_02", "TEST_DAU_01"], "run-b")
        self.assertEqual(ctx.exception.status, 409)
        reg.claim("real", "q1", ["TEST_DAU_02"], "run-c")    # lượt b không giữ gì khi bị từ chối
        reg.claim("fake", "q1", ["TEST_DAU_01"], "run-d")    # đích khác là câu khác
        reg.release_run("run-a")
        reg.claim("real", "q1", ["TEST_DAU_01"], "run-e")
