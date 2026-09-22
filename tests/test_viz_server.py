"""Web server trực quan hoá: trang tĩnh, trận offline/online bằng tiến trình con, chặn an toàn."""

import json
import os
import signal
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

from hexudon.cli import _install_exit_signals
from hexudon.client.web_games import CONTEST, PRACTICE, GameRef
from hexudon.offline.mapgen import generate
from hexudon.offline.web_server import WebWorld, serve_web
from hexudon.viz.events import read_events
from hexudon.viz.guard import GuardError
from hexudon.viz.server import VizApp, online_command, serve_viz


def small_config(days=2, day_seconds=2.0):
    config = generate(seed=41, size=8, agents=3, days=days)
    config.day_seconds = [day_seconds] * config.n_days
    config.__post_init__()
    return config


class VizServerTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.world = WebWorld([(77, "DAU 1"), (78, "DAU 2")])
        self.fake_httpd, _, self.fake_url = serve_web(self.world)
        self.addCleanup(self.fake_httpd.server_close)
        self.addCleanup(self.fake_httpd.shutdown)
        self.env = os.path.join(self.dir, "local.env")
        self.tokens = (self.world.issue_token(77), self.world.issue_token(78))
        with open(self.env, "w", encoding="utf-8") as fh:
            fh.write(f"TOKEN={self.tokens[0]}\nTEST_DAU_01={self.tokens[0]}\nTEST_DAU_02={self.tokens[1]}\n")
        # "real" cũng trỏ vào server giả: test không bao giờ gọi server thi thật
        self.app = VizApp(runs_dir=os.path.join(self.dir, "runs"),
                          targets={"fake": (self.fake_url, self.env), "real": (self.fake_url, self.env)})
        self.httpd, self.url = serve_viz(port=0, app=self.app)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.addCleanup(self.app.shutdown)

    def call(self, path, body=None, headers=None):
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self.url + path, data=data, method="GET" if body is None else "POST")
        if body is not None:
            req.add_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read() or b"{}")

    def raw_get(self, path):
        try:
            with urllib.request.urlopen(self.url + path, timeout=10) as resp:
                return resp.status, resp.headers.get("Content-Type", ""), resp.read()
        except urllib.error.HTTPError as err:
            return err.code, err.headers.get("Content-Type", ""), err.read()

    def wait_run(self, run_id, timeout=120):
        since, events = 0, []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            code, data = self.call(f"/api/runs/{run_id}/events?since={since}")
            self.assertEqual(code, 200, data)
            events += data["events"]
            since = data["next"]
            if not data["running"] and not data["more"]:
                return events
            time.sleep(0.3)
        self.fail(f"trận {run_id} không kết thúc trong {timeout}s")

    def test_page_and_modules_are_served_and_nothing_outside_static(self):
        for path, ctype in [("/", "text/html"), ("/static/style.css", "text/css"),
                            ("/static/js/app.js", "text/javascript"), ("/static/js/store.js", "text/javascript")]:
            code, got, _ = self.raw_get(path)
            self.assertEqual(code, 200, path)
            self.assertTrue(got.startswith(ctype), (path, got))
        code, _, _ = self.raw_get("/static/../server.py")
        self.assertEqual(code, 404)

    def test_options_list_maps_bots_and_test_tokens_only(self):
        code, data = self.call("/api/options")
        self.assertEqual(code, 200)
        self.assertIn("sample_16", data["maps"])
        self.assertEqual(set(data["bot_types"]), {"search", "greedy", "sample", "wait"})
        self.assertEqual(data["tokens"]["fake"], ["TEST_DAU_01", "TEST_DAU_02"])
        self.assertTrue(data["fake"]["reachable"])

    def test_offline_run_streams_events_and_can_be_reopened_later(self):
        code, data = self.call("/api/runs", {"mode": "offline", "map": "random8", "days": 1, "seed": 3,
                                             "bots": [{"type": "greedy"}, {"type": "wait"}]})
        self.assertEqual(code, 200, data)
        events = self.wait_run(data["id"])
        kinds = [e["kind"] for e in events]
        self.assertEqual(kinds[0], "match_start")
        self.assertIn("match_end", kinds)
        [end] = [e for e in events if e["kind"] == "run_end"]
        self.assertEqual(end["data"]["exit_code"], 0, end["data"]["stderr_tail"])
        _, runs = self.call("/api/runs")
        self.assertIn(data["id"], [r["id"] for r in runs["runs"]])
        fresh = VizApp(runs_dir=self.app.runs_dir, targets=self.app.targets)
        again = fresh.events(data["id"], 0)
        self.assertEqual(sorted(e["kind"] for e in again["events"]), sorted(kinds))
        self.assertFalse(again["running"])

    def test_bad_requests_are_refused_with_a_reason(self):
        for body in ({"mode": "offline", "map": "../../etc/passwd", "bots": [{"type": "greedy"}]},
                     {"mode": "offline", "map": "random8", "bots": []},
                     {"mode": "offline", "map": "random8", "bots": [{"type": "nope"}]},
                     {"mode": "nope"}):
            code, data = self.call("/api/runs", body)
            self.assertEqual(code, 400, (body, data))
            self.assertIn("error", data)
        code, _ = self.call("/api/runs/khong-co/events")
        self.assertEqual(code, 404)

    def test_post_needs_json_and_a_local_host_header(self):
        req = urllib.request.Request(self.url + "/api/runs", data=b"mode=offline", method="POST")
        req.add_header("Content-Type", "text/plain")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(ctx.exception.code, 415)
        code, _ = self.call("/api/options", headers={"Host": "evil.example:80"})
        self.assertEqual(code, 403)

    def test_real_server_refuses_contests_that_are_not_mock(self):
        qid = self.world.add_question("Q01", small_config(), 5, "Official Contest",
                                      selection_seconds=30.0, pre_window=600.0)
        code, data = self.call("/api/online/questions?target=real&token=TEST_DAU_01")
        self.assertEqual(code, 200, data)
        [row] = [r for r in data["rows"] if r["game_id"] == qid]
        self.assertFalse(row["allowed"])
        code, data = self.call("/api/runs", {"mode": "real", "game_id": qid, "tokens": ["TEST_DAU_01"]})
        self.assertEqual(code, 403, data)
        self.assertFalse(os.path.isdir(self.app.runs_dir) and os.listdir(self.app.runs_dir))

    def test_only_test_token_variables_can_play(self):
        qid = self.world.add_question("Q01", small_config(), 2, "Local Practice", practice=True)
        code, data = self.call("/api/runs", {"mode": "fake", "game_id": qid, "tokens": ["TOKEN"]})
        self.assertEqual(code, 403, data)

    def test_online_practice_plays_in_a_child_and_blocks_a_second_run_on_the_same_token(self):
        qid = self.world.add_question("Q01", small_config(), 2, "Local Practice", practice=True,
                                      selection_seconds=1.0)
        code, data = self.call("/api/runs", {"mode": "fake", "game_id": qid, "tokens": ["TEST_DAU_01"]})
        self.assertEqual(code, 200, data)
        code, dup = self.call("/api/runs", {"mode": "fake", "game_id": qid, "tokens": ["TEST_DAU_01"]})
        self.assertEqual(code, 409, dup)
        events = self.wait_run(data["id"], timeout=180)
        [done] = [e for e in events if e["kind"] == "run_end"]
        self.assertEqual(done["data"]["exit_code"], 0, done["data"]["stderr_tail"])
        [end] = [e for e in events if e["kind"] == "match_end"]
        self.assertIsNotNone(end["data"]["score"])
        run_dir = os.path.join(self.app.runs_dir, data["id"])
        for name in os.listdir(run_dir):
            path = os.path.join(run_dir, name)
            if os.path.isfile(path):
                with open(path, "rb") as fh:
                    content = fh.read()
                for token in self.tokens:
                    self.assertNotIn(token.encode(), content, f"token lộ trong {name}")
        code, again = self.call("/api/runs", {"mode": "fake", "game_id": qid, "tokens": ["TEST_DAU_01"]})
        self.assertEqual(code, 200, again)     # lượt trước xong thì được chạy lại
        self.call(f"/api/runs/{again['id']}/stop", {})

    def test_fake_server_button_refuses_a_busy_port(self):
        busy = socket.socket()
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        self.addCleanup(busy.close)
        app = VizApp(runs_dir=self.app.runs_dir,
                     targets={"fake": (f"http://127.0.0.1:{busy.getsockname()[1]}", self.env),
                              "real": self.app.targets["real"]})
        with self.assertRaises(GuardError) as ctx:
            app.start_fake(5)
        self.assertEqual(ctx.exception.status, 409)

    def test_online_command_never_goes_live(self):
        ref = GameRef("qid", "Q02", 14, "Mock Contest", CONTEST)
        cmd = online_command("python3", "https://x", "/tmp/.env", ref, "TEST_DAU_01", "/tmp/e.jsonl",
                             "/tmp/out", keep=False)
        self.assertNotIn("--live", cmd)
        self.assertEqual(cmd[cmd.index("--token-env") + 1], "TEST_DAU_01")
        self.assertNotIn("--practice", cmd)
        practice = online_command("python3", "https://x", "/tmp/.env",
                                  GameRef("q", "Q01", 11, "Practice Match", PRACTICE), "TEST_DAU_02",
                                  "/tmp/e.jsonl", "/tmp/out", keep=True)
        self.assertIn("--practice", practice)
        self.assertIn("--keep", practice)
        with self.assertRaises(GuardError):
            online_command("python3", "https://x", "/tmp/.env", ref, "TOKEN", "/tmp/e.jsonl", "/tmp/out",
                           keep=False)

    # ---- F1: shutdown() phải dọn cả tiến trình con còn sống, không chỉ gửi terminate rồi bỏ đó ----

    def test_shutdown_terminates_and_reaps_a_running_child(self):
        run_id, directory = self.app._new_run("offline")
        self.app._launch(run_id, directory, "offline", "title bất kỳ", {},
                         [("bot", [sys.executable, "-c", "import time; time.sleep(60)"])])
        [proc] = self.app.runs[run_id].procs
        self.assertIsNone(proc.popen.poll(), "tiến trình con phải còn sống trước khi shutdown")
        self.app.shutdown(grace=2.0)
        self.assertIsNotNone(proc.popen.poll(), "shutdown() phải dừng hẳn tiến trình con")

    # ---- F2: một trận phải tự cập nhật running/registry dù không ai gọi events() của nó ----

    def test_list_runs_reaps_ended_children_without_polling_events(self):
        run_id, directory = self.app._new_run("fake")
        self.app._launch(run_id, directory, "fake", "title", {},
                         [("TEST_DAU_01", [sys.executable, "-c", "pass"])])
        self.app.registry.claim("fake", "q-reap", ["TEST_DAU_01"], run_id)
        [proc] = self.app.runs[run_id].procs
        deadline = time.monotonic() + 10
        while proc.popen.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertIsNotNone(proc.popen.poll(), "tiến trình con không thoát trong 10s")
        rows = self.app.list_runs()      # không gọi events(run_id) trước
        [row] = [r for r in rows if r["id"] == run_id]
        self.assertFalse(row["running"])
        events, _ = read_events(os.path.join(directory, "events-server.jsonl"))
        self.assertIn("run_end", [e["kind"] for e in events])
        self.app.registry.claim("fake", "q-reap", ["TEST_DAU_01"], "other")   # không còn raise nữa

    # ---- F3: bấm Dừng không được hiện như crash ----

    def test_stop_marks_run_end_as_stopped_not_crashed(self):
        code, data = self.call("/api/runs", {"mode": "offline", "map": "sample_32", "seed": 5,
                                             "bots": [{"type": "search", "seconds": 3}, {"type": "greedy"}]})
        self.assertEqual(code, 200, data)
        run_id = data["id"]
        time.sleep(2)
        msg = self.app.get_run(run_id).stop()
        self.assertIn("Ctrl-C", msg)
        events = self.wait_run(run_id, timeout=60)
        [end] = [e for e in events if e["kind"] == "run_end"]
        self.assertTrue(end["data"]["stopped"], end["data"])
        self.assertEqual(end["data"]["exit_code"], 130, end["data"]["stderr_tail"])

    # ---- F4: regex phải fullmatch, không cho lọt chuỗi có \n thừa ----

    def test_offline_map_regex_is_anchored_with_fullmatch(self):
        code, data = self.call("/api/runs", {"mode": "offline", "map": "random8\n", "bots": [{"type": "greedy"}]})
        self.assertEqual(code, 400, data)


class ExitSignalsTest(unittest.TestCase):
    """F1: cmd_viz phải dừng theo SIGTERM/SIGHUP (đóng terminal), không chỉ Ctrl-C."""

    def test_install_exit_signals_makes_sigterm_and_sighup_raise_keyboard_interrupt(self):
        previous = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGHUP)}
        for sig, handler in previous.items():
            self.addCleanup(signal.signal, sig, handler)
        _install_exit_signals()
        for sig in (signal.SIGTERM, signal.SIGHUP):
            handler = signal.getsignal(sig)
            with self.assertRaises(KeyboardInterrupt):
                handler(sig, None)
