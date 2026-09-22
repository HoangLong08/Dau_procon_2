"""Token chọn theo tên biến; TOKEN thi thật chỉ đi cùng --live."""

import base64
import json
import os
import tempfile
import unittest

from hexudon.client.tokens import DEFAULT_TEST_VAR, TokenError, pick_tokens

NOW = 1_790_000_000.0


def fake_jwt(team_id, name, exp):
    def part(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()
    return f"{part({'alg': 'HS256'})}.{part({'id': team_id, 'name': name, 'exp': exp})}.sig"


class TokenPolicyTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.env = os.path.join(tmp.name, ".env")
        self.real = fake_jwt(77, "DAU 1", NOW + 48 * 3600)
        self.write(TOKEN=self.real, TEST_DAU_01=self.real,
                   TEST_DAU_02=fake_jwt(78, "DAU 2", NOW + 48 * 3600))

    def write(self, **values):
        with open(self.env, "w", encoding="utf-8") as fh:
            for key, value in values.items():
                fh.write(f"{key}={value}\n")

    def test_default_is_our_own_team_dau_1(self):
        [tok] = pick_tokens(self.env, [], live=False, now=NOW)
        self.assertEqual((tok.var, tok.team_id, tok.team_name), (DEFAULT_TEST_VAR, 77, "DAU 1"))
        self.assertEqual(tok.describe(), "DAU 1 (id 77)")

    def test_real_token_without_live_is_refused(self):
        with self.assertRaises(TokenError) as caught:
            pick_tokens(self.env, ["TOKEN"], live=False, now=NOW)
        self.assertIn("--live", str(caught.exception))

    def test_live_together_with_token_env_is_refused(self):
        with self.assertRaises(TokenError):
            pick_tokens(self.env, ["TEST_DAU_02"], live=True, now=NOW)

    def test_live_uses_token(self):
        [tok] = pick_tokens(self.env, [], live=True, now=NOW)
        self.assertEqual((tok.var, tok.team_id), ("TOKEN", 77))
        self.assertEqual(tok.warnings, [])

    def test_a_test_token_identical_to_the_real_one_is_flagged(self):
        [tok] = pick_tokens(self.env, ["TEST_DAU_01"], live=False, now=NOW)
        self.assertTrue(any("trùng TOKEN" in w for w in tok.warnings))

    def test_expired_token_is_refused_and_near_expiry_warns(self):
        self.write(TOKEN=self.real, TEST_DAU_01=fake_jwt(77, "DAU 1", NOW - 1))
        with self.assertRaises(TokenError) as caught:
            pick_tokens(self.env, [], live=False, now=NOW)
        self.assertIn("hết hạn", str(caught.exception))
        self.write(TOKEN=fake_jwt(77, "DAU 1", NOW + 3600))
        [tok] = pick_tokens(self.env, [], live=True, now=NOW)
        self.assertTrue(any("chưa tới 2 giờ" in w for w in tok.warnings))

    def test_missing_variable_lists_names_but_never_values(self):
        with self.assertRaises(TokenError) as caught:
            pick_tokens(self.env, ["TEST_DAU_09"], live=False, now=NOW)
        message = str(caught.exception)
        self.assertIn("TEST_DAU_02", message)
        self.assertNotIn(self.real, message)

    def test_two_teams_can_be_picked_at_once_without_duplicates(self):
        toks = pick_tokens(self.env, ["TEST_DAU_01", "TEST_DAU_02", "TEST_DAU_01"], live=False, now=NOW)
        self.assertEqual([t.team_id for t in toks], [77, 78])

    def test_reload_sees_a_new_value_pasted_into_env(self):
        [tok] = pick_tokens(self.env, [], live=False, now=NOW)
        self.assertFalse(tok.reload())
        fresh = fake_jwt(77, "DAU 1", NOW + 96 * 3600)
        self.write(TOKEN=self.real, TEST_DAU_01=fresh)
        self.assertTrue(tok.reload())
        self.assertEqual(tok.value, fresh)
        self.assertEqual(tok.expires_at, NOW + 96 * 3600)


if __name__ == "__main__":
    unittest.main()
