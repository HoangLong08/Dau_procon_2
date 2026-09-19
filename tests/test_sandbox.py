"""Kiểm chế độ sandbox JSON Lines qua stdin/stdout.

Bài kiểm mô phỏng đúng cách server sandbox hoạt động: mỗi khung day_state được
trả lời bằng ĐÚNG MỘT dòng kế hoạch, và trạng thái ngày sau là kết quả thật của
ngày trước theo simulator.
"""

import io
import json
import unittest

from hexudon import rules
from hexudon.client.sandbox_client import SandboxTransport
from hexudon.client.session import MatchSession
from hexudon.model import AgentState
from hexudon.offline.mapgen import generate
from hexudon.simulator import run_day


class ScriptedStdin:
    """stdin giả: sinh khung ngày kế tiếp dựa trên kế hoạch bot vừa ghi ra stdout."""

    def __init__(self, config, sink):
        self.config = config
        self.sink = sink
        self.day = -1
        self.kinds = None
        self.agents = [AgentState(rules.PATROL, p, config.fuel_limits) for p in config.agent_starts]
        self.queue = [json.dumps(config.to_json())]
        self.results = []

    def readline(self):
        if self.queue:
            return self.queue.pop(0) + "\n"
        written = self.sink.take()
        if written is None:
            return ""
        if self.kinds is None:
            self.kinds = written
            for i, a in enumerate(self.agents):
                a.kind = self.kinds[i]
                a.fuel = self.config.fuel_limits if a.kind == rules.PATROL else 0
        else:
            steps = self.config.steps_of_day(self.day)
            result = run_day(self.config, steps, self.agents, written,
                             [s.stocks for s in self.config.spots], {})
            self.results.append(result)
            if not result.ok:
                return ""
            self.agents = [AgentState(self.kinds[i], result.end_pos[i], result.end_fuel[i])
                           for i in range(self.config.n_agents)]
        self.day += 1
        if self.day >= self.config.n_days:
            return json.dumps({"standings": []}) + "\n"
        return json.dumps({
            "day": self.day,
            "agents": [{"kind": a.kind, "pos": a.pos, "fuel": a.fuel} for a in self.agents],
            "others": [],
            "traffics": [],
        }) + "\n"


class CapturingStdout:
    def __init__(self):
        self.lines = []
        self._buffer = ""

    def write(self, text):
        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            if line.strip():
                self.lines.append(json.loads(line))

    def flush(self):
        pass

    def take(self):
        return self.lines.pop(0) if self.lines else None


class SandboxTest(unittest.TestCase):
    def test_one_plan_per_day_and_all_valid(self):
        config = generate(seed=4, size=10, agents=3, days=4)
        config.day_seconds = [2] * config.n_days
        config.__post_init__()

        stdout = CapturingStdout()
        stdin = ScriptedStdin(config, stdout)
        transport = SandboxTransport(stdin=stdin, stdout=stdout)
        session = MatchSession(transport, search_seconds=0.3, net_margin=0.0, role_seconds=1.0)
        session.run()

        self.assertEqual(len(stdin.results), config.n_days,
                         "phải trả lời đúng một kế hoạch cho mỗi ngày")
        for day, result in enumerate(stdin.results):
            self.assertTrue(result.ok, f"kế hoạch ngày {day} không hợp lệ: {result.error}")
        self.assertGreater(sum(len(r.brands) for r in stdin.results), 0)

    def test_sandbox_does_not_allow_resubmit(self):
        self.assertFalse(SandboxTransport.supports_resubmit)


if __name__ == "__main__":
    unittest.main()
