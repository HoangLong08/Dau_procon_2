"""Bảo hiểm giao thức: server lệch cách đánh số ngày hoặc đòi kiểu bọc JSON khác."""

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from hexudon import rules
from hexudon.model import AgentState
from hexudon.simulator import run_day
from hexudon.client.http_client import HttpTransport
from hexudon.client.session import MatchSession
from hexudon.offline.mapgen import generate


class QuirkyServer:
    """Server giả: chỉ nhận body bọc {"day","actions"} và đánh số ngày từ 1."""

    def __init__(self, config, day_base=1, require_wrapped=True):
        self.config = config
        self.day_base = day_base
        self.require_wrapped = require_wrapped
        self.day = 0
        self.accepted = []
        self.rejections = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, code, payload):
                body = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                route = self.path.rstrip("/").rsplit("/", 1)[-1]
                if route == "setup":
                    return self._send(200, outer.config.to_json())
                if route == "state":
                    if outer.day >= outer.config.n_days:
                        return self._send(409, {"error": "finished"})
                    agents = [{"kind": rules.PATROL, "pos": p, "fuel": outer.config.fuel_limits}
                              for p in outer.config.agent_starts]
                    return self._send(200, {"day": outer.day + outer.day_base,
                                            "agents": agents, "others": [], "traffics": []})
                if route == "result":
                    return self._send(200, {"standings": []})
                return self._send(404, {})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length) or b"null")
                route = self.path.rstrip("/").rsplit("/", 1)[-1]
                if route == "assignment":
                    if not isinstance(payload, dict) or "types" not in payload:
                        outer.rejections.append(("assignment", "E_BAD_FORMAT"))
                        return self._send(400, {"ok": False, "reason": "E_BAD_FORMAT"})
                    return self._send(200, {"ok": True})
                if route == "actions":
                    if outer.require_wrapped and not isinstance(payload, dict):
                        outer.rejections.append(("actions", "E_BAD_FORMAT"))
                        return self._send(400, {"ok": False, "reason": "E_BAD_FORMAT"})
                    plan = payload["actions"] if isinstance(payload, dict) else payload
                    steps = outer.config.day_steps[outer.day]
                    agents = [AgentState(rules.PATROL, p, outer.config.fuel_limits)
                              for p in outer.config.agent_starts]
                    check = run_day(outer.config, steps, agents, plan,
                                    [sp.stocks for sp in outer.config.spots], {})
                    if not check.ok:
                        outer.rejections.append(("actions", check.error.code))
                        return self._send(400, {"ok": False, "reason": check.error.code})
                    outer.accepted.append((outer.day, plan))
                    outer.day += 1
                    return self._send(200, {"ok": True})
                return self._send(404, {})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class ProtocolRobustnessTest(unittest.TestCase):
    def setUp(self):
        self.config = generate(seed=15, size=10, agents=3, days=4)
        # số bước khác nhau theo ngày để cách đánh số ngày sai bị lộ ngay
        self.config.day_steps = [20, 30, 40, 50]
        self.config.day_seconds = [2, 2, 2, 2]
        self.config.__post_init__()

    def test_client_finds_the_accepted_json_shape(self):
        server = QuirkyServer(self.config, day_base=0, require_wrapped=True)
        try:
            transport = HttpTransport(server.url, "m", "tok", poll_seconds=0.02)
            ack = transport.send_assignment([0, 0, 1])
            self.assertTrue(ack.ok, "phải tự tìm được kiểu bọc mà server nhận")
            ack = transport.send_actions(0, [[-20]] * self.config.n_agents)
            self.assertTrue(ack.ok)
            self.assertEqual(transport.actions_style, "wrapped")
            # lần sau dùng luôn kiểu đã học, không thử lại kiểu sai
            before = len(server.rejections)
            ack = transport.send_actions(1, [[-30]] * self.config.n_agents)
            self.assertTrue(ack.ok)
            self.assertEqual(len(server.rejections), before,
                             "đã học được kiểu bọc thì không thử lại kiểu sai")
        finally:
            server.close()

    def test_session_recovers_from_one_based_day_numbering(self):
        server = QuirkyServer(self.config, day_base=1, require_wrapped=False)
        try:
            transport = HttpTransport(server.url, "m", "tok", poll_seconds=0.02)
            session = MatchSession(transport, search_seconds=0.2, net_margin=0.0,
                                   role_seconds=1.0, seed=3)
            session.run()
            self.assertEqual(session.day_offset, -1, "phải nhận ra server đánh số ngày từ 1")
            self.assertEqual(len(server.accepted), self.config.n_days,
                             "mọi ngày đều phải có bài được chấp nhận")
        finally:
            server.close()


if __name__ == "__main__":
    unittest.main()
