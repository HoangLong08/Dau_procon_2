"""Đọc dữ liệu thật của server thi (fixture chụp ngày 2026-09-21) và dựng request đúng."""

import json
import os
import unittest

from hexudon.client.net import Response
from hexudon.client.web_api import GameStatus, WebApi, error_detail
from hexudon.model import DayState, MatchConfig

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "web")


def fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        return json.load(fh)


class RecordingHttp:
    """Thay JsonHttp: ghi lại lời gọi, trả một response định sẵn."""

    def __init__(self):
        self.calls = []

    def call(self, method, path, params=None, body=None, headers=None, deadline=None, timeout=None):
        self.calls.append({"method": method, "path": path, "params": params, "body": body,
                           "headers": headers or {}, "deadline": deadline, "timeout": timeout})
        return Response(200, {})


class RealPayloadTest(unittest.TestCase):
    def test_config_parses_with_the_existing_reader(self):
        raw = fixture("config.json")
        config = MatchConfig.from_json(raw)
        self.assertEqual((config.width, config.height), (32, 32))
        self.assertEqual(len(config.cells), 32 * 32)
        self.assertEqual(config.n_agents, len(raw["agents"]))
        self.assertEqual(config.n_days, len(raw["daySteps"]))
        self.assertEqual(config.players, raw["players"])
        self.assertEqual(config.seconds_of_day(0), 30.0)

    def test_day_parses_with_the_existing_reader(self):
        raw = fixture("day.json")
        state = DayState.from_json(raw, None, width=32, fuel_limits=468)
        self.assertEqual(state.day, raw["day"])
        self.assertEqual([a.pos for a in state.agents], [a["pos"] for a in raw["agents"]])
        self.assertEqual(len(state.others), len(raw["others"]))
        self.assertEqual(state.ends_at, raw["endsAt"])

    def test_state_in_progress(self):
        st = GameStatus.from_json(fixture("state_in_progress.json"), team_id=78)
        self.assertEqual((st.status, st.day, st.steps_today), ("in_progress", 8, 193))
        self.assertIsInstance(st.day_deadline_in, float)
        self.assertIsNone(st.selection_deadline_in)
        self.assertEqual(st.selection_limit, 60.0)
        self.assertEqual(st.team["agents"][0]["type"], "patrol")

    def test_state_of_a_practice_before_selection(self):
        st = GameStatus.from_json(fixture("state_practice.json"), team_id=78)
        self.assertEqual((st.status, st.day), ("selecting_agents", -1))
        self.assertIsNone(st.day_deadline_in)
        self.assertIsNone(st.selection_deadline_in)
        self.assertIsNone(st.selection_opens_in)
        self.assertEqual(st.selection_limit, 30.0)
        self.assertFalse(st.team["types_selected"])

    def test_unknown_team_gives_an_empty_team(self):
        self.assertEqual(GameStatus.from_json(fixture("state_in_progress.json"), team_id=999).team, {})

    def test_error_detail_reads_fastapi_shapes(self):
        self.assertEqual(error_detail(fixture("day_409.json")),
                         "no day is open yet (the match starts with the agent-kind window)")
        self.assertEqual(error_detail({"detail": [{"loc": ["body", "day"], "msg": "field required"}]}),
                         "body.day: field required")
        self.assertEqual(error_detail({"raw": "error code: 1010"}), "error code: 1010")
        self.assertEqual(error_detail({"message": "Unauthorized"}), "Unauthorized")
        self.assertEqual(error_detail(None), "")


class RequestShapeTest(unittest.TestCase):
    def setUp(self):
        self.http = RecordingHttp()
        self.api = WebApi(self.http, lambda: "jwt-abc")

    def test_game_service_uses_bearer_and_game_id_in_the_query(self):
        self.api.state("q1")
        call = self.http.calls[-1]
        self.assertEqual((call["method"], call["path"], call["params"]),
                         ("GET", "/api/game/state", {"game_id": "q1"}))
        self.assertEqual(call["headers"]["Authorization"], "Bearer jwt-abc")

    def test_manager_service_uses_the_bare_token(self):
        self.api.questions()
        self.api.matches()
        paths = [c["path"] for c in self.http.calls]
        self.assertEqual(paths, ["/manager/api/question", "/manager/api/match"])
        self.assertTrue(all(c["headers"]["Authorization"] == "jwt-abc" for c in self.http.calls))

    def test_post_bodies_match_the_openapi_schema(self):
        self.api.agent_types("q1", [0, 1, 0])
        self.api.actions("q1", 3, [[1, -2]])
        self.api.practice_actions("q1:78", 0, [[-5]])
        self.api.practice_reset("q1:78")
        self.assertEqual([(c["method"], c["path"], c["body"]) for c in self.http.calls], [
            ("POST", "/api/game/agent-types", {"game_id": "q1", "types": [0, 1, 0]}),
            ("POST", "/api/game/actions", {"game_id": "q1", "day": 3, "actions": [[1, -2]]}),
            ("POST", "/api/game/practice/actions", {"game_id": "q1:78", "day": 0, "actions": [[-5]]}),
            ("POST", "/api/game/practice/reset", {"game_id": "q1:78"}),
        ])

    def test_post_match_reads_can_ask_for_a_longer_timeout(self):
        self.api.result("q1", timeout=30.0)
        self.api.replay("q1", timeout=30.0)
        self.api.practice_score("q1:78", timeout=30.0)
        self.api.state("q1")
        self.assertEqual([c["timeout"] for c in self.http.calls], [30.0, 30.0, 30.0, None])

    def test_the_token_is_read_fresh_on_every_call(self):
        token = {"value": "cu"}
        api = WebApi(self.http, lambda: token["value"])
        api.day("q1")
        token["value"] = "moi"
        api.day("q1")
        self.assertEqual([c["headers"]["Authorization"] for c in self.http.calls[-2:]],
                         ["Bearer cu", "Bearer moi"])


if __name__ == "__main__":
    unittest.main()
