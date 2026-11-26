"""Vòng đời một trận: không biết thuật toán, không biết kênh truyền."""

import json
import os
import time
from typing import Callable, List, Optional

from .. import rules
from ..model import DayState, MatchConfig
from ..planner.decoder import Decoded
from ..planner.solution import Weights
from ..planner.strategy import DEFAULT_ROLE_SECONDS, MatchStrategist
from ..simulator import idle_plan, run_day, wait_plan
from .transport import MatchEnded, SelectionWindow, Transport

#: --rust-seconds mặc định của các lệnh thi đấu (play, sandbox, web, web-bench): mỗi ngày tối đa
#: ngần này giây cho bộ giải tour bằng Rust (Weights.rs_seconds). Chưa build
#: rust/tour/target/release/tour thì planner tự bỏ qua bước này
RUST_SECONDS = 3.0


def match_weights(rust_seconds: Optional[float]) -> Optional[Weights]:
    """Trọng số của bot thi đấu: mặc định, cộng bộ giải Rust nếu rust_seconds > 0."""
    if rust_seconds is None or rust_seconds <= 0:
        return None
    return Weights(rs_seconds=float(rust_seconds))

WATCHDOG_SECONDS = 1.5

#: lập kế hoạch trước: ngày không có bài lập sẵn dùng ngần này phần ngân sách cho chính nó
PIPELINE_SPLIT = 0.55
#: ngày đầu tiên lập trước ngày sau (ngày 0 giữ trọn ngân sách: L thường chốt ở ngày 0)
PIPELINE_FROM_DAY = 1
#: còn ít hơn ngần này giây tới hạn của ngày thì không lập trước nữa
PIPELINE_MIN_SECONDS = 2.0
#: lập kế hoạch trước, contest: chọn vai trò dùng ngần này phần thời gian còn lại của cửa sổ chọn
#: xe; phần sau lập sẵn ngày 0 (trạng thái đầu ngày 0 biết trước khi đã có vai trò)
ROLE_SHARE = 0.6
#: lập kế hoạch trước: bài lập sẵn không dùng được ngần này ngày liền thì tắt cho cả trận
MISS_LIMIT = 2


class ReplayWriter:
    def __init__(self, path: Optional[str]):
        self.path = path
        self.handle = None
        if path:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            self.handle = open(path, "a", encoding="utf-8")

    def __call__(self, entry: dict) -> None:
        if self.handle is None:
            return
        try:
            self.handle.write(json.dumps(entry, default=str, ensure_ascii=False) + "\n")
            self.handle.flush()
        except Exception:
            pass

    def close(self):
        if self.handle is not None:
            self.handle.close()
            self.handle = None


