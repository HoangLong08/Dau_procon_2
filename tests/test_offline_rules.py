"""Môi trường offline phải chấm đúng luật của đề: xếp hạng, thông tin đầu ngày, tham số map."""

import io
import json
import unittest
from collections import deque

from hexudon import rules
from hexudon.client.sandbox_client import SandboxTransport
from hexudon.offline.bots import Bot, GreedyBot, SearchBot
from hexudon.offline.mapgen import generate, load_or_generate
from hexudon.offline.runner import MatchOutcome, TeamRecord, run_match
from hexudon.offline.web_server import ContestGame
from hexudon.simulator import wait_plan


class SpyBot(Bot):
    """Đứng yên, chỉ ghi lại vị trí các đội khác mà nó được cho biết mỗi sáng."""

    name = "spy"

    def __init__(self, config):
        self.config = config
        self.seen = []

    def select_roles(self):
        return [rules.PATROL] * self.config.n_agents

    def plan(self, state):
        self.seen.append([[a.pos for a in team] for team in state.others])
        return wait_plan(self.config.n_agents, self.config.steps_of_day(state.day))


class RunnerTest(unittest.TestCase):
    def test_ranking_breaks_equal_scores_by_total_response_time(self):
        slow, fast = TeamRecord(name="chậm", kinds=[]), TeamRecord(name="nhanh", kinds=[])
        for team in (slow, fast):
            team.brands, team.daily_types, team.servings = {1, 2}, 4, 6
        slow.response_seconds, fast.response_seconds = 9.0, 2.5
        outcome = MatchOutcome(teams=[slow, fast], days=[])
        self.assertEqual([t.name for t in outcome.ranking()], ["nhanh", "chậm"],
                         "bằng L, D, Q thì đội có tổng thời gian phản hồi thấp hơn thắng (tiêu chí 4)")

    def test_every_team_sees_the_same_start_of_day_positions(self):
        config = generate(seed=5, size=10, agents=3, days=2)
        mover, spy = GreedyBot(config, seed=1), SpyBot(config)
        run_match(config, [mover, spy])
        self.assertEqual(spy.seen[0], [config.agent_starts],
                         "ngày 1 phải thấy đội kia ở vị trí xuất phát, không phải vị trí cuối ngày")

    def test_search_bot_reports_when_its_last_answer_was_sent(self):
        config = generate(seed=6, size=10, agents=3, days=2)
        config.players = 1
        bot = SearchBot(config, search_seconds=0.4, role_seconds=0.5, seed=1)
        outcome = run_match(config, [bot])
        [team] = outcome.teams
        self.assertIsNotNone(bot.last_response_seconds)
        self.assertLessEqual(team.response_seconds, sum(team.plan_seconds) + 1e-6)


def connected(config) -> bool:
    passable = [c for c in range(config.n_cells) if config.cells[c] != rules.POND]
    seen = {passable[0]}
    queue = deque([passable[0]])
    while queue:
        cell = queue.popleft()
        for nb in config.neighbors[cell]:
            if nb >= 0 and nb not in seen and config.cells[nb] != rules.POND:
                seen.add(nb)
                queue.append(nb)
    return len(seen) == len(passable)


class MapgenTest(unittest.TestCase):
    """Map sinh ngẫu nhiên phủ đủ phạm vi tham số của đề (cases §4)."""

    def test_random_maps_stay_within_the_official_limits_and_cover_their_range(self):
        rectangles = 0
        unsorted_days = 0
        for seed in range(20):
            config = generate(seed=seed)
            self.assertTrue(8 <= config.width <= 32 and 8 <= config.height <= 32)
            self.assertTrue(4 <= config.n_days <= 10)
            self.assertTrue(3 <= config.n_agents <= 8)
            self.assertTrue(1 <= len(config.brands) <= len(config.spots))
            self.assertLessEqual(config.busy_threshold, config.jammed_threshold)
            spot_cells = {s.pos for s in config.spots}
            for spot in config.spots:
                self.assertEqual(config.cells[spot.pos], rules.PLAIN)
                self.assertTrue(1 <= spot.stocks <= config.n_agents)
            for start in config.agent_starts:
                self.assertEqual(config.cells[start], rules.PLAIN)
                self.assertNotIn(start, spot_cells)
            self.assertTrue(connected(config), f"seed {seed}: có ô đi được mà không tới được")
            rectangles += config.width != config.height
            unsorted_days += config.day_steps != sorted(config.day_steps)
        self.assertGreater(rectangles, 0, "phải có map rộng khác cao (cases 6.15)")
        self.assertGreater(unsorted_days, 0, "số bước từng ngày không nhất thiết tăng dần (cases 5.7)")

    def test_map_names_select_square_long_or_fully_random_boards(self):
        self.assertEqual((load_or_generate("random12").width, load_or_generate("random12").height), (12, 12))
        long_map = load_or_generate("random8x32", seed=3)
        self.assertEqual((long_map.width, long_map.height), (8, 32))
        self.assertTrue(connected(long_map))
        free = load_or_generate("random", seed=2)
        self.assertTrue(8 <= free.width <= 32 and 8 <= free.height <= 32)


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


class ContestResponseTimeTest(unittest.TestCase):
    def test_last_valid_answer_sets_the_response_time_and_breaks_ties(self):
        clock = Clock()
        config = generate(seed=21, size=8, agents=3, days=1)
        config.day_seconds = [10]
        config.__post_init__()
        game = ContestGame(config, [(77, "DAU 1"), (78, "DAU 2")], selection_seconds=5, clock=clock)
        steps = config.steps_of_day(0)
        clock.now += 5                       # cửa sổ chọn xe đóng, ngày 0 mở
        game.sync()
        clock.now += 1
        self.assertEqual(game.submit(77, 0, wait_plan(3, steps))[0], 200)
        clock.now += 2
        self.assertEqual(game.submit(78, 0, wait_plan(3, steps))[0], 200)
        clock.now += 1                       # đội 77 gửi lại: bài cuối cùng mới tính
        self.assertEqual(game.submit(77, 0, wait_plan(3, steps))[0], 200)
        clock.now += 10
        game.sync()
        result = game.board.result_payload()
        self.assertEqual(result["detail"]["77"]["cumulative_response_time"], 4.0)
        self.assertEqual(result["detail"]["78"]["cumulative_response_time"], 3.0)
        self.assertEqual(result["ranking"], ["78", "77"], "cùng L, D, Q thì nhanh hơn thắng")


class SandboxDayFrameTest(unittest.TestCase):
    def test_hidden_fuel_reads_as_a_full_tank_and_pairs_read_as_cells(self):
        config = generate(seed=4, size=10, agents=3, days=1)
        day = {"day": 0, "others": [], "traffics": [],
               "agents": [{"kind": 0, "pos": [p % config.width, p // config.width]}
                          for p in config.agent_starts]}
        stdin = io.StringIO(json.dumps(config.to_json()) + "\n" + json.dumps(day) + "\n")
        transport = SandboxTransport(stdin=stdin, stdout=io.StringIO())
        transport.get_setup()
        state = transport.wait_day_state([rules.PATROL] * 3)
        self.assertEqual([a.pos for a in state.agents], config.agent_starts)
        self.assertEqual([a.fuel for a in state.agents], [config.fuel_limits] * 3,
                         "đọc thành 0 sẽ cắt mọi xe về lệnh đợi")


if __name__ == "__main__":
    unittest.main()
