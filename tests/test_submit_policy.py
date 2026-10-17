"""Nộp bài theo cases 11.1–11.8: bài hợp lệ cuối cùng được áp dụng và tính thời gian phản hồi."""

import time
import unittest
from unittest import mock

from hexudon import rules
from hexudon.client.session import MatchSession
from hexudon.client.transport import MatchEnded, Transport
from hexudon.model import Ack, AgentState, DayState
from hexudon.offline.mapgen import generate
from hexudon.planner.decoder import Decoded
from hexudon.planner.strategy import MAX_SUBMITS_PER_DAY, MatchStrategist
from hexudon.simulator import DayResult, run_day


def first_state(config, kinds, day=0, steps=None):
    agents = [AgentState(k, p, config.fuel_limits) for k, p in zip(kinds, config.agent_starts)]
    return DayState(day=day, agents=agents, road_status={}, steps=steps)


class PolicyTest(unittest.TestCase):
    """Chính sách gửi lại, tách khỏi tìm kiếm: plan_day giả phát ra đúng chuỗi nghiệm cho trước."""

    def run_policy(self, sequence, accept=True):
        config = generate(seed=3, size=8, agents=3, days=2)
        strat = MatchStrategist(config, search_seconds=0.1, net_margin=0.0, seed=1)
        strat.kinds = [rules.PATROL] * config.n_agents
        state = first_state(config, strat.kinds)
        attempts = []

        def make(ctx, tag, brands, servings, score):
            result = DayResult(ok=True, end_pos=[a.pos for a in ctx.agents],
                               end_fuel=[a.fuel for a in ctx.agents],
                               collections=[(1, 0, 0)] * servings, stay={},
                               stocks_left=list(ctx.stocks), brands=set(brands))
            return Decoded(plan=[["nghiệm", tag]], result=result, score=score)

        def fake_plan_day(ctx, deadline=None, seed=0, on_plan=None, on_improve=None, **hooks):
            made = {}
            last = None
            for tag, (hook, brands, servings, score) in enumerate(sequence):
                key = (tuple(sorted(brands)), servings, score)
                decoded = made.setdefault(key, make(ctx, tag, brands, servings, score))
                (on_plan if hook == "plan" else on_improve)(decoded)
                last = decoded
            return last

        def submit(plan):
            attempts.append(plan[0][1])
            return accept

        with mock.patch("hexudon.planner.strategy.plan_day", fake_plan_day):
            applied = strat.plan_day(state, deadline=time.monotonic() + 1.0, submit=submit)
        return attempts, applied

    def test_only_worthwhile_improvements_are_sent_and_the_last_slot_is_kept_for_the_final(self):
        sent, applied = self.run_policy([
            ("plan", {0}, 1, 1000.0),        # 0: bài an toàn -> gửi
            ("improve", {0}, 1, 1100.0),     # 1: cùng L/D/Q, chỉ hơn 0,1B -> bỏ
            ("improve", {0, 1}, 2, 2500.0),  # 2: D tăng -> gửi giữa chừng
            ("improve", {0, 1}, 3, 2600.0),  # 3: Q tăng nhưng hết lượt giữa chừng -> chờ
            ("plan", {0, 1}, 3, 2600.0),     # bài cuối (chính là 3) -> gửi bằng lượt chừa lại
        ])
        self.assertEqual(sent, [0, 2, 3])
        self.assertLessEqual(len(sent), MAX_SUBMITS_PER_DAY)
        self.assertEqual(applied.plan, [["nghiệm", 3]], "bài áp dụng là bài gửi cuối cùng")

    def test_a_final_plan_already_sent_is_not_sent_again(self):
        sent, applied = self.run_policy([
            ("plan", {0}, 1, 1000.0),
            ("improve", {0, 1}, 2, 2500.0),
            ("plan", {0, 1}, 2, 2500.0),     # bài cuối trùng bài vừa gửi giữa chừng
        ])
        self.assertEqual(sent, [0, 1], "gửi lại bài cũ chỉ làm chậm thời gian phản hồi")
        self.assertEqual(applied.plan, [["nghiệm", 1]])

    def test_a_big_enough_score_gain_alone_is_worth_sending(self):
        sent, _ = self.run_policy([
            ("plan", {0}, 1, 1000.0),
            ("plan", {0}, 1, 1300.0),        # cùng L/D/Q nhưng hơn 0,3B (ví dụ: vị trí cuối ngày)
        ])
        self.assertEqual(sent, [0, 1])

    def test_rejected_attempts_are_capped_too(self):
        sent, applied = self.run_policy([
            ("plan", {0}, 1, 1000.0),
            ("improve", {0, 1}, 2, 2500.0),
            ("improve", {0, 1, 2}, 3, 3500.0),
            ("plan", {0, 1, 2}, 3, 3500.0),
        ], accept=False)
        self.assertEqual(len(sent), MAX_SUBMITS_PER_DAY, "server từ chối cũng không được gửi dồn dập")
        self.assertEqual(sent, [0, 1, 2])


