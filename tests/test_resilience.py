"""Bot phải sống sót qua mọi sự cố trong ngày thi.

Mỗi test ở đây tái hiện một cách thua trận đã quan sát được, chứ không phải
một tình huống tưởng tượng. Không bài nào được phép làm chết vòng lặp trận.
"""

import json
import random
import socket
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from hexudon import rules
from hexudon.model import Ack, AgentState
from hexudon.planner.construct import construct
from hexudon.planner.decoder import decode
from hexudon.planner.engine import make_context
from hexudon.client.http_client import HttpTransport
from hexudon.client.session import MatchSession
from hexudon.client.transport import MatchEnded
from hexudon.offline.local_server import LocalMatch
from hexudon.offline.mapgen import generate
from hexudon.simulator import wait_plan
from hexudon.planner.roles import rollout
from hexudon.planner.strategy import MatchStrategist
from hexudon.traffic import TrafficForecast


class FakeServer:
    """Server giả điều khiển được: mỗi tuyến có thể bị bẻ theo ý test."""

    def __init__(self, config, setup_hook=None, state_hook=None,
                 actions_hook=None, result_hook=None):
        self.config = config
        self.setup_hook = setup_hook
        self.state_hook = state_hook
        self.actions_hook = actions_hook
        self.result_hook = result_hook
        self.day = 0
        self.submissions = []          # (day, plan) — mọi lần gửi, kể cả gửi lại
        self.plan_of_day = {}          # ngày -> bài hợp lệ cuối cùng
        self.setup_hits = 0
        self.state_hits = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, code, payload):
                body = b"" if payload is None else json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                route = self.path.rstrip("/").rsplit("/", 1)[-1]
                if route == "setup":
                    outer.setup_hits += 1
                    payload = outer.config.to_json()
                    if outer.setup_hook:
                        code, payload = outer.setup_hook(outer.setup_hits, payload)
                        return self._send(code, payload)
                    return self._send(200, payload)
                if route == "state":
                    outer.state_hits += 1
                    # ngày chỉ đóng khi client hỏi trạng thái mới, giống luật
                    # "bài hợp lệ cuối cùng trước hạn được áp dụng"
                    if outer.day in outer.plan_of_day:
                        outer.day += 1
                    if outer.day >= outer.config.n_days:
                        return self._send(409, {"error": "finished"})
                    payload = {
                        "day": outer.day,
                        "endsAt": time.time() + 30.0,
                        "agents": [{"kind": rules.PATROL, "pos": p,
                                    "fuel": outer.config.fuel_limits}
                                   for p in outer.config.agent_starts],
                        "others": [],
                        "traffics": [],
                    }
                    if outer.state_hook:
                        code, payload = outer.state_hook(outer.state_hits, payload)
                        return self._send(code, payload)
                    return self._send(200, payload)
                if route == "result":
                    if outer.result_hook:
                        code, payload = outer.result_hook(outer.day)
                        return self._send(code, payload)
                    finished = outer.day >= outer.config.n_days
                    return self._send(200, {"finished": finished, "standings": []})
                return self._send(404, {})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length) or b"null")
                route = self.path.rstrip("/").rsplit("/", 1)[-1]
                if route == "assignment":
                    return self._send(200, {"ok": True})
                if route == "actions":
                    plan = payload["actions"] if isinstance(payload, dict) else payload
                    code, body = (200, {"ok": True})
                    if outer.actions_hook:
                        code, body = outer.actions_hook(outer.day, plan)
                    if code == 200:
                        outer.submissions.append((outer.day, plan))
                        outer.plan_of_day[outer.day] = plan
                    return self._send(code, body)
                return self._send(404, {})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def small_config():
    config = generate(seed=15, size=10, agents=3, days=4)
    config.day_steps = [20, 24, 28, 32]
    config.day_seconds = [2, 2, 2, 2]
    config.__post_init__()
    return config


def new_session(server, **kwargs):
    kwargs.setdefault("search_seconds", 0.2)
    kwargs.setdefault("net_margin", 0.0)
    kwargs.setdefault("role_seconds", 0.5)
    kwargs.setdefault("seed", 3)
    transport = HttpTransport(server.url, "m", "tok", poll_seconds=0.02)
    return MatchSession(transport, **kwargs)


class PlannerErrorTest(unittest.TestCase):
    """Một lỗi bất ngờ của planner chỉ được làm hỏng đúng một ngày."""

    def test_match_continues_after_a_planner_error(self):
        config = small_config()
        server = FakeServer(config)
        original = MatchStrategist.plan_day
        broken_day = 1

        def flaky(self, state, **kwargs):
            if state.day == broken_day:
                raise RuntimeError("planner lỗi giả lập")
            return original(self, state, **kwargs)

        MatchStrategist.plan_day = flaky
        try:
            new_session(server).run()
        finally:
            MatchStrategist.plan_day = original
            server.close()

        self.assertEqual(sorted(server.plan_of_day), list(range(config.n_days)),
                         "lỗi planner một ngày không được làm mất các ngày sau")


