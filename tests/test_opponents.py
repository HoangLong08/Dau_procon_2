"""Bot đối thủ giả lập (hexudon/offline/opponents.py): luôn hợp lệ, tất định, đúng kiểu chơi."""

import json
import os
import unittest

from hexudon import rules
from hexudon.model import MatchConfig
from hexudon.offline.bots import BOT_TYPES
from hexudon.offline.mapgen import generate, load_or_generate
from hexudon.offline.opponents import (
    OPPONENT_TYPES, AllPatrolBot, EscortBot, HubBot, RoadBlockBot, ServingsAlnsBot,
)
from hexudon.offline.runner import run_match

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTED = ["nearest", "chain_greedy", "escort", "hub", "road_block"]


def jp_example() -> MatchConfig:
    path = os.path.join(HERE, "..", "data", "sample_maps", "jp_server_example_config.json")
    with open(path, encoding="utf-8") as fh:
        return MatchConfig.from_problem_json(json.load(fh))


def maps():
    yield "jp_example", jp_example()
    yield "sample_16", load_or_generate("sample_16")
    yield "sample_24", load_or_generate("sample_24")
    for seed in (3, 11):
        yield f"random{seed}", generate(seed=seed)


class RecordingObserver:
    """Giữ vị trí cuối ngày và trạng thái đường đầu ngày của mọi đội."""

    def __init__(self):
        self.status = []
        self.end = []

    def match_ready(self, records):
        pass

    def day_start(self, day, steps, status, records):
        self.status.append(dict(status))
        self.end.append({})

    def team_day(self, idx, rec, state, plan, result, elapsed, error, trace):
        self.end[-1][idx] = (list(result.end_pos), list(result.end_fuel))


class TestOpponents(unittest.TestCase):
    def test_registered(self):
        for name in OPPONENT_TYPES:
            self.assertIn(name, BOT_TYPES)

    def test_scripted_bots_always_valid_in_multi_team_match(self):
        for label, cfg in maps():
            with self.subTest(map=label):
                bots = [OPPONENT_TYPES[n](cfg, seed=i) for i, n in enumerate(SCRIPTED)]
                cfg.players = len(bots)
                out = run_match(cfg, bots)
                for bot, rec in zip(bots, out.teams):
                    self.assertEqual(rec.invalid_days, 0, f"{label} {rec.name}")
                    self.assertEqual(bot.fallbacks, 0, f"{label} {rec.name}")
                # đội biết chuỗi phải thu được ít nhất một loại
                by_name = {r.name: r for r in out.teams}
                self.assertGreater(by_name["chain_greedy"].score()[0], 0)

    def test_deterministic(self):
        cfg = load_or_generate("sample_16")
        scores = []
        for _ in range(2):
            bots = [OPPONENT_TYPES[n](cfg, seed=5) for n in SCRIPTED]
            cfg.players = len(bots)
            scores.append([r.score() for r in run_match(cfg, bots).teams])
        self.assertEqual(scores[0], scores[1])

    def test_roles(self):
        cfg = load_or_generate("sample_32")  # 7 xe
        self.assertEqual(OPPONENT_TYPES["nearest"](cfg).select_roles().count(rules.REFUEL), 0)
        self.assertEqual(OPPONENT_TYPES["chain_greedy"](cfg).select_roles().count(rules.REFUEL), 1)
        self.assertEqual(EscortBot(cfg).select_roles().count(rules.REFUEL), 3)
        self.assertEqual(HubBot(cfg).select_roles().count(rules.REFUEL), 1)
        self.assertEqual(RoadBlockBot(cfg).select_roles().count(rules.REFUEL), 3)

    def test_escort_keeps_paired_patrols_full(self):
        cfg = load_or_generate("sample_16")
        cfg.players = 1
        bot = EscortBot(cfg, seed=0)
        obs = RecordingObserver()
        run_match(cfg, [bot], observer=obs)
        kinds = bot.kinds
        paired = set(bot.pair_of_patrol)
        self.assertTrue(paired)
        for day in range(1, cfg.n_days):
            pos, fuel = obs.end[day][0]
            for p in paired:
                car = bot.pair_of_patrol[p]
                self.assertEqual(kinds[car], rules.REFUEL)
                self.assertEqual(pos[p], pos[car], f"ngày {day}: đoàn xe tách nhau")
                self.assertEqual(fuel[p], cfg.fuel_limits, f"ngày {day}: xe tuần tra có hộ tống phải đầy")

    def test_hub_car_parks_on_central_plain(self):
        cfg = load_or_generate("sample_16")
        cfg.players = 1
        bot = HubBot(cfg, seed=0)
        obs = RecordingObserver()
        run_match(cfg, [bot], observer=obs)
        car = bot.kinds.index(rules.REFUEL)
        self.assertEqual(cfg.cells[bot.hub], rules.PLAIN)
        self.assertEqual(obs.end[-1][0][0][car], bot.hub)

    def test_road_block_jams_its_cell(self):
        cfg = load_or_generate("sample_16")
        cfg.players = 2
        blocker = RoadBlockBot(cfg, seed=0)
        bots = [blocker, OPPONENT_TYPES["nearest"](cfg, seed=1)]
        obs = RecordingObserver()
        run_match(cfg, bots, observer=obs)
        cells = set(blocker.parking.values())
        self.assertTrue(cells)
        for c in cells:
            self.assertEqual(cfg.cells[c], rules.ROAD)
        # xe đỗ cả ngày để lại hàng chục lượt dừng: ngày cuối mọi ô bị chặn đều tắc
        last = obs.status[-1]
        self.assertTrue(all(last.get(c) == rules.JAMMED for c in cells), last)

    def test_alns_variants_valid(self):
        cfg = jp_example()
        cfg.players = 2
        bots = [ServingsAlnsBot(cfg, seed=1, search_iterations=20, role_iterations=5),
                AllPatrolBot(cfg, seed=2, search_iterations=20, role_iterations=5)]
        out = run_match(cfg, bots)
        for rec in out.teams:
            self.assertEqual(rec.invalid_days, 0, rec.name)
        self.assertEqual(out.teams[1].kinds, [rules.PATROL] * cfg.n_agents)
        self.assertEqual([r.name for r in out.teams], ["servings_alns", "all_patrol"])


if __name__ == "__main__":
    unittest.main()
