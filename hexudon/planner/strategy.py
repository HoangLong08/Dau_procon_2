"""Bộ nhớ trận: giữ trạng thái xuyên ngày và điều phối planner trong ngân sách."""

import time
from typing import Callable, Dict, List, Optional, Sequence, Set

from .. import rules
from ..model import AgentState, DayState, MatchConfig
from ..simulator import run_day, wait_plan
from ..traffic import TrafficForecast, counts_from_positions
from .decoder import Decoded
from .engine import make_context, plan_day
from .objective import lexicographic_gain
from .roles import select_roles
from .solution import Weights

DEFAULT_SEARCH_SECONDS = 3.0
DEFAULT_NET_MARGIN = 1.5
DEFAULT_ROLE_SECONDS = 3.0
#: số lần gửi tối đa mỗi ngày: bài an toàn, một bài giữa chừng, bài cuối (cases 11.5)
MAX_SUBMITS_PER_DAY = 3
#: chỉ gửi lại khi L, D hoặc Q hôm nay tăng, hoặc điểm tăng ít nhất ngần này × B:
#: thời gian phản hồi tính theo bài hợp lệ cuối cùng, nên gửi lại vì một chênh
#: lệch nhỏ hơn chỉ làm chậm thêm (cases 11.2, 11.8)
RESUBMIT_MIN_GAIN = 0.25


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
        emit: Optional[Callable[[str, dict], None]] = None,
        search_iterations: Optional[int] = None,
        role_iterations: Optional[int] = None,
    ):
        self.config = config
        self.weights = weights or Weights()
        self.search_seconds = search_seconds
        #: benchmark offline: ngân sách theo SỐ VÒNG ALNS thay vì giây, để hai lần chạy
        #: (hay hai tiến trình song song) ra đúng cùng kết quả. Khi thi để None.
        self.search_iterations = search_iterations
        self.role_iterations = role_iterations
        self.net_margin = net_margin
        self.role_seconds = role_seconds
        self.seed = seed
        self.log = log or (lambda msg: None)
        #: web trực quan hoá; None thì không phát gì và không tốn thêm thời gian
        self.emit = emit

        self.kinds: List[int] = [rules.PATROL] * config.n_agents
        self.visited_brands: Set[int] = set()
        self.daily_types_total = 0
        self.servings_total = 0
        self.history: List[dict] = []
        self.status_yesterday: Optional[Dict[int, int]] = None
        #: stay-step của chính đội hôm qua: một nửa cửa sổ hai ngày của luật (cases 2, 8.13)
        self.own_stay_yesterday: Optional[Dict[int, int]] = None
        self.forecast = TrafficForecast(config, n_patrols=max(1, config.n_agents - 1))
        self.predicted_state: Optional[List[AgentState]] = None
        #: bài cuối cùng submit đã nhận trong lần plan_day gần nhất
        self.last_sent: Optional[Decoded] = None

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
        if cap_by_start and self.role_iterations is None:
            if self.config.starts_at:
                cap = self.config.starts_at - time.time() - self.net_margin
                why = "trận mở"
            else:
                # không có startsAt nghĩa là trận có thể ĐANG chạy: mỗi giây ở đây là
                # một giây ăn trộm của ngày 0, nên chặn theo độ dài ngày 0
                # search_seconds=None (tự động) vẫn phải trừ đúng ngân sách search sẽ dùng
                search = (self.search_seconds if self.search_seconds is not None
                         else self.config.seconds_of_day(0) - self.net_margin)
                cap = self.config.seconds_of_day(0) - self.net_margin - search
                why = "ngày 0 có thể đã mở"
            if cap < budget:
                self.log(f"{why}: rút ngân sách chọn vai trò {budget:.1f}s -> {max(0.0, cap):.1f}s")
                budget = max(0.0, cap)
        emit = self.emit
        extra = {}
        if emit is not None:
            extra["on_candidate"] = lambda row: emit("roles_candidate", row)
        if self.role_iterations is not None:      # chỉ benchmark offline; khi thi lời gọi như cũ
            extra["iterations_per_day"] = self.role_iterations
        kinds, options = select_roles(
            self.config, budget_seconds=budget, weights=self.weights, seed=self.seed, log=self.log, **extra
        )
        self.kinds = kinds
        self.forecast = TrafficForecast(
            self.config,
            n_patrols=max(1, sum(1 for k in kinds if k == rules.PATROL)),
            pop=self.forecast.pop,
        )
        n_cars = sum(1 for k in kinds if k == rules.REFUEL)
        self.log(f"chọn vai trò: {n_cars} xe tiếp nhiên liệu / {len(kinds)} agent -> {kinds}")
        if emit is not None:
            emit("roles_chosen", {"kinds": list(kinds)})
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
        record: bool = True,
    ) -> Decoded:
        """Lập kế hoạch cho một ngày; gọi submit ngay khi có bài an toàn.

        Trả về bài đang được áp dụng: bài cuối cùng submit nhận (cases 11.3), hoặc
        bài tốt nhất khi không có submit. `record=False` khi bên gọi tự ghi bộ nhớ
        bằng record_applied, sau khi biết chắc server đã nhận bài nào.
        """
        cfg = self.config
        started = time.monotonic()
        self.reconcile(state)
        # vai trò server báo mới là sự thật: lập kế hoạch theo vai trò ta MONG
        # muốn trong khi server nghĩ khác sẽ làm mọi bài bị từ chối vì E_NO_FUEL
        agents = [AgentState(a.kind, a.pos, a.fuel) for a in state.agents]
        server_kinds = [a.kind for a in state.agents]
        if server_kinds and server_kinds != self.kinds[:len(server_kinds)]:
            self.log(f"lệch vai trò: ta {self.kinds}, server {server_kinds}; theo server")
            self.kinds = list(server_kinds)
        status = dict(state.road_status)
        forecast_status = self.forecast.predict(
            status, self.status_yesterday, own_stay_yesterday=self.own_stay_yesterday,
            floor_today=state.day == 1,
        )
        ctx = make_context(
            cfg, state.day, agents, status, [s.stocks for s in cfg.spots],
            self.visited_brands, weights=self.weights, forecast_status=forecast_status,
            steps_override=steps_override,
        )
        # theo số vòng thì chỉ chặn giờ khi server có hạn thật (phòng hờ)
        if deadline is None and (self.search_iterations is None or state.ends_at is not None):
            deadline = time.monotonic() + self.budget_for(state)
        max_iterations = self.search_iterations if self.search_iterations is not None else 1_000_000

        sent = {"count": 0, "decoded": None, "gain": None}
        self.last_sent = None
        on_safe, hooks = self._watch(state, agents, status, ctx.steps, started)

        def offer(decoded: Decoded, final: bool) -> None:
            """Gửi nếu đáng: tối đa MAX_SUBMITS_PER_DAY lần, luôn chừa một lần cho bài cuối."""
            if submit is None:
                return
            limit = MAX_SUBMITS_PER_DAY if final else MAX_SUBMITS_PER_DAY - 1
            if sent["count"] >= limit:
                return
            gain = lexicographic_gain(ctx, decoded.result)
            last = sent["decoded"]
            if last is not None and not (
                    gain > sent["gain"]
                    or decoded.score >= last.score + RESUBMIT_MIN_GAIN * ctx.weights.B):
                return
            sent["count"] += 1
            if submit(decoded.plan):
                sent["decoded"] = decoded
                sent["gain"] = gain
                self.last_sent = decoded

        def on_plan(decoded: Decoded):
            nonlocal on_safe
            if on_safe is not None:
                on_safe(decoded)
                on_safe = None
            offer(decoded, final=True)

        best = plan_day(ctx, deadline=deadline, seed=self.seed, on_plan=on_plan,
                        on_improve=lambda decoded: offer(decoded, final=False),
                        max_iterations=max_iterations, **hooks)
        applied = best if submit is None or sent["decoded"] is None else sent["decoded"]
        if record:
            self.record(state, applied, status)
        return applied

    def _watch(self, state: DayState, agents: List[AgentState], status: Dict[int, int],
               steps: int, started: float):
        """Móc quan sát cho web: (hàm cho bài an toàn, {on_better, on_progress}).

        Không có emit thì (None, {}): planner chạy đúng như cũ.
        """
        emit = self.emit
        if emit is None:
            return None, {}
        from ..viz.shapes import simulate

        stocks = [s.stocks for s in self.config.spots]

        def describe(decoded: Decoded) -> dict:
            _, trace = simulate(self.config, agents, decoded.plan, steps, stocks, status)
            return {"day": state.day, "elapsed": round(time.monotonic() - started, 3),
                    "score": round(decoded.score, 3), "types": len(decoded.result.brands),
                    "servings": decoded.result.servings, "trace": trace}

        def on_safe(decoded: Decoded) -> None:
            row = describe(decoded)
            row["ms"] = round(row.pop("elapsed") * 1000, 1)
            emit("plan_safe", row)

        return on_safe, {
            "on_better": lambda decoded: emit("alns_better", describe(decoded)),
            "on_progress": lambda row: emit("alns_progress", dict(
                row, day=state.day, elapsed=round(time.monotonic() - started, 3))),
        }

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
            own_stay_yesterday=self.own_stay_yesterday,
            opponents_parked=counts_from_positions(opponents, self.config.road_cells),
            steps_today=state.steps or self.config.steps_of_day(state.day),
            floor_today=state.day == 1,
        )
        self.own_stay_yesterday = dict(result.stay)
        if self.emit is not None and state.day + 1 < self.config.n_days:
            from ..viz.shapes import status_payload
            self.emit("forecast", {"day": state.day + 1, "status": status_payload(self.forecast_next)})
        self.predicted_state = [
            AgentState(self.kinds[i], result.end_pos[i], result.end_fuel[i])
            for i in range(self.config.n_agents)
        ]

    def record_applied(self, state: DayState, plan: Optional[List[List[int]]], steps: int,
                       known: Sequence[Optional[Decoded]] = ()) -> Decoded:
        """Ghi vào bộ nhớ trận đúng bài server áp dụng cho ngày này.

        Server áp dụng bài hợp lệ cuối cùng nó nhận (cases 11.3); không nhận bài nào
        thì mọi xe đứng yên cả ngày (Formats, cases 11.4). Kết quả luôn được mô phỏng
        lại từ trạng thái đầu ngày; điểm lấy từ bài trong `known` nếu trùng kế hoạch.
        """
        status = dict(state.road_status)
        agents = [AgentState(a.kind, a.pos, a.fuel) for a in state.agents]
        stocks = [s.stocks for s in self.config.spots]
        if plan is None:
            plan = wait_plan(len(agents), steps)
        result = run_day(self.config, steps, agents, plan, stocks, status)
        if not result.ok:          # bài server đã nhận thì không thể sai; phòng hờ
            plan = wait_plan(len(agents), steps)
            result = run_day(self.config, steps, agents, plan, stocks, status)
        score, truncated = 0.0, 0
        for decoded in known:
            if decoded is not None and decoded.plan == plan:
                score, truncated = decoded.score, decoded.truncated
                break
        applied = Decoded(plan=plan, result=result, score=score, truncated=truncated)
        self.record(state, applied, status)
        return applied

    def score_tuple(self):
        return (len(self.visited_brands), self.daily_types_total, self.servings_total)
