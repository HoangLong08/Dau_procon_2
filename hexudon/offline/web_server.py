"""Server giả nói đúng giao thức của hexudon.hairbui76.id.vn.

Dùng cho test end-to-end và để tập dượt ngày thi trên máy, không cần mạng.
Luật trong ngày lấy từ simulator của repo; dòng thời gian (cửa sổ chọn xe giấu
bản đồ, hạn ngày, 409 giữa hai ngày, practice tự nhịp, replay từng bước cho đội
gọi API, Cloudflare chặn Python-urllib) chép theo quan sát server thật ngày
2026-09-21.
"""

import base64
import copy
import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import parse_qs, urlparse

from .. import rules
from ..model import AgentState, MatchConfig
from ..simulator import road_status_from_history, run_day, wait_plan

SELECTING = "selecting_agents"
IN_PROGRESS = "in_progress"
FINISHED = "finished"
NO_DAY = "no day is open yet (the match starts with the agent-kind window)"
TYPE_NAME = {rules.PATROL: "patrol", rules.REFUEL: "refuel"}


def mint_token(team_id: int, name: str, ttl: float = 48 * 3600,
               now: Optional[float] = None) -> str:
    """Token dạng JWT không ký: đủ để client đọc id, tên, hạn như token thật."""
    now = time.time() if now is None else now

    def part(obj) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    claims = {"id": team_id, "name": name, "is_admin": False,
              "iat": int(now), "exp": int(now + ttl)}
    return f"{part({'alg': 'HS256', 'typ': 'JWT'})}.{part(claims)}.{uuid.uuid4().hex}"


def frames_from_trace(config: MatchConfig, kinds: Sequence[int], trace, collections) -> List[dict]:
    """Frame bước 0…N theo đúng dạng replay của server thật."""
    frames = []
    for t, pos, fuel in trace:
        got = [c for c in collections if c[0] <= t]
        frames.append({
            "step": t,
            "agents": [{"cell": pos[i], "fuel": fuel[i], "type": TYPE_NAME[kinds[i]]}
                       for i in range(len(pos))],
            "collected": [config.spots[s].pos for (step, _, s) in got if step == t],
            "servings": len(got),
            "types": len({config.spots[s].brand for (_, _, s) in got}),
        })
    return frames


@dataclass
class TeamState:
    team_id: int
    name: str
    kinds: List[int]
    agents: List[AgentState]
    types_selected: bool = False
    missed_selection: bool = False
    plans: Dict[int, List[List[int]]] = field(default_factory=dict)
    brands: set = field(default_factory=set)
    daily_types: int = 0
    servings: int = 0
    days_submitted: int = 0
    submit_count: int = 0
    #: tổng thời gian phản hồi của bài hợp lệ cuối cùng mỗi ngày (tiêu chí 4 của đề)
    response_seconds: float = 0.0
    #: ngày -> thời gian phản hồi của bài hợp lệ cuối cùng đã nhận trong ngày đó
    responses: Dict[int, float] = field(default_factory=dict)

    def score(self) -> Tuple[int, int, int]:
        return (len(self.brands), self.daily_types, self.servings)

    def rank_key(self):
        """Thứ tự xếp hạng của đề: L, D, Q, rồi tổng thời gian phản hồi thấp hơn."""
        return (*self.score(), -self.response_seconds)

    def detail(self) -> dict:
        return {"distinct_types": len(self.brands), "cumulative_daily_types": self.daily_types,
                "total_servings": self.servings,
                "cumulative_response_time": round(self.response_seconds, 3),
                "missed_selection": self.missed_selection, "days_submitted": self.days_submitted}


