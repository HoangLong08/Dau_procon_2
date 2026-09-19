"""Mô hình dữ liệu và đọc/ghi JSON theo format chính thức."""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from . import rules
from .geometry import build_direction_index, build_neighbor_table


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
    def from_json(obj: dict) -> "MatchConfig":
        """Đọc format setup của server (PTIT / định dạng BTC)."""
        m = obj["map"]
        cells = m["cells"]
        if cells and isinstance(cells[0], list):
            flat = [v for row in cells for v in row]
        else:
            flat = list(cells)
        spots = [Spot(int(s["brand"]), int(s["pos"]), int(s.get("stocks", 1))) for s in obj.get("spots", [])]
        return MatchConfig(
            width=int(m["width"]),
            height=int(m["height"]),
            cells=flat,
            spots=spots,
            agent_starts=[int(p) for p in obj["agents"]],
            fuel_limits=int(obj["fuelLimits"]),
            day_steps=[int(v) for v in obj["daySteps"]],
            day_seconds=[int(v) for v in obj.get("daySeconds", [])],
            players=int(obj.get("players", 1)),
            busy_threshold=int(obj.get("busyThreshold", 1)),
            jammed_threshold=int(obj.get("jammedThreshold", 2)),
            starts_at=float(obj.get("startsAt", 0)),
        )

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

    @staticmethod
    def from_json(obj: dict, kinds: Optional[Sequence[int]] = None) -> "DayState":
        agents = []
        for i, a in enumerate(obj.get("agents", [])):
            kind = a.get("kind")
            if kind is None:
                kind = kinds[i] if kinds is not None and i < len(kinds) else rules.PATROL
            fuel = a.get("fuel")
            agents.append(AgentState(int(kind), int(a["pos"]), int(fuel) if fuel is not None else 0))
        others = []
        for team in obj.get("others", []) or []:
            team_agents = []
            for a in team.get("agents", []) or []:
                fuel = a.get("fuel")
                team_agents.append(
                    AgentState(int(a.get("kind", rules.PATROL)), int(a["pos"]), int(fuel) if fuel is not None else -1)
                )
            others.append(team_agents)
        status = {}
        for t in obj.get("traffics", []) or []:
            status[int(t["pos"])] = int(t["status"])
        return DayState(
            day=int(obj.get("day", 0)),
            agents=agents,
            road_status=status,
            others=others,
            ends_at=float(obj["endsAt"]) if obj.get("endsAt") is not None else None,
        )


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
