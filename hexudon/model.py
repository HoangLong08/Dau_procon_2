"""Mô hình dữ liệu và đọc/ghi JSON theo format chính thức."""

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence

from . import rules
from .geometry import build_direction_index, build_neighbor_table


class ProtocolError(ValueError):
    """Payload của server không đọc được.

    Ném ra thay cho KeyError/TypeError để tầng client bắt được và hỏi lại,
    thay vì để một trường lạ giết cả trận.
    """


_MISSING = object()


def _pick(obj: dict, *names, default=_MISSING):
    """Lấy trường đầu tiên có mặt; BTC có thể đổi tên giữa các bản server."""
    for name in names:
        value = obj.get(name)
        if value is not None:
            return value
    if default is _MISSING:
        raise ProtocolError(f"thiếu trường {names[0]!r}; payload có {sorted(obj)}")
    return default


def _as_pos(value, width: Optional[int]) -> int:
    """Vị trí có thể là chỉ số phẳng hoặc cặp (col, row)."""
    if isinstance(value, (list, tuple)):
        if len(value) != 2 or width is None:
            raise ProtocolError(f"không đọc được vị trí {value!r}")
        col, row = int(value[0]), int(value[1])
        return row * width + col
    return int(value)


@dataclass(frozen=True)
class Spot:
    brand: int
    pos: int
    stocks: int


@dataclass
class MatchConfig:
    width: int
    height: int
    cells: List[int]          # phẳng, dài width*height
    spots: List[Spot]
    agent_starts: List[int]
    fuel_limits: int
    day_steps: List[int]
    day_seconds: List[int]
    players: int
    busy_threshold: int
    jammed_threshold: int
    starts_at: float = 0.0

    # dẫn xuất, tính trong __post_init__
    n_cells: int = field(init=False)
    n_agents: int = field(init=False)
    n_days: int = field(init=False)
    neighbors: List[List[int]] = field(init=False, repr=False)
    dir_index: List[Dict[int, int]] = field(init=False, repr=False)
    spot_index_at: Dict[int, int] = field(init=False, repr=False)
    road_cells: List[int] = field(init=False, repr=False)
    brands: List[int] = field(init=False, repr=False)
    spots_by_brand: Dict[int, List[int]] = field(init=False, repr=False)

    def __post_init__(self):
        self.n_cells = self.width * self.height
        self.n_agents = len(self.agent_starts)
        self.n_days = len(self.day_steps)
        self.neighbors = build_neighbor_table(self.width, self.height)
        self.dir_index = build_direction_index(self.neighbors)
        self.spot_index_at = {s.pos: i for i, s in enumerate(self.spots)}
        self.road_cells = [c for c in range(self.n_cells) if self.cells[c] == rules.ROAD]
        by_brand: Dict[int, List[int]] = {}
        for i, s in enumerate(self.spots):
            by_brand.setdefault(s.brand, []).append(i)
        self.spots_by_brand = by_brand
        self.brands = sorted(by_brand)

    def terrain(self, pos: int) -> int:
        return self.cells[pos]

    def steps_of_day(self, day: int) -> int:
        return self.day_steps[day] if 0 <= day < self.n_days else self.day_steps[-1]

    def seconds_of_day(self, day: int) -> float:
        if not self.day_seconds:
            return 5.0
        return float(self.day_seconds[day] if 0 <= day < len(self.day_seconds) else self.day_seconds[-1])

    @staticmethod
    def from_json(obj: dict, warn: Optional[Callable[[str], None]] = None) -> "MatchConfig":
        """Đọc format setup của server (PTIT / định dạng BTC).

        Khoan dung với tên trường vì định dạng dây của BTC chưa chắc chắn, nhưng
        mọi trường mang luật chơi mà phải lấy mặc định đều được báo ra `warn`:
        đọc sai `players` hay ngưỡng giao thông làm hỏng toàn bộ dự báo.
        """
        warn = warn or (lambda msg: None)
        try:
            m = obj.get("map") if isinstance(obj.get("map"), dict) else obj
            cells = _pick(m, "cells")
            if cells and isinstance(cells[0], list):
                flat = [v for row in cells for v in row]
            else:
                flat = list(cells)
            width = int(_pick(m, "width", "w"))
            spots = [
                Spot(int(_pick(s, "brand", "type")), _as_pos(_pick(s, "pos", "position", "cell"), width),
                     int(s.get("stocks", 1)))
                for s in obj.get("spots", []) or []
            ]

            def rule_param(name: str, *aliases, default: int) -> int:
                value = _pick(obj, name, *aliases, default=None)
                if value is None:
                    warn(f"setup thiếu {name!r}, tạm dùng {default} — dự báo giao thông sẽ lệch")
                    return default
                return int(value)

            return MatchConfig(
                width=width,
                height=int(_pick(m, "height", "h")),
                cells=flat,
                spots=spots,
                agent_starts=[_as_pos(p, width) for p in _pick(obj, "agents", "agentStarts", "agent_starts")],
                fuel_limits=int(_pick(obj, "fuelLimits", "fuel_limits", "fuelLimit")),
                day_steps=[int(v) for v in _pick(obj, "daySteps", "day_steps")],
                day_seconds=[int(v) for v in _pick(obj, "daySeconds", "day_seconds", default=[])],
                players=rule_param("players", "nPlayers", "teams", default=1),
                busy_threshold=rule_param("busyThreshold", "busy_threshold", default=1),
                jammed_threshold=rule_param("jammedThreshold", "jammed_threshold", default=2),
                starts_at=float(_pick(obj, "startsAt", "starts_at", default=0) or 0),
            )
        except ProtocolError:
            raise
        except Exception as exc:
            raise ProtocolError(f"setup không đọc được: {exc!r}") from exc

    @staticmethod
    def from_problem_json(obj: dict, players: Optional[int] = None) -> "MatchConfig":
        """Đọc format cấu hình của server giả lập Nhật: {"problem": {...}, "teams": [...]}"""
        p = obj["problem"] if "problem" in obj else obj
        teams = obj.get("teams") or []
        cells = p["cells"]
        flat = [v for row in cells for v in row] if cells and isinstance(cells[0], list) else list(cells)
        spots = [Spot(int(s["brand"]), int(s["pos"]), int(s.get("stocks", 1))) for s in p.get("spots", [])]
        return MatchConfig(
            width=int(p["width"]),
            height=int(p["height"]),
            cells=flat,
            spots=spots,
            agent_starts=[int(x) for x in p["agentStarts"]],
            fuel_limits=int(p["fuelLimits"]),
            day_steps=[int(v) for v in p["daySteps"]],
            day_seconds=[int(v) for v in p.get("daySeconds", [])],
            players=int(players if players is not None else (len(teams) or 1)),
            busy_threshold=int(p.get("busyThreshold", 1)),
            jammed_threshold=int(p.get("jammedThreshold", 2)),
        )

    def to_json(self) -> dict:
        rows = [self.cells[r * self.width:(r + 1) * self.width] for r in range(self.height)]
        return {
            "startsAt": self.starts_at,
            "daySeconds": list(self.day_seconds),
            "daySteps": list(self.day_steps),
            "map": {"width": self.width, "height": self.height, "cells": rows},
            "spots": [{"brand": s.brand, "pos": s.pos, "stocks": s.stocks} for s in self.spots],
            "agents": list(self.agent_starts),
            "fuelLimits": self.fuel_limits,
            "players": self.players,
            "busyThreshold": self.busy_threshold,
            "jammedThreshold": self.jammed_threshold,
        }