class Board:
    """Một bàn chơi: luật trong ngày, giao thông giữa các ngày, replay."""

    def __init__(self, config: MatchConfig, teams: Sequence[Tuple[int, str]]):
        self.config = config
        self.teams: Dict[int, TeamState] = {
            team_id: TeamState(team_id, name, [rules.PATROL] * config.n_agents,
                               [AgentState(rules.PATROL, p, config.fuel_limits)
                                for p in config.agent_starts])
            for team_id, name in teams
        }
        self.day = -1                          # -1: chưa có ngày nào mở
        self.finished = False
        self.road = {c: rules.SMOOTH for c in config.road_cells}
        self.stay_history: List[Dict[int, int]] = []
        self.replay_days: List[dict] = []

    def status(self) -> str:
        if self.finished:
            return FINISHED
        return IN_PROGRESS if self.day >= 0 else SELECTING

    def set_kinds(self, team_id: int, types) -> Tuple[int, dict]:
        n = self.config.n_agents
        if (not isinstance(types, list) or len(types) != n
                or any(t not in (rules.PATROL, rules.REFUEL) for t in types)):
            return 422, {"detail": [{"loc": ["body", "types"], "msg": f"need {n} kinds, each 0 or 1"}]}
        team = self.teams[team_id]
        team.kinds = [int(t) for t in types]
        team.types_selected = True
        for agent, kind in zip(team.agents, team.kinds):
            agent.kind = kind
        return 200, {"ok": True, "types": list(team.kinds)}

    def open_first_day(self) -> None:
        for team in self.teams.values():
            if not team.types_selected:
                team.missed_selection = True
        self.day = 0

    def accept_plan(self, team_id: int, day, plan,
                    response: Optional[float] = None) -> Tuple[int, dict]:
        """Nhận bài nếu hợp lệ; `response` là số giây từ lúc mở ngày (contest)."""
        if self.status() != IN_PROGRESS:
            return 409, {"detail": NO_DAY if self.day < 0 else "the match is finished"}
        if day != self.day:
            return 400, {"detail": f"day {day} is not the current day {self.day}"}
        team = self.teams[team_id]
        result = run_day(self.config, self.config.steps_of_day(self.day), team.agents, plan,
                         [s.stocks for s in self.config.spots], self.road)
        if not result.ok:
            return 400, {"detail": f"{result.error.code}: {result.error}"}
        team.plans[self.day] = [list(seq) for seq in plan]
        team.submit_count += 1
        if response is not None:
            # bài hợp lệ cuối cùng của ngày mới tính thời gian phản hồi
            team.responses[self.day] = max(0.0, response)
        return 200, {"ok": True, "day": self.day}

    def resolve_day(self) -> None:
        cfg = self.config
        steps = cfg.steps_of_day(self.day)
        stocks = [s.stocks for s in cfg.spots]
        combined: Dict[int, int] = {}
        entries = []
        for team in self.teams.values():
            plan = team.plans.get(self.day)
            submitted = plan is not None
            trace: list = []
            result = run_day(cfg, steps, team.agents, plan or wait_plan(cfg.n_agents, steps),
                             stocks, self.road, trace=trace)
            if not result.ok:          # bài đã được kiểm lúc nhận; đây chỉ là phòng hờ
                submitted, trace = False, []
                result = run_day(cfg, steps, team.agents, wait_plan(cfg.n_agents, steps),
                                 stocks, self.road, trace=trace)
            team.brands |= result.brands
            team.daily_types += len(result.brands)
            team.servings += result.servings
            team.days_submitted += int(submitted)
            if submitted:
                team.response_seconds += team.responses.get(self.day, 0.0)
            entries.append({"team_id": str(team.team_id), "kinds": list(team.kinds),
                            "servings": result.servings, "types": len(result.brands),
                            "submitted": submitted,
                            "frames": frames_from_trace(cfg, team.kinds, trace, result.collections)})
            team.agents = [AgentState(team.kinds[i], result.end_pos[i], result.end_fuel[i])
                           for i in range(cfg.n_agents)]
            for cell, count in result.stay.items():
                combined[cell] = combined.get(cell, 0) + count
        self.replay_days.append({"day": self.day, "steps": steps,
                                 "road_condition": {str(c): s for c, s in sorted(self.road.items())},
                                 "teams": entries})
        self.stay_history.append(combined)
        self.day += 1
        if self.day >= cfg.n_days:
            self.finished = True
            return
        self.road = road_status_from_history(cfg, self.stay_history)

    # ---- dữ liệu trả cho client ----

    def team_payload(self, team: TeamState) -> dict:
        return {"types_selected": team.types_selected, "missed_selection": team.missed_selection,
                "days_submitted": team.days_submitted,
                "agents": [{"agent_id": str(i), "type": TYPE_NAME[a.kind], "cell": a.pos, "fuel": a.fuel}
                           for i, a in enumerate(team.agents)],
                "stock": {str(s.pos): s.stocks for s in self.config.spots}}

    def state_payload(self, selection_limit: float) -> dict:
        status = self.status()
        return {"status": status,
                "day": -1 if status == SELECTING else self.day,
                "steps_today": self.config.steps_of_day(self.day) if status == IN_PROGRESS else None,
                "day_deadline_in": None, "selection_deadline_in": None, "selection_opens_in": None,
                "agent_selection_time_limit": selection_limit,
                "road_condition": {str(c): s for c, s in sorted(self.road.items())},
                "teams": {str(t.team_id): self.team_payload(t) for t in self.teams.values()}}

    def day_payload(self, viewer: int, ends_at: Optional[float]) -> dict:
        team = self.teams[viewer]
        return {"endsAt": ends_at, "day": self.day,
                "agents": [{"kind": a.kind, "pos": a.pos, "fuel": a.fuel} for a in team.agents],
                "others": [{"id": t.team_id, "agents": [{"kind": a.kind, "pos": a.pos} for a in t.agents]}
                           for t in self.teams.values() if t.team_id != viewer],
                "traffics": [{"pos": c, "status": s} for c, s in sorted(self.road.items())]}

    def replay_payload(self, viewer: int) -> dict:
        """Đội gọi API thấy mọi frame của mình; đội khác chỉ có frame cuối ngày."""
        days = []
        for day in self.replay_days:
            teams = [dict(entry, frames=entry["frames"] if entry["team_id"] == str(viewer)
                          else entry["frames"][-1:])
                     for entry in day["teams"]]
            days.append(dict(day, teams=teams))
        return {"days": days}

    def result_payload(self) -> dict:
        ranked = sorted(self.teams.values(), key=lambda t: t.rank_key(), reverse=True)
        return {"ranking": [str(t.team_id) for t in ranked],
                "detail": {str(t.team_id): t.detail() for t in ranked}}


