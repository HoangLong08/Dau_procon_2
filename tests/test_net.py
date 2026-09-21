"""Tầng HTTP chung: .env, JWT, SSL dự phòng, User-Agent, không lộ token."""

import base64
import json
import os
import ssl
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from hexudon.client.net import USER_AGENT, JsonHttp, jwt_claims, load_env, make_ssl_context


def fake_jwt(claims: dict) -> str:
    def part(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()
    return f"{part({'alg': 'HS256', 'typ': 'JWT'})}.{part(claims)}.chu-ky"


class EchoServer:
    """Trả lại User-Agent và đường dẫn; chặn Python-urllib như Cloudflare của server thật."""

    def __init__(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self, code, body, content_type):
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                ua = self.headers.get("User-Agent", "")
                if ua.startswith("Python-urllib"):
                    return self._reply(403, b"error code: 1010", "text/plain")
                code = 500 if self.path.startswith("/boom") else 200
                body = json.dumps({"ua": ua, "path": self.path}).encode()
                self._reply(code, body, "application/json")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class EnvFileTest(unittest.TestCase):
    def test_reads_quotes_comments_and_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, ".env")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write('# bình luận\nTOKEN=abc\n\nTEST_DAU_01="x.y.z"\n'
                         "export TEST_DAU_02='q'\nDONG_RAC\n")
            self.assertEqual(load_env(path),
                             {"TOKEN": "abc", "TEST_DAU_01": "x.y.z", "TEST_DAU_02": "q"})

    def test_missing_file_is_empty(self):
        self.assertEqual(load_env("/khong/co/file/nay.env"), {})


class JwtTest(unittest.TestCase):
    def test_reads_team_id_name_and_expiry(self):
        token = fake_jwt({"id": 78, "name": "DAU 2", "exp": 1790065363})
        self.assertEqual(jwt_claims(token), {"id": 78, "name": "DAU 2", "exp": 1790065363})

    def test_broken_token_gives_empty_claims(self):
        self.assertEqual(jwt_claims("khong-phai-jwt"), {})
        self.assertEqual(jwt_claims("a.@@@.c"), {})


class SslTest(unittest.TestCase):
    @unittest.skipUnless(os.path.isfile("/etc/ssl/cert.pem"), "máy không có /etc/ssl/cert.pem")
    def test_falls_back_to_the_system_store_when_python_has_none(self):
        empty = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self.assertEqual(empty.get_ca_certs(), [])
        messages = []
        ctx = make_ssl_context(messages.append, base=empty,
                               ca_files=["/khong/co", "/etc/ssl/cert.pem"])
        self.assertGreater(len(ctx.get_ca_certs()), 0)
        self.assertTrue(any("/etc/ssl/cert.pem" in m for m in messages))

    def test_verification_is_never_disabled(self):
        ctx = make_ssl_context(base=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT), ca_files=[])
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(ctx.check_hostname)


class JsonHttpTest(unittest.TestCase):
    def setUp(self):
        self.server = EchoServer()
        self.addCleanup(self.server.close)

    def test_requests_carry_our_user_agent_not_python_urllib(self):
        resp = JsonHttp(self.server.url).call("GET", "/x")
        self.assertEqual(resp.status, 200, "Cloudflare giả chặn nghĩa là UA vẫn là Python-urllib")
        self.assertEqual(resp.payload["ua"], USER_AGENT)

    def test_query_parameters_are_encoded(self):
        resp = JsonHttp(self.server.url).call("GET", "/api/game/state", params={"game_id": "q1:78"})
        self.assertEqual(resp.payload["path"], "/api/game/state?game_id=q1%3A78")

    def test_http_errors_come_back_with_status_and_body(self):
        resp = JsonHttp(self.server.url).call("GET", "/boom")
        self.assertEqual(resp.status, 500)
        self.assertEqual(resp.payload["path"], "/boom")

    def test_token_never_reaches_log_or_replay(self):
        secret = "SECRET-TOKEN-123"
        logs, entries = [], []
        http = JsonHttp(self.server.url, log=logs.append, replay=entries.append,
                        secrets=lambda: [secret])
        http.call("GET", "/boom", headers={"Authorization": "Bearer " + secret})
        JsonHttp("http://127.0.0.1:1", timeout=0.5, log=logs.append, replay=entries.append,
                 secrets=lambda: [secret]).call("GET", "/x", headers={"Authorization": secret})
        self.assertNotIn(secret, json.dumps(entries))
        self.assertNotIn(secret, " ".join(logs))
        self.assertEqual(http.scrub(f"lỗi {secret} ở đây"), "lỗi <token> ở đây")

    def test_unreachable_server_is_status_zero_not_an_exception(self):
        resp = JsonHttp("http://127.0.0.1:1", timeout=0.5).call("GET", "/x")
        self.assertEqual(resp.status, 0)


if __name__ == "__main__":
    unittest.main()
