"""Bộ nhớ trận: giữ trạng thái xuyên ngày và điều phối planner trong ngân sách."""

import dataclasses
import os
import threading
import time
from typing import Callable, Dict, List, Optional, Sequence, Set

from .. import rules
from ..model import AgentState, DayState, MatchConfig
from ..simulator import run_day, wait_plan
from ..traffic import ForecastCalibrator, TrafficForecast, counts_from_positions, parked_stay
from .decoder import Decoded
from .engine import day_upper_bound, make_context, plan_day, today_hard_len
from .objective import lexicographic_gain
from .parallel import DayJob, SeedPool, run_job, total_of
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
#: R7: số seed mặc định khi để "auto": vừa đủ dùng lõi mà vẫn chạy được vài câu cùng lúc
AUTO_SEARCH_JOBS_MAX = 4
#: R7: tiến trình con phải trả lời ping trong ngần này giây, không thì bỏ Pool cả trận
POOL_PING_SECONDS = 20.0


def resolve_search_jobs(value: Optional[int]) -> int:
    """None ("auto") = min(4, số lõi // 2), ít nhất 1."""
    if value is None:
        return max(1, min(AUTO_SEARCH_JOBS_MAX, (os.cpu_count() or 2) // 2))
    return max(1, int(value))


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
        search_jobs: Optional[int] = 1,
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
        #: R7: số seed ALNS mỗi ngày (1 = như cũ). Theo giây: các seed phụ chạy trên Pool
        #: tiến trình giữ suốt trận; theo vòng: chạy tuần tự để kết quả tất định
        self.search_jobs = resolve_search_jobs(search_jobs)
        self._pool: Optional[SeedPool] = None
        self._pool_failed = False
        self._pool_thread: Optional[threading.Thread] = None
        self._pool_ready = threading.Event()
        self._closed = False
        #: giây ước lượng cho một lần chơi tiếp chấm bài cuối; đo lại mỗi khi phải chơi tiếp
        self._roll_seconds: Optional[float] = None

        self.kinds: List[int] = [rules.PATROL] * config.n_agents
        self.visited_brands: Set[int] = set()
        self.daily_types_total = 0
        self.servings_total = 0
        self.history: List[dict] = []
        self.status_yesterday: Optional[Dict[int, int]] = None
        #: stay-step của chính đội hôm qua: một nửa cửa sổ hai ngày của luật (cases 2, 8.13)
        self.own_stay_yesterday: Optional[Dict[int, int]] = None
        self.forecast = TrafficForecast(config, n_patrols=max(1, config.n_agents - 1))
        #: R6: chọn giả định lưu lượng theo đường thật mỗi sáng; None = dự báo cũ
        self.calibrator: Optional[ForecastCalibrator] = None
        #: vị trí xe đối thủ sáng hôm qua theo [đội][xe], để nhận ra xe đỗ (parked_stay)
        self.others_yesterday: Optional[List[List[int]]] = None
        #: lượt dừng của xe đối thủ thấy sáng nay (mô hình R6) và số xe (dự báo cũ)
        self.parked_today: Dict[int, int] = {}
        self._reset_forecast(self.forecast)
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
        # chơi thử cả trận từ ngày 0: chưa biết D có an toàn không, nên với soft_auto thì hạng
        # mục mềm theo B như ngày 0 thật (day_weights); để soft_unit cố định thì đổi cả cách
        # chia xe (đã đo: mất 1 chuỗi L ở bản đồ Nhật trận 4, sảnh L2, seed 2)
        role_weights = self.weights
        if role_weights.soft_auto > 0 and role_weights.soft_unit > 0:
            role_weights = dataclasses.replace(role_weights, soft_unit=0.0)
        if role_weights.or_seconds > 0:
            # Bước E: chơi thử hàng chục trận, MILP từng ngày quá chậm cho việc đó
            role_weights = dataclasses.replace(role_weights, or_seconds=0.0)
        kinds, options = select_roles(
            self.config, budget_seconds=budget, weights=role_weights, seed=self.seed, log=self.log, **extra
        )
        self.kinds = kinds
        self._reset_forecast(TrafficForecast(
            self.config,
            n_patrols=max(1, sum(1 for k in kinds if k == rules.PATROL)),
            pop=self.forecast.pop,
        ))
        n_cars = sum(1 for k in kinds if k == rules.REFUEL)
        self.log(f"chọn vai trò: {n_cars} xe tiếp nhiên liệu / {len(kinds)} agent -> {kinds}")
        self._ensure_pool()             # mở Pool trước ngày 0 để giấu thời gian khởi động
        if emit is not None:
            emit("roles_chosen", {"kinds": list(kinds)})
        return kinds

    def _ensure_pool(self) -> Optional[SeedPool]:
        """Pool cho các seed phụ (R7), sẵn sàng thì trả về, không thì None (không bao giờ chờ).

        Lần gọi đầu mở Pool trong luồng nền; Pool chỉ được dùng sau khi mọi tiến trình con
        trả lời ping. Chưa sẵn sàng thì ngày đó tìm một seed như cũ.
        """
        if (self.search_jobs <= 1 or self.search_iterations is not None or self._pool_failed
                or self._closed):
            return None
        if self._pool_thread is None:
            self._pool_thread = threading.Thread(target=self._open_pool, name="hexudon-pool", daemon=True)
            self._pool_thread.start()
        if not self._pool_ready.is_set():
            return None
        pool = self._pool
        if pool is None or not pool.ok:
            self._pool_failed = True
            return None
        return pool

    def _open_pool(self) -> None:
        pool = None
        try:
            pool = SeedPool(self.search_jobs - 1)
            if not pool.ping(POOL_PING_SECONDS):
                raise RuntimeError(f"tiến trình con không trả lời trong {POOL_PING_SECONDS:.0f}s")
        except Exception as exc:
            self._pool_failed = True
            if pool is not None:
                pool.close()
            self.log(f"không mở được tiến trình con ({exc!r}): tìm kiếm một seed")
            return
        if self._closed:
            pool.close()
            return
        self._pool = pool
        self._pool_ready.set()
        self.log(f"tìm kiếm song song: {self.search_jobs} seed mỗi ngày")

    def wait_pool(self, timeout: float) -> bool:
        """Chờ Pool sẵn sàng (test, công cụ); False nếu không cần, hỏng hoặc quá hạn."""
        self._ensure_pool()
        if self._pool_thread is None:
            return False
        self._pool_thread.join(timeout)
        return self._pool_ready.is_set() and not self._pool_failed

    def close(self) -> None:
        """Đóng Pool của R7 (gọi khi trận kết thúc). Luồng mở Pool còn chạy thì nó tự đóng."""
        self._closed = True
        if self._pool is not None:
            self._pool.close()
            self._pool = None

    def _reset_forecast(self, forecast: TrafficForecast) -> None:
        """Đặt dự báo của trận; R6 bật thì kèm calibrator mới, mô hình ban đầu là PRIOR."""
        self.forecast = forecast
        if self.weights.traffic_calib > 0:
            self.calibrator = ForecastCalibrator(forecast)
            forecast.model = self.calibrator.best()
        else:
            self.calibrator = None
            forecast.model = None

    # ---- mỗi ngày ----

    def _observe_morning(self, state: DayState, status: Dict[int, int], steps: int) -> None:
        """Xe đỗ của đối thủ sáng nay, và (R6) chấm lại các mô hình dự báo theo đường thật."""
        others = [[a.pos for a in team] for team in state.others]
        road = self.config.road_cells
        self.parked_today = parked_stay(self.others_yesterday, others, road, status, steps) if others else {}
        if self.calibrator is None:
            return
        before = self.forecast.model
        model = self.calibrator.morning(
            state.day, status, parked=self.parked_today, steps=steps,
            counts=counts_from_positions([p for team in others for p in team], road))
        self.forecast.model = model
        if model != before:
            self.log(f"ngày {state.day + 1}: dự báo giao thông theo mô hình \"{model.label()}\"")

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
        steps_today = steps_override or state.steps or cfg.steps_of_day(state.day)
        self._observe_morning(state, status, steps_today)
        if self.calibrator is not None:
            # vết hôm nay của ta chưa có (đang lập kế hoạch): tạm lấy vết hôm qua, vì số
            # hạng "các đội khác như ta" của hôm nay lớn gấp (số đội − 1) lần vết của ta
            forecast_status = self.forecast.predict(
                status, self.status_yesterday, own_stay_today=self.own_stay_yesterday,
                own_stay_yesterday=self.own_stay_yesterday, parked_stay=self.parked_today,
                floor_today=state.day == 1,
            )
        else:
            forecast_status = self.forecast.predict(
                status, self.status_yesterday, own_stay_yesterday=self.own_stay_yesterday,
                floor_today=state.day == 1,
            )
        day_weights = self.day_weights()
        ctx = make_context(
            cfg, state.day, agents, status, [s.stocks for s in cfg.spots],
            self.visited_brands, weights=day_weights, forecast_status=forecast_status,
            steps_override=steps_override,
        )
        # theo số vòng thì chỉ chặn giờ khi server có hạn thật (phòng hờ)
        if deadline is None and (self.search_iterations is None or state.ends_at is not None):
            deadline = time.monotonic() + self.budget_for(state)
        max_iterations = self.search_iterations if self.search_iterations is not None else 1_000_000

        sent = {"count": 0, "decoded": None, "gain": None}
        self.last_sent = None
        on_safe, hooks = self._watch(state, agents, status, ctx.steps, started)
        look = self._lookahead(state, status, ctx.steps, weights=day_weights)

        # R7: giao các seed phụ trước khi tự tìm, để chúng chạy cùng lúc
        extra_seeds = [self.seed + 7919 * k for k in range(1, self.search_jobs)]
        base_job = None
        handles = None
        main_deadline = deadline
        if extra_seeds:
            # Bước E: chỉ seed chính giải MILP từng xe (4 luồng); seed phụ giữ ALNS thuần cho đa
            # dạng và để không tranh lõi với HiGHS
            extra_weights = (dataclasses.replace(day_weights, or_seconds=0.0)
                             if day_weights.or_seconds > 0 else day_weights)
            base_job = DayJob(
                day=state.day, kinds=list(self.kinds), agents=agents, status=status,
                visited=set(self.visited_brands), weights=extra_weights, forecast_status=forecast_status,
                steps_override=steps_override, seed=self.seed, max_iterations=max_iterations,
                forecast=self.forecast if look is not None else None,
                status_yesterday=self.status_yesterday, stay_yesterday=self.own_stay_yesterday,
                horizon=int(self.weights.rollout_days) or None, rollout_seed=self.seed)
            pool = self._ensure_pool() if deadline is not None else None
            if pool is not None:
                span = max(0.0, deadline - time.monotonic())
                # chừa giờ cho một lần chơi tiếp chấm bài cuối (khi plan_day chưa chấm sẵn)
                reserve = min(0.3 * span, max(0.25, 1.25 * self._roll_estimate(base_job.horizon) + 0.1))
                wall = time.time() + span
                handles = pool.start(cfg, [dataclasses.replace(base_job, seed=sd, wall_deadline=wall)
                                           for sd in extra_seeds], reserve)
                if handles is not None:
                    main_deadline = deadline - reserve
        # theo vòng mà server vẫn có hạn thật: không chạy tuần tự các seed phụ (sẽ vượt hạn)
        parallel = base_job is not None and (
            handles is not None or (self.search_iterations is not None and deadline is None))
        final_calls = {"n": 0}

        def offer(decoded: Decoded, final: bool) -> None:
            """Gửi nếu đáng: tối đa MAX_SUBMITS_PER_DAY lần, luôn chừa một lần cho bài cuối."""
            if submit is None:
                return
            limit = MAX_SUBMITS_PER_DAY if final else MAX_SUBMITS_PER_DAY - 1
            if sent["count"] >= limit:
                return
            gain = lexicographic_gain(ctx, decoded.result)
            last = sent["decoded"]
            # bài cuối do rollout chọn (R4) có thể có score_day thấp hơn bài đã gửi: vẫn
            # gửi, vì nó được chọn chính là để đổi phần thu hôm nay lấy các ngày sau
            rollout_final = final and decoded.picked_by in ("rollout", "song_song") and (
                last is None or decoded.plan != last.plan)
            if last is not None and not (
                    gain > sent["gain"]
                    or decoded.score >= last.score + RESUBMIT_MIN_GAIN * ctx.weights.B
                    or rollout_final):
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
            final_calls["n"] += 1
            # R7: bài cuối của seed chính chờ so với các seed phụ rồi mới gửi
            if parallel and final_calls["n"] > 1:
                return
            offer(decoded, final=True)

        best = plan_day(ctx, deadline=main_deadline, seed=self.seed, on_plan=on_plan,
                        on_improve=lambda decoded: offer(decoded, final=False),
                        max_iterations=max_iterations, lookahead=look, **hooks)
        if parallel:
            best = self._pick_seed(ctx, best, look, base_job, extra_seeds, handles, deadline, max_iterations)
            offer(best, final=True)
        if best.picked_by == "rollout":
            g = lexicographic_gain(ctx, best.result)
            self.log(f"ngày {state.day + 1}: chọn bài theo rollout các ngày sau "
                     f"({g[1]} loại, {g[2]} phần hôm nay)")
        applied = best if submit is None or sent["decoded"] is None else sent["decoded"]
        if record:
            self.record(state, applied, status)
        return applied

    def _pick_seed(self, ctx, best: Decoded, look, base_job: DayJob, extra_seeds: List[int],
                   handles, deadline: Optional[float], max_iterations: int) -> Decoded:
        """R7: chọn giữa bài của seed chính và các seed phụ theo (chuỗi mới, D, Q) của hôm nay
        cộng phần chơi tiếp các ngày sau; hoà thì theo điểm. Không nhận bài ít chuỗi mới hơn
        hôm nay (L cứng, như R4); ở ngày D an toàn với rollout_d_hard thì không nhận bài có
        (chuỗi mới, D) kém seed tốt nhất. Bài của seed phụ thắng thì mang picked_by = "song_song".

        Ngày cuối không có phần chơi tiếp: bài đã đạt cận trên của hôm nay không thể bị vượt,
        nên trả ngay thay vì chờ seed phụ tới hạn. Nhờ vậy dừng sớm của R3 vẫn rút được thời
        gian phản hồi (tiêu chí 4), quyết định ở ca kho = 1."""
        if (ctx.days_left == 0 and best.result.ok
                and lexicographic_gain(ctx, best.result) == day_upper_bound(ctx)):
            return best
        if handles is not None:
            until = deadline if deadline is not None else time.monotonic()
            results = SeedPool.collect(handles, until)
        else:                       # theo vòng: tuần tự, tất định
            results = [run_job(self.config, dataclasses.replace(base_job, seed=sd, max_iterations=max_iterations))
                       for sd in extra_seeds]
        hard = today_hard_len(ctx, [best.result] + [res[0].result for res in results if res is not None])
        own = lexicographic_gain(ctx, best.result)[:hard]
        base = own
        if hard > 1:
            # ngày D an toàn: sàn là (chuỗi mới, D) lớn nhất trong mọi seed, kể cả seed phụ
            for res in results:
                if res is not None and res[0].result.ok:
                    base = max(base, lexicographic_gain(ctx, res[0].result)[:hard])
        main_ok = own >= base
        no_time = (deadline is not None
                   and time.monotonic() + self._roll_estimate(base_job.horizon) > deadline)
        if main_ok and best.total is None and look is not None and handles is not None and no_time:
            self.log(f"ngày {ctx.day + 1}: không kịp chấm bài của seed chính, giữ bài đó")
            return best
        cands = []
        if main_ok:
            t0 = time.monotonic()
            own_total = total_of(ctx, best, look)
            if best.total is None and look is not None:
                self._note_roll(time.monotonic() - t0)
            cands.append((own_total, best.score, 0, best))
        for k, res in enumerate(results, 1):
            if res is None:
                continue
            dec, total = res
            if not dec.result.ok or lexicographic_gain(ctx, dec.result)[:hard] < base:
                continue
            cands.append((tuple(total), dec.score, -k, dec))
        done = len(cands) - (1 if main_ok else 0)
        _, _, neg_k, pick = max(cands, key=lambda c: (c[0], c[1], c[2]))
        if neg_k != 0:
            pick.picked_by = "song_song"
            g = lexicographic_gain(ctx, pick.result)
            self.log(f"ngày {ctx.day + 1}: chọn bài của seed song song {-neg_k} "
                     f"({g[1]} loại, {g[2]} phần hôm nay; {done}/{len(extra_seeds)} seed phụ kịp)")
        elif handles is not None and done < len(extra_seeds):
            self.log(f"ngày {ctx.day + 1}: {done}/{len(extra_seeds)} seed phụ kịp hạn")
        return pick

    def _roll_estimate(self, horizon: Optional[int]) -> float:
        """Giây cho một lần chơi tiếp: đo được thì dùng số đo, chưa thì ước theo cỡ bản đồ
        (đo 17/11: 16×16 khoảng 0,04 s/ngày chơi tiếp, 32×32 khoảng 0,4 s/ngày)."""
        if self._roll_seconds is not None:
            return self._roll_seconds
        days = horizon or max(1, self.config.n_days - 1)
        cells = self.config.width * self.config.height
        return 0.04 * days * (cells / 256.0) ** 1.6

    def _note_roll(self, seconds: float) -> None:
        old = self._roll_seconds
        self._roll_seconds = seconds if old is None else max(seconds, 0.5 * (old + seconds))

    def day_weights(self) -> Weights:
        """Trọng số cho ngày sắp lập kế hoạch.

        soft_auto > 0 (cùng soft_unit > 0): hạng mục cuối ngày mềm chỉ tính theo soft_unit khi
        D đã an toàn, tức đã đủ L (mọi chuỗi đã thu trong trận) và ngày hôm trước đạt trần D;
        ngày khác (ngày 0, hoặc vừa hụt chuỗi) dùng B như cũ để giữ D.

        rollout_d_streak > 0: luật D cứng của rollout (rollout_d_hard) chỉ giữ khi đã chơi ít
        nhất ngần ấy ngày và ngày nào cũng đạt trần D; không thì tắt cho ngày này.
        """
        w = self.weights
        n = len(self.config.brands)
        if w.rollout_d_hard > 0 and w.rollout_d_streak > 0:
            easy = (len(self.history) >= w.rollout_d_streak
                    and all(h.get("types", 0) >= n for h in self.history))
            if not easy:
                w = dataclasses.replace(w, rollout_d_hard=0.0)
        if w.soft_unit <= 0 or w.soft_auto <= 0:
            return w
        if w.soft_day0 > 0 and not self.history:
            return w            # ngày 0: hạng mục mềm theo phần, hạng mục cứng giữ L vẫn theo B
        safe = (len(self.visited_brands) >= n and bool(self.history)
                and self.history[-1].get("types", 0) >= n)
        return w if safe else dataclasses.replace(w, soft_unit=0.0)

    def _lookahead(self, state: DayState, status: Dict[int, int], steps: int,
                   weights: Optional[Weights] = None):
        """Hàm chấm một bài hôm nay bằng (chuỗi mới, D, Q) của các ngày sau (R4), hoặc None.

        Chơi tiếp bằng bộ dựng tham lam từ trạng thái cuối ngày của bài, với giao thông
        theo dự báo của chính strategist (như record() dự báo cho ngày mai).
        """
        if self.weights.rollout_k < 1 or state.day + 1 >= self.config.n_days:
            return None
        from .lookahead import future_gain

        config = self.config
        kinds = list(self.kinds)
        visited = set(self.visited_brands)
        status_today = dict(status)
        status_yesterday = self.status_yesterday
        stay_yesterday = self.own_stay_yesterday
        weights = weights or self.weights
        forecast = self.forecast
        seed = self.seed
        day = state.day

        horizon = int(weights.rollout_days) or None

        def look(decoded: Decoded):
            return future_gain(config, kinds, forecast, day, decoded.result, visited, status_today,
                               status_yesterday, stay_yesterday, weights=weights, seed=seed,
                               max_days=horizon)

        return look

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
            parked_stay=self.parked_today,
        )
        if self.calibrator is not None:
            self.calibrator.evening(state.day, result.stay)
        self.others_yesterday = [[a.pos for a in team] for team in state.others]
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
