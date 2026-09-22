"""Trận offline n bot cho web: đủ sự kiện, xếp hạng trùng runner, lệnh arena ghi file."""

import contextlib
import io
import json
import os
import tempfile
import unittest

from hexudon.cli import main
from hexudon.offline.bots import GreedyBot, SampleBot
from hexudon.offline.mapgen import generate
from hexudon.offline.runner import run_match
from hexudon.viz.arena import BotSpec, run_arena
from hexudon.viz.events import read_events


def collect():
    events = []

    def emit_for(bot):
        def emit(kind, data):
            json.dumps(data)
            events.append({"bot": bot, "kind": kind, "data": data})
        return emit
    return events, emit_for


class BotSpecTest(unittest.TestCase):
    def test_parse_and_label(self):
        self.assertEqual(BotSpec.parse("search:3"), BotSpec("search", 3.0))
        self.assertEqual(BotSpec.parse(" greedy "), BotSpec("greedy", 1.0))
        self.assertEqual(BotSpec.parse("search:0.5").label(0), "1. search 0.5s")
        self.assertEqual(BotSpec.parse("sample").label(2), "3. sample")
        for bad in ("foo", "search:0", "search:x"):
            with self.assertRaises(ValueError):
                BotSpec.parse(bad)

    def test_search_seconds_are_bounded_and_finite(self):
        self.assertEqual(BotSpec.parse("search:60"), BotSpec("search", 60.0))
        for bad in ("search:nan", "search:inf", "search:61"):
            with self.assertRaises(ValueError, msg=bad):
                BotSpec.parse(bad)


class ObserverTest(unittest.TestCase):
    def test_an_observer_does_not_change_the_match(self):
        class Quiet:
            def match_ready(self, records):
                pass

            def day_start(self, day, steps, status, records):
                pass

            def team_day(self, idx, rec, state, plan, result, elapsed, error, trace):
                pass

        def play(observer):
            config = generate(seed=31, size=10, agents=3, days=3)
            config.players = 2
            return run_match(config, [GreedyBot(config, seed=1), SampleBot(config, seed=2)],
                             observer=observer)

        plain, watched = play(None), play(Quiet())
        self.assertEqual([t.score() for t in plain.teams], [t.score() for t in watched.teams])


class ArenaTest(unittest.TestCase):
    def test_three_bots_produce_a_complete_replayable_event_stream(self):
        config = generate(seed=41, size=8, agents=3, days=2)
        events, emit_for = collect()
        specs = [BotSpec("search", 0.2), BotSpec("greedy"), BotSpec("sample")]
        outcome = run_arena(config, specs, emit_for, seed=0, role_seconds=0.3)
        names = ["1. search 0.2s", "2. greedy", "3. sample"]

        def by(kind):
            return [e for e in events if e["kind"] == kind]

        self.assertEqual(events[0]["kind"], "match_start")
        self.assertEqual([b["name"] for b in events[0]["data"]["bots"]], names)
        self.assertEqual(events[0]["data"]["config"]["n_days"], 2)
        self.assertTrue(by("roles_candidate"))
        self.assertTrue(all(e["bot"] == names[0] for e in by("roles_candidate")))
        self.assertEqual({e["bot"] for e in by("roles_chosen")}, set(names))
        self.assertEqual([e["data"]["day"] for e in by("day_start")], [0, 1])
        self.assertEqual(set(by("day_start")[0]["data"]["teams"]), set(names))
        results = by("day_result")
        self.assertEqual(len(results), 2 * 3)
        for e in results:
            steps = config.steps_of_day(e["data"]["day"])
            self.assertEqual(len(e["data"]["trace"]["pos"]), steps + 1)
            if e["bot"] in names[:2]:
                self.assertTrue(e["data"]["ok"], e["data"]["error"])
        self.assertEqual({e["bot"] for e in by("alns_progress")}, {names[0]})
        self.assertEqual({e["bot"] for e in by("submit")}, {names[0]})
        [end] = by("match_end")
        ranking = [(r["name"], r["L"], r["D"], r["Q"]) for r in end["data"]["ranking"]]
        self.assertEqual(ranking, [(r.name, *r.score()) for r in outcome.ranking()])


class ArenaCliTest(unittest.TestCase):
    def test_arena_command_writes_an_event_file(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "events.jsonl")
        with contextlib.redirect_stderr(io.StringIO()):
            code = main(["arena", "--map", "random8", "--days", "1", "--bot", "greedy", "--bot", "wait",
                         "--events", path])
        self.assertEqual(code, 0)
        events, _ = read_events(path)
        self.assertEqual(events[0]["kind"], "match_start")
        self.assertEqual(events[-1]["kind"], "match_end")
        self.assertEqual(events[0]["data"]["config"]["n_days"], 1)

    def test_arena_command_rejects_an_unknown_bot(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["arena", "--bot", "nope", "--events", os.devnull]), 2)
