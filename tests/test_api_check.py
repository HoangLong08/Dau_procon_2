"""api-check (R0): đặc tả server thi so với các endpoint client dùng, chạy offline trên bản đã lưu."""

import copy
import unittest

from hexudon.client import api_check


class TestApiCheck(unittest.TestCase):
    def setUp(self):
        self.spec = api_check.load_fixture()
        self.assertIsNotNone(self.spec, "thiếu tests/fixtures/openapi_2026-11-16.json")

    def test_saved_spec_matches_the_client(self):
        report = api_check.check(self.spec, self.spec)
        self.assertTrue(report.ok, report.render())
        self.assertEqual(report.new_endpoints, [])
        self.assertIn("đủ endpoint", report.render())

    def test_missing_endpoint_is_reported(self):
        spec = copy.deepcopy(self.spec)
        del spec["paths"]["/api/game/actions"]["post"]
        report = api_check.check(spec, self.spec)
        self.assertFalse(report.ok)
        self.assertIn("POST /api/game/actions", report.missing)
        self.assertIn("POST /api/game/actions", report.removed_endpoints)

    def test_renamed_body_field_is_reported(self):
        spec = copy.deepcopy(self.spec)
        schema = spec["components"]["schemas"]["SubmitActionsRequest"]
        schema["properties"]["plan"] = schema["properties"].pop("actions")
        schema["required"] = ["game_id", "day", "plan"]
        report = api_check.check(spec)
        self.assertFalse(report.ok)
        text = " ".join(report.body_problems)
        self.assertIn("actions", text)
        self.assertIn("plan", text)

    def test_new_endpoint_is_listed_but_not_an_error(self):
        known = copy.deepcopy(self.spec)
        del known["paths"]["/api/game/competitive/state"]
        report = api_check.check(self.spec, known)
        self.assertTrue(report.ok)
        self.assertIn("GET /api/game/competitive/state", report.new_endpoints)


if __name__ == "__main__":
    unittest.main()
