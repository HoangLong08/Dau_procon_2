"""Bộ nhớ trận: giữ trạng thái xuyên ngày và điều phối planner trong ngân sách."""

import time
from typing import Callable, Dict, List, Optional, Sequence, Set

from .. import rules
from ..model import AgentState, DayState, MatchConfig
from ..traffic import TrafficForecast, counts_from_positions
from .decoder import Decoded
from .engine import make_context, plan_day
from .roles import select_roles
from .solution import Weights

DEFAULT_SEARCH_SECONDS = 3.0
DEFAULT_NET_MARGIN = 1.5
DEFAULT_ROLE_SECONDS = 20.0


class MatchStrategist:
    """Một trận, một đối tượng. Không biết gì về mạng."""

    def __init__(
        self,
        config: MatchConfig,
        weights: Optional[Weights] = None,
        search_seconds: float = DEFAULT_SEARCH_SECONDS,
        net_margin: float = DEFAULT_NET_MARGIN,
        role_seconds: float = DEFAULT_ROLE_SECONDS,
        seed: int = 0,
        log: Optional[Callable[[str], None]] = None,
    ):
        self.config = config
        self.weights = weights or Weights()
        self.search_seconds = search_seconds
        self.net_margin = net_margin
        self.role_seconds = role_seconds
        self.seed = seed
        self.log = log or (lambda msg: None)

        self.kinds: List[int] = [rules.PATROL] * config.n_agents
        self.visited_brands: Set[int] = set()
        self.daily_types_total = 0
        self.servings_total = 0
        self.history: List[dict] = []
        self.status_yesterday: Optional[Dict[int, int]] = None
        self.forecast = TrafficForecast(config, n_patrols=max(1, config.n_agents - 1))
        self.predicted_state: Optional[List[AgentState]] = None

    # ---- trước trận ----

    def select_roles(self, budget_seconds: Optional[float] = None) -> List[int]:
        budget = self.role_seconds if budget_seconds is None else budget_seconds
        kinds, options = select_roles(
            self.config, budget_seconds=budget, weights=self.weights, seed=self.seed, log=self.log
        )
        self.kinds = kinds
        self.forecast = TrafficForecast(
            self.config,
            n_patrols=max(1, sum(1 for k in kinds if k == rules.PATROL)),
            pop=self.forecast.pop,
        )
        n_cars = sum(1 for k in kinds if k == rules.REFUEL)
        self.log(f"chọn vai trò: {n_cars} xe tiếp nhiên liệu / {len(kinds)} agent -> {kinds}")
        return kinds

    # ---- mỗi ngày ----

    def budget_for(self, state: DayState, now: Optional[float] = None) -> float:
        """Ngân sách tìm kiếm, chừa lề cho mạng."""
        budget = self.search_seconds
        if state.ends_at is not None:
            remaining = state.ends_at - time.time() - self.net_margin
            budget = min(budget, max(0.0, remaining))
        return budget

    def reconcile(self, state: DayState) -> None:
        """Đối chiếu trạng thái server với dự đoán; server luôn thắng."""
        if self.predicted_state is None:
            return
        for i, (pred, real) in enumerate(zip(self.predicted_state, state.agents)):
            if pred.pos != real.pos:
                self.log(f"lệch vị trí agent {i}: dự đoán {pred.pos}, server {real.pos}")
            elif real.fuel is not None and pred.fuel != real.fuel and real.kind == rules.PATROL:
                self.log(f"lệch xăng agent {i}: dự đoán {pred.fuel}, server {real.fuel}")

    def plan_day(
        self,
        state: DayState,
        deadline: Optional[float] = None,
        submit: Optional[Callable[[List[List[int]]], bool]] = None,
        steps_override: Optional[int] = None,
    ) -> Decoded:
        """Lập kế hoạch cho một ngày; gọi submit ngay khi có bài an toàn."""
        cfg = self.config
        self.reconcile(state)
        agents = [
            AgentState(self.kinds[i] if i < len(self.kinds) else a.kind, a.pos, a.fuel)
            for i, a in enumerate(state.agents)
        ]
        status = dict(state.road_status)
        forecast_status = self.forecast.predict(status, self.status_yesterday)
        ctx = make_context(
            cfg, state.day, agents, status, [s.stocks for s in cfg.spots],
            self.visited_brands, weights=self.weights, forecast_status=forecast_status,
            steps_override=steps_override,
        )
        if deadline is None:
            deadline = time.monotonic() + self.budget_for(state)

        sent = {"score": None}

        def on_plan(decoded: Decoded):
            if submit is None:
                return
            if sent["score"] is not None and decoded.score <= sent["score"]:
                return
            if submit(decoded.plan):
                sent["score"] = decoded.score

        best = plan_day(ctx, deadline=deadline, seed=self.seed, on_plan=on_plan)
        self.record(state, best, status)
        return best

    def record(self, state: DayState, decoded: Decoded, status: Dict[int, int]) -> None:
        """Ghi nhớ kết quả của bài đã gửi để dùng cho ngày sau."""
        result = decoded.result
        self.visited_brands |= result.brands
        self.daily_types_total += len(result.brands)
        self.servings_total += result.servings
        opponents = [a.pos for team in state.others for a in team]
        self.history.append({
            "day": state.day,
            "types": len(result.brands),
            "new_types": len(result.brands - (self.visited_brands - result.brands)),
            "servings": result.servings,
            "score": decoded.score,
            "truncated": decoded.truncated,
        })
        self.status_yesterday = status
        self.forecast_next = self.forecast.predict(
            status,
            self.status_yesterday,
            own_stay_today=result.stay,
            opponents_parked=counts_from_positions(opponents, self.config.road_cells),
            steps_today=self.config.steps_of_day(state.day),
        )
        self.predicted_state = [
            AgentState(self.kinds[i], result.end_pos[i], result.end_fuel[i])
            for i in range(self.config.n_agents)
        ]

    def score_tuple(self):
        return (len(self.visited_brands), self.daily_types_total, self.servings_total)