class OneDayTransport(Transport):
    """Kênh giả một ngày: ghi mọi bài gửi tới; `accept(n, plan)` quyết định có nhận không."""

    def __init__(self, config, day=0, steps=None, accept=None):
        self.config = config
        self.day = day
        self.steps = steps
        self.accept = accept or (lambda n, plan: True)
        self.plans = []
        self.kinds = None
        self.served = False

    def get_setup(self):
        return self.config

    def send_assignment(self, kinds, deadline=None):
        self.kinds = list(kinds)
        return Ack(ok=True)

    def wait_day_state(self, kinds=None):
        if self.served:
            raise MatchEnded("hết trận")
        self.served = True
        state = first_state(self.config, self.kinds, day=self.day, steps=self.steps)
        state.ends_at = time.time() + 30
        return state

    def send_actions(self, day, plan, deadline=None):
        self.plans.append(plan)
        ok, reason = self.accept(len(self.plans), plan)
        return Ack(ok=ok, reason=reason)


def small_config():
    config = generate(seed=9, size=14, agents=4, days=3)
    config.day_steps = [24, 36, 30]
    config.day_seconds = [30, 30, 30]
    config.__post_init__()
    return config


class SessionMemoryTest(unittest.TestCase):
    """Bộ nhớ trận phải theo bài server đã nhận, không theo bài tốt nhất ta nghĩ ra (cases 11.3)."""

    def test_memory_follows_the_accepted_plan_when_later_plans_are_refused(self):
        config = small_config()
        transport = OneDayTransport(
            config, accept=lambda n, plan: (True, None) if n == 1 else (False, "E_REJECTED"))
        session = MatchSession(transport, search_seconds=1.0, net_margin=0.0, role_seconds=0.5, seed=1)
        session.run()
        accepted = transport.plans[0]
        state = first_state(config, transport.kinds)
        expected = run_day(config, config.steps_of_day(0), state.agents, accepted,
                           [s.stocks for s in config.spots], {})
        strat = session.strategist
        self.assertEqual([a.pos for a in strat.predicted_state], expected.end_pos)
        self.assertEqual(strat.visited_brands, expected.brands)
        self.assertEqual(strat.servings_total, expected.servings)

    def test_memory_records_a_waiting_day_when_nothing_is_accepted(self):
        config = small_config()
        transport = OneDayTransport(config, accept=lambda n, plan: (False, "E_REJECTED"))
        session = MatchSession(transport, search_seconds=0.3, net_margin=0.0, role_seconds=0.5, seed=1)
        session.run()
        strat = session.strategist
        self.assertEqual([a.pos for a in strat.predicted_state], config.agent_starts,
                         "không bài nào được nhận thì mọi xe đứng yên cả ngày")
        self.assertLessEqual(len(transport.plans), MAX_SUBMITS_PER_DAY + 2)


class ServerStepsTest(unittest.TestCase):
    """Số bước server báo (steps_today) là chuẩn; không đoán cách đánh số ngày (Formats: day = 0)."""

    def test_plans_use_the_steps_the_server_reports(self):
        config = small_config()
        steps = 19                      # khác daySteps[0] = 24 trong config

        def accept(n, plan):
            state = first_state(config, transport.kinds)
            check = run_day(config, steps, state.agents, plan, [s.stocks for s in config.spots], {})
            return (True, None) if check.ok else (False, check.error.code)

        transport = OneDayTransport(config, steps=steps, accept=accept)
        session = MatchSession(transport, search_seconds=0.5, net_margin=0.0, role_seconds=0.5, seed=1)
        session.run()
        self.assertTrue(transport.plans)
        for plan in transport.plans:
            state = first_state(config, transport.kinds)
            self.assertTrue(run_day(config, steps, state.agents, plan,
                                    [s.stocks for s in config.spots], {}).ok)
        self.assertEqual(session.day_offset, 0)

    def test_a_step_rejection_does_not_shift_days_when_the_server_reports_steps(self):
        config = small_config()
        # ngày 1 (day = 1) server báo 36 bước, đúng daySteps[1]; lần gửi đầu bị từ chối vì "STEP"
        transport = OneDayTransport(
            config, day=1, steps=36,
            accept=lambda n, plan: (False, "E_STEP_OVERFLOW") if n == 1 else (True, None))
        session = MatchSession(transport, search_seconds=0.3, net_margin=0.0, role_seconds=0.5, seed=1)
        session.run()
        self.assertEqual(session.day_offset, 0, "server đã báo số bước thì không đoán lệch ngày")
        state = first_state(config, transport.kinds, day=1)
        for plan in transport.plans[1:]:
            self.assertTrue(run_day(config, 36, state.agents, plan,
                                    [s.stocks for s in config.spots], {}).ok)


if __name__ == "__main__":
    unittest.main()
