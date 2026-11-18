"""R2 (16/11/2026): pha Q khoá (chuỗi mới, D) hôm nay rồi mới tìm thêm phần."""

import unittest
from dataclasses import replace
from unittest import mock

from hexudon.model import AgentState
from hexudon.offline.mapgen import load_or_generate
from hexudon.planner import engine
from hexudon.planner.engine import make_context, plan_day, q_phase_weights
from hexudon.planner.objective import lexicographic_gain, score_day
from hexudon.planner.roles import candidates
from hexudon.planner.solution import Weights


def day0_context(weights):
    cfg = load_or_generate("sample_16")
    kinds = candidates(cfg)[1].kinds  # 1 xe tiếp
    agents = [AgentState(kinds[i], cfg.agent_starts[i], cfg.fuel_limits) for i in range(cfg.n_agents)]
    return make_context(cfg, 0, agents, {}, [s.stocks for s in cfg.spots], set(), weights=weights)


class TestQPhase(unittest.TestCase):
    def test_soft_terms_are_scaled_and_guards_are_kept(self):
        w = Weights(q_end_scale=0.1, q_B=0.0)
        q = q_phase_weights(w)
        for name in engine.SOFT_END_TERMS:
            self.assertAlmostEqual(getattr(q, name), getattr(w, name) * 0.1)
        for name in ("B", "new_brand", "serving", "cover_penalty", "strand", "deadline"):
            self.assertEqual(getattr(q, name), getattr(w, name))

    def test_shrunk_b_keeps_the_absolute_size_of_the_guards(self):
        w = Weights(q_B=0.05)
        q = q_phase_weights(w)
        self.assertEqual(q.B, 0.05)
        for name in ("cover_penalty", "strand", "deadline"):
            self.assertAlmostEqual(getattr(q, name) * q.B, getattr(w, name) * w.B)
        self.assertEqual(q.serving, w.serving)
        # q_B = 0 giữ B như pha 1
        self.assertEqual(q_phase_weights(Weights(q_B=0.0)).B, Weights().B)
        ctx = day0_context(w)
        final = plan_day(ctx, seed=4, max_iterations=120)
        self.assertTrue(final.result.ok)
        self.assertAlmostEqual(final.score, score_day(ctx, final.result, truncated=final.truncated,
                                                      dropped_meets=final.dropped_meets))

    def test_phase_two_never_loses_chains_found_in_phase_one(self):
        n = 120
        w = Weights(q_share=0.5)
        ctx = day0_context(w)
        final = plan_day(ctx, seed=3, max_iterations=n)
        # pha 1 của lần chạy trên = một lần chạy không có pha Q với đúng số vòng ấy
        phase1 = plan_day(day0_context(replace(w, q_share=0.0)), seed=3, max_iterations=n // 2)
        self.assertGreaterEqual(lexicographic_gain(ctx, final.result)[:2],
                                lexicographic_gain(ctx, phase1.result)[:2])
        # bài trả về mang điểm theo trọng số của ctx, không theo trọng số pha Q
        self.assertAlmostEqual(final.score, score_day(ctx, final.result, truncated=final.truncated,
                                                      dropped_meets=final.dropped_meets))

    def test_deterministic(self):
        a = plan_day(day0_context(Weights()), seed=1, max_iterations=80)
        b = plan_day(day0_context(Weights()), seed=1, max_iterations=80)
        self.assertEqual(a.plan, b.plan)

    def test_phase_two_reports_only_strict_lexicographic_gains(self):
        # pha 2 chỉ báo on_improve khi (chuỗi mới, D, Q) hôm nay tăng ngặt: offer() không
        # bao giờ gửi lại vì một thay đổi chỉ có trong điểm nội bộ (tiêu chí 4)
        ctx = day0_context(Weights(q_share=0.5))
        in_q = {"on": False}
        calls = []
        real_improve = engine.improve

        def spy(*args, **kwargs):
            if kwargs.get("band") is not None:
                in_q["on"] = True
            return real_improve(*args, **kwargs)

        with mock.patch.object(engine, "improve", spy):
            final = plan_day(ctx, seed=2, max_iterations=160,
                             on_improve=lambda d: calls.append((in_q["on"], lexicographic_gain(ctx, d.result),
                                                                d.result.ok)))
        self.assertTrue(in_q["on"], "pha 2 phải chạy")
        q_gains = [g for q, g, _ in calls if q]
        self.assertTrue(all(b > a for a, b in zip(q_gains, q_gains[1:])), q_gains)
        self.assertTrue(all(ok for _, _, ok in calls))
        self.assertTrue(final.result.ok)


if __name__ == "__main__":
    unittest.main()
