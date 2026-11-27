"""Bộ giải tour bằng Rust (rust/tour, planner/rust_tours.py, 25/11/2026). Cần build trước:
`cargo build --release` trong rust/tour; không có file nhị phân thì bỏ qua."""

import dataclasses
import unittest

from hexudon import rules
from hexudon.model import AgentState
from hexudon.offline.mapgen import generate
from hexudon.planner import rust_tours
from hexudon.planner.decoder import decode
from hexudon.planner.engine import fuel_ready, make_context, plan_day, rust_start
from hexudon.planner.objective import lexicographic_gain
from hexudon.planner.solution import DaySolution, Weights
from hexudon.simulator import run_day
from tests.test_orienteering import brute_force, small_ctx, tour_steps


@unittest.skipUnless(rust_tours.available(), "chưa build rust/tour")
class RustToursTest(unittest.TestCase):
    def test_matches_brute_force_when_every_spot_is_one_chain(self):
        # một chuỗi duy nhất: D luôn 1, nên bộ giải chỉ còn tối đa số spot; kho ≥ số xe nên tối
        # ưu chung = tổng tối ưu của từng xe (vét cạn)
        for seed, size, steps in ((1, 8, 24), (1, 9, 14), (2, 10, 16), (3, 10, 16)):
            ctx = small_ctx(seed=seed, size=size, steps=steps)
            ctx = dataclasses.replace(ctx, stocks=[ctx.n_patrols] * len(ctx.stocks))   # kho không ràng buộc
            ctx.spot_brand = [0] * len(ctx.spot_brand)    # trường tự tính (init=False)
            tours = rust_tours.solve(ctx, 0.5)
            self.assertIsNotNone(tours)
            for slot, tour in enumerate(tours):
                self.assertEqual(len(tour), len(set(tour)), "không ghé lại spot")
                self.assertLessEqual(tour_steps(ctx, slot, tour), ctx.steps)
            self.assertEqual(sum(len(t) for t in tours),
                             sum(brute_force(ctx, s) for s in range(ctx.n_patrols)), f"seed {seed}")

    def test_tours_decode_to_valid_plans(self):
        for seed in (4, 5, 6):
            ctx = small_ctx(seed=seed, size=10)
            tours = rust_tours.solve(ctx, 0.3, seed=seed)
            dec = decode(ctx, DaySolution(tours=tours, meets=[], waits={}))
            self.assertTrue(dec.result.ok)
            res = run_day(ctx.config, ctx.steps, ctx.agents, dec.plan, ctx.stocks, ctx.road_status)
            self.assertTrue(res.ok, res.error)

    def test_plan_day_with_the_rust_start(self):
        ctx = small_ctx(seed=6, size=10, weights=Weights(rs_seconds=0.3))
        base = plan_day(small_ctx(seed=6, size=10), seed=0, max_iterations=30)
        dec = plan_day(ctx, seed=0, max_iterations=30)
        self.assertTrue(dec.result.ok)
        self.assertGreaterEqual(lexicographic_gain(ctx, dec.result), lexicographic_gain(ctx, base.result))

    def test_off_by_default(self):
        self.assertEqual(Weights().rs_seconds, 0.0)
        self.assertEqual((Weights().rs_look, Weights().rs_ready), (1.0, 1.0))
        # 27/11: nới hai điều kiện ở ngày D an toàn và ngày 0 (chỉ có tác dụng khi rs_seconds > 0)
        self.assertEqual((Weights().rs_safe_ld, Weights().rs_day0_lex, Weights().rs_safe_free), (1.0, 1.0, 0.0))

    def test_safe_days_skip_the_fuel_gate_when_asked(self):
        # rs_safe_free (26/11): ngày D an toàn (soft_unit > 0 sau day_weights) bỏ rs_ready; ngày chưa
        # an toàn (strategy.day_weights đặt soft_unit = 0) thì vẫn giữ
        cfg = generate(seed=4, size=10, agents=3, days=3)
        agents = [AgentState(rules.PATROL, cfg.agent_starts[i], cfg.fuel_limits) for i in range(cfg.n_agents)]
        for soft_unit, taken in ((20.0, True), (0.0, False)):
            ctx = make_context(cfg, 0, agents, {}, [s.stocks for s in cfg.spots], set(),
                               weights=Weights(rs_seconds=0.3, rs_safe_free=1.0, soft_unit=soft_unit))
            ctx = dataclasses.replace(ctx, next_steps=cfg.fuel_limits)
            idle = DaySolution(tours=[[] for _ in ctx.patrols], meets=[], waits={})
            idle_dec = decode(ctx, idle)
            _, dec = rust_start(ctx, idle, idle_dec, None, lambda s, d: None)
            self.assertEqual(dec is not idle_dec, taken, f"soft_unit {soft_unit}")

    def test_safe_ld_keeps_the_fuel_gate_only_while_fuel_binds(self):
        # rs_safe_ld (26/11): ngày D an toàn bỏ rs_ready khi một bình đủ cho mọi ngày còn lại,
        # còn xăng là ràng buộc (fuel_limits < số bước lớn nhất còn lại) thì giữ
        cfg = generate(seed=4, size=10, agents=3, days=3)
        agents = [AgentState(rules.PATROL, cfg.agent_starts[i], cfg.fuel_limits) for i in range(cfg.n_agents)]
        for rest, taken in ((cfg.fuel_limits, True), (cfg.fuel_limits + 1, False)):
            cfg.day_steps = [cfg.day_steps[0]] + [rest] * (len(cfg.day_steps) - 1)
            ctx = make_context(cfg, 0, agents, {}, [s.stocks for s in cfg.spots], set(),
                               weights=Weights(rs_seconds=0.3, rs_safe_ld=1.0))
            ctx = dataclasses.replace(ctx, next_steps=cfg.fuel_limits)
            idle = DaySolution(tours=[[] for _ in ctx.patrols], meets=[], waits={})
            idle_dec = decode(ctx, idle)
            _, dec = rust_start(ctx, idle, idle_dec, None, lambda s, d: None)
            self.assertEqual(dec is not idle_dec, taken, f"bước còn lại {rest}")

    def test_safe_ld_compares_only_l_and_d_of_the_lookahead(self):
        # chơi tiếp cho bài Rust ít Q hơn nhưng cùng (chuỗi mới, D): rs_look bác, rs_safe_ld nhận;
        # ít D hơn thì cả hai đều bác
        ctx0 = small_ctx(seed=5, size=10)
        idle = DaySolution(tours=[[] for _ in ctx0.patrols], meets=[], waits={})
        for d_off, q_off, taken in ((0, -10 ** 6, True), (-1, 0, False)):
            for ld in (0.0, 1.0):
                ctx = dataclasses.replace(ctx0, weights=Weights(rs_seconds=0.3, rs_ready=0.0, rs_safe_ld=ld))
                idle_dec = decode(ctx, idle)
                base = lexicographic_gain(ctx, idle_dec.result)

                def look(d):
                    # tổng hôm nay + chơi tiếp của bài Rust: cùng (chuỗi mới, D) với bài đứng yên,
                    # cộng d_off vào D và q_off vào Q
                    if d is idle_dec:
                        return (0, 0, 0)
                    m = lexicographic_gain(ctx, d.result)
                    return (base[0] - m[0], base[1] - m[1] + d_off, q_off)
                _, dec = rust_start(ctx, idle, idle_dec, None, lambda s, d: None, lookahead=look)
                self.assertEqual(dec is not idle_dec, taken and ld > 0, f"D {d_off} Q {q_off} ld {ld}")

    def test_day0_takes_more_servings_by_the_lexicographic_gain(self):
        # rs_day0_lex: ngày 0 bài Rust có (chuỗi mới, D, Q) cao hơn thì được nhận dù score_day không
        # cao hơn (ở đây ép bằng một bài đang có điểm rất cao)
        ctx = small_ctx(seed=5, size=10, weights=Weights(rs_seconds=0.3, rs_day0_lex=1.0, rs_ready=0.0,
                                                         rs_look=0.0))
        idle = DaySolution(tours=[[] for _ in ctx.patrols], meets=[], waits={})
        idle_dec = decode(ctx, idle)
        idle_dec.score = 1e18
        _, dec = rust_start(ctx, idle, idle_dec, None, lambda s, d: None)
        self.assertIsNot(dec, idle_dec)
        off = dataclasses.replace(ctx, weights=Weights(rs_seconds=0.3, rs_ready=0.0, rs_look=0.0,
                                                       rs_day0_lex=0.0))
        _, dec = rust_start(off, idle, idle_dec, None, lambda s, d: None)
        self.assertIs(dec, idle_dec)

    def test_match_commands_turn_it_on(self):
        # bật ở lệnh thi đấu (--rust-seconds), không bật trong Weights(): test, bench, sim giữ tất định
        from hexudon.cli import build_parser
        from hexudon.client.session import RUST_SECONDS, match_weights
        self.assertGreater(RUST_SECONDS, 0)
        for argv in (["web", "--game", "g"], ["play", "--url", "http://x", "--match", "m", "--token", "t"],
                     ["sandbox"]):
            self.assertEqual(build_parser().parse_args(argv).rust_seconds, RUST_SECONDS)
        self.assertIsNone(match_weights(0.0))
        self.assertEqual(match_weights(2.5), Weights(rs_seconds=2.5))

    def test_fuel_ready_counts_patrols_for_tomorrow(self):
        ctx = small_ctx(seed=5, size=10)
        self.assertGreater(ctx.next_steps, 0)
        dec = decode(ctx, DaySolution(tours=rust_tours.solve(ctx, 0.2), meets=[], waits={}))
        want = sum(min(f, ctx.next_steps) for a, f in zip(ctx.agents, dec.result.end_fuel)
                   if a.kind == rules.PATROL)
        self.assertEqual(fuel_ready(ctx, dec), want)
        self.assertEqual(fuel_ready(dataclasses.replace(ctx, next_steps=0), dec), 0)

    def test_rust_start_keeps_fuel_for_tomorrow(self):
        # không xe tiếp, ngày mai cần cả bình: mọi bài có di chuyển đều bớt xăng sẵn cho ngày mai
        # so với bài đứng yên, nên rs_ready bác bài Rust dù điểm hôm nay cao hơn
        cfg = generate(seed=4, size=10, agents=3, days=3)
        agents = [AgentState(rules.PATROL, cfg.agent_starts[i], cfg.fuel_limits) for i in range(cfg.n_agents)]
        for ready in (1.0, 0.0):
            ctx = make_context(cfg, 0, agents, {}, [s.stocks for s in cfg.spots], set(),
                               weights=Weights(rs_seconds=0.3, rs_ready=ready, rs_safe_ld=0.0))
            ctx = dataclasses.replace(ctx, next_steps=cfg.fuel_limits)
            idle = DaySolution(tours=[[] for _ in ctx.patrols], meets=[], waits={})
            idle_dec = decode(ctx, idle)
            calls = []
            sol, dec = rust_start(ctx, idle, idle_dec, None, lambda s, d: calls.append(d))
            if ready:
                self.assertIs(dec, idle_dec)
                self.assertEqual(calls, [])
            else:
                self.assertGreater(dec.score, idle_dec.score)
                self.assertLess(fuel_ready(ctx, dec), fuel_ready(ctx, idle_dec))
                self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
