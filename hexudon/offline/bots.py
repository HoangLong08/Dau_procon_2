"""Các bot dùng để luyện offline: bot của ta, baseline, và bot mẫu."""

import random
import time
from collections import deque
from typing import Dict, List, Optional, Sequence

from .. import rules
from ..model import AgentState, DayState, MatchConfig
from ..planner.decoder import decode
from ..planner.construct import construct
from ..planner.engine import make_context, plan_day
from ..planner.solution import Weights
from ..planner.strategy import MatchStrategist
from ..traffic import TrafficForecast


class Bot:
    """Giao diện chung cho runner offline."""

    name = "bot"

    def select_roles(self) -> List[int]:
        raise NotImplementedError

    def plan(self, state: DayState) -> List[List[int]]:
        raise NotImplementedError

    def observe(self, state: DayState, result) -> None:
        pass


class SearchBot(Bot):
    """Bot thi đấu: chọn vai trò bằng rollout rồi ALNS mỗi ngày."""

    name = "search"

    def __init__(self, config: MatchConfig, search_seconds: float = 1.0,
                 role_seconds: float = 5.0, seed: int = 0, weights: Optional[Weights] = None,
                 kinds: Optional[Sequence[int]] = None):
        self.config = config
        self.seed = seed
        self.fixed_kinds = list(kinds) if kinds is not None else None
        self.role_seconds = role_seconds
        self.strategist = MatchStrategist(
            config, weights=weights, search_seconds=search_seconds,
            net_margin=0.0, role_seconds=role_seconds, seed=seed,
        )

    def select_roles(self) -> List[int]:
        if self.fixed_kinds is not None:
            self.strategist.kinds = list(self.fixed_kinds)
            return list(self.fixed_kinds)
        return self.strategist.select_roles()

    def plan(self, state: DayState) -> List[List[int]]:
        decoded = self.strategist.plan_day(state)
        return decoded.plan


class GreedyBot(Bot):
    """Baseline: chèn tham lam một lần, không tìm kiếm. Cũng là bài dự phòng."""

    name = "greedy"

    def __init__(self, config: MatchConfig, seed: int = 0, n_cars: int = 1,
                 weights: Optional[Weights] = None):
        self.config = config
        self.seed = seed
        self.n_cars = min(n_cars, max(0, config.n_agents - 2))
        self.weights = weights or Weights()
        self.visited: set = set()
        self.kinds: List[int] = []
        self.forecast = TrafficForecast(config, n_patrols=config.n_agents - self.n_cars)

    def select_roles(self) -> List[int]:
        kinds = [rules.PATROL] * self.config.n_agents
        for i in range(self.n_cars):
            kinds[-(i + 1)] = rules.REFUEL
        self.kinds = kinds
        return kinds

    def plan(self, state: DayState) -> List[List[int]]:
        cfg = self.config
        agents = [AgentState(self.kinds[i], a.pos, a.fuel) for i, a in enumerate(state.agents)]
        ctx = make_context(
            cfg, state.day, agents, dict(state.road_status),
            [s.stocks for s in cfg.spots], self.visited, weights=self.weights,
            forecast_status=self.forecast.static_guess(state.day + 1),
        )
        decoded = decode(ctx, construct(ctx, random.Random(self.seed + state.day)))
        self.visited |= decoded.result.brands
        return decoded.plan


class SampleBot(Bot):
    """Mô phỏng bot mẫu của ban tổ chức: BFS theo số ô, bỏ qua chi phí địa hình.

    Dùng làm đối thủ 'đội yếu' để tái hiện giao thông thực tế trong benchmark.
    """

    name = "sample"

    def __init__(self, config: MatchConfig, seed: int = 0):
        self.config = config
        self.seed = seed
        self.kinds: List[int] = []

    def select_roles(self) -> List[int]:
        n = self.config.n_agents
        kinds = [rules.PATROL] * n
        if n > 1:
            kinds[-1] = rules.REFUEL
        self.kinds = kinds
        return kinds

    def _bfs(self, src: int, targets: set) -> List[int]:
        cfg = self.config
        prev = {src: -1}
        q = deque([src])
        while q:
            c = q.popleft()
            if c != src and c in targets:
                path = []
                while c != src:
                    path.append(c)
                    c = prev[c]
                return path[::-1]
            for nb in cfg.neighbors[c]:
                if nb < 0 or nb in prev or cfg.cells[nb] == rules.POND:
                    continue
                prev[nb] = c
                q.append(nb)
        return []

    def plan(self, state: DayState) -> List[List[int]]:
        cfg = self.config
        steps = cfg.steps_of_day(state.day)
        status = dict(state.road_status)
        targets = {s.pos for s in cfg.spots}
        plan = []
        for i, a in enumerate(state.agents):
            kind = self.kinds[i] if i < len(self.kinds) else a.kind
            if kind != rules.PATROL:
                plan.append([-steps])
                continue
            path = self._bfs(a.pos, targets)
            seq: List[int] = []
            cur = a.pos
            used = 0
            fuel = a.fuel
            for nxt in path:
                terrain = cfg.cells[cur]
                cost = rules.STEP_ROAD[status.get(cur, 0)] if terrain == rules.ROAD else rules.step_cost(terrain)
                need = rules.fuel_cost(terrain)
                if cost <= 0 or used + cost > steps or fuel < need:
                    break
                d = cfg.dir_index[cur].get(nxt)
                if d is None:
                    break
                seq.append(d)
                used += cost
                fuel -= need
                cur = nxt
            if used < steps:
                seq.append(-(steps - used))
            plan.append(seq if seq else [-steps])
        return plan


class WaitBot(Bot):
    """Đứng yên cả trận. Dùng làm mốc dưới."""

    name = "wait"

    def __init__(self, config: MatchConfig, seed: int = 0):
        self.config = config

    def select_roles(self) -> List[int]:
        return [rules.PATROL] * self.config.n_agents

    def plan(self, state: DayState) -> List[List[int]]:
        steps = self.config.steps_of_day(state.day)
        return [[-steps] for _ in state.agents]


BOT_TYPES = {
    "search": SearchBot,
    "greedy": GreedyBot,
    "sample": SampleBot,
    "wait": WaitBot,
}
