"""R7 (17/11/2026): nhiều seed ALNS mỗi ngày (tiến trình con theo giây, tuần tự theo vòng)."""

import argparse
import dataclasses
import time
import unittest
from unittest import mock

from hexudon.cli import build_parser
from hexudon.model import AgentState, DayState
from hexudon.offline.mapgen import load_or_generate
from hexudon.planner import strategy
from hexudon.planner.engine import make_context
from hexudon.planner.parallel import total_of
from hexudon.planner.roles import candidates
from hexudon.planner.strategy import MatchStrategist, resolve_search_jobs


def day0(cfg, kinds):
    agents = [AgentState(kinds[i], cfg.agent_starts[i], cfg.fuel_limits) for i in range(cfg.n_agents)]
    return DayState(day=0, agents=agents, road_status={})


def strategist(cfg, kinds, **kw):
    s = MatchStrategist(cfg, net_margin=0.0, seed=1, **kw)
    s.kinds = list(kinds)
    return s


class IterationModeTest(unittest.TestCase):
    def setUp(self):
        self.cfg = load_or_generate("sample_16")
        self.kinds = candidates(self.cfg)[1].kinds

    def _plan(self, jobs, log=None):
        s = strategist(self.cfg, self.kinds, search_iterations=25, search_jobs=jobs, log=log)
        return s, s.plan_day(day0(self.cfg, self.kinds), record=False)

    def test_deterministic_and_never_worse_than_one_seed_by_total(self):
        _, one = self._plan(1)
        s3, three = self._plan(3)
        _, again = self._plan(3)
        self.assertEqual(three.plan, again.plan, "theo vòng phải tất định")
        state = day0(self.cfg, self.kinds)
        ctx = make_context(self.cfg, 0, state.agents, {}, [sp.stocks for sp in self.cfg.spots], set(),
                           weights=s3.weights, forecast_status=s3.forecast.predict({}, None))
        look = s3._lookahead(state, {}, ctx.steps)
        self.assertGreaterEqual((total_of(ctx, three, look), three.score), (total_of(ctx, one, look), one.score))
        if three.plan != one.plan:
            self.assertEqual(three.picked_by, "song_song")

    def test_one_job_is_exactly_the_old_behaviour(self):
        s = strategist(self.cfg, self.kinds, search_iterations=25)
        self.assertEqual(s.search_jobs, 1)
        with mock.patch.object(strategy, "run_job") as spy:
            s.plan_day(day0(self.cfg, self.kinds), record=False)
        spy.assert_not_called()

    def test_a_better_side_seed_is_sent_on_the_final_slot(self):
        s = strategist(self.cfg, self.kinds, search_iterations=10, search_jobs=2)
        sent = []
        real_run_job = strategy.run_job

        def fake_run_job(config, job, reserve=0.0):
            dec, total = real_run_job(config, job, reserve)
            # một bài khác hẳn bài của seed chính (ít vòng thì hai seed hay ra cùng một bài,
            # và bài trùng bài đã gửi thì không gửi lại)
            other = [[-1] + list(dec.plan[0])] + [list(a) for a in dec.plan[1:]]
            return dataclasses.replace(dec, plan=other), (total[0], total[1] + 100, total[2])

        with mock.patch.object(strategy, "run_job", fake_run_job):
            final = s.plan_day(day0(self.cfg, self.kinds), submit=lambda plan: sent.append(plan) or True,
                               record=False)
        self.assertEqual(final.picked_by, "song_song")
        self.assertEqual(sent[-1], final.plan)
        self.assertLessEqual(len(sent), strategy.MAX_SUBMITS_PER_DAY)