class ContestGame:
    """Một câu contest: mọi đội chung bàn, dòng thời gian theo đồng hồ."""

    def __init__(self, config: MatchConfig, teams: Sequence[Tuple[int, str]],
                 selection_seconds: float, pre_window: float = 0.0,
                 auto_reset: Optional[float] = None, clock: Callable[[], float] = time.time):
        self.template = config
        self.team_list = list(teams)
        self.selection_seconds = selection_seconds
        self.pre_window = pre_window
        self.auto_reset = auto_reset
        self.clock = clock
        self.restart()

    def restart(self) -> None:
        now = self.clock()
        self.created = now
        self.config = copy.deepcopy(self.template)
        self.config.players = max(1, len(self.team_list))
        self.window_opens = now + self.pre_window
        self.starts_at = self.window_opens + self.selection_seconds
        self.config.starts_at = self.starts_at
        self.day_ends: List[float] = []
        end = self.starts_at
        for d in range(self.config.n_days):
            end += self.config.seconds_of_day(d)
            self.day_ends.append(end)
        self.board = Board(self.config, self.team_list)

    def sync(self) -> None:
        now = self.clock()
        if self.auto_reset and now >= self.created + self.auto_reset:
            self.restart()
        board = self.board
        if board.day < 0 and now >= self.starts_at:
            board.open_first_day()
        while not board.finished and board.day >= 0 and now >= self.day_ends[board.day]:
            board.resolve_day()

    def owner_of(self, team_id: int) -> int:
        return team_id

    def config_payload(self) -> dict:
        if self.board.status() == SELECTING and self.clock() < self.window_opens:
            return {"board_withheld": True, "startsAt": self.starts_at,
                    "agent_selection_time_limit": self.selection_seconds}
        return dict(self.config.to_json(), agent_selection_time_limit=self.selection_seconds)

    def state_payload(self) -> dict:
        now = self.clock()
        payload = self.board.state_payload(self.selection_seconds)
        if payload["status"] == IN_PROGRESS:
            payload["day_deadline_in"] = self.day_ends[self.board.day] - now
        elif payload["status"] == SELECTING:
            if now < self.window_opens:
                payload["selection_opens_in"] = self.window_opens - now
            else:
                payload["selection_deadline_in"] = self.starts_at - now
        return payload

    def day_ends_at(self) -> Optional[float]:
        return self.day_ends[self.board.day] if self.board.status() == IN_PROGRESS else None

    def select(self, team_id: int, types) -> Tuple[int, dict]:
        if self.board.status() != SELECTING or self.clock() < self.window_opens:
            return 409, {"detail": "the agent-kind window is not open"}
        return self.board.set_kinds(team_id, types)

    def submit(self, team_id: int, day, plan) -> Tuple[int, dict]:
        response = None
        if isinstance(day, int) and 0 <= day < len(self.day_ends):
            opened = self.day_ends[day] - self.config.seconds_of_day(day)
            response = self.clock() - opened
        return self.board.accept_plan(team_id, day, plan, response=response)