class MalformedPayloadTest(unittest.TestCase):
    """Trường lạ trong /setup hay /state không được giết bot."""

    def test_setup_missing_a_field_is_retried_not_fatal(self):
        config = small_config()

        def hook(hits, payload):
            if hits == 1:
                payload = dict(payload)
                del payload["fuelLimits"]      # server lỗi ở lần đầu
            return 200, payload

        server = FakeServer(config, setup_hook=hook)
        try:
            new_session(server).run()
        finally:
            server.close()

        self.assertGreaterEqual(server.setup_hits, 2, "phải hỏi lại setup chứ không chết")
        self.assertEqual(sorted(server.plan_of_day), list(range(config.n_days)))

    def test_state_with_renamed_position_field_does_not_kill_the_bot(self):
        config = small_config()

        def hook(hits, payload):
            for agent in payload["agents"]:
                agent["position"] = agent.pop("pos")   # server đổi tên trường
            return 200, payload

        server = FakeServer(config, state_hook=hook)
        try:
            new_session(server).run()
        finally:
            server.close()

        self.assertEqual(sorted(server.plan_of_day), list(range(config.n_days)),
                         "tên trường khác vẫn phải chơi được trọn trận")


class DribbleServer:
    """Server nhả body từng byte một: hợp lệ về giao thức, nhưng rất chậm.

    Đây là hình dạng của server BTC lúc chuyển ngày khi hàng chục đội cùng gọi.
    `timeout` của socket là theo mỗi lần đọc, không phải theo cả cuộc gọi, nên
    server kiểu này kéo dài một request vô hạn mà không hề gây timeout.
    """

    def __init__(self, body=b'{"finished": false}', delay=0.2):
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.url = f"http://127.0.0.1:{self.sock.getsockname()[1]}"
        self.stop = False
        self.body = body
        self.delay = delay
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while not self.stop:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            try:
                conn.recv(65535)
                conn.sendall(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    b"Content-Length: %d\r\nConnection: close\r\n\r\n" % len(self.body)
                )
                for byte in self.body:
                    if self.stop:
                        break
                    conn.sendall(bytes([byte]))
                    time.sleep(self.delay)
                conn.close()
            except OSError:
                pass

    def close(self):
        self.stop = True
        self.sock.close()


class NetworkDeadlineTest(unittest.TestCase):
    """Không cuộc gọi nào được sống lâu hơn ngân sách của ngày."""

    def test_a_trickling_server_cannot_outlast_the_timeout(self):
        # 20 byte x 0.2 s = 4 s nếu không có chặn cứng; timeout đặt 0.5 s
        server = DribbleServer(body=b'{"finished": false}\n', delay=0.2)
        try:
            transport = HttpTransport(server.url, "m", "tok", timeout=0.5)
            started = time.monotonic()
            status, _ = transport._call("GET", "/state")
            elapsed = time.monotonic() - started
        finally:
            server.close()

        self.assertLess(elapsed, 2.0,
                        f"timeout=0.5s mà cuộc gọi kéo {elapsed:.1f}s")
        self.assertEqual(status, 0, "cuộc gọi bị cắt phải được báo là lỗi mạng")

    def test_submitting_gives_up_when_the_day_deadline_passes(self):
        config = small_config()

        def always_500(day, plan):
            return 500, {"error": "quá tải"}

        server = FakeServer(config, actions_hook=always_500)
        try:
            transport = HttpTransport(server.url, "m", "tok", timeout=0.3)
            started = time.monotonic()
            ack = transport.send_actions(0, [[-20]] * config.n_agents,
                                         deadline=time.monotonic() + 0.5)
            elapsed = time.monotonic() - started
        finally:
            server.close()

        self.assertFalse(ack.ok)
        self.assertLess(elapsed, 2.0,
                        f"phải bỏ cuộc ở hạn 0.5s, nhưng mất {elapsed:.1f}s")

    def test_a_failing_server_cannot_make_a_day_outlive_its_deadline(self):
        config = small_config()
        ends_in = 2.0

        def always_500(day, plan):
            return 500, {"error": "quá tải"}

        def short_day(hits, payload):
            payload["endsAt"] = time.time() + ends_in
            return 200, payload

        server = FakeServer(config, actions_hook=always_500, state_hook=short_day)
        try:
            session = new_session(server, search_seconds=0.3, net_margin=0.2)
            session.config = session.transport.get_setup()
            session.strategist = MatchStrategist(session.config, search_seconds=0.3,
                                                 net_margin=0.2, seed=3)
            state = session.transport.wait_day_state([rules.PATROL] * config.n_agents)
            started = time.monotonic()
            session.play_day(state)
            elapsed = time.monotonic() - started
        finally:
            server.close()

        self.assertLess(elapsed, ends_in + 0.6,
                        f"ngày dài {ends_in}s mà play_day mất {elapsed:.1f}s")


