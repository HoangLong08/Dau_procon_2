"""R1 (16/11/2026): không bao giờ bỏ sót một chuỗi tới được.

Lỗi đo được trong giải đấu giả lập: InsertionState chỉ xét NEAR_K = 10 spot gần điểm
neo, nên một chuỗi chỉ có một spot ở góc xa không bao giờ được thử chèn, dù thưởng
chuỗi mới lớn hơn mọi D trong ngày. Bản đồ dưới đây tái hiện đúng hình đó.
"""

import random
import unittest
from dataclasses import replace

from hexudon import rules
from hexudon.model import AgentState, MatchConfig, Spot
from hexudon.planner.construct import NEAR_K, InsertionState, construct
from hexudon.planner.decoder import decode
from hexudon.planner.engine import ensure_new_chains, make_context, plan_day
from hexudon.planner.objective import end_value
from hexudon.planner.solution import Weights

W = H = 12
OLD = Weights(new_chain_first=0.0, deadline=0.0, q_share=0.0)


def pos(row, col):
    return row * W + col


def corner_config(n_days=3):
    """Đồng bằng 12x12; xe ở góc trên-trái; 11 spot gần; chuỗi 10 chỉ có spot ở góc dưới-phải."""
    near = [pos(0, 3), pos(0, 5), pos(1, 4), pos(2, 1), pos(2, 3), pos(3, 0), pos(3, 2),
            pos(4, 4), pos(1, 6), pos(4, 1), pos(2, 6)]
    spots = [Spot(brand=i % 10, pos=c, stocks=2) for i, c in enumerate(near)]
    far = pos(11, 11)
    spots.append(Spot(brand=10, pos=far, stocks=1))
    return MatchConfig(
        width=W, height=H, cells=[rules.PLAIN] * (W * H), spots=spots,
        agent_starts=[pos(0, 0), pos(0, 1), pos(1, 0)], fuel_limits=80,
        day_steps=[48] * n_days, day_seconds=[5] * n_days, players=1,
        busy_threshold=1, jammed_threshold=2,
    ), far


def context(cfg, weights, day=0, visited=frozenset(), agents=None):
    kinds = [rules.PATROL, rules.PATROL, rules.REFUEL]
    agents = agents or [AgentState(kinds[i], cfg.agent_starts[i], cfg.fuel_limits)
                        for i in range(cfg.n_agents)]
    return make_context(cfg, day, agents, {}, [s.stocks for s in cfg.spots], set(visited),
                        weights=weights)


class TestHardL(unittest.TestCase):
    def setUp(self):
        self.cfg, self.far = corner_config()
        self.far_spot = self.cfg.spot_index_at[self.far]

    def test_far_spot_is_outside_the_near_list(self):
        # điều kiện của lỗi: spot xa không nằm trong NEAR_K spot gần ô xuất phát nào
        ctx = context(self.cfg, OLD)
        state = InsertionState(ctx)
        for slot in range(ctx.n_patrols):
            self.assertNotIn(self.far_spot, state.candidate_spots(slot, 0))
        self.assertEqual(NEAR_K, 10)

    def test_new_chain_spots_are_always_candidates(self):
        ctx = context(self.cfg, Weights())
        state = InsertionState(ctx)
        for slot in range(ctx.n_patrols):
            self.assertIn(self.far_spot, state.candidate_spots(slot, 0))
        # đã thu chuỗi 10 trong trận thì không còn là ứng viên bắt buộc nữa
        ctx_seen = context(self.cfg, Weights(), visited={10})
        self.assertNotIn(self.far_spot, InsertionState(ctx_seen).candidate_spots(0, 0))

    def test_construct_visits_the_far_chain_on_day_zero(self):
        old = decode(context(self.cfg, OLD), construct(context(self.cfg, OLD), random.Random(0)))
        new_ctx = context(self.cfg, Weights())
        new = decode(new_ctx, construct(new_ctx, random.Random(0)))
        self.assertNotIn(10, old.result.brands, "bản cũ phải tái hiện lỗi bỏ sót")
        self.assertIn(10, new.result.brands)
        self.assertTrue(new.result.ok)

    def test_plan_day_visits_the_far_chain(self):
        ctx = context(self.cfg, Weights())
        decoded = plan_day(ctx, seed=0, max_iterations=60)
        self.assertIn(10, decoded.result.brands)

    def test_ensure_new_chains_patches_a_plan_that_skipped_it(self):
        old_ctx = context(self.cfg, OLD)
        sol = construct(old_ctx, random.Random(0))
        ctx = context(self.cfg, Weights())
        before = decode(ctx, sol)
        self.assertNotIn(10, before.result.brands)
        patched_sol, patched = ensure_new_chains(ctx, sol, before)
        self.assertIn(10, patched.result.brands)
        self.assertGreater(patched.score, before.score)
        self.assertTrue(patched.result.ok)
        # tắt cờ thì không làm gì
        same_sol, same = ensure_new_chains(replace(ctx, weights=OLD), sol, before)
        self.assertIs(same, before)

    def test_deadline_penalty_when_tomorrow_is_the_last_chance(self):
        # 2 ngày: cuối ngày 0 mọi xe vẫn ở góc trên-trái, chuỗi 10 cách xa hơn nửa số bước còn lại
        cfg, far = corner_config(n_days=2)
        ctx_on = context(cfg, Weights())
        ctx_off = context(cfg, Weights(deadline=0.0))
        stay = decode(ctx_on, ctx_on.empty_solution()).result
        self.assertNotIn(10, stay.brands)
        gap = end_value(ctx_off, stay) - end_value(ctx_on, stay)
        # mọi chuỗi chưa thu đều ở xa? không: chỉ những chuỗi xa hơn 0,5 × 48 bước bị phạt
        far_brands = 0
        for brand in cfg.brands:
            best = min(ctx_on.dist_step.cost(a.pos, cfg.spots[si].pos)
                       for a in ctx_on.agents if a.kind == rules.PATROL
                       for si in cfg.spots_by_brand[brand])
            if 0.5 * 48 < best <= 48:
                far_brands += 1
        self.assertGreaterEqual(far_brands, 1)
        self.assertAlmostEqual(gap, far_brands * ctx_on.new_brand_value)


if __name__ == "__main__":
    unittest.main()