class MatchSession:
    """Chạy trọn một trận qua một kênh truyền bất kỳ."""

    def __init__(
        self,
        transport: Transport,
        search_seconds: Optional[float] = 3.0,
        net_margin: float = 1.5,
        role_seconds: Optional[float] = 3.0,
        seed: int = 0,
        weights: Optional[Weights] = None,
        log: Optional[Callable[[str], None]] = None,
        replay: Optional[ReplayWriter] = None,
        emit: Optional[Callable[[str, dict], None]] = None,
        search_jobs: Optional[int] = 1,
        pipeline: bool = False,
    ):
        self.transport = transport
        #: lập kế hoạch trước (25/11/2026): trong lúc ngày d đang chạy, lập sẵn bài ngày d+1 từ
        #: trạng thái dự đoán, rồi gửi ngay khi ngày mở; chỉ với ngày có giờ (contest)
        self.pipeline = pipeline
        #: ngày không có bài lập sẵn (ngày 1, hoặc trạng thái lệch dự đoán): phần ngân sách cho
        #: chính ngày đó, phần còn lại để lập trước ngày sau
        self.pipeline_split = PIPELINE_SPLIT
        #: còn ít hơn ngần này giây tới hạn của ngày thì không lập trước nữa
        self.pipeline_min_seconds = PIPELINE_MIN_SECONDS
        self.role_share = ROLE_SHARE
        #: mốc time.time() ngày 0 mở (hạn cửa sổ chọn xe), nếu kênh truyền biết
        self._day0_opens: Optional[float] = None
        #: tắt cho phần còn lại của trận khi trạng thái thật lệch dự đoán MISS_LIMIT ngày liền
        self.pipeline_off = False
        #: số ngày liền bài lập sẵn không dùng được (xe hoặc đường khác dự đoán)
        self._misses = 0
        #: R7: số seed ALNS mỗi ngày; None = auto (strategy.resolve_search_jobs)
        self.search_jobs = search_jobs
        self.search_seconds = search_seconds
        self.net_margin = net_margin
        self.role_seconds = role_seconds
        self.seed = seed
        self.weights = weights
        self.log = log or (lambda msg: None)
        self.replay = replay or (lambda entry: None)
        #: web trực quan hoá: emit(kind, data) hoặc None
        self.emit = emit
        if emit is not None:
            from ..viz.events import log_level

            plain_log = self.log

            def log_both(msg: str) -> None:
                plain_log(msg)
                emit("log", {"text": msg, "level": log_level(msg)})

            self.log = log_both
        self.strategist: Optional[MatchStrategist] = None
        self.config: Optional[MatchConfig] = None
        #: server có thể đánh số ngày từ 1 thay vì 0; học từ lần bị từ chối đầu tiên
        self.day_offset = 0

    def run(self) -> Optional[dict]:
        try:
            self.config = self.transport.get_setup()
        except MatchEnded as end:
            # câu đã xong trước khi bot vào, hoặc mất liên lạc ngay từ đầu
            self.log(f"trận kết thúc trước khi bắt đầu: {end.args[0] if end.args else ''}")
            return self.transport.get_result()
        if self.emit is not None:
            from ..viz.shapes import config_payload
            self.emit("match_start", {"mode": "online", "config": config_payload(self.config)})
        self.strategist = MatchStrategist(
            self.config, weights=self.weights, search_seconds=self.search_seconds,
            net_margin=self.net_margin, role_seconds=self.role_seconds,
            seed=self.seed, log=self.log, emit=self.emit, search_jobs=self.search_jobs,
        )
        try:
            return self._play(self.strategist)
        finally:
            self.strategist.close()

    def _play(self, strategist) -> Optional[dict]:
        kinds = self.choose_roles()
        if self.emit is not None:
            # vai trò thật sự dùng: có thể khác vai trò đã chọn khi server không nhận
            self.emit("roles_chosen", {"kinds": list(kinds)})

        pre = self._preplan_day0()   # (trạng thái dự đoán, bài lập sẵn) của ngày kế tiếp
        while True:
            try:
                if pre is not None and getattr(self.transport, "supports_fast_state", False):
                    # có bài lập sẵn: hỏi dày quanh giờ mở và dựng trạng thái từ /state (bỏ GET /day)
                    state = self.transport.wait_day_state(kinds, fast=True)
                else:
                    state = self.transport.wait_day_state(kinds)
            except MatchEnded as end:
                self.log(f"trận kết thúc: {end.args[0] if end.args else ''}")
                break
            use = pre if pre is not None and pre[0].day == state.day else None
            pre = None
            matched = False
            if use is not None:
                reason = self._mismatch(use, state)
                matched = reason is None
                if matched:
                    self._misses = 0
                else:
                    agents_off = not self._same_state(use[0], state, roads=False)
                    foreign = agents_off and (state.day - 1) in getattr(self.transport, "foreign_submits", {})
                    if foreign:
                        reason = "bài server áp dụng hôm qua không phải của bot này: có client khác nộp bài"
                    self.log(f"{self.day_label(state.day)}: bài lập sẵn không dùng được ({reason})")
                    # một ngày lệch (client khác nộp đè, tắc đường bất chợt): hôm nay lập trực tiếp với
                    # một phần ngân sách, phần còn lại lập trước ngày mai từ trạng thái THẬT. Lệch
                    # MISS_LIMIT ngày liền thì dự đoán không theo kịp server: tắt cho cả trận. Bài bị
                    # client khác đè không nói gì về dự đoán nên không tính
                    if not foreign:
                        self._misses += 1
                    if self._misses >= MISS_LIMIT:
                        self.pipeline_off = True
                        self.log(f"{self.day_label(state.day)}: lệch {self._misses} ngày liền; tắt lập kế "
                                 f"hoạch trước cho phần còn lại của trận")
            pipe = self._pipeline_ready(state, preplanned=matched)
            try:
                # ngày không có bài lập sẵn dùng được: chừa phần cuối ngân sách để lập trước ngày sau
                scale = self.pipeline_split if pipe and not matched else 1.0
                self.play_day(state, preplanned=use, budget_scale=scale)
            except Exception as exc:
                # mất một ngày còn hơn mất cả trận: ngày sau vẫn phải được chơi
                self.log(f"{self.day_label(state.day)}: lỗi không lường trước {exc!r}; bỏ qua ngày này")
            if pipe and not self.pipeline_off:
                pre = self._preplan(state)

        result = self.transport.get_result()
        if self.strategist is not None:
            L, D, Q = self.strategist.score_tuple()
            self.log(f"điểm dự kiến của ta: L={L} D={D} Q={Q}")
        return result

    def choose_roles(self) -> List[int]:
        """Chọn và gửi vai trò theo cửa sổ mà kênh truyền báo.

        Kênh cũ (PTIT, sandbox) không biết cửa sổ: giữ cách chặn theo startsAt.
        Kênh web biết chính xác: cửa sổ đã đóng thì bỏ qua (vào muộn, khởi động
        lại giữa trận — loại xe lấy theo server); practice không giới hạn giờ;
        contest chặn theo hạn thật của cửa sổ.
        """
        assert self.strategist is not None and self.config is not None
        window: Optional[SelectionWindow] = self.transport.selection_window()
        if window is not None and not window.open:
            self.log("cửa sổ chọn xe đã đóng: chơi với loại xe server đang giữ")
            return list(self.strategist.kinds)
        t0 = time.monotonic()
        send_deadline = None
        if window is None:
            kinds = self.strategist.select_roles()
        else:
            budget = self.role_seconds
            if budget is None:
                budget = window.limit - self.net_margin if window.limit is not None else DEFAULT_ROLE_SECONDS
            if window.deadline is not None:
                left = window.deadline - time.time()
                # chừa đủ giờ để vai trò tới được server: lỡ cửa sổ là toàn xe tuần tra cả câu
                budget = min(budget, left - max(self.net_margin, window.reserve))
                send_deadline = time.monotonic() + max(0.0, left)
                if self.pipeline and getattr(self.transport, "supports_resubmit", True):
                    # phần còn lại của cửa sổ để lập sẵn ngày 0 (_preplan_day0)
                    budget = min(budget, self.role_share * max(0.0, left - window.reserve))
                    self._day0_opens = window.deadline
            kinds = self.strategist.select_roles(budget_seconds=max(0.0, budget), cap_by_start=False)
        self.log(f"chọn vai trò xong trong {time.monotonic() - t0:.1f}s")
        ack = self.transport.send_assignment(kinds, deadline=send_deadline)
        self.replay({"assignment": kinds, "ok": ack.ok})
        if not ack.ok:
            # mặc định của server là toàn xe tuần tra; lập kế hoạch như thể có xe
            # tiếp nhiên liệu sẽ làm mọi bài bị từ chối vì E_NO_FUEL, cả trận
            kinds = [rules.PATROL] * self.config.n_agents
            self.strategist.kinds = list(kinds)
            self.log("server không nhận vai trò; chuyển sang toàn xe tuần tra như server")
        return kinds

    # ---- lập kế hoạch trước (25/11/2026) ----

    def _pipeline_ready(self, state: DayState, preplanned: bool = False) -> bool:
        """Có lập trước ngày sau không: bật, ngày có giờ và gửi lại được (contest), không phải
        ngày cuối. Ngày chơi bằng bài lập sẵn thì luôn lập trước ngày sau (cả trọn ngày rảnh);
        ngày lập trực tiếp thì chỉ từ PIPELINE_FROM_DAY (ngày 0 giữ trọn ngân sách vì L)."""
        assert self.config is not None
        return (self.pipeline and not self.pipeline_off and state.ends_at is not None
                and getattr(self.transport, "supports_resubmit", True)
                and (preplanned or PIPELINE_FROM_DAY <= state.day) and state.day < self.config.n_days - 1)

    def _usable(self, pre, real: DayState) -> bool:
        return self._mismatch(pre, real) is None

    def _mismatch(self, pre, real: DayState) -> Optional[str]:
        """None nếu bài lập sẵn dùng được nguyên cho ngày thật, không thì lý do. Dùng được: xe đúng
        như dự đoán (vị trí, xăng, vai trò, số bước), và đường cũng đúng; hoặc đường khác (có tắc mà
        dự báo không thấy) nhưng bài mô phỏng lại trên trạng thái thật vẫn hợp lệ và (chuỗi mới, D,
        Q) không kém dự đoán."""
        assert self.config is not None and self.strategist is not None
        pred, ready = pre
        if not self._same_state(pred, real, roads=False):
            return "xe khác dự đoán (vị trí, xăng, vai trò hoặc số bước)"
        if self._same_state(pred, real):
            return None
        steps = self.steps_for(real)
        res = run_day(self.config, steps, real.agents, ready.plan, [s.stocks for s in self.config.spots],
                      dict(real.road_status))
        if not res.ok:
            return "đường khác dự báo, bài lập sẵn không còn hợp lệ"
        seen = self.strategist.visited_brands
        gain = lambda r: (len(r.brands - seen), len(r.brands), r.servings)
        if gain(res) < gain(ready.result):
            return f"đường khác dự báo, bài lập sẵn kém đi: {gain(res)} so với {gain(ready.result)}"
        return None

    def _same_state(self, pred: DayState, real: DayState, roads: bool = True) -> bool:
        """Trạng thái thật có đúng như dự đoán không: cùng ngày, số bước, vai trò, vị trí, xăng
        của xe tuần tra, và tình trạng đường (ô vắng mặt coi là thông)."""
        if pred.day != real.day or self.steps_for(real) != pred.steps or len(pred.agents) != len(real.agents):
            return False
        for p, r in zip(pred.agents, real.agents):
            if p.kind != r.kind or p.pos != r.pos:
                return False
            if r.kind == rules.PATROL and r.fuel is not None and p.fuel != r.fuel:
                return False
        if not roads:
            return True
        norm = lambda status: {c: v for c, v in status.items() if v}
        return norm(pred.road_status) == norm(real.road_status)

    def _preplan(self, state: DayState):
        """Lập sẵn bài ngày state.day + 1 trong phần còn lại của ngày hiện tại; trả
        (trạng thái dự đoán, bài) hoặc None."""
        assert self.strategist is not None and self.config is not None
        if state.ends_at is None:
            return None
        deadline = time.monotonic() + (state.ends_at - time.time()) - self.net_margin
        if deadline - time.monotonic() < self.pipeline_min_seconds:
            self.log(f"{self.day_label(state.day + 1)}: không đủ giờ để lập trước")
            return None
        steps = self.config.steps_of_day(state.day + 1 + self.day_offset)
        pred = self.strategist.predict_next(state, steps)
        if pred is None:
            return None
        return self._plan_ahead(pred, deadline)

    def _preplan_day0(self):
        """Lập sẵn ngày 0 trong phần còn lại của cửa sổ chọn xe (vai trò đã gửi)."""
        assert self.strategist is not None
        if not self.pipeline or self._day0_opens is None:
            return None
        deadline = time.monotonic() + (self._day0_opens - time.time()) - self.net_margin
        if deadline - time.monotonic() < self.pipeline_min_seconds:
            self.log(f"{self.day_label(0)}: không đủ giờ để lập trước trong cửa sổ chọn xe")
            return None
        return self._plan_ahead(self.strategist.predict_day0(), deadline)

    def _plan_ahead(self, pred: DayState, deadline: float):
        assert self.strategist is not None
        steps = pred.steps
        t0 = time.monotonic()
        emit, self.strategist.emit = self.strategist.emit, None   # web: không vẽ ngày chưa tới
        try:
            decoded = self.strategist.plan_day(pred, deadline=deadline, submit=None, steps_override=steps,
                                               record=False, observe=False)
        except Exception as exc:
            self.log(f"{self.day_label(pred.day)}: lập trước lỗi {exc!r}; ngày đó sẽ lập như thường")
            return None
        finally:
            self.strategist.emit = emit
        self.log(f"{self.day_label(pred.day)}: đã lập sẵn {len(decoded.result.brands)} loại, "
                 f"{decoded.result.servings} phần ({time.monotonic() - t0:.1f}s)")
        return pred, decoded

    def day_label(self, day: int) -> str:
        """Số ngày theo cách website hiển thị (từ 1); JSON vẫn giữ chỉ số API từ 0."""
        total = self.config.n_days if self.config is not None else "?"
        return f"Ngày {day + 1}/{total}"

    def steps_for(self, state: DayState) -> int:
        """Số bước của ngày. Server báo (steps_today) là chuẩn; không có thì theo
        daySteps, đã tính tới khả năng server đánh số ngày từ 1."""
        assert self.config is not None
        if state.steps:
            return int(state.steps)
        return self.config.steps_of_day(state.day + self.day_offset)

    def maybe_fix_day_indexing(self, ack, state: DayState) -> None:
        """Server đánh số ngày từ 1 sẽ làm tổng số bước sai và bài bị từ chối.

        Theo Formats ngày đầu có day = 0, và server web báo luôn số bước của ngày,
        nên chỉ đoán khi server không báo số bước (PTIT, sandbox): nhận ra qua mã
        lỗi E_STEP_OVERFLOW ngay lần gửi đầu, rồi lùi chỉ số ngày một đơn vị cho
        phần còn lại của trận. Đoán nhầm sẽ làm sai số bước mọi ngày sau.
        """
        assert self.config is not None
        if state.steps or self.day_offset != 0 or not ack.reason:
            return
        if "STEP" not in str(ack.reason).upper():
            return
        alt = state.day - 1
        if 0 <= alt < self.config.n_days and self.config.day_steps[alt] != self.config.steps_of_day(state.day):
            self.day_offset = -1
            self.log(f"server có vẻ đánh số ngày từ 1: chuyển sang dùng daySteps[{alt}]")

    def play_day(self, state: DayState, preplanned=None, budget_scale: float = 1.0) -> Optional[Decoded]:
        """Chơi một ngày. `preplanned` = (trạng thái dự đoán, bài) lập sẵn từ hôm trước: trạng
        thái thật đúng như dự đoán thì gửi ngay bài đó và xong ngày; lệch thì gửi ngay nếu nó vẫn
        hợp lệ, rồi lập kế hoạch như thường và chỉ gửi lại khi hơn nó. `budget_scale` thu ngân
        sách của ngày (chừa phần sau cho việc lập trước ngày mai)."""
        assert self.strategist is not None and self.config is not None
        started = time.monotonic()
        steps = self.steps_for(state)
        if state.steps and state.steps != self.config.steps_of_day(state.day):
            self.log(f"{self.day_label(state.day)}: server báo {state.steps} bước, daySteps ghi "
                     f"{self.config.steps_of_day(state.day)}; theo server")
        budget = self.strategist.budget_for(state) * budget_scale
        deadline = started + budget
        #: hạn cứng của ngày theo đồng hồ đơn điệu; mọi lần gửi phải nằm trong đó
        hard_deadline = None
        if state.ends_at is not None:
            hard_deadline = started + max(0.0, state.ends_at - time.time())
        sent = {"count": 0, "ok": False}
        accepted = {"plan": None}
        if self.emit is not None:
            from ..viz.shapes import agents_payload, status_payload
            self.emit("day_start", {"day": state.day, "steps": steps,
                                    "status": status_payload(state.road_status),
                                    "agents": agents_payload(state.agents),
                                    "others": [agents_payload(team) for team in state.others]})

        def submit(plan: List[List[int]]) -> bool:
            if hard_deadline is not None and time.monotonic() >= hard_deadline:
                # gửi muộn có thể rơi vào ngày sau và ghi đè bài tốt của ngày đó
                self.log(f"{self.day_label(state.day)}: quá hạn, không gửi nữa")
                return False
            ack = self.transport.send_actions(state.day, plan, deadline=hard_deadline)
            sent["count"] += 1
            sent["ok"] = sent["ok"] or ack.ok
            if ack.ok:
                accepted["plan"] = plan
            if self.emit is not None:
                self.emit("submit", {"day": state.day, "n": sent["count"], "ok": ack.ok,
                                     "reason": ack.reason,
                                     "elapsed": round(time.monotonic() - started, 3)})
            self.replay({"day": state.day, "plan": plan, "ok": ack.ok, "reason": ack.reason})
            if not ack.ok:
                self.log(f"{self.day_label(state.day)}: bài bị từ chối ({ack.reason})")
                self.maybe_fix_day_indexing(ack, state)
            return ack.ok

        resubmit = getattr(self.transport, "supports_resubmit", True)
        decoded = None
        prior_plan = None
        if preplanned is not None and resubmit:
            pred, ready = preplanned
            if self._usable(preplanned, state):
                # đúng như dự đoán: bài lập sẵn chính là bài lập lúc này với cùng ngân sách
                self.strategist.observe(state, steps)
                if submit(ready.plan):
                    applied = self._remember(state, accepted["plan"], ready) or ready
                    self._emit_day_result(state, accepted["plan"])
                    self.log(f"{self.day_label(state.day)}: bài lập sẵn, {len(applied.result.brands)} loại, "
                             f"{applied.result.servings} phần, gửi sau {time.monotonic() - started:.2f}s")
                    return applied
            else:
                prior_plan = ready.plan
        try:
            # bộ nhớ trận ghi SAU khi biết server đã nhận bài nào (_remember), không ghi ở đây
            decoded = self.strategist.plan_day(
                state, deadline=deadline, submit=submit if resubmit else None,
                steps_override=steps, record=False, prior_plan=prior_plan,
            )
            if not resubmit:
                # kênh chỉ nhận một dòng trả lời cho mỗi ngày: gửi đúng bài tốt nhất
                submit(decoded.plan)
        except Exception as exc:
            # planner hỏng thì ngày này chơi bài dự phòng, nhưng trận vẫn tiếp
            self.log(f"{self.day_label(state.day)}: planner lỗi {exc!r}")
            if not sent["ok"]:
                self._send_fallback(state, submit)
            self._remember(state, accepted["plan"], decoded)
            self._emit_day_result(state, accepted["plan"])
            return None

        if sent["count"] == 0 or not sent["ok"]:
            self.log(f"{self.day_label(state.day)}: chưa có bài nào được nhận, gửi bài dự phòng")
            self._send_fallback(state, submit)

        if not sent["ok"] and self.day_offset != 0:
            # đã phát hiện lệch cách đánh số ngày: lập lại kế hoạch với số bước đúng
            self.log(f"{self.day_label(state.day)}: lập lại kế hoạch với {self.steps_for(state)} bước")
            decoded = self.strategist.plan_day(
                state, deadline=time.monotonic() + max(0.2, deadline - time.monotonic()),
                submit=None, steps_override=self.steps_for(state), record=False,
            )
            submit(decoded.plan)

        applied = self._remember(state, accepted["plan"], decoded) or decoded
        self._emit_day_result(state, accepted["plan"])
        elapsed = time.monotonic() - started
        self.log(
            f"{self.day_label(state.day)}: {len(applied.result.brands)} loại, {applied.result.servings} phần, "
            f"gửi {sent['count']} lần, {elapsed:.2f}s (ngân sách {budget:.2f}s)"
        )
        return applied

    def _send_fallback(self, state: DayState, submit: Callable[[List[List[int]]], bool]) -> None:
        """Bài dự phòng khi chưa có bài nào được nhận: rời ô đường rồi đứng yên (cases 11.4).

        Bị từ chối thì gửi bài đứng yên thuần, cách hiểu luật nào cũng nhận.
        """
        assert self.config is not None
        plan = idle_plan(self.config, state.agents, self.steps_for(state), state.road_status)
        if submit(plan):
            return
        fallback = wait_plan(self.config.n_agents, self.steps_for(state))
        if fallback != plan:
            submit(fallback)

    def _remember(self, state: DayState, plan: Optional[List[List[int]]],
                  decoded: Optional[Decoded]) -> Optional[Decoded]:
        """Ghi vào bộ nhớ trận đúng bài server đang áp dụng: bài hợp lệ cuối cùng nó
        nhận, hoặc đứng yên cả ngày nếu không nhận bài nào (cases 11.3, 11.4)."""
        assert self.strategist is not None
        try:
            return self.strategist.record_applied(state, plan, self.steps_for(state),
                                                  known=(decoded, self.strategist.last_sent))
        except Exception as exc:
            self.log(f"{self.day_label(state.day)}: không ghi được bộ nhớ ngày ({exc!r})")
            return None

    def _emit_day_result(self, state: DayState, plan: Optional[List[List[int]]]) -> None:
        """Mô phỏng bài được nhận cuối cùng để web vẽ lại từng bước; điểm thật vẫn là của server."""
        if self.emit is None or self.config is None:
            return
        from ..viz.shapes import simulate

        steps = self.steps_for(state)
        applied = plan if plan is not None else wait_plan(self.config.n_agents, steps)
        result, trace = simulate(self.config, state.agents, applied, steps,
                                 [s.stocks for s in self.config.spots], state.road_status)
        error = trace["error"]
        if plan is None:
            error = {"code": "NOT_ACCEPTED", "agent": -1, "step": 0,
                     "detail": "server không nhận bài nào trong ngày"}
        total = list(self.strategist.score_tuple()) if self.strategist is not None else None
        self.emit("day_result", {"day": state.day, "ok": plan is not None and result.ok, "error": error,
                                 "types": len(result.brands), "servings": result.servings,
                                 "brands": sorted(result.brands), "trace": trace, "total": total})
