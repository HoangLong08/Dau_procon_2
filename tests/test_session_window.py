"""Session chọn vai trò theo cửa sổ mà kênh truyền báo."""

import time
import unittest
from unittest import mock

from hexudon import rules
from hexudon.client.session import MatchSession
from hexudon.client.transport import MatchEnded, SelectionWindow, Transport
from hexudon.model import Ack, AgentState, DayState
from hexudon.offline.mapgen import generate
from hexudon.planner.strategy import MatchStrategist


def small_config():
    config = generate(seed=13, size=8, agents=3, days=2)
    config.day_seconds = [2] * config.n_days
    config.__post_init__()
    config.starts_at = time.time() - 30 * 24 * 3600     # như practice: startsAt nằm ở quá khứ
    return config


class ScriptedTransport(Transport):
    supports_resubmit = False

    def __init__(self, config, window, setup_error=None):
        self.config = config
        self.window = window
        self.setup_error = setup_error
        self.assignments = []
        self.plans = {}
        self.day = -1

    def get_setup(self):
        if self.setup_error is not None:
            raise self.setup_error
        return self.config

    def selection_window(self):
        return self.window

    def send_assignment(self, kinds, deadline=None):
        self.assignments.append((list(kinds), deadline))
        return Ack(ok=True)

    def wait_day_state(self, kinds=None):
        self.day += 1
        if self.day >= self.config.n_days:
            raise MatchEnded("hết")
        agents = [AgentState(rules.PATROL, p, self.config.fuel_limits) for p in self.config.agent_starts]
        return DayState(day=self.day, agents=agents, road_status={})

    def send_actions(self, day, plan, deadline=None):
        self.plans[day] = plan
        return Ack(ok=True)

    def get_result(self):
        return {"done": True}


class SelectionWindowTest(unittest.TestCase):
    def patch_roles(self):
        """Ghi lại ngân sách session đưa cho select_roles, thay vì chạy rollout thật."""
        calls = []

        def fake(strategist, budget_seconds=None, cap_by_start=True):
            calls.append((budget_seconds, cap_by_start))
            strategist.kinds = [rules.PATROL] * strategist.config.n_agents
            return list(strategist.kinds)

        patcher = mock.patch.object(MatchStrategist, "select_roles", fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    def session(self, transport, **kw):
        kw.setdefault("search_seconds", 0.2)
        kw.setdefault("net_margin", 0.1)
        return MatchSession(transport, seed=1, **kw)

    def test_a_closed_window_skips_selection_and_assignment(self):
        calls = self.patch_roles()
        transport = ScriptedTransport(small_config(), SelectionWindow(open=False))
        self.session(transport, role_seconds=5.0).run()
        self.assertEqual(calls, [])
        self.assertEqual(transport.assignments, [])
        self.assertEqual(sorted(transport.plans), [0, 1], "vào muộn vẫn phải chơi mọi ngày còn lại")

    def test_practice_uses_the_server_limit_not_the_old_start_time(self):
        calls = self.patch_roles()
        transport = ScriptedTransport(small_config(), SelectionWindow(open=True, deadline=None, limit=4.0))
        self.session(transport, role_seconds=None, net_margin=0.5).run()
        self.assertEqual(calls, [(3.5, False)])
        self.assertIsNone(transport.assignments[0][1], "practice không có hạn gửi vai trò")

    def test_a_contest_window_caps_the_budget_and_the_send_deadline(self):
        calls = self.patch_roles()
        window = SelectionWindow(open=True, deadline=time.time() + 2.0, limit=30.0)
        transport = ScriptedTransport(small_config(), window)
        started = time.monotonic()
        self.session(transport, role_seconds=10.0, net_margin=0.5).run()
        budget, cap = calls[0]
        self.assertFalse(cap)
        self.assertLessEqual(budget, 1.5)
        self.assertGreater(budget, 1.0)
        self.assertAlmostEqual(transport.assignments[0][1] - started, 2.0, delta=0.3)

    def test_the_role_budget_leaves_the_transport_its_send_reserve(self):
        # lề mạng 0,5 s không đủ cho một POST 5 s và một lần thử lại: kênh truyền đòi 6 s
        calls = self.patch_roles()
        window = SelectionWindow(open=True, deadline=time.time() + 8.0, limit=30.0, reserve=6.0)
        self.session(ScriptedTransport(small_config(), window), role_seconds=10.0, net_margin=0.5).run()
        budget, cap = calls[0]
        self.assertFalse(cap)
        self.assertLessEqual(budget, 2.0)
        self.assertGreater(budget, 1.5)

    def test_a_zero_length_window_gives_a_zero_role_budget(self):
        calls = self.patch_roles()
        transport = ScriptedTransport(small_config(), SelectionWindow(open=True, deadline=None, limit=0.0))
        self.session(transport, role_seconds=None, net_margin=0.5).run()
        self.assertEqual(calls, [(0.0, False)])

    def test_a_legacy_transport_keeps_the_start_time_cap(self):
        calls = self.patch_roles()
        self.session(ScriptedTransport(small_config(), None), role_seconds=3.0).run()
        self.assertEqual(calls, [(None, True)])

    def test_a_match_already_over_at_setup_returns_the_result(self):
        transport = ScriptedTransport(small_config(), None, setup_error=MatchEnded("đã xong"))
        self.assertEqual(self.session(transport).run(), {"done": True})

    def test_day_labels_follow_the_website_numbering(self):
        session = self.session(ScriptedTransport(small_config(), None))
        session.config = small_config()
        self.assertEqual(session.day_label(0), "Ngày 1/2")


class AutoBudgetTest(unittest.TestCase):
    def test_auto_search_budget_follows_the_configured_day_length(self):
        strategist = MatchStrategist(small_config(), search_seconds=None, net_margin=0.5)
        state = DayState(day=0, agents=[], road_status={})
        self.assertAlmostEqual(strategist.budget_for(state), 1.5)
        state.ends_at = time.time() + 1.0
        self.assertLessEqual(strategist.budget_for(state), 0.5)
        state.ends_at = time.time() - 5
        self.assertEqual(strategist.budget_for(state), 0.0)

    def test_the_legacy_day_zero_cap_counts_the_auto_search_budget(self):
        config = small_config()
        config.starts_at = 0.0          # server không báo giờ mở: nhánh chặn theo ngày 0
        strategist = MatchStrategist(config, search_seconds=None, net_margin=0.5, role_seconds=3.0)
        calls = []

        def fake_select_roles(config, budget_seconds=None, weights=None, seed=0, log=None):
            calls.append(budget_seconds)
            return [rules.PATROL] * config.n_agents, []

        with mock.patch("hexudon.planner.strategy.select_roles", fake_select_roles):
            strategist.select_roles()

        # ngày 0 dài 2s, chừa 0.5s mạng, rồi trừ tiếp ngân sách search tự động
        # (2s - 0.5s = 1.5s) -> 2 - 0.5 - 1.5 = 0
        self.assertEqual(calls, [0.0])


if __name__ == "__main__":
    unittest.main()
