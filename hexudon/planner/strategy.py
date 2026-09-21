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
DEFAULT_ROLE_SECONDS = 3.0


class MatchStrategist:
    """Một trận, một đối tượng. Không biết gì về mạng."""

    def __init__(
        self,
        config: MatchConfig,
        weights: Optional[Weights] = None,
        search_seconds: Optional[float] = DEFAULT_SEARCH_SECONDS,
        net_margin: float = DEFAULT_NET_MARGIN,
        role_seconds: Optional[float] = DEFAULT_ROLE_SECONDS,
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

    def select_roles(self, budget_seconds: Optional[float] = None,
                     cap_by_start: bool = True) -> List[int]:
        """Chọn vai trò, nhưng không bao giờ lấn sang ngày thi đầu tiên.

        `startsAt` của server là mốc trận mở. Chạy quá mốc đó thì ngày 0 (và có
        khi ngày 1, 2) trôi qua trong lúc ta còn đang thử phương án — đã đo được
        mất trọn 3 ngày với --role-seconds 20 trên map 16x16.

        `cap_by_start=False` khi kênh truyền đã tự tính hạn của cửa sổ chọn xe
        (server web báo selection_deadline_in): khi đó startsAt không còn là mốc
        đúng — với practice nó nằm ở quá khứ và từng làm ngân sách về 0.
        """
        budget = self.role_seconds if budget_seconds is None else budget_seconds
        if budget is None:
            budget = DEFAULT_ROLE_SECONDS
        if cap_by_start:
            if self.config.starts_at:
                cap = self.config.starts_at - time.time() - self.net_margin
                why = "trận mở"
            else:
                # không có startsAt nghĩa là trận có thể ĐANG chạy: mỗi giây ở đây là
                # một giây ăn trộm của ngày 0, nên chặn theo độ dài ngày 0
                cap = self.config.seconds_of_day(0) - self.net_margin - (self.search_seconds or 0.0)
                why = "ngày 0 có thể đã mở"
            if cap < budget:
                self.log(f"{why}: rút ngân sách chọn vai trò {budget:.1f}s -> {max(0.0, cap):.1f}s")
                budget = max(0.0, cap)
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
        """Ngân sách tìm kiếm, chừa lề cho mạng.

        `search_seconds=None` là tự động: dùng độ dài ngày BTC cấu hình
        (daySeconds) trừ lề, để practice tự nhịp cũng chạy đúng nhịp thi thật;
        trận có giờ còn bị chặn theo hạn chót thật ngay bên dưới.
        """
        budget = self.search_seconds
        if budget is None:
            budget = self.config.seconds_of_day(state.day) - self.net_margin
        if state.ends_at is not None:
            budget = min(budget, state.ends_at - time.time() - self.net_margin)
        return max(0.0, budget)

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
        # vai trò server báo mới là sự thật: lập kế hoạch theo vai trò ta MONG
        # muốn trong khi server nghĩ khác sẽ làm mọi bài bị từ chối vì E_NO_FUEL
        agents = [AgentState(a.kind, a.pos, a.fuel) for a in state.agents]
        server_kinds = [a.kind for a in state.agents]
        if server_kinds and server_kinds != self.kinds[:len(server_kinds)]:
            self.log(f"lệch vai trò: ta {self.kinds}, server {server_kinds}; theo server")
            self.kinds = list(server_kinds)
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
        new_brands = result.brands - self.visited_brands   # phải tính TRƯỚC khi gộp
        self.visited_brands |= result.brands
        self.daily_types_total += len(result.brands)
        self.servings_total += result.servings
        opponents = [a.pos for team in state.others for a in team]
        self.history.append({
            "day": state.day,
            "types": len(result.brands),
            "new_types": len(new_brands),
            "servings": result.servings,
            "score": decoded.score,
            "truncated": decoded.truncated,
        })
        # phải giữ HÔM QUA trước khi ghi đè bằng HÔM NAY, nếu không công thức
        # hai ngày của luật biến thành "hôm nay tính hai lần"
        yesterday = self.status_yesterday
        self.status_yesterday = status
        self.forecast_next = self.forecast.predict(
            status,
            yesterday,
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