class MatchLoopExitTest(unittest.TestCase):
    """Trận chỉ được kết thúc khi server nói đã kết thúc — và phải kết thúc được."""

    def test_a_transient_server_error_does_not_end_the_match(self):
        config = small_config()

        def flaky_state(hits, payload):
            if hits == 3:
                return 503, {"error": "tạm thời quá tải"}
            return 200, payload

        def result_says_running(day):
            # /result trả 200 cả khi trận chưa xong — bẫy đã làm bot bỏ cuộc sớm
            return 200, {"finished": day >= config.n_days, "standings": []}

        server = FakeServer(config, state_hook=flaky_state, result_hook=result_says_running)
        try:
            new_session(server).run()
        finally:
            server.close()

        self.assertEqual(sorted(server.plan_of_day), list(range(config.n_days)),
                         "một lỗi 503 thoáng qua không được kết thúc trận")

    def test_polling_gives_up_when_the_server_disappears(self):
        config = small_config()
        server = FakeServer(config)
        transport = HttpTransport(server.url, "m", "tok", poll_seconds=0.02,
                                  timeout=0.2, give_up_seconds=1.0)
        transport.get_setup()
        server.close()          # server biến mất giữa trận

        started = time.monotonic()
        with self.assertRaises(MatchEnded):
            transport.wait_day_state([rules.PATROL] * config.n_agents)
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 5.0,
                        f"phải bỏ cuộc sau ~1s, nhưng quay {elapsed:.1f}s")


class SilentFailureTest(unittest.TestCase):
    """Ba cách thua mà log vẫn xanh từ đầu tới cuối."""

    def test_a_rejection_sent_as_http_200_is_not_treated_as_success(self):
        config = small_config()

        def reject_with_200(day, plan):
            # server báo lỗi trong thân, không đổi mã HTTP
            return 200, {"day": day + 1, "reason": "E_STEP_OVERFLOW"}

        server = FakeServer(config, actions_hook=reject_with_200)
        try:
            transport = HttpTransport(server.url, "m", "tok", poll_seconds=0.02)
            ack = transport.send_actions(0, [[-20]] * config.n_agents)
        finally:
            server.close()

        self.assertFalse(ack.ok, "200 kèm reason E_* là bài bị từ chối, không phải thành công")
        self.assertEqual(ack.reason, "E_STEP_OVERFLOW")

    def test_hidden_fuel_is_read_as_a_full_tank_not_empty(self):
        config = small_config()

        def hide_fuel(hits, payload):
            for agent in payload["agents"]:
                agent["fuel"] = None       # server ẩn nhiên liệu
            return 200, payload

        server = FakeServer(config, state_hook=hide_fuel)
        try:
            transport = HttpTransport(server.url, "m", "tok", poll_seconds=0.02)
            transport.get_setup()
            state = transport.wait_day_state([rules.PATROL] * config.n_agents)
        finally:
            server.close()

        self.assertEqual([a.fuel for a in state.agents],
                         [config.fuel_limits] * config.n_agents,
                         "đọc thành 0 sẽ cắt mọi xe về lệnh đợi tới hết trận")

    def test_server_agent_kinds_win_when_the_assignment_was_refused(self):
        config = small_config()
        wanted = [rules.REFUEL] + [rules.PATROL] * (config.n_agents - 1)
        # ta muốn 1 xe tiếp, nhưng server từ chối và giữ mọi xe là xe tuần tra
        original_assign = HttpTransport.send_assignment
        original_roles = MatchStrategist.select_roles
        HttpTransport.send_assignment = lambda self, kinds, deadline=None: Ack(
            ok=False, reason="429")

        def pick_a_refuel_car(self, budget_seconds=None):
            self.kinds = list(wanted)
            return list(wanted)

        MatchStrategist.select_roles = pick_a_refuel_car
        server = FakeServer(config)
        try:
            session = new_session(server)
            session.run()
            kinds = session.strategist.kinds
        finally:
            HttpTransport.send_assignment = original_assign
            MatchStrategist.select_roles = original_roles
            server.close()

        self.assertEqual(kinds, [rules.PATROL] * config.n_agents,
                         "vai trò server báo mới là sự thật; ta không được tự cho là có xe tiếp")