class TimeModeTest(unittest.TestCase):
    def setUp(self):
        self.cfg = load_or_generate("sample_16")
        self.kinds = candidates(self.cfg)[1].kinds

    def test_side_seeds_run_on_processes_and_the_day_keeps_its_deadline(self):
        logs = []
        s = strategist(self.cfg, self.kinds, search_seconds=None, search_jobs=2, log=logs.append)
        try:
            self.assertTrue(s.wait_pool(60), logs)      # lúc thi: mở từ lúc chọn vai trò
            budget = 3.0
            t0 = time.monotonic()
            final = s.plan_day(day0(self.cfg, self.kinds), deadline=t0 + budget, record=False)
            elapsed = time.monotonic() - t0
            self.assertTrue(final.result.ok)
            self.assertLess(elapsed, budget + 0.6, f"vượt hạn {elapsed - budget:.2f}s")
            self.assertTrue(any("song song" in m for m in logs), logs)
            self.assertIsNotNone(s._pool)
        finally:
            s.close()
        self.assertIsNone(s._pool)
        s.close()                                   # gọi lại vẫn an toàn

    def test_falls_back_to_one_seed_when_processes_cannot_start(self):
        logs = []
        s = strategist(self.cfg, self.kinds, search_seconds=None, search_jobs=3, log=logs.append)
        with mock.patch.object(strategy, "SeedPool", side_effect=OSError("không có tiến trình")):
            self.assertFalse(s.wait_pool(5))
            t0 = time.monotonic()
            final = s.plan_day(day0(self.cfg, self.kinds), deadline=t0 + 0.8, record=False)
        self.assertTrue(final.result.ok)
        self.assertLess(time.monotonic() - t0, 1.4)
        self.assertTrue(any("không mở được" in m for m in logs), logs)
        self.assertNotEqual(final.picked_by, "song_song")


    def test_a_pool_that_never_starts_cannot_stall_the_day(self):
        # 17/11: tiến trình con chết lúc khởi động làm hàm dựng Pool bị chặn mãi trên Windows
        s = strategist(self.cfg, self.kinds, search_seconds=None, search_jobs=3)

        def stuck(workers):
            time.sleep(8)
            raise OSError("không bao giờ mở được")

        with mock.patch.object(strategy, "SeedPool", side_effect=stuck):
            t0 = time.monotonic()
            self.assertIsNone(s._ensure_pool())
            self.assertLess(time.monotonic() - t0, 0.5, "mở Pool không được chặn luồng chính")
            final = s.plan_day(day0(self.cfg, self.kinds), deadline=time.monotonic() + 0.8, record=False)
            self.assertTrue(final.result.ok)
            self.assertLess(time.monotonic() - t0, 1.5)
            s.close()
        self.assertLess(time.monotonic() - t0, 2.0, "close không chờ luồng mở Pool")


    def test_last_day_at_the_upper_bound_is_sent_without_waiting_for_side_seeds(self):
        # R3 + R7: ngày cuối đạt cận trên thì trả ngay; chờ seed phụ chỉ làm chậm tiêu chí 4
        from hexudon import rules
        from hexudon.model import MatchConfig, Spot
        from hexudon.planner.engine import safe_solution
        from hexudon.planner.parallel import DayJob
        w = 8
        cfg = MatchConfig(width=w, height=w, cells=[rules.PLAIN] * (w * w), spots=[Spot(0, 9, 1)],
                          agent_starts=[10, 40], fuel_limits=20, day_steps=[12], day_seconds=[5],
                          players=1, busy_threshold=1, jammed_threshold=2)
        kinds = [rules.PATROL, rules.REFUEL]
        s = strategist(cfg, kinds, search_seconds=None, search_jobs=2)
        state = day0(cfg, kinds)
        ctx = make_context(cfg, 0, state.agents, {}, [1], set(), weights=s.weights)
        _, best = safe_solution(ctx, 0)

        class SlowSeed:
            """Seed phụ chỉ trả lời khi hết hạn chờ."""
            def get(self, timeout):
                time.sleep(timeout)
                raise TimeoutError

        job = DayJob(day=0, kinds=kinds, agents=state.agents, status={}, visited=set(), weights=s.weights,
                     forecast_status=None, steps_override=None, seed=1)
        t0 = time.monotonic()
        pick = s._pick_seed(ctx, best, None, job, [7], [SlowSeed()], t0 + 2.0, 10)
        self.assertIs(pick, best)
        self.assertLess(time.monotonic() - t0, 0.3, "đã đạt cận trên mà vẫn chờ seed phụ")
        # chưa đạt cận trên thì vẫn chờ seed phụ như thường
        with mock.patch.object(strategy, "day_upper_bound", return_value=(9, 9, 9)):
            t0 = time.monotonic()
            pick = s._pick_seed(ctx, best, None, job, [7], [SlowSeed()], t0 + 0.6, 10)
            self.assertGreaterEqual(time.monotonic() - t0, 0.5)
        self.assertIs(pick, best)


class FlagTest(unittest.TestCase):
    def test_search_jobs_flag(self):
        parser = build_parser()
        self.assertIsNone(parser.parse_args(["sim", "--search-jobs", "auto"]).search_jobs)
        self.assertEqual(parser.parse_args(["sim", "--search-jobs", "3"]).search_jobs, 3)
        self.assertIsNone(parser.parse_args(["sim"]).search_jobs)
        with self.assertRaises(SystemExit), mock.patch("sys.stderr"):
            parser.parse_args(["sim", "--search-jobs", "0"])

    def test_auto_is_bounded(self):
        self.assertGreaterEqual(resolve_search_jobs(None), 1)
        self.assertLessEqual(resolve_search_jobs(None), strategy.AUTO_SEARCH_JOBS_MAX)
        self.assertEqual(resolve_search_jobs(5), 5)

    def test_child_processes_get_the_flag(self):
        from hexudon.client.web_runner import child_command
        from hexudon.client.web_games import GameRef
        args = argparse.Namespace(url="http://x", env=".env", timeout=5.0, net_margin=2.0, poll_seconds=0.5,
                                  give_up_seconds=60.0, seed=0, search_seconds=None, role_seconds=None,
                                  search_jobs=3, live=False, keep=False)
        ref = mock.Mock(spec=GameRef, game_id="g1", mode="contest")
        cmd = child_command(args, ref, "TOKEN", "s.json", "nhãn")
        self.assertEqual(cmd[cmd.index("--search-jobs") + 1], "3")


if __name__ == "__main__":
    unittest.main()
