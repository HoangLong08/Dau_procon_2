"""Server luyện tập tại chỗ, nói đúng giao thức HTTP của server thi đấu.

Dùng để kiểm đường đi thật của bot (setup -> assignment -> state -> actions ->
result) mà không cần mạng của ban tổ chức. Đội bot nội bộ đóng vai đối thủ.
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional, Sequence
from urllib.parse import urlparse

from .. import rules
from ..model import AgentState, DayState, MatchConfig
from ..simulator import road_status_from_history, run_day, wait_plan


class Team:
    def __init__(self, token: str, name: str, config: MatchConfig, bot=None):
        self.token = token
        self.name = name
        self.bot = bot
        self.kinds: List[int] = [rules.PATROL] * config.n_agents
        self.agents = [AgentState(rules.PATROL, config.agent_starts[i], config.fuel_limits)
                       for i in range(config.n_agents)]
        self.plan: Optional[List[List[int]]] = None
        self.submitted_day = -1
        self.brands: set = set()
        self.daily_types = 0
        self.servings = 0
        self.invalid = 0
        self.response_seconds = 0.0

    def score(self):
        return (len(self.brands), self.daily_types, self.servings)


class LocalMatch:
    """Trạng thái một trận, an toàn khi nhiều luồng cùng truy cập."""

    def __init__(self, config: MatchConfig, tokens: Sequence[str], bots: Sequence = (),
                 start_delay: float = 0.0, log=None):
        self.config = config
        self.log = log or (lambda msg: None)
        self.lock = threading.RLock()
        self.teams: List[Team] = [Team(t, f"team-{i}", config) for i, t in enumerate(tokens)]
        for i, bot in enumerate(bots):
            team = Team(f"bot-{i}", getattr(bot, "name", f"bot-{i}"), config, bot=bot)
            team.kinds = list(bot.select_roles())
            for j, a in enumerate(team.agents):
                a.kind = team.kinds[j]
            self.teams.append(team)
        self.config.players = max(config.players, len(self.teams))
        self.by_token = {t.token: t for t in self.teams}
        self.day = 0
        self.started_at = time.time() + start_delay
        # báo đúng giờ mở trận cho client, như server thật: nếu không, bot không
        # có cách nào biết ngân sách chọn vai trò đang ăn vào ngày thi
        self.config.starts_at = self.started_at
        self.day_deadline = self.started_at + config.seconds_of_day(0)
        self.stay_history: List[Dict[int, int]] = []
        self.status = {c: rules.SMOOTH for c in config.road_cells}
        self.finished = False

    # ---- trợ giúp ----

    def open_for_play(self) -> bool:
        return time.time() >= self.started_at

    def state_for(self, team: Team) -> dict:
        others = []
        for i, other in enumerate(self.teams):
            if other is team:
                continue
            others.append({
                "id": i,
                "agents": [{"kind": a.kind, "pos": a.pos, "fuel": a.fuel} for a in other.agents],
            })
        return {
            "day": self.day,
            "endsAt": self.day_deadline,
            "agents": [{"kind": a.kind, "pos": a.pos, "fuel": a.fuel} for a in team.agents],
            "others": others,
            "traffics": [{"pos": c, "status": s} for c, s in sorted(self.status.items())],
        }

    def submit(self, team: Team, plan) -> dict:
        with self.lock:
            if self.finished:
                return {"ok": False, "reason": "E_FINISHED"}
            steps = self.config.steps_of_day(self.day)
            result = run_day(self.config, steps, team.agents, plan,
                             [s.stocks for s in self.config.spots], self.status)
            if not result.ok:
                return {"ok": False, "reason": result.error.code, "detail": str(result.error)}
            team.plan = [list(seq) for seq in plan]
            team.submitted_day = self.day
            team.response_seconds = max(0.0, time.time() - (self.day_deadline - self.config.seconds_of_day(self.day)))
            return {"ok": True, "day": self.day + 1}

    def maybe_advance(self) -> None:
        """Sang ngày mới khi hết hạn.

        KHÔNG đóng ngày sớm chỉ vì mọi đội đã nộp: luật là "bài hợp lệ cuối cùng
        TRƯỚC HẠN được áp dụng", nên bot còn quyền gửi lại bài tốt hơn tới phút
        chót. Đóng sớm làm bài an toàn 10 ms khoá luôn ngày, bài tinh chỉnh bị
        từ chối vì rơi sang ngày sau, và mọi đo đạc tại chỗ hoá ra chỉ đo bộ dựng
        nghiệm tham lam chứ không đo bot.
        """
        with self.lock:
            if self.finished or not self.open_for_play():
                return
            if time.time() < self.day_deadline:
                return
            self.resolve_day()

    def resolve_day(self) -> None:
        cfg = self.config
        steps = cfg.steps_of_day(self.day)
        combined: Dict[int, int] = {}
        for team in self.teams:
            plan = team.plan
            if team.bot is not None:
                state = DayState(day=self.day,
                                 agents=[AgentState(a.kind, a.pos, a.fuel) for a in team.agents],
                                 road_status=dict(self.status), others=[], ends_at=self.day_deadline)
                try:
                    plan = team.bot.plan(state)
                except Exception as exc:
                    self.log(f"bot nội bộ lỗi: {exc!r}")
                    plan = None
            stocks = [s.stocks for s in cfg.spots]
            result = None
            if plan is not None and team.submitted_day == self.day or team.bot is not None:
                if plan is not None:
                    result = run_day(cfg, steps, team.agents, plan, stocks, self.status)
                    if not result.ok:
                        team.invalid += 1
                        result = None
            if result is None:
                result = run_day(cfg, steps, team.agents, wait_plan(cfg.n_agents, steps),
                                 stocks, self.status)
            team.brands |= result.brands
            team.daily_types += len(result.brands)
            team.servings += result.servings
            team.agents = [AgentState(team.kinds[i], result.end_pos[i], result.end_fuel[i])
                           for i in range(cfg.n_agents)]
            team.plan = None
            for cell, n in result.stay.items():
                combined[cell] = combined.get(cell, 0) + n
        self.stay_history.append(combined)
        self.log(f"ngày {self.day} đã giải: " + ", ".join(
            f"{t.name} {t.score()}" for t in self.teams))
        self.day += 1
        if self.day >= cfg.n_days:
            self.finished = True
            return
        self.status = road_status_from_history(cfg, self.stay_history)
        self.day_deadline = time.time() + cfg.seconds_of_day(self.day)

    def standings(self) -> dict:
        ranked = sorted(self.teams, key=lambda t: t.score(), reverse=True)
        return {
            "finished": self.finished,
            "standings": [
                {"name": t.name, "types": len(t.brands), "dailyTypes": t.daily_types,
                 "servings": t.servings, "invalid": t.invalid}
                for t in ranked
            ],
        }


class _Handler(BaseHTTPRequestHandler):
    server_version = "hexudon-local/1.0"
    match: LocalMatch = None  # gán khi tạo server

    def log_message(self, fmt, *args):
        pass

    def _team(self) -> Optional[Team]:
        auth = self.headers.get("Authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else ""
        return self.match.by_token.get(token)

    def _send(self, code: int, payload=None):
        body = json.dumps(payload).encode() if payload is not None else b""
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _route(self) -> str:
        path = urlparse(self.path).path.rstrip("/")
        return path.rsplit("/", 1)[-1] if path else ""

    def do_GET(self):
        team = self._team()
        if team is None:
            return self._send(401, {"error": "token không hợp lệ"})
        route = self._route()
        match = self.match
        match.maybe_advance()
        if route == "setup":
            if not match.open_for_play():
                return self._send(425, {"error": "chưa mở"})
            return self._send(200, match.config.to_json())
        if route == "state":
            if not match.open_for_play():
                return self._send(425, {"error": "chưa mở"})
            if match.finished:
                return self._send(409, {"error": "đã kết thúc"})
            return self._send(200, match.state_for(team))
        if route == "result":
            return self._send(200, match.standings())
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        team = self._team()
        if team is None:
            return self._send(401, {"error": "token không hợp lệ"})
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"null")
        except json.JSONDecodeError:
            return self._send(400, {"ok": False, "reason": "E_BAD_FORMAT"})
        route = self._route()
        match = self.match
        match.maybe_advance()
        if route == "assignment":
            if not isinstance(payload, list) or len(payload) != match.config.n_agents:
                return self._send(400, {"ok": False, "reason": "E_BAD_FORMAT"})
            with match.lock:
                team.kinds = [int(v) for v in payload]
                for i, a in enumerate(team.agents):
                    a.kind = team.kinds[i]
                    a.fuel = match.config.fuel_limits if a.kind == rules.PATROL else 0
            return self._send(200, {"ok": True})
        if route == "actions":
            if match.finished:
                return self._send(409, {"ok": False, "reason": "E_FINISHED"})
            answer = match.submit(team, payload)
            return self._send(200 if answer.get("ok") else 400, answer)
        return self._send(404, {"error": "not found"})


def serve(config: MatchConfig, tokens: Sequence[str], bots: Sequence = (),
          host: str = "127.0.0.1", port: int = 0, start_delay: float = 0.0, log=None):
    """Khởi động server; trả (httpd, thread, match, url)."""
    match = LocalMatch(config, tokens, bots, start_delay=start_delay, log=log)
    handler = type("BoundHandler", (_Handler,), {"match": match})
    httpd = ThreadingHTTPServer((host, port), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    url = f"http://{host}:{httpd.server_address[1]}"
    return httpd, thread, match, url
