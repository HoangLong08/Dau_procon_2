"""Lập kế hoạch trước (25/11/2026): lập sẵn bài ngày d+1 trong lúc ngày d chạy, gửi ngay khi ngày mở.

Thời gian phản hồi (tiêu chí 4) là thời điểm gửi bài hợp lệ cuối cùng mỗi ngày. Bot gửi bài cuối
quanh giây 27/30 (khoảng 250 s mỗi trận). Trên server VN đường không bao giờ tắc và bộ mô phỏng
khớp server (replay_check), nên trạng thái đầu ngày mai biết trước được từ bài của chính ta.
"""

import time
import unittest

from hexudon import rules
from hexudon.client.session import PIPELINE_FROM_DAY, MatchSession
from hexudon.client.transport import MatchEnded, Transport
from hexudon.model import Ack, AgentState, DayState
from hexudon.offline.mapgen import generate
from hexudon.simulator import run_day, wait_plan


class TimedTransport(Transport):
    """Server giả có giờ: ngày d mở lúc start + d·T, hết giờ thì áp dụng bài hợp lệ cuối cùng."""

    def __init__(self, config, day_seconds, perturb_day=None):
        self.config = config
        self.T = day_seconds
        self.perturb_day = perturb_day
        self.kinds = None
        self.day = -1
        self.start = None
        self.agents = None
        self.submits = {}       # ngày -> [(giây kể từ lúc mở, nhận?)]
        self.accepted = {}
        self.servings = 0

    def get_setup(self):
        return self.config

    def send_assignment(self, kinds, deadline=None):
        self.kinds = list(kinds)
        return Ack(ok=True)

    def opens(self, day):
        return self.start + day * self.T

    def stocks(self):
        return [s.stocks for s in self.config.spots]

    def wait_day_state(self, kinds=None):
        if self.start is None:
            self.start = time.time() + 0.05
            self.agents = [AgentState(k, p, self.config.fuel_limits)
                           for k, p in zip(self.kinds, self.config.agent_starts)]
        else:
            time.sleep(max(0.0, self.opens(self.day + 1) - time.time()))
            steps = self.config.steps_of_day(self.day)
            plan = self.accepted.get(self.day) or wait_plan(len(self.agents), steps)
            res = run_day(self.config, steps, self.agents, plan, self.stocks(), {})
            self.servings += res.servings
            self.agents = [AgentState(a.kind, res.end_pos[i], res.end_fuel[i]) for i, a in enumerate(self.agents)]
        self.day += 1
        if self.day >= self.config.n_days:
            raise MatchEnded("hết trận")
        time.sleep(max(0.0, self.opens(self.day) - time.time()))
        seen = [AgentState(a.kind, a.pos, a.fuel) for a in self.agents]
        if self.day == self.perturb_day:
            # server báo khác dự đoán (như khi có tắc đường): bot phải nhận ra và lập lại
            i = next(i for i, a in enumerate(seen) if a.kind == rules.PATROL)
            seen[i] = AgentState(seen[i].kind, seen[i].pos, seen[i].fuel - 1)
        return DayState(day=self.day, agents=seen, road_status={}, ends_at=self.opens(self.day + 1),
                        steps=self.config.steps_of_day(self.day))

    def send_actions(self, day, plan, deadline=None):
        t = time.time() - self.opens(day)
        res = run_day(self.config, self.config.steps_of_day(day), self.agents, plan, self.stocks(), {})
        ok = day == self.day and t < self.T and res.ok
        self.submits.setdefault(day, []).append((t, ok))
        if ok:
            self.accepted[day] = plan
        return Ack(ok=ok, reason=None if ok else "E_REJECTED")


def config(days=5, T=1.5):
    cfg = generate(seed=11, size=10, agents=3, days=days)
    cfg.day_seconds = [T] * days
    cfg.__post_init__()
    return cfg


def play(pipeline, perturb_day=None, T=1.5):
    cfg = config(T=T)
    transport = TimedTransport(cfg, T, perturb_day=perturb_day)
    logs = []
    session = MatchSession(transport, search_seconds=None, net_margin=0.2, role_seconds=0.3, seed=1,
                           log=logs.append, search_jobs=1, pipeline=pipeline)
    session.pipeline_min_seconds = 0.2   # ngày giả chỉ 1,5 s
    session.run()
    return cfg, transport, session, logs


class PipelineTest(unittest.TestCase):
    def test_preplanned_days_are_sent_once_right_after_opening(self):
        cfg, tr, session, logs = play(True)
        self.assertEqual(sorted(tr.accepted), list(range(cfg.n_days)), "mọi ngày có bài được nhận")
        for day in range(PIPELINE_FROM_DAY + 1, cfg.n_days):
            sent = tr.submits[day]
            self.assertEqual(len(sent), 1, f"ngày {day}: chỉ gửi bài lập sẵn")
            self.assertLess(sent[0][0], 0.3, f"ngày {day}: gửi ngay khi ngày mở")
        # ngày 1 chưa có bài lập sẵn: lập với một phần ngân sách, phần còn lại lập trước ngày 2
        self.assertLess(tr.submits[PIPELINE_FROM_DAY][-1][0], 0.75 * tr.T)
        self.assertFalse(session.pipeline_off)
        self.assertTrue(any("đã lập sẵn" in m for m in logs))
        # bộ nhớ trận khớp đúng những gì server áp dụng
        self.assertEqual(session.strategist.servings_total, tr.servings)

    def test_a_state_that_differs_from_the_prediction_turns_it_off(self):
        cfg, tr, session, logs = play(True, perturb_day=PIPELINE_FROM_DAY + 1)
        self.assertTrue(session.pipeline_off)
        self.assertTrue(any("khác dự đoán" in m for m in logs))
        self.assertEqual(sorted(tr.accepted), list(range(cfg.n_days)))
        # bài lập sẵn vẫn hợp lệ trên trạng thái thật nên được gửi ngay; sau đó lập như thường
        self.assertLess(tr.submits[PIPELINE_FROM_DAY + 1][0][0], 0.3)
        self.assertEqual(session.strategist.servings_total, tr.servings)

    def test_off_without_a_timed_day(self):
        cfg = config()
        session = MatchSession(TimedTransport(cfg, 1.5), pipeline=True)
        session.config = cfg
        agents = [AgentState(rules.PATROL, c, cfg.fuel_limits) for c in cfg.agent_starts]
        self.assertFalse(session._pipeline_ready(DayState(day=1, agents=agents, road_status={})))
        self.assertTrue(session._pipeline_ready(DayState(day=1, agents=agents, road_status={},
                                                         ends_at=time.time() + 5)))
        self.assertFalse(session._pipeline_ready(DayState(day=cfg.n_days - 1, agents=agents, road_status={},
                                                          ends_at=time.time() + 5)), "ngày cuối")
        self.assertFalse(MatchSession(TimedTransport(cfg, 1.5)).pipeline, "mặc định tắt")


if __name__ == "__main__":
    unittest.main()
