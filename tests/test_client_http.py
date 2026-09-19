"""Kiểm đường đi HTTP thật: bot chơi trọn một trận với server luyện tập tại chỗ."""

import unittest

from hexudon import rules
from hexudon.client.http_client import HttpTransport
from hexudon.client.session import MatchSession
from hexudon.offline.bots import SampleBot
from hexudon.offline.local_server import serve
from hexudon.offline.mapgen import generate


class HttpEndToEndTest(unittest.TestCase):
    def setUp(self):
        self.config = generate(seed=11, size=10, agents=4, days=4)
        self.config.day_seconds = [3] * self.config.n_days
        self.config.__post_init__()
        self.httpd, self.thread, self.match, self.url = serve(
            self.config, tokens=["tok-a"], bots=[SampleBot(self.config)], start_delay=0.0
        )

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def test_full_match_over_http(self):
        transport = HttpTransport(self.url, "m-test", "tok-a", poll_seconds=0.05)
        session = MatchSession(
            transport, search_seconds=0.4, net_margin=0.3, role_seconds=1.5, seed=5
        )
        result = session.run()

        self.assertTrue(self.match.finished, "trận phải kết thúc")
        us = self.match.by_token["tok-a"]
        self.assertEqual(us.invalid, 0, "không được có bài không hợp lệ")
        self.assertGreater(len(us.brands), 0, "phải thu được ít nhất một loại udon")
        self.assertIsNotNone(result)
        self.assertIn("standings", result)
        # vai trò phải được server ghi nhận và cố định
        self.assertEqual(len(us.kinds), self.config.n_agents)
        self.assertIn(rules.PATROL, us.kinds)

    def test_bad_token_is_rejected(self):
        transport = HttpTransport(self.url, "m-test", "sai-token", poll_seconds=0.02)
        status, _ = transport._call("GET", "/setup")
        self.assertEqual(status, 401)


if __name__ == "__main__":
    unittest.main()
