"""Chế độ nhanh của WebTransport (25/11/2026): có bài lập sẵn thì dựng trạng thái ngày thẳng từ
/state, bỏ một lượt GET /day (khoảng 0,5 s trên server thật, nằm ngay trên đường gửi bài)."""

import unittest

from hexudon import rules
from hexudon.client.net import Response
from hexudon.client.web_api import GameStatus
from hexudon.client.web_games import PRACTICE
from hexudon.model import DayState
from tests.test_web_transport import ScriptedApi, scripted_transport, small_config, status


def team_json(cells, fuels, kinds):
    return {"agents": [{"agent_id": str(i), "type": "refuel" if k == rules.REFUEL else "patrol",
                        "cell": c, "fuel": None if k == rules.REFUEL else f}
                       for i, (c, f, k) in enumerate(zip(cells, fuels, kinds))]}


class FastStateTest(unittest.TestCase):
    def setUp(self):
        self.config = small_config()
        self.kinds = [rules.PATROL, rules.PATROL, rules.REFUEL]
        self.cells = [c + 1 for c in self.config.agent_starts]
        self.fuels = [7, 9, 0]
        teams = {"78": team_json(self.cells, self.fuels, self.kinds),
                 "6": team_json(self.config.agent_starts, [5, 5, 5], [rules.PATROL] * 3)}
        self.state = status("in_progress", 1, day_deadline_in=20.0, steps_today=17,
                            road_condition={"4": 1, "5": 0}, teams=teams)
        self.day = Response(200, {
            "endsAt": 0.0, "day": 1,
            "agents": [{"kind": k, "pos": c, "fuel": f} for c, f, k in zip(self.cells, self.fuels, self.kinds)],
            "others": [{"id": 6, "agents": [{"kind": 0, "pos": p} for p in self.config.agent_starts]}],
            "traffics": [{"pos": 4, "status": 1}, {"pos": 5, "status": 0}]})

    def transport(self, api, **kw):
        t = scripted_transport(api, clock=lambda: 1000.0, **kw)
        t.config = self.config
        return t

    def test_state_from_status_matches_the_day_endpoint(self):
        fast_api = ScriptedApi(state=[self.state], day=[self.day])
        fast = self.transport(fast_api).wait_day_state(self.kinds, fast=True)
        self.assertEqual(fast_api.count("day"), 0, "chế độ nhanh không gọi /day")
        slow_api = ScriptedApi(state=[self.state], day=[self.day])
        slow = self.transport(slow_api).wait_day_state(self.kinds)
        self.assertEqual(slow_api.count("day"), 1)
        for a, b in zip(fast.agents, slow.agents):
            self.assertEqual((a.kind, a.pos, a.fuel), (b.kind, b.pos, b.fuel))
        norm = lambda st: {c: v for c, v in st.road_status.items() if v}
        self.assertEqual(norm(fast), norm(slow))
        self.assertEqual((fast.day, fast.steps, fast.ends_at), (slow.day, slow.steps, slow.ends_at))
        self.assertEqual([[a.pos for a in t] for t in fast.others], [[a.pos for a in t] for t in slow.others])

    def test_missing_patrol_fuel_falls_back_to_the_day_endpoint(self):
        self.state.payload["teams"]["78"]["agents"][0]["fuel"] = None
        api = ScriptedApi(state=[self.state], day=[self.day])
        state = self.transport(api).wait_day_state(self.kinds, fast=True)
        self.assertEqual(api.count("day"), 1)
        self.assertIsInstance(state, DayState)

    def test_never_in_practice(self):
        # practice: mỗi lần nộp là chốt ngày, không có giờ để lập trước
        t = self.transport(ScriptedApi(state=[self.state], day=[self.day]), mode=PRACTICE)
        self.assertFalse(t.supports_fast_state)
        st = GameStatus.from_json(self.state.payload, 78)
        self.assertIsNone(t._state_from_status(st))


if __name__ == "__main__":
    unittest.main()
