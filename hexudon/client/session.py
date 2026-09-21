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
from ..simulator import wait_plan
from .transport import MatchEnded, SelectionWindow, Transport

WATCHDOG_SECONDS = 1.5


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
    ):
        self.transport = transport
        self.search_seconds = search_seconds
        self.net_margin = net_margin
        self.role_seconds = role_seconds
        self.seed = seed
        self.weights = weights
        self.log = log or (lambda msg: None)
        self.replay = replay or (lambda entry: None)
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
        self.strategist = MatchStrategist(
            self.config, weights=self.weights, search_seconds=self.search_seconds,
            net_margin=self.net_margin, role_seconds=self.role_seconds,
            seed=self.seed, log=self.log,
        )
        kinds = self.choose_roles()

        while True:
            try:
                state = self.transport.wait_day_state(kinds)
            except MatchEnded as end:
                self.log(f"trận kết thúc: {end.args[0] if end.args else ''}")
                break
            try:
                self.play_day(state)
            except Exception as exc:
                # mất một ngày còn hơn mất cả trận: ngày sau vẫn phải được chơi
                self.log(f"{self.day_label(state.day)}: lỗi không lường trước {exc!r}; bỏ qua ngày này")

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
                budget = window.limit - self.net_margin if window.limit else DEFAULT_ROLE_SECONDS
            if window.deadline is not None:
                left = window.deadline - time.time()
                budget = min(budget, left - self.net_margin)
                send_deadline = time.monotonic() + max(0.0, left)
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

    def day_label(self, day: int) -> str:
        """Số ngày theo cách website hiển thị (từ 1); JSON vẫn giữ chỉ số API từ 0."""
        total = self.config.n_days if self.config is not None else "?"
        return f"Ngày {day + 1}/{total}"

    def steps_for(self, day: int) -> int:
        """Số bước của ngày, đã tính tới khả năng server đánh số ngày từ 1."""
        assert self.config is not None
        return self.config.steps_of_day(day + self.day_offset)

    def maybe_fix_day_indexing(self, ack, state: DayState) -> None:
        """Server đánh số ngày từ 1 sẽ làm tổng số bước sai và bài bị từ chối.

        Nhận ra qua mã lỗi E_STEP_OVERFLOW ngay lần gửi đầu, rồi lùi chỉ số ngày
        một đơn vị cho phần còn lại của trận.
        """
        assert self.config is not None
        if self.day_offset != 0 or not ack.reason:
            return
        if "STEP" not in str(ack.reason).upper():
            return
        alt = state.day - 1
        if 0 <= alt < self.config.n_days and self.config.day_steps[alt] != self.config.steps_of_day(state.day):
            self.day_offset = -1
            self.log(f"server có vẻ đánh số ngày từ 1: chuyển sang dùng daySteps[{alt}]")

    def play_day(self, state: DayState) -> Optional[Decoded]:
        assert self.strategist is not None and self.config is not None
        started = time.monotonic()
        steps = self.steps_for(state.day)
        budget = self.strategist.budget_for(state)
        deadline = started + budget
        #: hạn cứng của ngày theo đồng hồ đơn điệu; mọi lần gửi phải nằm trong đó
        hard_deadline = None
        if state.ends_at is not None:
            hard_deadline = started + max(0.0, state.ends_at - time.time())
        sent = {"count": 0, "ok": False}

        def submit(plan: List[List[int]]) -> bool:
            if hard_deadline is not None and time.monotonic() >= hard_deadline:
                # gửi muộn có thể rơi vào ngày sau và ghi đè bài tốt của ngày đó
                self.log(f"{self.day_label(state.day)}: quá hạn, không gửi nữa")
                return False
            ack = self.transport.send_actions(state.day, plan, deadline=hard_deadline)
            sent["count"] += 1
            sent["ok"] = sent["ok"] or ack.ok
            self.replay({"day": state.day, "plan": plan, "ok": ack.ok, "reason": ack.reason})
            if not ack.ok:
                self.log(f"{self.day_label(state.day)}: bài bị từ chối ({ack.reason})")
                self.maybe_fix_day_indexing(ack, state)
            return ack.ok

        resubmit = getattr(self.transport, "supports_resubmit", True)
        try:
            decoded = self.strategist.plan_day(
                state, deadline=deadline, submit=submit if resubmit else None,
                steps_override=steps,
            )
            if not resubmit:
                # kênh chỉ nhận một dòng trả lời cho mỗi ngày: gửi đúng bài tốt nhất
                submit(decoded.plan)
        except Exception as exc:
            # planner hỏng thì ngày này chơi bài đứng yên, nhưng trận vẫn tiếp
            self.log(f"{self.day_label(state.day)}: planner lỗi {exc!r}")
            if not sent["ok"]:
                submit(wait_plan(self.config.n_agents, self.steps_for(state.day)))
            return None

        if sent["count"] == 0 or not sent["ok"]:
            self.log(f"{self.day_label(state.day)}: chưa có bài nào được nhận, gửi bài đứng yên")
            submit(wait_plan(self.config.n_agents, self.steps_for(state.day)))

        if not sent["ok"] and self.day_offset != 0:
            # đã phát hiện lệch cách đánh số ngày: lập lại kế hoạch với số bước đúng
            self.log(f"{self.day_label(state.day)}: lập lại kế hoạch với {self.steps_for(state.day)} bước")
            decoded = self.strategist.plan_day(
                state, deadline=time.monotonic() + max(0.2, deadline - time.monotonic()),
                submit=None, steps_override=self.steps_for(state.day),
            )
            submit(decoded.plan)

        elapsed = time.monotonic() - started
        self.log(
            f"{self.day_label(state.day)}: {len(decoded.result.brands)} loại, {decoded.result.servings} phần, "
            f"gửi {sent['count']} lần, {elapsed:.2f}s (ngân sách {budget:.2f}s)"
        )
        return decoded
