"""Số phần trong ngày (20/11/2026): giao chuỗi thiếu cho các xe (MILP), V_near theo tầm, ngày 0.

Sau 2 contest thua DAU 2 tối 19/11: cùng D nhưng kém 26–32 phần mỗi trận, và một trận hụt 1 D ở
ngày 84 bước dù từ đúng vị trí đó có bài đủ 20 chuỗi.
"""

import unittest
from types import SimpleNamespace
from unittest import mock

from hexudon import rules
from hexudon.model import AgentState
from hexudon.offline.mapgen import generate, load_or_generate
from hexudon.planner import orienteering
from hexudon.planner.engine import make_context
from hexudon.planner.objective import _near_chains
from hexudon.planner.solution import Weights
from hexudon.planner.strategy import MatchStrategist


def fake_ctx():
    """3 xe tuần tra, 4 chuỗi, spot k thuộc chuỗi k % 4; không xe nào đứng trên spot."""
    return SimpleNamespace(
        config=SimpleNamespace(brands=[0, 1, 2, 3], spot_index_at={}),
        agents=[SimpleNamespace(pos=100 + i, fuel=50) for i in range(3)],
        patrols=[0, 1, 2], spot_brand=[k % 4 for k in range(12)],
    )


class AssignMissingTest(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(orienteering, "_reach_brands", lambda ctx, slot: {0, 1, 2, 3})
        p.start()
        self.addCleanup(p.stop)

    def test_gives_the_missing_chain_to_the_vehicle_that_loses_least(self):
        ctx = fake_ctx()
        tours = [[0, 1], [5, 6], [9]]          # chuỗi 0,1 | 1,2 | 1: thiếu chuỗi 3
        seen = {}

        def solve(ctx_, slot, tl, k_near=0, fuel_cap=None, required_brands=()):
            seen[slot] = set(required_brands)
            # xe 0 phải bỏ một spot để thêm chuỗi 3; xe 1 thêm được không mất gì; xe 2 không tới
            return {0: [0, 3], 1: [5, 6, 7], 2: None}[slot]

        with mock.patch.object(orienteering, "solve_patrol", solve):
            new = orienteering.assign_missing(ctx, tours, [False] * 3, 5.0)
        self.assertEqual(new, [[0, 1], [5, 6, 7], [9]])
        # mỗi xe giữ chuỗi chỉ nó phủ (xe 0: chuỗi 0; xe 1: chuỗi 2), cộng chuỗi được giao
        self.assertEqual(seen[0], {0, 3})
        self.assertEqual(seen[1], {2, 3})

    def test_none_when_no_vehicle_can_cover(self):
        ctx = fake_ctx()
        with mock.patch.object(orienteering, "solve_patrol", lambda *a, **k: None):
            self.assertIsNone(orienteering.assign_missing(ctx, [[0, 1], [5, 6], [9]], [False] * 3, 5.0))
        # xe đã có điểm hẹn (fixed) giữ nguyên tour, không được giải lại
        self.assertIsNone(orienteering.assign_missing(ctx, [[0, 1], [5, 6], [9]], [True] * 3, 5.0))

    def test_many_missing_chains_are_split_by_detour(self):
        # thiếu 3 chuỗi (> EXHAUSTIVE_MISSING): giao theo độ vòng, mỗi xe giải lại một lần
        ctx = fake_ctx()
        ctx.spot_cells = list(range(12))
        ctx.stocks = [1] * 12
        # bước = khoảng cách trên trục số; xe i đứng ở ô 100 + i, tour rỗng nên độ vòng = khoảng cách
        ctx.dist_step = SimpleNamespace(cost=lambda a, b: abs(a - b))
        ctx.agents = [SimpleNamespace(pos=p, fuel=50) for p in (1, 6, 11)]
        tours = [[0], [], []]                  # chỉ chuỗi 0: thiếu 1, 2, 3
        calls = []

        def solve(ctx_, slot, tl, k_near=0, fuel_cap=None, required_brands=()):
            calls.append((slot, frozenset(required_brands)))
            return [k for k in range(12) if k % 4 in required_brands and abs(k - ctx.agents[slot].pos) <= 1] or None

        with mock.patch.object(orienteering, "solve_patrol", solve):
            new = orienteering.assign_missing(ctx, tours, [False] * 3, 5.0)
        self.assertIsNotNone(new)
        self.assertFalse({0, 1, 2, 3} - orienteering._covered(ctx, new))
        self.assertLessEqual(len(calls), 3)

    def test_exact_tours_uses_it_only_when_enabled(self):
        cfg = generate(seed=3, size=9, agents=3, days=3)
        kinds = [rules.PATROL, rules.PATROL, rules.REFUEL]
        agents = [AgentState(kinds[i], cfg.agent_starts[i], cfg.fuel_limits) for i in range(3)]
        for flag, want in ((0.0, 0), (1.0, 1)):
            ctx = make_context(cfg, 0, agents, {}, [s.stocks for s in cfg.spots], set(),
                               weights=Weights(or_assign=flag))
            first = [k for k, b in enumerate(ctx.spot_brand) if b == ctx.spot_brand[0]][:1]
            called = []
            with mock.patch.object(orienteering, "solve_patrol", lambda *a, **k: list(first)), \
                    mock.patch.object(orienteering, "assign_missing",
                                      lambda *a, **k: called.append(1)):
                orienteering.exact_tours(ctx, 1.0)
            self.assertEqual(len(called), want, f"or_assign={flag}")


class NearReachTest(unittest.TestCase):
    def test_full_day_reach_counts_at_least_as_many_chains(self):
        cfg = load_or_generate("sample_16")
        agents = [AgentState(rules.PATROL, c, cfg.fuel_limits) for c in cfg.agent_starts]
        for reach in (0.5, 1.0):
            ctx = make_context(cfg, 0, agents, {}, [s.stocks for s in cfg.spots], set(),
                               weights=Weights(near_reach=reach))
            cells = [a.pos for a in agents]
            if reach == 0.5:
                half = _near_chains(ctx, cells)
            else:
                full = _near_chains(ctx, cells)
        self.assertGreaterEqual(full, half)
        self.assertEqual(Weights().near_reach, 0.5)


class SoftDay0Test(unittest.TestCase):
    def test_day0_keeps_the_unit_only_when_enabled(self):
        cfg = load_or_generate("sample_16")
        n = len(cfg.brands)
        for flag, want in ((0.0, 0.0), (1.0, 20.0)):
            w = Weights(soft_unit=20.0, soft_auto=1.0, soft_day0=flag)
            s = MatchStrategist(cfg, net_margin=0.0, seed=1, weights=w, search_iterations=10)
            self.assertEqual(s.day_weights().soft_unit, want, f"soft_day0={flag}")
            # ngày sau vẫn theo soft_auto: hôm trước hụt trần D thì không an toàn
            s.visited_brands = set(cfg.brands)
            s.history = [{"types": n - 1}]
            self.assertEqual(s.day_weights().soft_unit, 0.0)
        self.assertEqual(Weights().soft_day0, 0.0)


if __name__ == "__main__":
    unittest.main()
