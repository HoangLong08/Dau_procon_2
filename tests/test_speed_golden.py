"""Bài vàng: tăng tốc planner không được đổi kết quả (18/11/2026).

plan_day với seed và số vòng cố định trên ba ngữ cảnh phải ra ĐÚNG từng bài như lúc chụp
fixture (tests/fixtures/golden_plans.json). Fixture gắn với trọng số mặc định: khi cố ý đổi
hành vi hoặc mặc định của planner thì chụp lại bằng

    python -m tests.test_speed_golden --tao
"""

import json
import os
import sys
import unittest

from hexudon import rules
from hexudon.model import AgentState, MatchConfig
from hexudon.offline.mapgen import generate, load_or_generate
from hexudon.planner.engine import make_context, plan_day, safe_solution
from hexudon.planner.objective import lexicographic_gain
from hexudon.planner.roles import candidates
from hexudon.planner.solution import Weights

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
GOLDEN = os.path.join(FIX, "golden_plans.json")
#: bài vàng kiểm phần ALNS: MILP orienteering (Bước E, theo giây nên không tất định) để tắt
W = Weights(or_seconds=0.0)


def _ctx_sample16():
    cfg = load_or_generate("sample_16")
    kinds = candidates(cfg)[1].kinds
    agents = [AgentState(kinds[i], cfg.agent_starts[i], cfg.fuel_limits) for i in range(cfg.n_agents)]
    return make_context(cfg, 0, agents, {}, [s.stocks for s in cfg.spots], set(), weights=W)


def _ctx_vn():
    with open(os.path.join(FIX, "vn_1addebd7_ngay3.json"), encoding="utf-8") as fh:
        d = json.load(fh)
    cfg = MatchConfig.from_json(d["config"])
    agents = [AgentState(d["kinds"][i], c, f) for i, (c, f) in enumerate(d["dau"])]
    return make_context(cfg, d["day"], agents, {}, [s.stocks for s in cfg.spots], set(cfg.brands), weights=W)


def _ctx_mapgen():
    cfg = generate(seed=21, size=14, agents=5, days=4)
    kinds = [rules.PATROL] * 3 + [rules.REFUEL] * 2
    agents = [AgentState(kinds[i], cfg.agent_starts[i], cfg.fuel_limits // 2 if kinds[i] == rules.PATROL else 0)
              for i in range(cfg.n_agents)]
    return make_context(cfg, 1, agents, {}, [s.stocks for s in cfg.spots], set(cfg.brands[:3]), weights=W)


CASES = [("sample16", _ctx_sample16, 0, 120), ("vn_1addebd7", _ctx_vn, 1, 80), ("mapgen21", _ctx_mapgen, 2, 120)]


def run_cases():
    out = {}
    for name, make, seed, iters in CASES:
        ctx = make()
        _, safe = safe_solution(ctx, seed)
        dec = plan_day(ctx, seed=seed, max_iterations=iters)
        out[name] = {"safe": safe.plan, "plan": dec.plan, "score": round(dec.score, 6),
                     "gain": list(lexicographic_gain(ctx, dec.result))}
    return out


class GoldenPlansTest(unittest.TestCase):
    def test_plans_are_unchanged(self):
        if not os.path.exists(GOLDEN):
            self.skipTest("chưa có fixture: python -m tests.test_speed_golden --tao")
        with open(GOLDEN, encoding="utf-8") as fh:
            want = json.load(fh)
        got = run_cases()
        for name in want:
            with self.subTest(case=name):
                self.assertEqual(got[name]["safe"], want[name]["safe"], "bài an toàn đổi")
                self.assertEqual(got[name]["gain"], want[name]["gain"])
                self.assertEqual(got[name]["plan"], want[name]["plan"], "bài cuối đổi")
                self.assertAlmostEqual(got[name]["score"], want[name]["score"], places=4)


class SimulatorEventSkipTest(unittest.TestCase):
    """run_day không ghi vết nhảy thẳng tới bước có sự kiện; phải ra đúng như từng bước (có vết)."""

    def test_event_skip_matches_step_by_step(self):
        import random

        from hexudon.planner.decoder import decode
        from hexudon.planner.solution import DaySolution, Meet
        from hexudon.simulator import run_day

        rng = random.Random(9)
        checked = errors = 0
        for seed in range(25):
            cfg = generate(seed=300 + seed, size=rng.choice([8, 10, 12]), agents=rng.randint(3, 5), days=3)
            n = cfg.n_agents
            kinds = [rules.PATROL] * n
            for i in range(rng.randint(0, 2)):
                kinds[n - 1 - i] = rules.REFUEL
            agents = [AgentState(kinds[i], cfg.agent_starts[i],
                                 rng.randint(0, cfg.fuel_limits) if kinds[i] == rules.PATROL else 0)
                      for i in range(n)]
            steps = cfg.steps_of_day(0)
            road = {c: rng.choice([0, 0, 1, 2]) for c in cfg.road_cells}
            stocks = [sp.stocks for sp in cfg.spots]
            plans = []
            ctx = make_context(cfg, 0, agents, road, stocks, set())
            for k in range(3):
                tours = [rng.sample(range(len(cfg.spots)), rng.randint(0, min(5, len(cfg.spots))))
                         for _ in range(ctx.n_patrols)]
                sol = DaySolution(tours=tours)
                if ctx.n_cars:
                    sol.meets.append(Meet(0, rng.randrange(-1, max(0, len(tours[0]))), 0))
                plans.append(decode(ctx, sol).plan)
            # bài thô ngẫu nhiên (thường sai: hết xăng, ra ngoài, thiếu hoặc thừa bước)
            for k in range(3):
                plans.append([[rng.choice([0, 1, 2, 3, 4, 5, -1, -3]) for _ in range(rng.randint(1, steps))]
                              for _ in range(n)])
            for plan in plans:
                for strict in (False, True):
                    fast = run_day(cfg, steps, agents, plan, stocks, road, strict_fuel=strict)
                    slow = run_day(cfg, steps, agents, plan, stocks, road, trace=[], strict_fuel=strict)
                    self.assertEqual(fast.ok, slow.ok)
                    if not fast.ok:
                        errors += 1
                        self.assertEqual((fast.error.code, fast.error.agent, fast.error.token_index, fast.error.step),
                                         (slow.error.code, slow.error.agent, slow.error.token_index, slow.error.step))
                        continue
                    checked += 1
                    self.assertEqual(fast.end_pos, slow.end_pos)
                    self.assertEqual(fast.end_fuel, slow.end_fuel)
                    self.assertEqual(fast.collections, slow.collections)
                    self.assertEqual(fast.stay, slow.stay)
                    self.assertEqual(fast.stocks_left, slow.stocks_left)
        self.assertGreater(checked, 50)
        self.assertGreater(errors, 20)


if __name__ == "__main__":
    if "--tao" in sys.argv:
        data = run_cases()
        with open(GOLDEN, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        print({k: v["gain"] for k, v in data.items()})
    else:
        unittest.main()