class PracticeGame:
    """Bản practice của một đội: tự nhịp, sang ngày ngay khi nộp bài."""

    def __init__(self, config: MatchConfig, owner: int, name: str, selection_seconds: float):
        self.template = config
        self.owner = owner
        self.name = name
        self.selection_seconds = selection_seconds
        self.reset()

    def reset(self) -> None:
        self.config = copy.deepcopy(self.template)
        self.config.players = 1
        self.board = Board(self.config, [(self.owner, self.name)])

    def sync(self) -> None:
        pass

    def owner_of(self, team_id: int) -> int:
        return self.owner

    def config_payload(self) -> dict:
        return dict(self.config.to_json(), agent_selection_time_limit=self.selection_seconds)

    def state_payload(self) -> dict:
        return self.board.state_payload(self.selection_seconds)

    def day_ends_at(self) -> Optional[float]:
        return None

    def select(self, team_id: int, types) -> Tuple[int, dict]:
        if self.board.status() != SELECTING:
            return 409, {"detail": "agent kinds are already chosen"}
        code, body = self.board.set_kinds(team_id, types)
        if code == 200:
            self.board.open_first_day()
        return code, body

    def submit(self, team_id: int, day, plan) -> Tuple[int, dict]:
        code, body = self.board.accept_plan(team_id, day, plan)
        if code == 200:
            self.board.resolve_day()
        return code, body


@dataclass
class Question:
    qid: str
    name: str
    match_id: int
    match_name: str
    practice: bool
    config: MatchConfig
    selection_seconds: float
    auto_reset: Optional[float] = None


@dataclass
class Faults:
    reject_python_ua: bool = True     # như Cloudflare của server thật: 403 "error code: 1010"
    rate_limit_every: int = 0         # cứ N request thì một lần 429
    retry_after: float = 0.2          # header Retry-After kèm 429 (server thật không gửi)
    error_every: int = 0              # cứ N request thì một lần 503
    trickle_seconds: float = 0.0      # nhả body /state từng byte, cách nhau ngần này giây


