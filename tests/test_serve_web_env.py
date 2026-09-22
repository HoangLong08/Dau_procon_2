"""serve --web vào cổng bận phải dừng mà không đè token của server giả đang chạy."""

import contextlib
import io
import os
import socket
import tempfile
import unittest

from hexudon.cli import build_parser, cmd_serve_web


class ServeWebEnvTest(unittest.TestCase):
    def test_a_busy_port_leaves_the_running_servers_tokens_alone(self):
        busy = socket.socket()
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        self.addCleanup(busy.close)
        port = busy.getsockname()[1]
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        old = os.getcwd()
        os.chdir(tmp.name)
        self.addCleanup(os.chdir, old)
        os.makedirs("experiments")
        with open(os.path.join("experiments", "local.env"), "w", encoding="utf-8") as fh:
            fh.write("TEST_DAU_01=cu\n")
        args = build_parser().parse_args(["serve", "--web", "--port", str(port)])
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cmd_serve_web(args), 2)
        with open(os.path.join("experiments", "local.env"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "TEST_DAU_01=cu\n")