class RoleBudgetTest(unittest.TestCase):
    """Chọn vai trò không được ăn vào những ngày thi đầu tiên."""

    def test_role_selection_stops_when_the_match_starts(self):
        config = small_config()
        config.starts_at = time.time() + 1.0        # trận mở sau 1 giây
        strategist = MatchStrategist(config, role_seconds=20.0, net_margin=0.0, seed=3)

        started = time.monotonic()
        strategist.select_roles()
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 2.5,
                        f"trận mở sau 1s mà chọn vai trò mất {elapsed:.1f}s")

    def test_role_selection_is_capped_by_day_zero_when_start_time_is_unknown(self):
        """Không có startsAt nghĩa là trận có thể ĐANG chạy.

        Khi đó mỗi giây chọn vai trò là một giây ăn trộm của ngày 0, nên ngân
        sách phải bị chặn bởi độ dài ngày 0 chứ không phải bởi --role-seconds.
        """
        config = small_config()
        config.starts_at = 0.0                      # server không báo giờ mở
        config.day_seconds = [4] * config.n_days
        config.__post_init__()
        strategist = MatchStrategist(config, role_seconds=20.0, net_margin=0.5,
                                     search_seconds=1.0, seed=3)

        started = time.monotonic()
        strategist.select_roles()
        elapsed = time.monotonic() - started

        # ngày 0 dài 4s, chừa 0.5s mạng và 1.0s tìm kiếm -> tối đa ~2.5s
        self.assertLess(elapsed, 3.5,
                        f"ngày 0 chỉ dài 4s mà chọn vai trò mất {elapsed:.1f}s")

    def test_a_truncated_rollout_is_not_ranked_against_complete_ones(self):
        config = small_config()
        forecast = TrafficForecast(config, n_patrols=config.n_agents)
        kinds = [rules.PATROL] * config.n_agents

        full = rollout(config, kinds, forecast, seed=1)
        cut, days_done = rollout(config, kinds, forecast, seed=1,
                                 deadline=time.monotonic() - 1.0, with_days=True)

        self.assertEqual(days_done, 0, "hết giờ ngay thì không ngày nào được chơi")
        self.assertNotEqual(cut, full, "mẫu bị cắt không được giả vờ là mẫu đủ")


class ResubmissionTest(unittest.TestCase):
    """Hợp đồng quan trọng nhất về thời gian: bài gửi SAU mới là bài được dùng.

    Server luyện tập từng đóng ngày ngay khi mọi đội nộp, nên bài an toàn 10 ms
    khoá luôn ngày và bài tinh chỉnh bị từ chối. Mọi số đo tại chỗ khi đó chỉ đo
    bộ dựng nghiệm tham lam. Test này khoá lại hành vi đúng.
    """

    def test_a_later_plan_replaces_an_earlier_one_within_the_same_day(self):
        config = small_config()
        config.day_seconds = [30] * config.n_days   # ngày dài để chắc chắn còn hạn
        config.__post_init__()
        match = LocalMatch(config, tokens=["tok-a"])
        team = match.by_token["tok-a"]

        first = wait_plan(config.n_agents, config.steps_of_day(0))
        second = [[-1, -(config.steps_of_day(0) - 1)] for _ in range(config.n_agents)]

        self.assertTrue(match.submit(team, first)["ok"])
        day_after_first = match.day
        self.assertTrue(match.submit(team, second)["ok"])

        self.assertEqual(match.day, day_after_first,
                         "nộp bài không được tự đóng ngày trước hạn")
        self.assertEqual(team.plan, second,
                         "bài hợp lệ cuối cùng trước hạn mới là bài được áp dụng")


class DecoderFailureTest(unittest.TestCase):
    """Khi không dựng nổi bài hợp lệ, bộ giải mã phải trả điểm thua, không ném lỗi."""

    def test_an_unrepairable_day_scores_worst_instead_of_crashing(self):
        config = small_config()
        config.day_steps = [0] + config.day_steps[1:]   # ngày 0 bước: không bài nào hợp lệ
        config.__post_init__()
        agents = [AgentState(rules.PATROL, p, config.fuel_limits)
                  for p in config.agent_starts]
        ctx = make_context(config, 0, agents, {}, [s.stocks for s in config.spots], set())

        decoded = decode(ctx, construct(ctx, random.Random(1)))

        self.assertFalse(decoded.result.ok, "ngày này đúng là không có bài hợp lệ")
        self.assertEqual(decoded.score, float("-inf"),
                         "bài không hợp lệ phải là điểm tệ nhất, để ALNS không bao giờ chọn")


if __name__ == "__main__":
    unittest.main()