@dataclass
class AgentState:
    kind: int          # rules.PATROL / rules.REFUEL
    pos: int
    fuel: int


@dataclass
class DayState:
    day: int                             # 0-based
    agents: List[AgentState]
    road_status: Dict[int, int]          # ô đường -> 0/1/2 (ô vắng mặt coi là thông thoáng)
    others: List[List[AgentState]] = field(default_factory=list)
    ends_at: Optional[float] = None      # UNIX time hạn chót, nếu server cấp
    steps: Optional[int] = None          # số bước của ngày, nếu server báo (/state steps_today)

    @staticmethod
    def from_json(obj: dict, kinds: Optional[Sequence[int]] = None,
                  width: Optional[int] = None,
                  fuel_limits: Optional[int] = None) -> "DayState":
        """Đọc trạng thái ngày.

        `fuel_limits` là mặc định khi server ẩn nhiên liệu của chính ta: đọc
        thành 0 sẽ cắt mọi xe về lệnh đợi cho tới hết trận, nên bình đầy là
        mặc định duy nhất an toàn — bài sai còn bị từ chối, bài đứng yên thì không.
        """
        try:
            default_fuel = fuel_limits if fuel_limits is not None else 0
            agents = []
            for i, a in enumerate(obj.get("agents", []) or []):
                kind = a.get("kind")
                if kind is None:
                    kind = kinds[i] if kinds is not None and i < len(kinds) else rules.PATROL
                fuel = a.get("fuel")
                agents.append(AgentState(
                    int(kind),
                    _as_pos(_pick(a, "pos", "position", "cell"), width),
                    int(fuel) if fuel is not None else default_fuel,
                ))
            others = []
            for team in obj.get("others", []) or []:
                team_agents = []
                for a in team.get("agents", []) or []:
                    fuel = a.get("fuel")
                    team_agents.append(AgentState(
                        int(a.get("kind", rules.PATROL)),
                        _as_pos(_pick(a, "pos", "position", "cell"), width),
                        int(fuel) if fuel is not None else -1,
                    ))
                others.append(team_agents)
            status = {}
            for t in obj.get("traffics", []) or []:
                status[_as_pos(_pick(t, "pos", "position", "cell"), width)] = int(_pick(t, "status", default=0))
            ends_at = _pick(obj, "endsAt", "ends_at", default=None)
            return DayState(
                day=int(_pick(obj, "day", "dayIndex", "currentDay", default=0)),
                agents=agents,
                road_status=status,
                others=others,
                ends_at=float(ends_at) if ends_at is not None else None,
            )
        except ProtocolError:
            raise
        except Exception as exc:
            raise ProtocolError(f"trạng thái ngày không đọc được: {exc!r}") from exc


@dataclass
class Score:
    """Bộ điểm từ điển: (số loại cả trận, tổng loại theo ngày, tổng phần)."""
    types: int = 0
    daily_types: int = 0
    servings: int = 0

    def as_tuple(self):
        return (self.types, self.daily_types, self.servings)

    def __lt__(self, other):
        return self.as_tuple() < other.as_tuple()

    def __str__(self):
        return f"L={self.types} D={self.daily_types} Q={self.servings}"


@dataclass
class Ack:
    ok: bool
    reason: Optional[str] = None
    raw: Optional[dict] = None