class WebWorld:
    """Toàn bộ trạng thái server giả; an toàn khi nhiều luồng cùng gọi."""

    def __init__(self, teams: Sequence[Tuple[int, str]], faults: Optional[Faults] = None,
                 clock: Callable[[], float] = time.time):
        self.lock = threading.RLock()
        self.teams = list(teams)
        self.names = dict(teams)
        self.faults = faults or Faults()
        self.clock = clock
        self.tokens: Dict[str, int] = {}
        self.questions: Dict[str, Question] = {}
        self.contests: Dict[str, ContestGame] = {}
        self.practices: Dict[Tuple[str, int], PracticeGame] = {}
        self.requests = 0
        self.hits: Dict[str, int] = {}

    # ---- dựng thế giới ----

    def issue_token(self, team_id: int, ttl: float = 48 * 3600) -> str:
        token = mint_token(team_id, self.names[team_id], ttl=ttl, now=self.clock())
        with self.lock:
            self.tokens[token] = team_id
        return token

    def revoke(self, token: str) -> None:
        with self.lock:
            self.tokens.pop(token, None)

    def add_question(self, name: str, config: MatchConfig, match_id: int, match_name: str,
                     practice: bool = False, selection_seconds: float = 30.0,
                     pre_window: float = 0.0, auto_reset: Optional[float] = None) -> str:
        qid = str(uuid.uuid4())
        with self.lock:
            self.questions[qid] = Question(qid, name, match_id, match_name, practice, config,
                                           selection_seconds, auto_reset)
            if not practice:
                self.contests[qid] = ContestGame(config, self.teams, selection_seconds,
                                                 pre_window, auto_reset, clock=self.clock)
        return qid

    def practice_game(self, qid: str, team_id: int) -> PracticeGame:
        with self.lock:
            key = (qid, team_id)
            if key not in self.practices:
                q = self.questions[qid]
                self.practices[key] = PracticeGame(q.config, team_id,
                                                   self.names.get(team_id, str(team_id)),
                                                   q.selection_seconds)
            return self.practices[key]

    def game_for(self, game_id: str, team_id: int):
        """Tìm game như server thật: practice nhận cả ID ghép "<câu>:<đội>" lẫn ID trần."""
        qid, _, owner = game_id.partition(":")
        q = self.questions.get(qid)
        if q is None:
            return None
        if not q.practice:
            return None if owner else self.contests[qid]
        if owner and not owner.isdigit():
            return None
        return self.practice_game(qid, int(owner) if owner else team_id)

    # ---- manager API ----

    def questions_payload(self) -> dict:
        rows = [{"id": q.qid, "match_id": q.match_id, "name": q.name,
                 "auto_reset_minutes": int((q.auto_reset or 0) // 60),
                 "match": {"name": q.match_name, "is_active": True,
                           "is_practice": q.practice, "no_reset": False}}
                for q in self.questions.values()]
        return {"count": len(rows), "data": rows}

    def matches_payload(self) -> dict:
        matches: Dict[int, dict] = {}
        for q in self.questions.values():
            matches.setdefault(q.match_id, {
                "id": q.match_id, "name": q.match_name, "is_active": True,
                "is_practice": q.practice, "no_reset": False,
                "teams": [{"id": team_id, "name": name} for team_id, name in self.teams]})
        return {"count": len(matches), "data": list(matches.values())}

    # ---- định tuyến ----

    def _hit(self, key: str) -> None:
        self.hits[key] = self.hits.get(key, 0) + 1

    def handle_get(self, path: str, query: Dict[str, str], team_id: int) -> Tuple[int, object]:
        with self.lock:
            self._hit(f"GET {path}")
            if path == "/manager/api/question":
                return 200, self.questions_payload()
            if path == "/manager/api/match":
                return 200, self.matches_payload()
            game_id = query.get("game_id", "")
            if path in ("/api/game/practice/score", "/api/game/practice/peer") and ":" not in game_id:
                return 404, {"detail": "game not found"}
            game = self.game_for(game_id, team_id)
            if game is None:
                return 404, {"detail": "game not found"}
            game.sync()
            viewer = game.owner_of(team_id)
            board = game.board
            if path == "/api/game/config":
                return 200, game.config_payload()
            if path == "/api/game/state":
                return 200, game.state_payload()
            if path == "/api/game/day":
                if board.status() != IN_PROGRESS:
                    return 409, {"detail": NO_DAY if board.day < 0 else "the match is finished"}
                return 200, board.day_payload(viewer, game.day_ends_at())
            if path in ("/api/game/replay", "/api/game/practice/peer"):
                return 200, board.replay_payload(viewer)
            if path in ("/api/game/result", "/api/game/practice/score"):
                return 200, board.result_payload()
            if path == "/api/game/actions":
                plans = board.teams[viewer].plans
                return 200, {"actions": [{"day": d, "actions": p} for d, p in sorted(plans.items())]}
            return 404, {"detail": "Not Found"}

    def handle_post(self, path: str, body, team_id: int) -> Tuple[int, object]:
        with self.lock:
            self._hit(f"POST {path}")
            if not isinstance(body, dict) or not isinstance(body.get("game_id"), str):
                return 422, {"detail": [{"loc": ["body", "game_id"], "msg": "field required"}]}
            game = self.game_for(body["game_id"], team_id)
            if game is None:
                return 404, {"detail": "game not found"}
            practice = isinstance(game, PracticeGame)
            if practice and game.owner != team_id:
                return 400, {"detail": "not your practice game"}
            game.sync()
            if path == "/api/game/agent-types":
                return game.select(team_id, body.get("types"))
            if path == "/api/game/practice/reset":
                if not practice:
                    return 400, {"detail": "not a practice game"}
                game.reset()
                return 200, {"ok": True}
            if path in ("/api/game/actions", "/api/game/practice/actions"):
                if practice != (path == "/api/game/practice/actions"):
                    return 400, {"detail": "wrong endpoint for this game mode"}
                day, plan = body.get("day"), body.get("actions")
                if not isinstance(day, int) or not isinstance(plan, list):
                    return 422, {"detail": [{"loc": ["body", "actions"], "msg": "field required"}]}
                return game.submit(team_id, day, plan)
            return 404, {"detail": "Not Found"}


class _Handler(BaseHTTPRequestHandler):
    world: WebWorld = None      # gán khi tạo server
    server_version = "hexudon-web-fake/1.0"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code: int, payload=None, raw: Optional[bytes] = None,
              headers: Optional[Dict[str, str]] = None, trickle: float = 0.0) -> None:
        body = raw if raw is not None else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain" if raw is not None else "application/json")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        try:
            if trickle > 0:
                for i in range(len(body)):
                    self.wfile.write(body[i:i + 1])
                    self.wfile.flush()
                    time.sleep(trickle)
            else:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _gate(self, manager: bool) -> Optional[int]:
        """Các lỗi chung (Cloudflare, 429, 503, 401); trả id đội nếu được đi tiếp."""
        world = self.world
        faults = world.faults
        if faults.reject_python_ua and self.headers.get("User-Agent", "").startswith("Python-urllib"):
            self._send(403, raw=b"error code: 1010")
            return None
        with world.lock:
            world.requests += 1
            n = world.requests
        if faults.rate_limit_every and n % faults.rate_limit_every == 0:
            self._send(429, {"detail": "Too Many Requests"},
                       headers={"Retry-After": str(faults.retry_after)})
            return None
        if faults.error_every and n % faults.error_every == 0:
            self._send(503, {"detail": "Service Unavailable"})
            return None
        auth = self.headers.get("Authorization", "")
        token = auth if manager else (auth[len("Bearer "):] if auth.startswith("Bearer ") else "")
        with world.lock:
            team_id = world.tokens.get(token)
        if team_id is None:
            self._send(401, {"message": "Unauthorized"} if manager
                       else {"detail": "Could not validate credentials"})
            return None
        return team_id

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        team_id = self._gate(manager=path.startswith("/manager/"))
        if team_id is None:
            return
        code, payload = self.world.handle_get(path, query, team_id)
        trickle = self.world.faults.trickle_seconds if path == "/api/game/state" else 0.0
        self._send(code, payload, trickle=trickle)

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/")
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        team_id = self._gate(manager=False)
        if team_id is None:
            return
        try:
            body = json.loads(raw or b"null")
        except json.JSONDecodeError:
            body = None
        code, payload = self.world.handle_post(path, body, team_id)
        self._send(code, payload)


def serve_web(world: WebWorld, host: str = "127.0.0.1", port: int = 0):
    """Khởi động server giả; trả (httpd, thread, url)."""
    handler = type("BoundWebHandler", (_Handler,), {"world": world})
    httpd = ThreadingHTTPServer((host, port), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, thread, f"http://{host}:{httpd.server_address[1]}"
