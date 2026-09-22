"""Móc quan sát trong planner: gọi đúng lúc và không đổi kết quả tìm kiếm."""

import unittest

from hexudon import rules
from hexudon.model import AgentState
from hexudon.offline.mapgen import generate
from hexudon.planner.alns import DESTROY_OPS, REPAIR_OPS, improve
from hexudon.planner.engine import make_context, plan_day
from hexudon.planner.roles import select_roles


def day_context(seed=5):
    """Ngữ cảnh ngày 0 dựng mới mỗi lần gọi, để hai lần chạy không dùng chung bộ nhớ đệm."""
    config = generate(seed=seed, size=8, agents=3, days=2)
    kinds = [rules.PATROL, rules.PATROL, rules.REFUEL]
    agents = [AgentState(kinds[i], config.agent_starts[i], config.fuel_limits) for i in range(3)]
    status = {c: rules.SMOOTH for c in config.road_cells}
    return make_context(config, 0, agents, status, [s.stocks for s in config.spots], set())


class AlnsProgressTest(unittest.TestCase):
    def test_progress_callback_does_not_change_the_search(self):
        plain = improve(day_context(), max_iterations=200, seed=3)
        rows = []
        watched = improve(day_context(), max_iterations=200, seed=3,
                          on_progress=rows.append, progress_every=0.0)
        self.assertEqual(watched.decoded.plan, plain.decoded.plan)
        self.assertEqual(watched.decoded.score, plain.decoded.score)
        self.assertEqual(watched.iterations, plain.iterations)
        self.assertGreaterEqual(len(rows), 200)
        iterations = [r["iteration"] for r in rows]
        self.assertEqual(iterations, sorted(iterations))
        self.assertEqual(rows[-1]["iteration"], 200)
        self.assertEqual(rows[-1]["best"], round(watched.decoded.score, 3))
        self.assertEqual(set(rows[-1]["destroy"]), set(DESTROY_OPS))
        self.assertEqual(set(rows[-1]["repair"]), set(REPAIR_OPS))

    def test_without_a_callback_nothing_else_changes(self):
        result = improve(day_context(), max_iterations=50, seed=1)
        self.assertEqual(result.iterations, 50)


class PlanDayBetterTest(unittest.TestCase):
    def test_on_better_reports_only_strict_improvements_over_the_safe_plan(self):
        plain = plan_day(day_context(), max_iterations=300, seed=1)
        seen, sent = [], []
        watched = plan_day(day_context(), max_iterations=300, seed=1,
                           on_plan=sent.append, on_better=seen.append)
        self.assertEqual(watched.plan, plain.plan)
        scores = [d.score for d in seen]
        self.assertEqual(scores, sorted(set(scores)), "mỗi lần báo phải tốt hơn hẳn lần trước")
        self.assertTrue(all(s > sent[0].score for s in scores), "phải hơn cả bài an toàn")


class RoleCandidateTest(unittest.TestCase):
    def test_every_option_is_reported_with_its_rollout_score(self):
        config = generate(seed=7, size=8, agents=4, days=2)
        rows = []
        _, options = select_roles(config, budget_seconds=1.5, seed=0, on_candidate=rows.append)
        first_round = {r["label"]: r for r in rows if r["phase"] in ("rollout", "skipped")}
        for cand in options:
            self.assertIn(cand.label, first_round)
            if cand.evaluated:
                self.assertEqual(first_round[cand.label]["score"], list(cand.score))
                self.assertEqual(first_round[cand.label]["kinds"], list(cand.kinds))
        for row in rows:
            self.assertIn(row["phase"], ("rollout", "refine", "skipped"))
